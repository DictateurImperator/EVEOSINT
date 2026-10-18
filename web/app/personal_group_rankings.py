"""Progressive local rankings. Source is read-only; only connection-local score totals are accumulated."""
from collections import OrderedDict
from contextlib import closing
from datetime import timedelta
import logging
import threading
import time
from psycopg2 import sql
from .db import db
from . import personal_group_queries as q
from .killmail_rankings import KINDS,SORTS

LOG=logging.getLogger(__name__)
LIMIT=1000
LOCK_ID=920134802
_LOCK=threading.Lock()
_RUNS=OrderedDict()


def month_scores(conn,group,relation,start,end,zones,kind):
    month,kills,attackers=relation
    query,params=q.match_sql(group['definition'],group['prepared_rules'],kills,attackers,start,end,zones,
                             ship_terms=q.ship_terms(conn,group['definition']))
    actor,ap=q.actor_predicate(group['definition'],group['prepared_rules'],'a')
    attack_id=q.actor_fields('a')[kind];victim_id=q.actor_fields('v',victim=True)[kind]
    query+=sql.SQL(''', attacks AS MATERIALIZED (
        SELECT m.killmail_id,m.killmail_time,m.estimated_value_cents,{attack_id} AS entity_id,
          {actor} AS own,GREATEST(COALESCE(a.damage_done,0),0)::numeric AS damage,
          sum(GREATEST(COALESCE(a.damage_done,0),0)::numeric) OVER(PARTITION BY m.killmail_id,m.killmail_time) AS total
        FROM matches m JOIN {attackers} a USING(killmail_id,killmail_time)
      ), per_kill AS (
        SELECT entity_id,killmail_id,killmail_time,max(estimated_value_cents) AS cents,sum(damage) AS damage,max(total) AS total
        FROM attacks WHERE own AND entity_id>0 GROUP BY entity_id,killmail_id,killmail_time
      ), contributions AS (
        SELECT entity_id,COALESCE(cents,0)::numeric AS raw,
          CASE WHEN total>0 THEN COALESCE(cents,0)*damage/total ELSE 0 END AS weighted,
          0::numeric AS lost,1::bigint AS kills,0::bigint AS losses,(cents IS NULL)::int AS unvalued_kills,0 AS unvalued_losses
        FROM per_kill
        UNION ALL
        SELECT {victim_id},0,0,COALESCE(m.estimated_value_cents,0),0,1,0,(m.estimated_value_cents IS NULL)::int
        FROM matches m JOIN {kills} v USING(killmail_id,killmail_time) WHERE m.is_loss AND {victim_id}>0
      ) INSERT INTO group_scores AS old(entity_id,raw,weighted,lost,kills,losses,unvalued_kills,unvalued_losses)
      SELECT entity_id,sum(raw),sum(weighted),sum(lost),sum(kills),sum(losses),sum(unvalued_kills),sum(unvalued_losses)
      FROM contributions WHERE {players} GROUP BY entity_id
      ON CONFLICT(entity_id) DO UPDATE SET raw=old.raw+excluded.raw,weighted=old.weighted+excluded.weighted,
        lost=old.lost+excluded.lost,kills=old.kills+excluded.kills,losses=old.losses+excluded.losses,
        unvalued_kills=old.unvalued_kills+excluded.unvalued_kills,unvalued_losses=old.unvalued_losses+excluded.unvalued_losses
    ''').format(attack_id=sql.SQL(attack_id),victim_id=sql.SQL(victim_id),actor=sql.SQL(actor),
       attackers=attackers,kills=kills,players=sql.SQL('entity_id::text NOT IN (SELECT sde_key FROM public.sde_npccharacters WHERE sde_key IS NOT NULL)' if kind=='character' else 'true'))
    with conn.cursor() as cur:cur.execute(query,params+ap)


def score_table(conn):
    # Create before the read-only source snapshot. PostgreSQL permits DML on an
    # existing temporary table in a read-only transaction; permanent tables remain protected.
    with conn,conn.cursor() as cur:
        cur.execute('''CREATE TEMP TABLE group_scores(entity_id bigint PRIMARY KEY,raw numeric,weighted numeric,lost numeric,
            kills bigint,losses bigint,unvalued_kills bigint,unvalued_losses bigint)''')
    conn.set_session(readonly=True,isolation_level='REPEATABLE READ')


def top_rows(conn,kind,mode,sort):
    column='weighted' if mode=='weighted' else 'raw'
    metric={'destroyed':column,'net':column+'-lost','efficiency':f'100*{column}/NULLIF({column}+lost,0)'}.get(sort,sort)
    with conn.cursor() as cur:
        cur.execute(sql.SQL('''SELECT r.entity_id,r.{column} AS destroyed,r.raw,r.weighted,r.lost,r.kills,r.losses,
            r.unvalued_kills,r.unvalued_losses,r.{column}-r.lost AS net,
            100*r.{column}/NULLIF(r.{column}+r.lost,0) AS efficiency,e.name
            FROM (SELECT *,{metric} AS score FROM group_scores ORDER BY score DESC NULLS LAST,entity_id LIMIT %s) r
            LEFT JOIN entities.{table} e ON e.{id}=r.entity_id ORDER BY r.score DESC NULLS LAST,r.entity_id''').format(
             column=sql.Identifier(column),metric=sql.SQL(metric),table=sql.Identifier(KINDS[kind][1]),id=sql.Identifier(KINDS[kind][2])),(LIMIT,))
        names=[c.name for c in cur.description]
        return [dict(zip(names,row),rank=i+1) for i,row in enumerate(cur)]


def scan(group,result,kind,mode,sort,report):
    with closing(db()) as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT pg_try_advisory_lock(%s)',(LOCK_ID,))
            if not cur.fetchone()[0]:raise ValueError('Another local ranking is scanning history. Retry when it finishes.')
        conn.commit();score_table(conn)
        with conn,conn.cursor() as cur:
            cur.execute("SET LOCAL timezone='UTC';SET LOCAL statement_timeout='120s';SET LOCAL work_mem='32MB';SET LOCAL jit=off")
            start=result['from_date'];end=result['through_date']+timedelta(days=1)
            parts=[r for r in q.relations(conn) if r[0]<end and q.next_month(r[0])>start]
            zones=q.zone_ids(conn,group['definition'])
            report(checked=0,total=len(parts),phase='Scanning killmails')
            rows=[]
            for index,relation in enumerate(parts):
                month=relation[0]
                report(checked=index,current_month=month.strftime('%Y-%m'))
                month_scores(conn,group,relation,max(start,month),min(end,q.next_month(month)),zones,kind)
                rows=top_rows(conn,kind,mode,sort)
                report(checked=index+1,rows=rows)
            return rows


def state(group,result,kind,mode,sort):
    if kind not in KINDS or mode not in ('classic','weighted') or sort not in SORTS:raise ValueError('Invalid ranking selection.')
    key=(group['id'],group['revision'],str(result['built_at']),result['period'],kind,mode,sort)
    with _LOCK:
        now=time.monotonic()
        previous=_RUNS.get(key)
        if previous and (previous['status']=='running' or now-previous['updated']<(10 if previous['status']=='failed' else 300)):
            return dict(previous,progress=dict(previous['progress']),rows=list(previous['rows']))
        if any(run['status']=='running' for run in _RUNS.values()):
            return dict(status='busy',progress={},rows=[],error='Another local ranking is scanning history. Please retry shortly.')
        run=dict(status='running',progress=dict(checked=0,total=0,phase='Starting'),rows=[],updated=now)
        _RUNS[key]=run;_RUNS.move_to_end(key)
        while len(_RUNS)>8:_RUNS.popitem(last=False)
        def report(**values):
            with _LOCK:
                if 'rows' in values:run['rows']=values.pop('rows')
                run['progress'].update(values);run['updated']=time.monotonic()
        def worker():
            try:
                rows=scan(group,result,kind,mode,sort,report)
                with _LOCK:run.update(status='completed',rows=rows,updated=time.monotonic())
            except Exception as exc:
                LOG.exception('Group ranking failed group=%s',group['id'])
                with _LOCK:run.update(status='failed',error=str(exc) if isinstance(exc,ValueError) else 'This scan could not complete. Try a prepared month to narrow the period.',updated=time.monotonic())
        threading.Thread(target=worker,name=f"group-ranking-{group['id']}",daemon=True).start()
        return dict(run,progress=dict(run['progress']),rows=list(run['rows']))
