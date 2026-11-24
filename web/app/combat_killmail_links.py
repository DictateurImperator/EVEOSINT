"""Read a character's killboard; keep debug links only in the timeline SQLite DB."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import logging
import threading
from .db import db
from . import combat_timeline as store

logger = logging.getLogger(__name__)
_lock = threading.Lock()


def schema(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS combat_killmails (
            owner INTEGER NOT NULL, observer TEXT NOT NULL, character_id INTEGER NOT NULL,
            killmail_id INTEGER NOT NULL, occurred_at TEXT NOT NULL, side TEXT NOT NULL,
            ship_type_id INTEGER, ship_name TEXT, is_pod INTEGER,
            PRIMARY KEY(owner,observer,killmail_id));
        CREATE INDEX IF NOT EXISTS combat_killmails_time ON combat_killmails(occurred_at DESC,killmail_id DESC);
        CREATE TABLE IF NOT EXISTS combat_killmail_scans (
            owner INTEGER NOT NULL,observer TEXT NOT NULL,events INTEGER NOT NULL,
            checked_at TEXT NOT NULL,status TEXT NOT NULL,PRIMARY KEY(owner,observer));
        CREATE TABLE IF NOT EXISTS combat_loss_links (
            owner INTEGER NOT NULL,event_id TEXT NOT NULL,status TEXT NOT NULL,
            killmail_id INTEGER, candidates TEXT NOT NULL,checked_at TEXT NOT NULL,
            PRIMARY KEY(owner,event_id));
    ''')


def utc(value):
    return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def read_killboard(character_id, first, last):
    """The existing KM store is strictly read-only, including the transaction."""
    with closing(db()) as conn:
        conn.set_session(readonly=True)
        with conn, conn.cursor() as cur:
            cur.execute('''WITH entries AS (
                SELECT k.killmail_id,k.killmail_time,'loss' AS side
                FROM rawkm.killmails k WHERE k.victim_character_id=%s
                  AND k.killmail_time BETWEEN %s AND %s
                UNION
                SELECT a.killmail_id,a.killmail_time,'kill' AS side
                FROM rawkm.killmail_attackers a WHERE a.character_id=%s
                  AND a.killmail_time BETWEEN %s AND %s
                  AND EXISTS (SELECT 1 FROM rawkm.killmails v
                      WHERE v.killmail_id=a.killmail_id AND v.killmail_time=a.killmail_time
                        AND v.victim_character_id IS DISTINCT FROM %s)
            ) SELECT e.killmail_id,e.killmail_time,e.side,k.victim_ship_type_id,
                t.data->'name'->>'en',t.data->>'groupID'
            FROM entries e JOIN rawkm.killmails k
              ON k.killmail_id=e.killmail_id AND k.killmail_time=e.killmail_time
            LEFT JOIN public.sde_types t ON t.sde_key=k.victim_ship_type_id::text
            ORDER BY e.killmail_time,e.killmail_id''',
                (character_id,first,last,character_id,first,last,character_id))
            while True:
                rows=cur.fetchmany(1000)
                if not rows: break
                yield from rows


def choose_loss(at, candidates):
    """Keep ambiguous and unknown losses explicit; never invent a killmail ID."""
    nearby=[(abs((datetime.fromisoformat(k['occurred_at'].replace('Z','+00:00'))-at).total_seconds()), k['killmail_id'])
            for k in candidates]
    nearby=[p for p in nearby if p[0]<=30]
    if not nearby: return 'unknown',None,[]
    distance=min(p[0] for p in nearby)
    best=sorted({p[1] for p in nearby if p[0]==distance})
    return ('linked',best[0],best) if len(best)==1 else ('ambiguous',None,best)


def refresh():
    """Called by the site after intake and while the debug page is open."""
    with _lock:
        with store.connection() as conn:
            schema(conn)
            streams=[dict(r) for r in conn.execute('''SELECT s.*,x.events AS scanned_events,x.checked_at
                FROM streams s LEFT JOIN combat_killmail_scans x USING(owner,observer)''')]
        for stream in streams:
            checked=stream['checked_at']
            if checked:
                age=(datetime.now(timezone.utc)-datetime.fromisoformat(checked.replace('Z','+00:00'))).total_seconds()
                if age<30 or (stream['scanned_events']==stream['events'] and age<300): continue
            try:
                refresh_stream(stream)
            except Exception:
                logger.exception('Combat debug killboard lookup failed')
                with store.connection() as conn:
                    schema(conn)
                    conn.execute('INSERT OR REPLACE INTO combat_killmail_scans VALUES(?,?,?,?,?)',
                        (stream['owner'],stream['observer'],stream['events'],store.now(),'error'))
                    conn.commit()


def refresh_stream(stream):
    owner,observer=stream['owner'],stream['observer']
    with store.connection() as conn:
        bounds=conn.execute('SELECT min(occurred_at),max(occurred_at) FROM events WHERE owner=? AND observer=?',(owner,observer)).fetchone()
        identity=conn.execute("SELECT payload FROM events WHERE owner=? AND observer=? AND json_extract(payload,'$.character_id')>0 LIMIT 1",(owner,observer)).fetchone()
    if not identity: return
    character_id=json.loads(identity['payload'])['character_id']
    first=datetime.fromisoformat(bounds[0].replace('Z','+00:00'))-timedelta(seconds=30)
    last=datetime.fromisoformat(bounds[1].replace('Z','+00:00'))+timedelta(seconds=30)
    # PostgreSQL only supplies KB rows; every write below targets separate SQLite.
    with store.connection() as conn:
        schema(conn)
        for km_id,at,side,ship_id,ship_name,group_id in read_killboard(character_id,first,last):
            conn.execute('INSERT OR REPLACE INTO combat_killmails VALUES(?,?,?,?,?,?,?,?,?)',
                (owner,observer,character_id,km_id,utc(at),side,ship_id,ship_name,
                 None if group_id is None else int(group_id=='29')))
        losses=[dict(r) for r in conn.execute("SELECT * FROM combat_killmails WHERE owner=? AND observer=? AND side='loss'",(owner,observer))]
        signals=conn.execute("SELECT event_id,occurred_at FROM events WHERE owner=? AND observer=? AND kind='loss_signal'",(owner,observer)).fetchall()
        for signal in signals:
            status,km_id,candidates=choose_loss(datetime.fromisoformat(signal['occurred_at'].replace('Z','+00:00')),losses)
            conn.execute('INSERT OR REPLACE INTO combat_loss_links VALUES(?,?,?,?,?,?)',
                (owner,signal['event_id'],status,km_id,json.dumps(candidates),store.now()))
        conn.execute('INSERT OR REPLACE INTO combat_killmail_scans VALUES(?,?,?,?,?)',
            (owner,observer,stream['events'],store.now(),'complete'))
        conn.commit()


def report(observer='', page=1, date_from='', date_to='', month='', side='both', killmail_id=''):
    if observer: store.text(observer,128)
    page=max(1,int(page));per_page=100
    if side not in ('both','kills','losses'): raise ValueError('Invalid side')
    filters=[];values=[]
    if observer: filters.append('observer=?');values.append(observer)
    if month:
        datetime.strptime(month,'%Y-%m')
        filters.append('substr(occurred_at,1,7)=?');values.append(month)
    for value,operator in ((date_from,'>='),(date_to,'<=')):
        if value:
            store.timestamp(value);filters.append('occurred_at'+operator+'?');values.append(value)
    if killmail_id:
        if not str(killmail_id).isascii() or not str(killmail_id).isdigit(): raise ValueError('Invalid killmail id')
        filters.append('killmail_id=?');values.append(int(killmail_id))
    where=' WHERE '+' AND '.join(filters) if filters else ''
    # Deduplicate KMs shared by several observers. A loss takes precedence.
    grouped="SELECT killmail_id,occurred_at,max(side='loss') AS is_loss FROM combat_killmails"+where+' GROUP BY killmail_id,occurred_at'
    if side!='both': grouped+=' HAVING is_loss='+('1' if side=='losses' else '0')
    with store.connection() as conn:
        schema(conn)
        total=conn.execute('SELECT count(*) FROM ('+grouped+')',values).fetchone()[0]
        pages=max(1,(total+per_page-1)//per_page);page=min(page,pages)
        rows=[dict(r) for r in conn.execute(grouped+' ORDER BY occurred_at DESC,killmail_id DESC LIMIT ? OFFSET ?',values+[per_page,(page-1)*per_page])]
        # Keep the compact report fields used by the debug API.
        for row in rows:
            detail=conn.execute('SELECT * FROM combat_killmails WHERE killmail_id=? AND occurred_at=?'+(' AND observer=?' if observer else '')+" ORDER BY (side='loss') DESC LIMIT 1",
                [row['killmail_id'],row['occurred_at']]+([observer] if observer else [])).fetchone()
            row.update(dict(detail));row['side']='loss' if row['is_loss'] else 'kill';row['url']='/kill/'+str(row['killmail_id'])
        unknown_filters=["e.kind='loss_signal'","l.status!='linked'"];unknown_values=[]
        if observer: unknown_filters.append('e.observer=?');unknown_values.append(observer)
        if month: unknown_filters.append('substr(e.occurred_at,1,7)=?');unknown_values.append(month)
        for value,operator in ((date_from,'>='),(date_to,'<=')):
            if value: unknown_filters.append('e.occurred_at'+operator+'?');unknown_values.append(value)
        unknown=[dict(r) for r in conn.execute('SELECT e.observer,e.occurred_at,e.event_id,l.status,l.candidates FROM events e JOIN combat_loss_links l USING(owner,event_id) WHERE '+' AND '.join(unknown_filters)+' ORDER BY e.occurred_at DESC',unknown_values)]
        scans=[dict(r) for r in conn.execute('SELECT * FROM combat_killmail_scans'+(' WHERE observer=?' if observer else ''),([observer] if observer else []))]
        months=[r[0] for r in conn.execute("SELECT DISTINCT substr(occurred_at,1,7) FROM events"+(' WHERE observer=?' if observer else '')+' ORDER BY 1 DESC',([observer] if observer else []))]
    pagination=dict(page=page,per_page=per_page,total=total,pages=pages,has_prev=page>1,has_next=page<pages,prev_page=max(1,page-1),next_page=min(pages,page+1))
    return dict(killmails=rows,unmatched_losses=unknown,scans=scans,months=months,pagination=pagination)


def killboard_page(report, side='both'):
    # Reuse the production KB loader and row template, not a debug copy.
    from .entities import _hydrate_global_killmail_entries, _group_killmails_by_date
    entries=[(row['side'],row['killmail_id'],datetime.fromisoformat(row['occurred_at'].replace('Z','+00:00'))) for row in report['killmails']]
    rows=[]
    if entries:
        with closing(db()) as conn:
            conn.set_session(readonly=True)
            with conn: rows=_hydrate_global_killmail_entries(conn,entries)
    sides={row['killmail_id']:row['side'] for row in report['killmails']}
    for row in rows:
        row['side']=sides[row['killmail_id']];row['is_loss']=row['side']=='loss';row['is_kill']=not row['is_loss']
        row['detail_url']='/admin/debug/combat/kill/'+str(row['killmail_id'])
        row['source_tag']='TEST';row['tag_url']=row['detail_url']
    return dict(killmail_groups=_group_killmails_by_date(rows),killmail_pagination=report['pagination'],killmail_participation=side,killmail_mode='api')


def attach_links(payload):
    with store.connection() as conn:
        schema(conn)
        for event in payload['events']:
            if event['kind']!='loss_signal': continue
            row=conn.execute('SELECT status,killmail_id FROM combat_loss_links WHERE owner=? AND event_id=?',
                (event['collector_owner'],event['event_id'])).fetchone()
            if row:
                event['killmail_link']=dict(row)
                if row['killmail_id']: event['killmail_link']['url']='/admin/debug/combat/kill/'+str(row['killmail_id'])
    return payload
