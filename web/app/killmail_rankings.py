"""Read monthly ranking facts; bounded cache stores only the top 1,000 results."""
from collections import OrderedDict
from datetime import date
from decimal import Decimal
import re
import threading
import importlib
from functools import lru_cache
from pathlib import Path
import sys
import types

from . import config
from .db import db

@lru_cache(maxsize=1)
def engine():
    package=types.ModuleType('eveosint_ranking_scripts')
    package.__path__=[str(Path(__file__).resolve().parents[2]/'scripts')]
    sys.modules.setdefault(package.__name__,package)
    return importlib.import_module(package.__name__+'.killmail_rankings_query')


KINDS=engine().KINDS
SORTS=engine().SORTS
PAGE_SIZE=50
TOP_LIMIT=engine().TOP_LIMIT
CACHE_DIR=Path.home()/'eveosint/data/cache/killmail_rankings'
_CACHE=OrderedDict()
_LOCK=threading.Lock()
_BUILDING=set()
_SLOTS=threading.BoundedSemaphore(2)
RankingsBusy=engine().RankingsBusy


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
        cur.execute("SELECT to_regclass('analytics.killmail_ranking_current_state')")
        totals_ready=False
        if cur.fetchone()[0] is not None:
            cur.execute('SELECT ready FROM analytics.killmail_ranking_current_state WHERE singleton')
            state=cur.fetchone()
            totals_ready=bool(state and state[0])
        cur.execute('''SELECT m.month,m.built_at,m.killmails,m.valued,m.zero_damage,m.entity_rows,
                              m.built_revision<>COALESCE(v.revision,0) AS dirty
                       FROM analytics.killmail_ranking_months m
                       LEFT JOIN analytics.killmail_ranking_versions v USING(month) ORDER BY m.month''')
        return [dict(zip(('month','built_at','killmails','valued','zero_damage','entity_rows','dirty'),r),totals_ready=totals_ready) for r in cur]


def _query(kind, mode, sort, first, last):
    with db() as conn:
        return engine().query(conn,kind,mode,sort,first,last)


def ranking_rows(kind, mode, sort, first, last, months):
    if kind not in KINDS or mode not in ('classic','weighted') or sort not in SORTS:
        raise ValueError('Invalid ranking filter.')
    version=tuple((m['month'],m['built_at']) for m in months if first<=m['month']<=last)
    cfg=getattr(config,'DB_CONFIG',{})
    scope=tuple(cfg.get(k,'') for k in ('db_host','db_port','db_name'))
    key=engine().cache_key(kind,mode,sort,first,last,version,scope)
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key],True
        if key in _BUILDING:
            raise RankingsBusy('This period is being calculated. Retry in a moment.')
        if not _SLOTS.acquire(blocking=False):
            raise RankingsBusy('Two ranking calculations are running. Retry in a moment.')
        _BUILDING.add(key)
    try:
        rows=engine().load_cache(CACHE_DIR,key)
        cached=rows is not None
        if rows is None:
            with engine().cache_lock(CACHE_DIR,key):
                rows=engine().load_cache(CACHE_DIR,key)
                cached=rows is not None
                if rows is None:
                    rows=_query(kind,mode,sort,first,last)
                    engine().save_cache(CACHE_DIR,key,rows)
        with _LOCK:
            _CACHE[key]=rows
            _CACHE.move_to_end(key)
            while len(_CACHE)>16:
                _CACHE.popitem(last=False)
        return rows,cached
    finally:
        with _LOCK:
            _BUILDING.discard(key)
        _SLOTS.release()


@lru_cache(maxsize=1)
def explorer():
    return importlib.import_module(engine().__package__+'.killmail_ranking_explorer')


def parse_scope(values):
    result=[]
    if len(values)>20:
        raise ValueError('Choose at most 20 membership filters per list.')
    for value in values:
        if value not in ('universe:0','alliance:0') and not re.fullmatch(r'(coalition|alliance|corporation):[1-9][0-9]{0,17}',value):
            raise ValueError('Invalid membership filter.')
        if value not in result:
            result.append(value)
    return result


def lookup(query,kind,purpose='target',as_of=None):
    query=str(query or '').strip()[:100]
    if len(query)<2:
        return []
    if purpose=='scope':
        from .coalitions import search_member_entities
        special=[dict(entity_type='universe',entity_id=0,name='Universe',logo_url=None)]
        if kind!='alliance':
            special.append(dict(entity_type='alliance',entity_id=0,name='No alliance',logo_url=None))
        matches=[item for item in special if query.casefold() in item['name'].casefold()]
        return matches+[item for item in search_member_entities(query,limit=20)
                if kind!='alliance' or item['entity_type'] in ('coalition','alliance')]
    from psycopg2 import sql
    _kind_id,table,id_column=KINDS[kind]
    with db() as conn,conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout='5s'")
        # Match the expression used by the existing trigram indexes. A raw
        # name ILIKE forces a scan of millions of characters on each keystroke.
        needle=query.lower().replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
        entity_id=int(query) if query.isascii() and query.isdigit() and len(query)<=18 else None
        rows=[]
        if entity_id is not None:
            cur.execute(sql.SQL('SELECT {id},name FROM entities.{table} WHERE {id}=%s').format(
                id=sql.Identifier(id_column),table=sql.Identifier(table)),(entity_id,))
            rows=list(cur)
        if not rows:
            statement=sql.SQL('''SELECT {id},name FROM entities.{table}
                WHERE lower(COALESCE(name,'')) LIKE %s
                ORDER BY (lower(name)=%s) DESC,lower(name),{id} LIMIT 12''').format(
                    id=sql.Identifier(id_column),table=sql.Identifier(table))
            cur.execute(statement,(needle+'%',query.lower()))
            rows=list(cur)
            if not rows and len(query)>=3:
                cur.execute(statement,('%'+needle+'%',query.lower()))
                rows=list(cur)
        results=[dict(entity_type=kind,entity_id=r[0],name=r[1]) for r in rows]
        return explorer().enrich(conn,kind,results,as_of)


def scope_logo(token):
    if token in ('universe:0','alliance:0'):
        return None
    kind,id=token.split(':')
    if kind=='coalition':
        from .coalitions import coalition_logo_url
        return coalition_logo_url(int(id))
    return f'https://images.evetech.net/{"alliances" if kind=="alliance" else "corporations"}/{id}/logo?size=64'


def scope_labels(tokens):
    if not tokens:
        return {}
    from psycopg2 import sql
    result={token:label for token,label in [('universe:0','Universe'),('alliance:0','No alliance')] if token in tokens}
    tokens=[token for token in tokens if token not in result]
    if not tokens:
        return result
    with db() as conn,conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout='5s'")
        for kind,table,id_column in [('coalition','coalitions','coalition_id'),('alliance','alliances','alliance_id'),('corporation','corporations','corporation_id')]:
            ids=[int(token.split(':')[1]) for token in tokens if token.startswith(kind+':')]
            if ids:
                cur.execute(sql.SQL('SELECT {id},name,to_jsonb(e)->>%s FROM entities.{table} e WHERE {id}=ANY(%s)').format(
                    id=sql.Identifier(id_column),table=sql.Identifier(table)),('short_name' if kind=='coalition' else 'ticker',ids))
                result.update({f'{kind}:{id}':name+(f' [{ticker}]' if ticker else '') for id,name,ticker in cur})
    return result


def _explore_rows(kind,mode,sort,first,last,**options):
    from datetime import datetime,UTC,timedelta
    with db() as conn:
        rules={}
        if any(token.startswith('coalition:') for token in (*options.get('includes',()),*options.get('excludes',()))):
            from .map_data import _fight_heat_load_coalition_rules
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout='5s'")
                rules=_fight_heat_load_coalition_rules(cur)
            conn.commit()
        day=(explorer().month_end(last)-timedelta(days=1)).date() if options.get('as_of') else datetime.now(UTC).date()
        return explorer().explore(conn,engine(),kind,mode,sort,first,last,rules=rules,scope_day=day,**options)


def enrich_rows(kind,rows,as_of=None):
    if not rows or kind=='alliance':
        return rows
    with db() as conn:
        return explorer().enrich(conn,kind,rows,as_of)


def explore_rows(kind,mode,sort,first,last,**options):
    if not _SLOTS.acquire(blocking=False):
        raise RankingsBusy('Two ranking calculations are running. Retry in a moment.')
    try:
        return _explore_rows(kind,mode,sort,first,last,**options)
    finally:
        _SLOTS.release()
