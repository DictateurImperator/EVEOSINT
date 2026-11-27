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
        _status.update(status='running', recipients=0, killmails=0, scores=0)
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
                return dict(recipients=0,killmails=0,scores=0)
            first = max(first, date_from) if date_from else first
            last = min(last+timedelta(minutes=30), date_to) if date_to else last+timedelta(minutes=30)
            day = last.replace(hour=0,minute=0,second=0,microsecond=0)
            counts = dict(recipients=0,killmails=0,scores=0)
            while day <= last and day+timedelta(days=1) > first:
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
                day -= timedelta(days=1)
            return counts
        finally:
            lock.execute('SELECT pg_advisory_unlock(17320617,1)')
