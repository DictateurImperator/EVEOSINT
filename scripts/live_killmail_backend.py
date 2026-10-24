"""Single-KM adapter for the existing importer, ESI histories and pricing formula."""
from collections import OrderedDict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import uuid
import os
import time

import requests

if __package__:
    from . import sync_killmails as importer, esi_affiliation_refresh as affiliations
    from . import calculate_killmail_values as values
else:
    import sync_killmails as importer
    import esi_affiliation_refresh as affiliations
    import calculate_killmail_values as values


class LivePricing(values.Pricing):
    """Same formula; indexed, bounded on-demand quotes replace a full history replay."""
    def __init__(self, conn):
        self.conn = conn
        self.nearest = OrderedDict()
        self.metadata_checked=time.monotonic()
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('market.zkill_recovered_prices')")
            self.has_recovered=cur.fetchone()[0] is not None
        super().__init__(values.load_metadata(conn), {}, {}, valuation_mode='loss-day',
                         recipe_history=values.RecipeHistory(), abyssal_equivalents=values.load_equivalents(conn))

    def nearest_market(self, type_id, day):
        key = (type_id, day)
        if key in self.nearest:
            self.nearest.move_to_end(key)
            return self.nearest[key]
        with self.conn.cursor() as cur:
            # There are at most four candidates, regardless of market history size.
            # A dated snapshot wins over its recovered quote. Any earlier price
            # wins over every later one, matching the historical calculator.
            candidates = []
            for operator, order in (('<=', 'DESC'), ('>', 'ASC')):
                cur.execute(f'''SELECT price_date,(prices->>%s)::numeric FROM market.zkill_daily_prices
                    WHERE price_date {operator} %s AND COALESCE((prices->>%s)::numeric,0)>0
                    ORDER BY price_date {order} LIMIT 1''', (str(type_id),day,str(type_id)))
                row = cur.fetchone()
                if row:candidates.append(row)
                if not self.has_recovered:continue
                cur.execute(f'''SELECT r.price_date,r.unit_price_isk FROM market.zkill_recovered_prices r
                    JOIN market.zkill_daily_prices d ON d.price_date=r.price_date
                    WHERE r.item_type_id=%s AND r.price_date {operator} %s AND r.unit_price_isk>0
                    AND COALESCE((d.prices->>%s)::numeric,0)<=0 ORDER BY r.price_date {order} LIMIT 1''',
                            (type_id,day,str(type_id)))
                row = cur.fetchone()
                if row:candidates.append(row)
        earlier = [row for row in candidates if row[0] <= day]
        chosen = max(earlier, default=None) if earlier else min(candidates, default=None)
        result = (Decimal(chosen[1]), False) if chosen else None
        self.nearest[key] = result
        while len(self.nearest)>8192:self.nearest.popitem(last=False)
        return result

    def begin_kill(self):
        # Price imports/recovery may have completed since the previous KM.
        if time.monotonic()-self.metadata_checked>300:
            self.metadata=values.load_metadata(self.conn)
            self.abyssal_equivalents=values.load_equivalents(self.conn)
            self.recipe_history=values.RecipeHistory()
            self.recipe_day=None
            self.recipe_version='fixed'
            self.metadata_checked=time.monotonic()
            with self.conn.cursor() as cur:
                cur.execute("SELECT to_regclass('market.zkill_recovered_prices')")
                self.has_recovered=cur.fetchone()[0] is not None
        self.nearest.clear()
        self.cache.clear()
        self.quotes.market.clear()
        self.quotes.built.clear()


def decode_package(raw, sequence):
    if not isinstance(raw, dict) or raw.get('sequence_id') != sequence:
        raise ValueError('Live sequence does not match payload')
    payload = dict(raw.get('esi') or {})
    ident = raw.get('killmail_id')
    if payload.get('killmail_id') != ident or not isinstance(ident,int) or ident<=0:
        raise ValueError('Invalid killmail identity')
    instant = datetime.fromisoformat(payload['killmail_time'].replace('Z','+00:00'))
    if instant.tzinfo is None:raise ValueError('Killmail timestamp has no timezone')
    if not isinstance(payload.get('victim'),dict) or not isinstance(payload.get('attackers'),list):
        raise ValueError('Incomplete killmail')
    payload['killmail_hash'] = raw.get('hash') or raw.get('zkb',{}).get('hash')
    return payload, instant.astimezone(timezone.utc)


def item_value(item, pricing, day):
    flag = item.get('flag')
    if item.get('singleton') == 2 or flag == 179 or (item.get('item_type_id') == 33329 and flag == 89):
        quote = values.ONE_CENT
    else:
        quote, _missing = pricing.price(int(item['item_type_id']), day, day, True)
    quantity = int(item.get('quantity_destroyed') or 0)+int(item.get('quantity_dropped') or 0)
    return quote*quantity + sum((item_value(child,pricing,day) for child in item.get('items',[])),Decimal(0))


def kill_value(payload, pricing, day):
    pricing.begin_kill()
    hull, _missing = pricing.price(int(payload['victim']['ship_type_id']),day,day,True)
    amount = hull + sum((item_value(item,pricing,day) for item in payload['victim'].get('items',[])),Decimal(0))
    return min(values.MAX_BIGINT, values.cents(amount))


def damage_credits(attackers, amount):
    """Fractional cents: same denominator as existing KB/stats/rankings, including NPCs."""
    total=sum(max(0,int(a.get('damage_done') or 0)) for a in attackers)
    return [Decimal(amount)*max(0,int(a.get('damage_done') or 0))/total if total else Decimal(0) for a in attackers]


class Backend:
    def __init__(self):
        self.conn = importer.db()
        self.ensured = set()
        self.run_id = str(uuid.uuid4())
        self.metrics = dict(histories_refreshed=0, affiliations_deferred=0, entities_loaded=0, valuation_deferred=0)
        self.pricing = None
        self.on_progress=None
        self.last_progress=(None,0)
        affiliations.ensure_tables(self.conn)
        affiliations.create_run_row(self.conn,self.run_id,'rawkm.killmails','victim_character_id')
        with self.conn.cursor() as cur:
            cur.execute(Path(__file__).with_name('live_killmail_schema.sql').read_text())
        self.conn.commit()

    def progress(self,phase,**fields):
        if self.on_progress is None:return
        previous,at=self.last_progress
        now=time.monotonic()
        if previous==phase and now-at<1:return
        self.last_progress=(phase,now)
        self.on_progress(phase,**self.metrics,**fields)

    def close(self):
        try:
            self.conn.rollback()
            affiliations.finish_run_row(self.conn,self.run_id,'done')
        finally:self.conn.close()

    def cursor(self):
        with self.conn.cursor() as cur:
            cur.execute('SELECT next_sequence,updated_at FROM rawkm.live_killmail_cursor WHERE singleton')
            row=cur.fetchone()
        self.conn.commit()
        return row

    def initialize(self, sequence):
        with self.conn.cursor() as cur:
            cur.execute('INSERT INTO rawkm.live_killmail_cursor(singleton,next_sequence) VALUES(TRUE,%s) ON CONFLICT DO NOTHING',(sequence,))
        self.conn.commit()

    def skip_expired(self, previous, available):
        with self.conn.cursor() as cur:
            cur.execute('UPDATE rawkm.live_killmail_cursor SET next_sequence=%s,updated_at=now() WHERE singleton AND next_sequence=%s',(available,previous))
        self.conn.commit()

    def pending_work(self, ident, corporation=False):
        table = 'esi_affiliation_corporation_work' if corporation else 'esi_affiliation_character_work'
        column = 'corporation_id' if corporation else 'character_id'
        states = ('public_status','initial_history_status','refresh_status') if corporation else ('public_status','initial_history_status','affiliation_status','history_refresh_status')
        predicate=' OR '.join(f"w.{field} IN ('pending','running')" for field in states)
        with self.conn.cursor() as cur:
            cur.execute(f'''SELECT r.worker_pid FROM entities.{table} w
                JOIN entities.esi_affiliation_run r ON r.run_id=w.run_id
                WHERE w.{column}=%s AND r.status='running' AND ({predicate})''',(ident,))
            workers=cur.fetchall()
        for (pid,) in workers:
            if not pid:continue
            try:os.kill(pid,0);return True
            except ProcessLookupError:pass
            except PermissionError:return True
        return False

    def refresh_entities(self, payload):
        self.conn.commit()
        self.conn.autocommit=True
        try:return self._refresh_entities(payload)
        finally:self.conn.autocommit=False

    def _refresh_entities(self, payload):
        participants = [payload['victim']]+payload['attackers']
        characters = {int(p['character_id']):p for p in participants if p.get('character_id')}
        corp_ids = {int(p['corporation_id']) for p in participants if p.get('corporation_id')}
        alliance_ids = {int(p['alliance_id']) for p in participants if p.get('alliance_id')}
        # Release the raw-KM transaction before using the existing history
        # helpers (they deliberately commit their own reusable ESI operations).
        self.conn.commit()
        refreshed = []
        for number,(ident, participant) in enumerate(characters.items(),1):
            self.progress('affiliations',participants_done=number-1,participants_total=len(characters))
            if self.pending_work(ident):
                self.metrics['affiliations_deferred']+=1
                continue
            with self.conn.cursor() as cur:
                cur.execute('''SELECT c.is_deleted,a.corporation_id,a.alliance_id FROM entities.characters c
                    LEFT JOIN entities.character_current_affiliation a USING(character_id) WHERE c.character_id=%s''',(ident,))
                row=cur.fetchone()
            if row and row[0]:continue
            expected=(participant.get('corporation_id'),participant.get('alliance_id'))
            if row is None or tuple(row[1:]) != expected:
                affiliations.fetch_public_character(self.run_id,ident)
                affiliations.fetch_character_history(self.run_id,ident,force=True)
                self.metrics['histories_refreshed']+=1
                refreshed.append(ident)
                with self.conn.cursor() as cur:
                    cur.execute('SELECT corporation_id FROM entities.character_corporation_history WHERE character_id=%s',(ident,))
                    corp_ids.update(int(r[0]) for r in cur if r[0] and r[0]>0)
        self.progress('corporation_histories',participants_done=len(characters),participants_total=len(characters))
        for ident in sorted(corp_ids):
            if self.pending_work(ident,corporation=True):
                self.metrics['affiliations_deferred']+=1
                continue
            with self.conn.cursor() as cur:
                cur.execute('SELECT alliance_id FROM entities.corporation_current_affiliation WHERE corporation_id=%s',(ident,))
                row=cur.fetchone()
            expected={p.get('alliance_id') for p in participants if p.get('corporation_id')==ident}
            if not affiliations.corporation_exists(self.conn,ident):
                affiliations.fetch_public_corporation(self.run_id,ident)
                self.metrics['entities_loaded']+=1
            if row is None or (expected and row[0] not in expected):
                history_status=affiliations.fetch_corporation_history(self.run_id,ident,force=True)
                if history_status in ('loaded','not_found'):
                    with self.conn.cursor() as cur:
                        cur.execute('''INSERT INTO entities.corporation_current_affiliation(corporation_id,alliance_id,source_history_fetched_at,affiliation_checked_at)
                            VALUES(%s,NULL,now(),now()) ON CONFLICT DO NOTHING''',(ident,))
            with self.conn.cursor() as cur:
                cur.execute('SELECT alliance_id FROM entities.corporation_alliance_history WHERE corporation_id=%s',(ident,))
                alliance_ids.update(int(r[0]) for r in cur if r[0] and r[0]>0)
        self.progress('alliance_entities')
        for ident in sorted(alliance_ids):
            if not affiliations.alliance_exists(self.conn,ident):
                affiliations.fetch_public_alliance(self.run_id,ident)
                self.metrics['entities_loaded']+=1
        # Rebuild current alliance from the fetched corporation histories, not
        # from a possibly years-old KM's affiliation.
        if refreshed:
            with self.conn.cursor() as cur:
                cur.execute('''UPDATE entities.character_current_affiliation c
                    SET alliance_id=a.alliance_id,affiliation_checked_at=now(),updated_at=now()
                    FROM entities.corporation_current_affiliation a
                    WHERE c.character_id=ANY(%s) AND a.corporation_id=c.corporation_id''',(refreshed,))
        self.conn.commit()

    def process(self, raw, sequence):
        payload, instant = decode_package(raw,sequence)
        ident = payload['killmail_id']
        month = instant.date().replace(day=1)
        try:
            if month not in self.ensured:
                importer.ensure_month_partitions(self.conn,month,month)
                self.ensured.add(month)
            inserted=importer.insert_killmail(self.conn,payload,skip_existing=True)
            self.conn.commit()
            changed=bool(inserted)
            self.refresh_entities(payload)
            with self.conn.cursor() as cur:
                cur.execute('SELECT estimated_value_cents,imported_at FROM rawkm.killmails WHERE killmail_id=%s AND killmail_time=%s',(ident,instant))
                amount,imported_at=cur.fetchone()
                cur.execute('''SELECT EXISTS(SELECT 1 FROM market.live_valuation_claims w
                    JOIN pg_locks l ON l.pid=w.owner_pid AND l.locktype='advisory'
                    AND l.classid=0 AND l.objid=%s AND l.granted
                    WHERE w.month=%s AND COALESCE(%s::timestamptz,'-infinity'::timestamptz)<=w.imported_through)''',
                    (values.LOCK,month,imported_at))
                deferred=cur.fetchone()[0]
            self.progress('valuation')
            if deferred:
                self.metrics['valuation_deferred']+=1
            else:
                if amount is None:
                    if self.pricing is None:self.pricing=LivePricing(self.conn)
                    with self.conn.cursor() as cur:
                        cur.execute('SELECT victim_ship_type_id FROM rawkm.killmails WHERE killmail_id=%s AND killmail_time=%s',(ident,instant))
                        hull=cur.fetchone()[0]
                        cur.execute('SELECT item_type_id,flag,quantity_destroyed,quantity_dropped,singleton FROM rawkm.killmail_items WHERE killmail_id=%s AND killmail_time=%s',(ident,instant))
                        items=[dict(zip(('item_type_id','flag','quantity_destroyed','quantity_dropped','singleton'),row)) for row in cur]
                    amount=kill_value(dict(victim=dict(ship_type_id=hull,items=items)),self.pricing,instant.date())
                    with self.conn.cursor() as cur:
                        cur.execute("UPDATE rawkm.killmails SET estimated_value_cents=%s,live_price_date=(killmail_time AT TIME ZONE 'UTC')::date WHERE killmail_id=%s AND killmail_time=%s AND estimated_value_cents IS NULL",(amount,ident,instant))
                        changed |= bool(cur.rowcount)
                with self.conn.cursor() as cur:
                    cur.execute('SELECT damage_done FROM rawkm.killmail_attackers WHERE killmail_id=%s AND killmail_time=%s ORDER BY attacker_index',(ident,instant))
                    credits=damage_credits([dict(damage_done=row[0]) for row in cur],amount)
                # Shares are already reproducible by every existing KB/stat query
                # from the stored value and damage; do not duplicate them in DB.
                self.metrics['true_shares_calculated']=len(credits)
            if changed:importer.mark_months_dirty(self.conn,[month])
            with self.conn.cursor() as cur:
                cur.execute('''UPDATE rawkm.live_killmail_cursor SET next_sequence=%s,last_killmail_id=%s,
                    processed=processed+1,updated_at=now() WHERE singleton AND next_sequence=%s''',(sequence+1,ident,sequence))
                if cur.rowcount!=1:raise RuntimeError('Live cursor changed unexpectedly')
            self.conn.commit()
            return dict(killmail_id=ident, inserted=bool(inserted),valued=amount is not None,**self.metrics)
        except Exception:
            self.conn.rollback()
            raise
