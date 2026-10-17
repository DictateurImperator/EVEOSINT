"""Read monthly ranking facts; bounded cache stores only the top 1,000 results."""
from collections import OrderedDict
from datetime import date
from decimal import Decimal
import re
import threading
import time

from psycopg2 import sql
from .db import db

KINDS = {'character': (1,'characters','character_id'), 'corporation': (2,'corporations','corporation_id'),
         'alliance': (3,'alliances','alliance_id')}
SORTS = {'destroyed','lost','net','efficiency','kills','losses'}
PAGE_SIZE = 50
TOP_LIMIT = 1000
_CACHE = OrderedDict()
_LOCK = threading.Lock()
_BUILDING = set()
_SLOTS = threading.BoundedSemaphore(2)


class RankingsBusy(Exception):
    pass


def month_input(value):
    if not re.fullmatch(r'\d{4}-\d{2}', value or ''):
        raise ValueError('Choose a month in YYYY-MM format.')
    result = date.fromisoformat(value+'-01')
    if result.year<2003:
        raise ValueError('Earliest month: 2003-01.')
    return result


def compact_isk(cents):
    value = Decimal(cents)/100
    for scale, suffix in [(Decimal('1e12'),'T'),(Decimal('1e9'),'B'),(Decimal('1e6'),'M'),(Decimal('1e3'),'K')]:
        if abs(value)>=scale:
            return f'{value/scale:,.2f} {suffix}'
    return f'{value:,.2f}'


def full_isk(cents):
    return f'{Decimal(cents)/100:,.2f} ISK'


def catalog():
    with db() as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout='5s'")
        cur.execute("SELECT to_regclass('analytics.killmail_ranking_months')")
        if cur.fetchone()[0] is None:
            return []
        cur.execute('''SELECT m.month,m.built_at,m.killmails,m.valued,m.zero_damage,m.entity_rows,
                              m.built_revision<>COALESCE(v.revision,0) AS dirty
                       FROM analytics.killmail_ranking_months m
                       LEFT JOIN analytics.killmail_ranking_versions v USING(month) ORDER BY m.month''')
        return [dict(zip(('month','built_at','killmails','valued','zero_damage','entity_rows','dirty'),r)) for r in cur]


def _query(kind, mode, sort, first, last):
    kind_id, table, id_column = KINDS[kind]
    destroyed = 'weighted_destroyed_cents' if mode=='weighted' else 'destroyed_cents'
    # Both identifiers originate exclusively in the allowlists above.
    with db() as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout='25s'")
        cur.execute("SET LOCAL work_mem='32MB'")
        cur.execute('SET LOCAL max_parallel_workers_per_gather=2')
        totals = sql.SQL("""SELECT entity_id,{destroyed} AS destroyed,lost_cents AS lost,
                       kills,losses,unvalued_kills,unvalued_losses
                       FROM analytics.killmail_rankings_monthly
                       WHERE entity_kind=%s AND month BETWEEN %s AND %s""") if first==last else sql.SQL("""
            SELECT entity_id,sum({destroyed}) AS destroyed,sum(lost_cents) AS lost,
                   sum(kills) AS kills,sum(losses) AS losses,
                   sum(unvalued_kills) AS unvalued_kills,sum(unvalued_losses) AS unvalued_losses
            FROM analytics.killmail_rankings_monthly
            WHERE entity_kind=%s AND month BETWEEN %s AND %s GROUP BY entity_id""")
        cur.execute(sql.SQL('''WITH totals AS ({totals}), scored AS (
            SELECT *,destroyed-lost AS net,
                   CASE WHEN destroyed+lost>0 THEN 100*destroyed/(destroyed+lost) END AS efficiency
            FROM totals
        ), top_rows AS (
            SELECT * FROM scored ORDER BY {sort} DESC NULLS LAST,entity_id LIMIT %s
        ) SELECT r.*,e.name FROM top_rows r LEFT JOIN entities.{table} e ON e.{id_column}=r.entity_id
          ORDER BY r.{sort} DESC NULLS LAST,r.entity_id''').format(
              totals=totals.format(destroyed=sql.Identifier(destroyed)), sort=sql.Identifier(sort),
              table=sql.Identifier(table),id_column=sql.Identifier(id_column)),
              (kind_id,first,last,TOP_LIMIT))
        names = [c.name for c in cur.description]
        return [dict(zip(names,r)) for r in cur]


def ranking_rows(kind, mode, sort, first, last, months):
    if kind not in KINDS or mode not in ('classic','weighted') or sort not in SORTS:
        raise ValueError('Invalid ranking filter.')
    # Every committed rebuild invalidates all affected cached periods immediately.
    version = tuple((m['month'],m['built_at']) for m in months if first<=m['month']<=last)
    key = (kind,mode,sort,first,last,version)
    now = time.monotonic()
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and now-cached[0]<600:
            _CACHE.move_to_end(key)
            return cached[1], True
        if key in _BUILDING:
            raise RankingsBusy('This period is being calculated. Retry in a moment.')
        if not _SLOTS.acquire(blocking=False):
            raise RankingsBusy('Two ranking calculations are running. Retry in a moment.')
        _BUILDING.add(key)
    try:
        rows = _query(kind,mode,sort,first,last)
        with _LOCK:
            _CACHE[key] = (time.monotonic(),rows)
            _CACHE.move_to_end(key)
            while len(_CACHE)>16:
                _CACHE.popitem(last=False)
        return rows, False
    finally:
        with _LOCK:
            _BUILDING.discard(key)
        _SLOTS.release()
