"""Deferred support scores. Reads existing KMs, writes only devencour.

One chronological pass per recipient/day; a rolling 30-minute window avoids
re-reading events for each KM. Only final per-pilot scores are retained.
"""
from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timedelta, timezone
import logging
import threading
from psycopg2.extras import Json
from . import combat_timeline as store
from .combat_killmail_links import read_killboard
from .db import db

log = logging.getLogger(__name__)
KINDS = ('shield_in','armor_in','hull_in','shield_out','armor_out','hull_out','capacitor_in','capacitor_out')
_status = {'status': 'idle'}
_guard = threading.Lock()


def status():
    with _guard:
        return dict(_status)


def progress(**values):
    with _guard:
        _status.update(values)


def start():
    with _guard:
        if _status.get('status') == 'running':
            return False
        _status.clear()
        _status.update(status='running', recipients=0, killmails=0, scores=0,marked=0,purged=0)
    threading.Thread(target=_background, daemon=True, name='combat-support').start()
    return True


def _background():
    try:
        from .combat_killmail_links import refresh
        if not refresh(force=True):
            raise RuntimeError('Killmail matching is already running')
        result = recalculate()
        progress(status='finished', **result)
    except store.Maintenance:
        progress(status='paused', message='Maintenance: run recalculation again when ready.')
    except Exception:
        log.exception('Deferred combat support calculation failed')
        progress(status='error', message='Support recalculation failed; stored observations remain available.')


def actor_key(actor):
    return (actor.get('name') or '').casefold()


def transfers(events, recipient):
    """Normalize either player's view. A conflicting explicit ID never falls back to a name."""
    for e in events:
        if e.get('kind') not in KINDS or (e.get('amount') or 0) <= 0:
            continue
        incoming = e['kind'].endswith('_in')
        target = e.get('target') or {}
        if incoming:
            if e.get('character_id') != recipient['id']:
                continue
        elif target.get('character_id'):
            if target['character_id'] != recipient['id']:
                continue
        elif target.get('name', '').casefold() != recipient['name'].casefold():
            continue
        source = dict(e.get('source') or {})
        if not source.get('name') or actor_key(source) == recipient['name'].casefold() or source.get('character_id') == recipient['id']:
            continue
        at = datetime.fromisoformat(e['occurred_at'].replace('Z', '+00:00'))
        resource = 'capacitor' if e['kind'].startswith('capacitor') else 'healing'
        # Receiver and sender totals are compared, not added; duplicate owners
        # are eliminated by event_id in the read query before reaching this pass.
        view = ('in' if incoming else 'out', e['observer'].casefold())
        yield at, actor_key(source), resource, view, e['amount'], source


def rolling_scores(entries, events, recipient):
    """Yield recipient-local scores. The caller sums these across KM attackers."""
    queue = deque()
    totals = defaultdict(dict)
    actors = {}
    iterator = iter(transfers(events, recipient))
    current = next(iterator, None)
    for entry in sorted(entries, key=lambda r: (r[1], r[0])):
        km_id, end, side = entry[:3]
        start = end - timedelta(minutes=30)
        while current is not None and current[0] <= end:
            at, source, resource, view, value, actor = current
            key = (source, resource)
            totals[key][view] = totals[key].get(view, 0) + value
            actors.setdefault(source, {}).update({k:v for k,v in actor.items() if v})
            queue.append(current)
            current = next(iterator, None)
        while queue and queue[0][0] < start:
            _, source, resource, view, value, _ = queue.popleft()
            totals[(source, resource)][view] -= value
            if not any(totals[(source, resource)].values()):
                del totals[(source, resource)]
        yield km_id, side, {
            (source, resource): dict(amount=max(views.values()), source=dict(actors[source]))
            for (source, resource), views in totals.items() if max(views.values()) > 0
        }


def recipients(first, last):
    """Include targets whose own reader never uploaded: healer logs suffice."""
    with closing(db()) as conn:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute('''SELECT DISTINCT character_id,observer FROM devencour.collector_streams
                WHERE character_id>0 AND first_at<=%s AND last_at>=%s''', (last, first))
            rows = {r[0]: dict(id=r[0], name=r[1]) for r in cur.fetchall()}
            cur.execute('''SELECT DISTINCT payload->'target'->>'name' FROM devencour.raw_events
                WHERE occurred_at BETWEEN %s AND %s
                  AND kind IN ('shield_out','armor_out','hull_out','capacitor_out')''', (first, last))
            names = [r[0] for r in cur.fetchall() if r[0]]
            if names:
                cur.execute('SELECT character_id,name FROM entities.characters WHERE name=ANY(%s)', (names,))
                rows.update({r[0]: dict(id=r[0], name=r[1]) for r in cur.fetchall()})
    return list(rows.values())


def events_for(recipient, first, last):
    with closing(db()) as conn:
        conn.set_session(readonly=True)
        # Server-side cursor keeps a long history out of Python memory.
        with conn.cursor(name='combat_support_events') as cur:
            cur.itersize = 2000
            cur.execute('''SELECT payload FROM (
                SELECT DISTINCT ON(event_id) event_id,occurred_at,payload FROM devencour.raw_events
                WHERE occurred_at BETWEEN %s AND %s AND (
                  (character_id=%s AND kind IN ('shield_in','armor_in','hull_in','capacitor_in')) OR
                  (lower(payload->'target'->>'name')=%s AND kind IN ('shield_out','armor_out','hull_out','capacitor_out')))
                ORDER BY event_id,received_at,owner
              ) e ORDER BY occurred_at,event_id''', (first,last,recipient['id'],recipient['name'].casefold()))
            for row in cur:
                store.check_writing()
                yield row[0]


def summary_scores(km_id, recipient):
    with store.connection() as conn, conn.cursor() as cur:
        cur.execute('''SELECT s.payload FROM devencour.death_summaries s
            JOIN devencour.summary_killmail_links l USING(owner,summary_id)
            WHERE l.killmail_id=%s AND s.character_id=%s''', (km_id,recipient['id']))
        result = {}
        for (payload,) in cur.fetchall():
            for field,resource,unit in (('received_healing','healing','hp'),('received_capacitor','capacitor','gj')):
                for actor in payload.get(field, []):
                    key = (actor_key(actor),resource)
                    value = actor.get(unit,0)
                    if key[0] and value > result.get(key,{}).get('amount',0):
                        result[key] = dict(amount=value,source={k:v for k,v in actor.items() if k not in ('hp','gj')})
        return result


def save_scores(scores):
    store.check_writing()
    with store.connection() as conn, conn.cursor() as cur:
        for (km_id,side,source,resource), value in scores.items():
            store.check_writing()
            cur.execute('''INSERT INTO devencour.killmail_support
                (killmail_id,side,resource,supporter_key,amount,source) VALUES(%s,%s,%s,%s,%s,%s)
                ON CONFLICT(killmail_id,side,resource,supporter_key) DO UPDATE SET
                amount=greatest(killmail_support.amount,excluded.amount),
                source=killmail_support.source || excluded.source,calculated_at=clock_timestamp()''',
                (km_id,side,resource,source,value['amount'],Json(value['source'])))



def recover_activity(recipient, entries):
    """Explicit sync recovers missing death summaries from the victim's logs.

    Known losses provide the UTC end time. Deduplicate identical event IDs
    across owners; a window without recorded combat actions creates no row.
    Never rebuild a transmitted summary or modify an original killmail.
    """
    losses = sorted({(e[0],e[1]) for e in entries if e[2]=='loss'})
    if not losses:
        return
    first=min(at for _,at in losses)-timedelta(minutes=30)
    last=max(at for _,at in losses)
    store.check_writing()
    with store.connection() as conn,conn.cursor() as cur:
        cur.execute('''WITH observations AS (
            SELECT DISTINCT ON(event_id) event_id,occurred_at,kind,payload
            FROM devencour.raw_events
            WHERE character_id=%s AND occurred_at BETWEEN %s AND %s
              AND kind IN ('damage_in','damage_out','shield_in','shield_out',
                           'armor_in','armor_out','hull_in','hull_out','capacitor_in','capacitor_out')
            ORDER BY event_id,received_at,owner
          ), losses(killmail_id,at) AS (VALUES '''+','.join(['(%s::bigint,%s::timestamptz)']*len(losses))+''')
          SELECT k.killmail_id,
            coalesce(sum((e.payload->>'amount')::bigint) FILTER (WHERE e.kind='damage_out'),0),
            coalesce(sum((e.payload->>'amount')::bigint) FILTER (WHERE e.kind IN ('shield_out','armor_out','hull_out')),0),
            coalesce(sum((e.payload->>'amount')::bigint) FILTER (WHERE e.kind='capacitor_out'),0)
          FROM losses k JOIN observations e
            ON e.occurred_at BETWEEN k.at-interval '30 minutes' AND k.at
          WHERE NOT EXISTS (
            SELECT 1 FROM devencour.summary_killmail_links l
            JOIN devencour.death_summaries s USING(owner,summary_id)
            WHERE l.killmail_id=k.killmail_id AND s.character_id=%s)
          GROUP BY k.killmail_id''',
          [recipient['id'],first,last]+[v for loss in losses for v in loss]+[recipient['id']])
        rows=cur.fetchall()
        for km_id,damage,healing,capacitor in rows:
            store.check_writing()
            cur.execute('''INSERT INTO devencour.killmail_activity
                (killmail_id,character_id,damage_hp,healing_hp,capacitor_gj)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT(killmail_id) DO UPDATE SET
                damage_hp=greatest(killmail_activity.damage_hp,excluded.damage_hp),
                healing_hp=greatest(killmail_activity.healing_hp,excluded.healing_hp),
                capacitor_gj=greatest(killmail_activity.capacitor_gj,excluded.capacitor_gj),
                calculated_at=clock_timestamp()''',(km_id,recipient['id'],damage,healing,capacitor))


def processing_time(cur):
    cur.execute('SELECT clock_timestamp()')
    return cur.fetchone()[0]


def prepare_run(conn, first, last):
    """Freeze the eligible IDs, not intake: concurrent uploads stay unmarked.

    A successful first pass marks a row. Marked rows are due on every run
    during their first three days, then once more at age seven days. Only
    rows already marked when this run began can be removed after success.
    """
    with conn.cursor() as cur:
        started=processing_time(cur)
        cur.execute('''CREATE TEMP TABLE combat_completion_candidates ON COMMIT PRESERVE ROWS AS
            SELECT owner,event_id,occurred_at,processed_at IS NOT NULL AS previously_processed
            FROM devencour.raw_events
            WHERE occurred_at>=%s AND occurred_at<=%s AND (
                processed_at IS NULL OR occurred_at>%s OR occurred_at<=%s)''',
            (first,last-timedelta(minutes=30),started-timedelta(days=3),started-timedelta(days=7)))
        cur.execute('ALTER TABLE combat_completion_candidates ADD PRIMARY KEY(owner,event_id)')
        cur.execute('''SELECT DISTINCT day FROM (
            SELECT date_trunc('day',occurred_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS day
                FROM combat_completion_candidates
            UNION SELECT date_trunc('day',(occurred_at+interval '30 minutes') AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                FROM combat_completion_candidates
            UNION SELECT date_trunc('day',occurred_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                FROM devencour.death_summaries WHERE occurred_at BETWEEN %s AND %s
            UNION SELECT date_trunc('day',(occurred_at+interval '30 seconds') AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                FROM devencour.death_summaries WHERE occurred_at BETWEEN %s AND %s
            ) due ORDER BY day DESC''',(first,last,first,last))
        days=[row[0] for row in cur.fetchall() if row[0]<=last and row[0]+timedelta(days=1)>first]
    conn.commit()
    return started,days


def finish_run(conn, started):
    """Called only after ALL scores were saved. No destructive work on failure.

    Commit small batches so maintenance can pause cleanup between batches.
    Previously unmarked historical uploads get a first mark, never an
    immediate deletion: a later run must actually retry them first.
    """
    marked=purged=0
    batch='''SELECT owner,event_id,previously_processed FROM combat_completion_candidates
        ORDER BY owner,event_id LIMIT 5000'''
    while True:
        store.check_writing()
        with conn.cursor() as cur:
            cur.execute('UPDATE devencour.raw_events r SET processed_at=clock_timestamp() FROM ('+batch+
                ') c WHERE r.owner=c.owner AND r.event_id=c.event_id')
            marked_batch=cur.rowcount
            store.check_writing()
            cur.execute('DELETE FROM devencour.raw_events r USING ('+batch+
                ') c WHERE r.owner=c.owner AND r.event_id=c.event_id AND c.previously_processed '
                'AND r.processed_at IS NOT NULL AND r.occurred_at<=%s',(started-timedelta(days=7),))
            purged_batch=cur.rowcount
            cur.execute('DELETE FROM combat_completion_candidates r USING ('+batch+
                ') c WHERE r.owner=c.owner AND r.event_id=c.event_id')
            consumed=cur.rowcount
        conn.commit()
        marked+=marked_batch;purged+=purged_batch
        progress(marked=marked,purged=purged)
        if not consumed:
            return dict(marked=marked,purged=purged)


def recalculate(date_from=None, date_to=None):
    store.check_writing()
    # A DB lock also excludes a second CLI/process, not only another UI click.
    with store.connection() as lock_conn, lock_conn.cursor() as lock:
        lock.execute('SELECT pg_try_advisory_lock(17320617,1)')
        if not lock.fetchone()[0]:
            raise RuntimeError('Support recalculation is already running')
        try:
            with store.connection() as conn, conn.cursor() as cur:
                cur.execute('SELECT min(first_at),max(last_at) FROM devencour.collector_streams')
                first,last = cur.fetchone()
            if first is None:
                return dict(recipients=0,killmails=0,scores=0,marked=0,purged=0)
            first = max(first, date_from) if date_from else first
            last = min(last+timedelta(minutes=30), date_to) if date_to else last+timedelta(minutes=30)
            started,days=prepare_run(lock_conn,first,last)
            counts = dict(recipients=0,killmails=0,scores=0)
            for day in days:
                end = min(day+timedelta(days=1),last)
                begin = max(day,first)
                scores = {}
                for recipient in recipients(begin-timedelta(minutes=30),end):
                    store.check_writing()
                    # End is exclusive at midnight, preventing double scoring across days.
                    entries = [e for e in read_killboard(recipient['id'],begin,end)
                               if begin<=e[1]<=last and (e[1]<end or end==last)]
                    if not entries:
                        continue
                    recover_activity(recipient,entries)
                    counts['recipients'] += 1
                    for km_id,side,local in rolling_scores(entries,events_for(recipient,begin-timedelta(minutes=30),end),recipient):
                        if side=='loss':
                            for key,value in summary_scores(km_id,recipient).items():
                                if value['amount'] > local.get(key,{}).get('amount',0):
                                    local[key]=value
                        for (source,resource), value in local.items():
                            key=(km_id,'defense' if side=='loss' else 'attack',source,resource)
                            row=scores.setdefault(key,dict(amount=0,source={}))
                            row['amount']+=value['amount']
                            row['source'].update(value['source'])
                        counts['killmails'] += 1
                    progress(day=day.date().isoformat(), **counts)
                save_scores(scores)
                counts['scores'] += len(scores)
                progress(day=day.date().isoformat(), **counts)
            counts.update(finish_run(lock_conn,started))
            return counts
        finally:
            lock_conn.rollback()
            lock.execute('SELECT pg_advisory_unlock(17320617,1)')
