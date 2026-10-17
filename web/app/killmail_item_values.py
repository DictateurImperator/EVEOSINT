"""Read-only, on-demand breakdown using the same historical calculator as the jobs."""
import importlib
import json
import sys
import types
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from collections import OrderedDict
from threading import Lock
from time import monotonic
from pathlib import Path

from psycopg2.errors import QueryCanceled

_PRICE_CACHE = OrderedDict()
_PRICE_LOCK = Lock()


@lru_cache(maxsize=1)
def calculator():
    # The web process starts in web/, so scripts/ is not on its import path.
    name = 'eveosint_valuation_scripts'
    package = types.ModuleType(name)
    package.__path__ = [str(Path(__file__).resolve().parents[2] / 'scripts')]
    sys.modules.setdefault(name, package)
    return importlib.import_module(name + '.calculate_killmail_values')


@lru_cache(maxsize=2)
def recipe_history(epoch):
    return calculator().RecipeHistory()


def load_prices(conn, day, roots):
    key = (conn.info.dbname,conn.info.user,day,tuple(sorted(roots)),int(monotonic()//300))
    with _PRICE_LOCK:
        cached = _PRICE_CACHE.get(key)
        if cached is not None:
            _PRICE_CACHE.move_to_end(key)
            return dict(cached)
    result = _load_prices(conn,day,roots)
    if result is not None and all(value is not None for value in result.values()):
        with _PRICE_LOCK:
            _PRICE_CACHE[key] = dict(result)
            while len(_PRICE_CACHE) > 64:
                _PRICE_CACHE.popitem(last=False)
    return result


def _load_prices(conn, day, roots):
    """Batch nearest-date market quotes, including recursively required materials."""
    calc = calculator()
    history = recipe_history(int(monotonic() // 300))
    version = history.version_on(day)
    recipes = calc.load_blueprints(None, version, history) if version else {}
    def materials(type_id):
        # The special fuel construction also lives in the shared calculator.
        return recipes.get(type_id,(1,{}))[1] if type_id != 2233 else {3962:1,2867:1,2871:1,2876:1,2872:1}
    needed = set(roots)
    pending = list(roots)
    while pending:
        root = pending.pop()
        for material in materials(root):
            if material not in needed:
                needed.add(material)
                pending.append(material)
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('market.zkill_daily_prices'),to_regclass('market.zkill_recovered_prices')")
        daily, recovered = cur.fetchone()
        if not daily:
            return None
        restricted_supplements = False
        if recovered:
            cur.execute("SELECT has_table_privilege('market.zkill_recovered_prices','SELECT')")
            recovered = cur.fetchone()[0]
            restricted_supplements = not recovered
        cur.execute('SELECT max(price_date) FROM market.zkill_daily_prices')
        horizon = cur.fetchone()[0]
        if horizon is None:
            return None
        horizon = min(horizon, datetime.now(UTC).date()-timedelta(days=1))
        if day > horizon:
            return None
        cur.execute("""SELECT t.sde_key::bigint,(g.data->>'categoryID')::int,
                       (t.data->>'groupID')::int,COALESCE(t.data->'name'->>'en','')
                       FROM public.sde_types t LEFT JOIN public.sde_groups g ON g.sde_key=t.data->>'groupID'
                       WHERE t.sde_key=ANY(%s)""", ([str(t) for t in needed],))
        metadata = {t:(group,category,name) for t,category,group,name in cur}
        # Exact-day snapshots are one indexed read, even for hundreds of items.
        cur.execute('SELECT prices::text FROM market.zkill_daily_prices WHERE price_date=%s', (day,))
        row = cur.fetchone()
        exact = json.loads(row[0],parse_float=Decimal,parse_int=Decimal) if row else {}
        reachable = set(roots)
        pending = list(roots)
        while pending:
            node = pending.pop()
            group, category, _ = metadata.get(node,(None,None,''))
            has_exact = Decimal(str(exact.get(str(node)) or 0)) > 0
            if calc.fixed_price(node,day) is not None or category == 91 or group in (29,4513,4759):
                continue
            if (node in recipes and (not has_exact or group in (30,659) or category == 66)) or node == 2233:
                for material in materials(node):
                    if material not in reachable:
                        reachable.add(material)
                        pending.append(material)
        quotes = {}
        missing = []
        for type_id in reachable:
            value = Decimal(str(exact.get(str(type_id)) or 0))
            if value > 0:
                quotes[type_id] = (day,value)
            elif calc.fixed_price(type_id,day) is None and metadata.get(type_id,(None,None,''))[1] != 91 and metadata.get(type_id,(None,None,''))[0] not in (29,4513,4759):
                missing.append(type_id)
        if missing:
            # Prefer an existing supplementary quote on the day; it only fills
            # missing/zero official prices and avoids searching an item's entire history.
            if recovered:
                cur.execute('SELECT item_type_id,unit_price_isk FROM market.zkill_recovered_prices WHERE price_date=%s AND item_type_id=ANY(%s)', (day,missing))
                for type_id, value in cur:
                    quotes[type_id]=(day,Decimal(value))
            missing = [t for t in missing if t not in quotes]
        blocked = set(missing) if restricted_supplements else set()
        if blocked:
            missing = []
        if missing:
            if recovered:
                supplements = "UNION ALL SELECT price_date,unit_price_isk,1 source_rank FROM market.zkill_recovered_prices WHERE item_type_id=i.type_id AND price_date<=%s"
            else:
                supplements = ''
            # One batch SQL request for all types. Each lateral uses date ordering
            # to stop at the nearest earlier quote or first subsequent quote.
            query = """SELECT i.type_id,q.price_date,q.value FROM unnest(%s::bigint[]) i(type_id)
              LEFT JOIN LATERAL (
                SELECT * FROM (
                  (SELECT price_date,(prices->>i.type_id::text)::numeric value,0 source_rank
                   FROM market.zkill_daily_prices WHERE price_date<=%s
                   AND COALESCE((prices->>i.type_id::text)::numeric,0)>0 ORDER BY price_date DESC LIMIT 1)
                  UNION ALL
                  (SELECT price_date,(prices->>i.type_id::text)::numeric value,0 source_rank
                   FROM market.zkill_daily_prices WHERE price_date>%s AND price_date<=%s
                   AND COALESCE((prices->>i.type_id::text)::numeric,0)>0 ORDER BY price_date LIMIT 1)
                  """ + supplements + """
                ) c ORDER BY (price_date<=%s) DESC,
                  CASE WHEN price_date<=%s THEN price_date END DESC,
                  CASE WHEN price_date>%s THEN price_date END ASC, source_rank LIMIT 1
              ) q ON true"""
            params=[missing,day,day,horizon]
            if recovered:
                params.append(horizon)
            params += [day,day,day]
            try:
                cur.execute(query,params)
                for type_id, quote_day, value in cur:
                    if value is not None:
                        quotes[type_id]=(quote_day,Decimal(value))
            except QueryCanceled:
                # Keep exact-day lines usable if a rare missing type's lookup
                # exceeds the page budget. Never turn a timeout into a penny quote.
                conn.rollback()
                blocked.update(missing)
    engine=calc.Pricing(metadata,{}, {},valuation_mode='loss-day',recipe_history=history,first_prices=quotes)
    result={}
    for type_id in roots:
        try:
            value, unknown = engine.price(type_id,day,day)
            result[type_id]=None if type_id in blocked or unknown else value
        except calc.HistoricalRecipeUnavailable:
            result[type_id]=None
    return result


def breakdown(items, ship_type_id, prices):
    """Unrounded Decimal multiplication, then final rounding like the stored total."""
    penny=Decimal('.01')
    dropped=Decimal(0)
    destroyed=prices.get(ship_type_id) if prices is not None else None
    lines={}
    for item in items:
        special=item.get('singleton')==2 or item.get('flag')==179 or (item['type_id']==33329 and item.get('flag')==89)
        unit=penny if special else prices.get(item['type_id']) if prices is not None else None
        drop=Decimal(0) if not item.get('quantity_dropped',0) else unit*item['quantity_dropped'] if unit is not None else None
        loss=Decimal(0) if not item.get('quantity_destroyed',0) else unit*item['quantity_destroyed'] if unit is not None else None
        lines[item['item_index']]=dict(unit=unit,dropped=drop,destroyed=loss)
        dropped=dropped+drop if dropped is not None and drop is not None else None
        destroyed=destroyed+loss if destroyed is not None and loss is not None else None
    return dict(lines=lines,dropped=dropped,destroyed=destroyed,
                hull=prices.get(ship_type_id) if prices is not None else None,
                total=dropped+destroyed if dropped is not None and destroyed is not None else None)
