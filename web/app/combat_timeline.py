"""Private debug telemetry. Observations are untrusted; no killmail attribution."""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
import base64
import hashlib
import json
import re
import secrets
import sqlite3
import threading

ROOT = Path(__file__).resolve().parents[2] / 'data' / 'combat_timeline'
MAX_BODY = 262144
MAX_EVENTS = 200
KINDS = {'damage_in', 'damage_out', 'miss_in', 'miss_out', 'shield_in', 'shield_out',
         'armor_in', 'armor_out', 'hull_in', 'hull_out', 'capacitor_in', 'capacitor_out',
         'capacitor_neutralized', 'capacitor_drained', 'warp_scramble', 'warp_disruption',
         'jammed', 'loss_signal', 'jump', 'undock', 'warp', 'module', 'combat_other'}
HEX = re.compile(r'^[0-9a-f]{64}$')
_ready = set()
_lock = threading.Lock()


@contextmanager
def connection():
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = ROOT / 'timeline.sqlite3'
    if path.is_symlink():
        raise OSError('Timeline database must be a regular file')
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        with _lock:
            if str(path) not in _ready:
                path.chmod(0o600)
                conn.executescript('''PRAGMA journal_mode=WAL;
                    CREATE TABLE IF NOT EXISTS tokens(owner INTEGER PRIMARY KEY, digest TEXT UNIQUE NOT NULL, created TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS events(owner INTEGER NOT NULL,event_id TEXT NOT NULL,occurred_at TEXT NOT NULL,
                        observer TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,received_at TEXT NOT NULL,
                        PRIMARY KEY(owner,event_id));
                    CREATE INDEX IF NOT EXISTS events_kind_time ON events(kind,occurred_at);
                    CREATE INDEX IF NOT EXISTS events_time ON events(occurred_at DESC,event_id DESC,owner DESC);
                    CREATE INDEX IF NOT EXISTS events_observer_time ON events(observer,occurred_at DESC,event_id DESC,owner DESC);
                    CREATE TABLE IF NOT EXISTS streams(owner INTEGER NOT NULL,observer TEXT NOT NULL,events INTEGER NOT NULL,
                        last_at TEXT NOT NULL,received_at TEXT NOT NULL,PRIMARY KEY(owner,observer));''')
                conn.commit(); _ready.add(str(path))
        conn.execute('PRAGMA synchronous=FULL')
        yield conn
    finally:
        conn.close()


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def rotate_token(owner):
    token = 'evt_' + secrets.token_urlsafe(32)
    with connection() as conn:
        conn.execute('INSERT INTO tokens VALUES(?,?,?) ON CONFLICT(owner) DO UPDATE SET digest=excluded.digest,created=excluded.created',
                     (owner, hashlib.sha256(token.encode()).hexdigest(), now()))
        conn.commit()
    return token


def revoke_token(owner):
    with connection() as conn:
        conn.execute('DELETE FROM tokens WHERE owner=?', (owner,)); conn.commit()


def token_owner(token):
    if not isinstance(token, str) or not re.fullmatch(r'evt_[A-Za-z0-9_-]{43}', token):
        return None
    with connection() as conn:
        row = conn.execute('SELECT owner FROM tokens WHERE digest=?', (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    return int(row['owner']) if row else None


def text(value, maximum=256, empty=True):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value) or (not empty and not value.strip()):
        raise ValueError('Invalid text')
    return value


def actor(value):
    if not isinstance(value, dict) or set(value) - {'name', 'ship', 'corporation', 'alliance', 'character_id'}:
        raise ValueError('Invalid actor')
    for key, item in value.items():
        if key == 'character_id':
            if type(item) is not int or not 0 < item < 10**13: raise ValueError('Invalid character id')
        else: text(item, 256)
    return value


def timestamp(value):
    if not isinstance(value, str): raise ValueError('Invalid time')
    at = datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    if at.year < 2003 or at > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError('Invalid time')
    return value


def validate(body):
    p = json.loads(body, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid number')))
    if not isinstance(p, dict) or set(p) != {'version', 'events'} or type(p['version']) is not int or p['version'] != 1:
        raise ValueError('Invalid envelope')
    if not isinstance(p['events'], list) or not 1 <= len(p['events']) <= MAX_EVENTS: raise ValueError('Invalid event count')
    seen = set()
    for event in p['events']:
        required = {'event_id', 'occurred_at', 'observer', 'kind'}
        allowed = required | {'character_id','amount','deflected','source','target','weapon','quality','message','from_system','to_system'}
        if not isinstance(event, dict) or not required <= set(event) or set(event) - allowed: raise ValueError('Invalid fields')
        if not isinstance(event['event_id'], str) or not HEX.fullmatch(event['event_id']) or event['event_id'] in seen: raise ValueError('Invalid event id')
        seen.add(event['event_id']); timestamp(event['occurred_at']); text(event['observer'], 128, empty=False)
        if not isinstance(event['kind'], str) or event['kind'] not in KINDS: raise ValueError('Invalid event kind')
        for k, v in event.items():
            if k in {'source','target'}: actor(v)
            elif k in {'amount','deflected','character_id'}:
                if type(v) is not int or not (-10**12 <= v <= 10**12) or (k != 'amount' and v < 0): raise ValueError('Invalid amount or id')
            elif k not in required: text(v, 2048 if k == 'message' else 256)
    return p['events']


class EventConflict(ValueError):
    pass


def receive(owner, body, batch_id):
    if len(body) > MAX_BODY or not HEX.fullmatch(batch_id) or hashlib.sha256(body).hexdigest() != batch_id:
        raise ValueError('Invalid batch')
    events = validate(body); inserted = 0; received = now()
    with connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        for e in events:
            payload = json.dumps(e, sort_keys=True, ensure_ascii=True, separators=(',', ':'))
            old = conn.execute('SELECT payload FROM events WHERE owner=? AND event_id=?', (owner,e['event_id'])).fetchone()
            if old:
                if old['payload'] != payload: raise EventConflict('Event id reused with different data')
                continue
            conn.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?)',
                         (owner,e['event_id'],e['occurred_at'],e['observer'],e['kind'],payload,received))
            conn.execute('''INSERT INTO streams VALUES(?,?,1,?,?) ON CONFLICT(owner,observer) DO UPDATE SET
                events=streams.events+1,last_at=max(streams.last_at,excluded.last_at),received_at=excluded.received_at''',
                         (owner,e['observer'],e['occurred_at'],received))
            inserted += 1
        conn.commit()
    return {'version': 1, 'batch_id': batch_id, 'accepted': len(events), 'inserted': inserted, 'status': 'stored'}


def timeline(observer='', kind='', before='', date_from='', date_to='', limit=200):
    limit = max(1, min(200, int(limit)))
    where = []; values = []
    if observer: where.append('observer=?'); values.append(text(observer,128))
    if kind:
        if kind not in KINDS: raise ValueError('Invalid type')
        where.append('kind=?'); values.append(kind)
    for bound, operator in [(date_from, '>='), (date_to, '<=')]:
        if bound:
            timestamp(bound); where.append('occurred_at'+operator+'?'); values.append(bound)
    if before:
        if len(before)>512: raise ValueError('Invalid cursor')
        try:
            cur = json.loads(base64.urlsafe_b64decode(before+'='*(-len(before)%4)))
            if len(cur)!=3 or not HEX.fullmatch(cur[1]) or type(cur[2]) is not int: raise ValueError()
            timestamp(cur[0])
        except (ValueError, TypeError, KeyError): raise ValueError('Invalid cursor')
        where.append('(occurred_at,event_id,owner)<(?,?,?)'); values.extend(cur)
    with connection() as conn:
        rows = conn.execute('SELECT * FROM events '+('WHERE '+' AND '.join(where) if where else '')+
                            ' ORDER BY occurred_at DESC,event_id DESC,owner DESC LIMIT ?', values+[limit+1]).fetchall()
        streams = [dict(row) for row in conn.execute('SELECT observer,sum(events) AS events,max(last_at) AS last_at,max(received_at) AS received_at FROM streams GROUP BY observer ORDER BY observer')]
    more = len(rows)>limit; rows=rows[:limit]; cursor=''
    if more:
        last=rows[-1];cursor=base64.urlsafe_b64encode(json.dumps([last['occurred_at'],last['event_id'],last['owner']]).encode()).decode().rstrip('=')
    return {'events':[dict(json.loads(row['payload']),received_at=row['received_at'],collector_owner=row['owner']) for row in rows],
            'streams':streams,'next':cursor,'total':sum(row['events'] for row in streams)}
