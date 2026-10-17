"""Shared ranking query and bounded, versioned top-results cache (no cumulative facts)."""
from contextlib import contextmanager
from datetime import datetime, UTC
from decimal import Decimal
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile

from psycopg2 import sql

LOG = logging.getLogger(__name__)
CACHE_VERSION = 4
CACHE_LIMIT = 64
TOP_LIMIT = 1000
KINDS = {'character':(1,'characters','character_id'), 'corporation':(2,'corporations','corporation_id'),
         'alliance':(3,'alliances','alliance_id')}
SORTS = {'destroyed','lost','net','efficiency','kills','losses'}
DECIMALS = ('destroyed','lost','net','efficiency')


class RankingsBusy(Exception):
    pass


def player_character_filter(kind,entity_id):
    """Keep recorded NPC scores, but remove SDE NPCs before ordering/ranking."""
    if kind!='character':
        return sql.SQL('true')
    return sql.SQL('NOT EXISTS (SELECT 1 FROM public.sde_npccharacters npc WHERE npc.sde_key=({})::text)').format(entity_id)


def _current_rows(cur,kind_id,table,id_column,column,sort,first,last):
    cur.execute("SELECT to_regclass('analytics.killmail_ranking_current_state')")
    if cur.fetchone()[0] is None:
        return None
    cur.execute('SELECT ready FROM analytics.killmail_ranking_current_state WHERE singleton')
    state=cur.fetchone()
    if not state or not state[0]:
        return None
    cur.execute('SELECT min(month),max(month) FROM analytics.killmail_ranking_months')
    earliest,latest=cur.fetchone()
    if earliest is None or first>earliest:
        return None
    historical=last<latest
    fields={'destroyed':column,'lost':'lost_cents','kills':'kills','losses':'losses',
            'unvalued_kills':'unvalued_kills','unvalued_losses':'unvalued_losses'}
    params=[]
    if historical:
        facts=sql.SQL(',').join(sql.SQL('CASE WHEN c.last_month<=%s THEN c.{col} ELSE p.{cumulative} END AS {alias}').format(
            col=sql.Identifier(col),cumulative=sql.Identifier('cumulative_'+col),alias=sql.Identifier(alias))
            for alias,col in fields.items())
        params.extend([last]*len(fields))
        snapshot_fields=sql.SQL(',').join(sql.Identifier('cumulative_'+col) for col in fields.values())
        join=sql.SQL("""LEFT JOIN LATERAL (
            SELECT {fields} FROM analytics.killmail_rankings_monthly m
            WHERE m.entity_kind=c.entity_kind AND m.entity_id=c.entity_id
              AND m.month<=%s AND c.last_month>%s ORDER BY m.month DESC LIMIT 1
        ) p ON true""").format(fields=snapshot_fields)
        params.extend((last,last))
    else:
        facts=sql.SQL(',').join(sql.SQL('c.{col} AS {alias}').format(
            col=sql.Identifier(col),alias=sql.Identifier(alias)) for alias,col in fields.items())
        join=sql.SQL('')
    metric={'net':sql.SQL('destroyed-lost'),
            'efficiency':sql.SQL('100*destroyed/NULLIF(destroyed+lost,0)')}.get(sort,sql.Identifier(sort))
    params.extend((kind_id,TOP_LIMIT))
    cur.execute(sql.SQL('''WITH facts AS (
        SELECT c.entity_id,{facts} FROM analytics.killmail_rankings_current c {join}
        WHERE c.entity_kind=%s AND {players}
    ), top_rows AS (
        SELECT *,{metric} AS score FROM facts WHERE kills>0 OR losses>0
        ORDER BY score DESC NULLS LAST,entity_id LIMIT %s
    ) SELECT r.entity_id,r.destroyed,r.lost,r.kills,r.losses,r.unvalued_kills,r.unvalued_losses,
             r.destroyed-r.lost AS net,100*r.destroyed/NULLIF(r.destroyed+r.lost,0) AS efficiency,e.name
      FROM top_rows r LEFT JOIN entities.{table} e ON e.{id_column}=r.entity_id
      ORDER BY r.score DESC NULLS LAST,r.entity_id''').format(facts=facts,metric=metric,join=join,
        table=sql.Identifier(table),id_column=sql.Identifier(id_column),
        players=player_character_filter('character' if kind_id==1 else 'other',sql.SQL('c.entity_id'))),params)
    columns=[c.name for c in cur.description]
    return [dict(zip(columns,r)) for r in cur]


def query(conn, kind, mode, sort, first, last):
    """Rank by just the requested metric, then fetch full facts for the winners only."""
    if kind not in KINDS or mode not in ('classic','weighted') or sort not in SORTS:
        raise ValueError('Invalid ranking filter.')
    kind_id,table,id_column = KINDS[kind]
    column = 'weighted_destroyed_cents' if mode=='weighted' else 'destroyed_cents'
    multi = first!=last
    def amount(name):
        identifier=sql.Identifier(name)
        return sql.SQL('sum({})').format(identifier) if multi else identifier
    destroyed,lost=amount(column),amount('lost_cents')
    metrics = dict(destroyed=destroyed,lost=lost,kills=amount('kills'),losses=amount('losses'),
        net=sql.SQL('({}-{})').format(destroyed,lost),
        efficiency=sql.SQL('100*{d}/NULLIF({d}+{l},0)').format(d=destroyed,l=lost))
    # One snapshot covers both reads, including concurrent monthly rebuilds.
    conn.set_session(isolation_level='REPEATABLE READ', readonly=True)
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout='25s'")
        cur.execute("SET LOCAL work_mem='32MB'")
        cur.execute('SET LOCAL max_parallel_workers_per_gather=2')
        current=_current_rows(cur,kind_id,table,id_column,column,sort,first,last)
        if current is not None:
            return current
        cur.execute(sql.SQL('''SELECT entity_id,{metric} AS score
            FROM analytics.killmail_rankings_monthly
            WHERE entity_kind=%s AND month BETWEEN %s AND %s AND {players} {group}
            ORDER BY score DESC NULLS LAST,entity_id LIMIT %s''').format(
                metric=metrics[sort],players=player_character_filter(kind,sql.Identifier('entity_id')),
                group=sql.SQL('GROUP BY entity_id' if multi else '')),
                (kind_id,first,last,TOP_LIMIT))
        ids=[r[0] for r in cur]
        if not ids:
            return []
        # Hash/sequential filtering avoids summing every statistic for millions of entities.
        cur.execute(sql.SQL('''WITH totals AS (
            SELECT entity_id,sum({destroyed}) AS destroyed,sum(lost_cents) AS lost,
                   sum(kills) AS kills,sum(losses) AS losses,
                   sum(unvalued_kills) AS unvalued_kills,sum(unvalued_losses) AS unvalued_losses
            FROM analytics.killmail_rankings_monthly
            WHERE entity_kind=%s AND month BETWEEN %s AND %s AND entity_id=ANY(%s)
            GROUP BY entity_id
        ) SELECT r.*,r.destroyed-r.lost AS net,
                 100*r.destroyed/NULLIF(r.destroyed+r.lost,0) AS efficiency,e.name
          FROM totals r LEFT JOIN entities.{table} e ON e.{id_column}=r.entity_id''').format(
            destroyed=sql.Identifier(column),table=sql.Identifier(table),id_column=sql.Identifier(id_column)),
            (kind_id,first,last,ids))
        columns=[c.name for c in cur.description]
        rows={r[0]:dict(zip(columns,r)) for r in cur}
        return [rows[id] for id in ids if id in rows]


def cache_key(kind,mode,sort,first,last,version,scope=()):
    def encode(value):
        return value.astimezone(UTC).isoformat() if isinstance(value,datetime) else str(value)
    content=json.dumps([CACHE_VERSION,list(map(str,scope)),kind,mode,sort,first,last,version],default=encode)
    return hashlib.sha256(content.encode()).hexdigest()


def load_cache(directory,key):
    path=Path(directory)/(key+'.json')
    try:
        if path.stat().st_size>2*1024**2:
            return None
        data=json.loads(path.read_text())
        if data.get('version')!=CACHE_VERSION or not isinstance(data.get('rows'),list) or len(data['rows'])>TOP_LIMIT:
            return None
        for row in data['rows']:
            for field in DECIMALS:
                if row[field] is not None:
                    row[field]=Decimal(str(row[field]))
            for field in ('entity_id','kills','losses','unvalued_kills','unvalued_losses'):
                row[field]=int(row[field])
        return data['rows']
    except (OSError,ValueError,KeyError,TypeError,ArithmeticError):
        return None


def save_cache(directory,key,rows):
    """Only top rows, at most 64 files; lock files are a fixed set of 256 buckets."""
    directory=Path(directory)
    temporary=None
    try:
        directory.mkdir(parents=True,exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w',dir=directory,prefix='.rank-',suffix='.tmp',delete=False) as handle:
            temporary=handle.name
            json.dump(dict(version=CACHE_VERSION,rows=rows),handle,default=str)
        os.replace(temporary,directory/(key+'.json'))
        paths=sorted(directory.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
        for path in paths[CACHE_LIMIT:]:
            path.unlink(missing_ok=True)
    except OSError:
        LOG.warning('Ranking disk cache unavailable',exc_info=True)
    finally:
        if temporary:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


@contextmanager
def cache_lock(directory,key):
    handle=None
    try:
        Path(directory).mkdir(parents=True,exist_ok=True)
        handle=(Path(directory)/('lock-'+key[:2])).open('a')
    except OSError:
        LOG.warning('Ranking cache lock unavailable; using process limit',exc_info=True)
    try:
        if handle:
            try:
                fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RankingsBusy('This period is being calculated. Retry in a moment.') from exc
        yield
    finally:
        if handle:
            handle.close()
