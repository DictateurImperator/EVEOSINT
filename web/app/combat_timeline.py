"""Private PostgreSQL debug intake. Receipt writes only; no killmail lookups."""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
import base64
import hashlib
import json
import re
import secrets
from psycopg2.extras import RealDictCursor, Json
from .db import db

ROOT = Path(__file__).resolve().parents[2] / 'data' / 'combat_timeline'
MAINTENANCE = Path('/run/eveosint-maintenance/active')
MAX_BODY = 8 * 1024 * 1024  # Transport chunk size, never a history/file limit.
KINDS = {'damage_in','damage_out','miss_in','miss_out','shield_in','shield_out',
 'armor_in','armor_out','hull_in','hull_out','capacitor_in','capacitor_out',
 'capacitor_neutralized','capacitor_drained','warp_scramble','warp_disruption',
 'jammed','loss_signal','jump','undock','warp','module','combat_other','other'}
HEX = re.compile(r'^[0-9a-f]{64}$')

class EventConflict(ValueError): pass
class Maintenance(Exception): pass

@contextmanager
def connection():
    conn = db()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def setup():
    with connection() as conn, conn.cursor() as cur:
        cur.execute(Path(__file__).with_name('combat_dev_schema.sql').read_text())


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def utc(value):
    return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') if isinstance(value,datetime) else value


def check_writing():
    if MAINTENANCE.exists(): raise Maintenance()


def rotate_token(owner):
    token = 'evt_' + secrets.token_urlsafe(32)
    with connection() as conn, conn.cursor() as cur:
        cur.execute('''INSERT INTO devencour.collector_tokens(owner,digest) VALUES(%s,%s)
          ON CONFLICT(owner) DO UPDATE SET digest=excluded.digest,created_at=clock_timestamp()''',
          (owner, hashlib.sha256(token.encode()).hexdigest()))
    return token


def revoke_token(owner):
    with connection() as conn, conn.cursor() as cur:
        cur.execute('DELETE FROM devencour.collector_tokens WHERE owner=%s',(owner,))


def token_owner(token):
    if not isinstance(token,str) or not re.fullmatch(r'evt_[A-Za-z0-9_-]{43}',token): return None
    with connection() as conn, conn.cursor() as cur:
        cur.execute('SELECT owner FROM devencour.collector_tokens WHERE digest=%s',(hashlib.sha256(token.encode()).hexdigest(),))
        row=cur.fetchone()
    return int(row[0]) if row else None


def text(value,maximum=256,empty=True):
    if not isinstance(value,str) or len(value)>maximum or any(ord(c)<32 for c in value) or (not empty and not value.strip()):
        raise ValueError('Invalid text')
    return value


def timestamp(value):
    if not isinstance(value,str): raise ValueError('Invalid time')
    at=datetime.strptime(value,'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    if at.year<2003 or at>datetime.now(timezone.utc)+timedelta(minutes=5): raise ValueError('Invalid time')
    return value


def identifier(value):
    if not isinstance(value,str) or not HEX.fullmatch(value): raise ValueError('Invalid id')


def amount(value):
    if type(value) is not int or not 0<=value<=10**15: raise ValueError('Invalid amount')


def validate_event(e):
    if not isinstance(e,dict) or not {'version','event_id','occurred_at','observer','character_id','category','raw','kind'}<=set(e) or e['version']!=2:
        raise ValueError('Invalid event')
    identifier(e['event_id']);timestamp(e['occurred_at']);text(e['observer'],128,False)
    text(e['category']);text(e['kind'])
    if not isinstance(e['raw'],str) or '\x00' in e['raw']: raise ValueError('Invalid raw event')
    if type(e['character_id']) is not int or not 0<=e['character_id']<10**13: raise ValueError('Invalid character')
    for key in ('amount','deflected'):
        if key in e and (type(e[key]) is not int or not -10**15<=e[key]<=10**15): raise ValueError('Invalid amount')
    for key in ('source','target'):
        if key not in e: continue
        if not isinstance(e[key],dict): raise ValueError('Invalid actor')
        for field,value in e[key].items():
            if field=='character_id':
                if type(value) is not int or not 0<value<10**13: raise ValueError('Invalid actor id')
            else: text(value)
    for key in ('message','weapon','quality','from_system','to_system'):
        if key in e: text(e[key],2048 if key=='message' else 256)
    return e


def validate_summary(p):
    if not isinstance(p,dict) or set(p)!={'version','summary_id','character_id','character_name','death_signal_at','window_seconds','emitted','received_healing'} or p['version']!=2 or p['window_seconds']!=1800:
        raise ValueError('Invalid summary')
    identifier(p['summary_id']);timestamp(p['death_signal_at']);text(p['character_name'],128,False)
    if type(p['character_id']) is not int or p['character_id']<=0: raise ValueError('Invalid character')
    if not isinstance(p['emitted'],dict) or set(p['emitted'])!={'damage_hp','healing_hp','capacitor_gj'}: raise ValueError('Invalid totals')
    for v in p['emitted'].values(): amount(v)
    if not isinstance(p['received_healing'],list): raise ValueError('Invalid healing')
    names=set()
    for h in p['received_healing']:
        if not isinstance(h,dict) or not {'name','hp'}<=set(h) or set(h)-{'name','ship','corporation','alliance','character_id','hp'}: raise ValueError('Invalid healer')
        text(h['name'],256,False);amount(h['hp'])
        if h['name'].casefold() in names: raise ValueError('Duplicate healer')
        names.add(h['name'].casefold())
        for key in ('ship','corporation','alliance'):
            if key in h: text(h[key])
        if 'character_id' in h and (type(h['character_id']) is not int or h['character_id']<=0): raise ValueError('Invalid healer id')
    return p


def parse_json(body):
    def invalid(_): raise ValueError('Invalid JSON number')
    return json.loads(body,parse_constant=invalid)


def receive(owner,body,batch_id,summary=False):
    if len(body)>MAX_BODY or not HEX.fullmatch(batch_id) or hashlib.sha256(body).hexdigest()!=batch_id: raise ValueError('Invalid batch')
    check_writing()
    records=[validate_summary(parse_json(body))] if summary else [validate_event(parse_json(line)) for line in body.splitlines() if line.strip()]
    if not records: raise ValueError('Empty batch')
    inserted=0
    with connection() as conn, conn.cursor() as cur:
        for p in records:
            # Each raw record commits independently. A summary and ALL its healers
            # form one indivisible write. No additional write starts in maintenance.
            check_writing()
            if summary:
                cur.execute('''INSERT INTO devencour.death_summaries(owner,summary_id,character_id,observer,occurred_at,payload)
                  VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING summary_id''',
                  (owner,p['summary_id'],p['character_id'],p['character_name'],p['death_signal_at'],Json(p)))
                fresh=bool(cur.fetchone())
                if fresh:
                    for i,h in enumerate(p['received_healing']):
                        cur.execute('INSERT INTO devencour.received_healing VALUES(%s,%s,%s,%s)',(owner,p['summary_id'],i,Json(h)))
                table,key,ident='death_summaries','summary_id',p['summary_id']
            else:
                cur.execute('''INSERT INTO devencour.raw_events(owner,event_id,observer,character_id,occurred_at,kind,payload)
                  VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING event_id''',
                  (owner,p['event_id'],p['observer'],p['character_id'],p['occurred_at'],p['kind'],Json(p)))
                fresh=bool(cur.fetchone());table,key,ident='raw_events','event_id',p['event_id']
            if not fresh:
                cur.execute(f'SELECT payload FROM devencour.{table} WHERE owner=%s AND {key}=%s',(owner,ident))
                if cur.fetchone()[0]!=p: raise EventConflict('Id reused with different data')
            if fresh:
                at=p['death_signal_at'] if summary else p['occurred_at']
                name=p['character_name'] if summary else p['observer']
                cur.execute('''INSERT INTO devencour.collector_streams(owner,observer,character_id,events,first_at,last_at)
                  VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(owner,observer) DO UPDATE SET
                  character_id=greatest(collector_streams.character_id,excluded.character_id),
                  events=collector_streams.events+excluded.events,first_at=least(collector_streams.first_at,excluded.first_at),
                  last_at=greatest(collector_streams.last_at,excluded.last_at),received_at=clock_timestamp()''',
                  (owner,name,p['character_id'],0 if summary else 1,at,at))
            conn.commit()
            inserted+=int(fresh)
    return dict(version=2,batch_id=batch_id,accepted=len(records),inserted=inserted,status='stored')


def streams(cur):
    cur.execute('''SELECT observer,sum(events)::bigint AS events,min(first_at) AS first_at,max(last_at) AS last_at,
      max(received_at) AS received_at,max(character_id) AS character_id
      FROM devencour.collector_streams GROUP BY observer ORDER BY observer''')
    return [{k:utc(v) for k,v in dict(r).items()} for r in cur.fetchall()]


def timeline(observer='',kind='',before='',date_from='',date_to='',limit=200):
    limit=max(1,min(200,int(limit)));where=[];values=[]
    if observer: where.append('observer=%s');values.append(text(observer,128))
    if kind: where.append('kind=%s');values.append(text(kind))
    for bound,op in ((date_from,'>='),(date_to,'<=')):
        if bound: timestamp(bound);where.append('occurred_at'+op+'%s');values.append(bound)
    if before:
        try:
            cursor=json.loads(base64.urlsafe_b64decode(before+'='*(-len(before)%4)))
            if len(cursor)!=3 or type(cursor[2]) is not int: raise ValueError()
            timestamp(cursor[0]);identifier(cursor[1])
        except Exception: raise ValueError('Invalid cursor')
        where.append('(occurred_at,event_id,owner)<(%s,%s,%s)');values.extend(cursor)
    with connection() as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('SELECT * FROM devencour.raw_events '+('WHERE '+' AND '.join(where) if where else '')+' ORDER BY occurred_at DESC,event_id DESC,owner DESC LIMIT %s',values+[limit+1])
        rows=cur.fetchall();groups=streams(cur)
    more=len(rows)>limit;rows=rows[:limit];cursor=''
    if more:
        last=rows[-1];cursor=base64.urlsafe_b64encode(json.dumps([utc(last['occurred_at']),last['event_id'],last['owner']]).encode()).decode().rstrip('=')
    return dict(events=[dict(r['payload'],received_at=utc(r['received_at']),collector_owner=r['owner']) for r in rows],streams=groups,next=cursor,total=sum(r['events'] for r in groups))
