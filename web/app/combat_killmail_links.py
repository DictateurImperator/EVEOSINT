"""Explicit debug matching; existing killmails are read-only."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import logging
import threading
from psycopg2.extras import RealDictCursor
from .db import db
from . import combat_timeline as store

logger=logging.getLogger(__name__)
_lock=threading.Lock()
utc=store.utc

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



def refresh(force=False):
    if not _lock.acquire(blocking=False): return False
    try:
        store.check_writing()
        with store.connection() as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT * FROM devencour.collector_streams WHERE character_id>0 ORDER BY owner,observer')
            streams=cur.fetchall()
        for stream in streams:
            store.check_writing()
            try: refresh_stream(stream)
            except store.Maintenance: raise
            except Exception:
                logger.exception('Combat debug killboard lookup failed')
                record_scan(stream,'error')
        return True
    finally: _lock.release()


def record_scan(stream,status):
    store.check_writing()
    with store.connection() as conn,conn.cursor() as cur:
        cur.execute('''INSERT INTO devencour.killboard_scans VALUES(%s,%s,%s,clock_timestamp(),%s)
          ON CONFLICT(owner,observer) DO UPDATE SET events=excluded.events,checked_at=excluded.checked_at,status=excluded.status''',
          (stream['owner'],stream['observer'],stream['events'],status))


def refresh_stream(stream):
    owner,observer=stream['owner'],stream['observer']
    first=stream['first_at']-timedelta(seconds=30);last=stream['last_at']+timedelta(seconds=30)
    entries=list(read_killboard(stream['character_id'],first,last))
    losses=[dict(killmail_id=r[0],occurred_at=utc(r[1])) for r in entries if r[2]=='loss']
    store.check_writing()
    with store.connection() as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('DELETE FROM devencour.killboard_entries WHERE owner=%s AND observer=%s',(owner,observer))
        for km_id,at,side,ship_id,ship_name,group_id in entries:
            cur.execute('INSERT INTO devencour.killboard_entries VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
              (owner,observer,stream['character_id'],km_id,at,side,ship_id,ship_name,None if group_id is None else group_id=='29'))
        cur.execute('SELECT summary_id,occurred_at FROM devencour.death_summaries WHERE owner=%s AND observer=%s',(owner,observer))
        summaries=cur.fetchall()
        for summary in summaries:
            status,km_id,_=choose_loss(summary['occurred_at'],losses)
            cur.execute('DELETE FROM devencour.summary_killmail_links WHERE owner=%s AND summary_id=%s',(owner,summary['summary_id']))
            if status=='linked':
                cur.execute('INSERT INTO devencour.summary_killmail_links(owner,summary_id,killmail_id) VALUES(%s,%s,%s)',(owner,summary['summary_id'],km_id))
    record_scan(stream,'complete')


def report(observer='',page=1,date_from='',date_to='',month='',side='both',killmail_id=''):
    page=max(1,int(page));per_page=100
    if side not in ('both','kills','losses'): raise ValueError('Invalid side')
    filters=[];values=[]
    if observer: filters.append('observer=%s');values.append(store.text(observer,128))
    if month:
        datetime.strptime(month,'%Y-%m');filters.append("to_char(occurred_at AT TIME ZONE 'UTC','YYYY-MM')=%s");values.append(month)
    for value,op in ((date_from,'>='),(date_to,'<=')):
        if value: store.timestamp(value);filters.append('occurred_at'+op+'%s');values.append(value)
    if killmail_id: filters.append('killmail_id=%s');values.append(int(killmail_id))
    where=' WHERE '+' AND '.join(filters) if filters else ''
    grouped="SELECT killmail_id,occurred_at,bool_or(side='loss') AS is_loss FROM devencour.killboard_entries"+where+' GROUP BY killmail_id,occurred_at'
    if side!='both': grouped+=" HAVING bool_or(side='loss')="+('true' if side=='losses' else 'false')
    with store.connection() as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('SELECT count(*) AS n FROM ('+grouped+') x',values);total=cur.fetchone()['n']
        pages=max(1,(total+per_page-1)//per_page);page=min(page,pages)
        cur.execute(grouped+' ORDER BY occurred_at DESC,killmail_id DESC LIMIT %s OFFSET %s',values+[per_page,(page-1)*per_page])
        rows=[dict(r,occurred_at=utc(r['occurred_at']),side='loss' if r['is_loss'] else 'kill',url='/kill/'+str(r['killmail_id'])) for r in cur.fetchall()]
        bounds=[];params=[]
        if observer: bounds.append('s.observer=%s');params.append(observer)
        for v,op in ((date_from,'>='),(date_to,'<=')):
            if v: bounds.append('s.occurred_at'+op+'%s');params.append(v)
        cur.execute('''SELECT s.observer,s.occurred_at,s.summary_id AS event_id FROM devencour.death_summaries s
          LEFT JOIN devencour.summary_killmail_links l USING(owner,summary_id) WHERE l.summary_id IS NULL'''+(' AND '+' AND '.join(bounds) if bounds else '')+' ORDER BY s.occurred_at DESC',params)
        unknown=[dict(r,occurred_at=utc(r['occurred_at']),status='unknown',candidates=[]) for r in cur.fetchall()]
        cur.execute('SELECT * FROM devencour.killboard_scans'+(' WHERE observer=%s' if observer else ''),[observer] if observer else [])
        scans=[{k:utc(v) for k,v in dict(r).items()} for r in cur.fetchall()]
        cur.execute("SELECT DISTINCT to_char(first_at AT TIME ZONE 'UTC','YYYY-MM') AS month FROM devencour.collector_streams ORDER BY 1 DESC")
        months=[r['month'] for r in cur.fetchall()]
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
    signals=[(e['collector_owner'],e['event_id']) for e in payload['events'] if e['kind']=='loss_signal']
    if not signals: return payload
    with store.connection() as conn,conn.cursor() as cur:
        cur.execute('SELECT owner,summary_id,killmail_id FROM devencour.summary_killmail_links WHERE (owner,summary_id) IN ('+','.join(['(%s,%s)']*len(signals))+')',[v for pair in signals for v in pair])
        matched={(r[0],r[1]):r[2] for r in cur.fetchall()}
    for e in payload['events']:
        km=matched.get((e['collector_owner'],e['event_id']))
        if km: e['killmail_link']=dict(status='linked',killmail_id=km,url='/admin/debug/combat/kill/'+str(km))
    return payload
