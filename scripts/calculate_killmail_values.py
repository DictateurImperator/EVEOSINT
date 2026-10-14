#!/usr/bin/env python3
"""Calculate and store zKill-like values directly on rawkm.killmails."""

import argparse
import json
import logging
import os
import shutil
import signal
import threading
from collections import OrderedDict, defaultdict, deque
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from pathlib import Path

import psycopg2
from psycopg2 import sql
from psycopg2.errors import QueryCanceled
from psycopg2.extras import execute_values, register_default_jsonb

if __package__:
    from .killmail_recipe_history import RecipeHistory
else:
    from killmail_recipe_history import RecipeHistory

START = date(2003, 1, 1)
LOCK = 79130423
FORMULA_VERSION = "loss-day-v5-recipes"
SCHEMA = Path(__file__).with_name("killmail_values_schema.sql")
LOG = logging.getLogger("killmail_values")
ONE_CENT = Decimal("0.01")
MAX_BIGINT = 9223372036854775807

FIXED_PRICES = {
    12478: Decimal("0.01"), 34559: Decimal("0.01"),
    44265: Decimal("0.01"), 55511: Decimal("30000000"),
    34558: Decimal("0.01"), 34556: Decimal("0.01"),
    34560: Decimal("0.01"), 36902: Decimal("0.01"),
    34557: Decimal("0.01"), 44264: Decimal("0.01"),
    45645: Decimal("35000000000"), 87381: Decimal("45000000000"),
    42124: Decimal("45000000000"), 42243: Decimal("70000000000"),
    2834: Decimal("80000000000"), 3516: Decimal("80000000000"),
    11375: Decimal("80000000000"), 3518: Decimal("100000000000"),
    32788: Decimal("100000000000"), 32790: Decimal("100000000000"),
    32209: Decimal("100000000000"), 11942: Decimal("100000000000"),
    33673: Decimal("100000000000"), 35779: Decimal("120000000000"),
    42125: Decimal("120000000000"), 42246: Decimal("120000000000"),
    74141: Decimal("120000000000"), 2836: Decimal("150000000000"),
    33675: Decimal("150000000000"), 35781: Decimal("150000000000"),
    45530: Decimal("150000000000"), 48636: Decimal("150000000000"),
    60765: Decimal("150000000000"), 74316: Decimal("150000000000"),
    78414: Decimal("150000000000"), 33397: Decimal("200000000000"),
    42245: Decimal("200000000000"), 85062: Decimal("200000000000"),
    45531: Decimal("230000000000"), 89808: Decimal("230000000000"),
    48635: Decimal("230000000000"), 60764: Decimal("230000000000"),
    77726: Decimal("230000000000"), 85229: Decimal("250000000000"),
    47512: Decimal("60000000000"), 45647: Decimal("60000000000"),
    89807: Decimal("450000000000"), 45649: Decimal("550000000000"),
    9860: Decimal("1000000000000"), 11019: Decimal("1000000000000"),
    85236: Decimal("1250000000000"), 42126: Decimal("650000000000"),
    42242: Decimal("60000000000"), 635: Decimal("500000000000"),
    11011: Decimal("500000000000"), 25560: Decimal("500000000000"),
    33395: Decimal("500000000000"), 13202: Decimal("750000000000"),
    11936: Decimal("750000000000"), 11938: Decimal("750000000000"),
    26842: Decimal("750000000000"), 78576: Decimal("750000000000"),
    26840: Decimal("2500000000000"), 47514: Decimal("60000000000"),
}


class StopRequested(Exception):
    pass


class HistoricalRecipeUnavailable(Exception):
    pass


def next_month(value):
    return date(value.year + (value.month == 12), value.month % 12 + 1, 1)


def cents(value):
    return int((Decimal(value) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fixed_price(type_id, day, date_only=True):
    def before(cutoff):
        return day <= cutoff if date_only else day < cutoff

    if type_id == 88001 and before(date(2025, 6, 1)):
        return Decimal("10000000000")
    if type_id == 3514:
        return Decimal("100000000000") if before(date(2023, 12, 1)) else Decimal("250000000000")
    if type_id == 42241:
        return Decimal("350000000000") if before(date(2019, 7, 1)) else Decimal("650000000000")
    if type_id == 11940:
        return Decimal("500000000") if before(date(2020, 1, 25)) else Decimal("3400000000000")
    return FIXED_PRICES.get(type_id)


def rolling_price(history, exact=None):
    """Mirror Price::getItemPrice's 34-value trimming and downward date cap."""
    values = sorted(history)
    if len(values) == 34:
        values = values[2:-2]
    elif len(values) > 6:
        values = values[:-2]
    if not values:
        return ONE_CENT, True
    value = (sum(values) / len(values)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if exact is not None and exact > 0 and exact < value:
        value = exact
    return value, value <= ONE_CENT


def normalize_blueprints(rows):
    recipes = {}
    for data in rows:
        activities = data.get("activities", {})
        manufacturing = activities.get("manufacturing") or activities.get("1") or {}
        products = manufacturing.get("products", [])
        materials = manufacturing.get("materials", [])
        if not products or not materials:
            continue
        product = products[0]
        product_id = product.get("productTypeID", product.get("typeID"))
        if product_id is None:
            continue
        requirements = {}
        for material in materials:
            type_id = material.get("materialTypeID", material.get("typeID"))
            quantity = material.get("quantity")
            if type_id is not None and quantity is not None:
                requirements[int(type_id)] = max(
                    1, int((Decimal(int(quantity)) * Decimal("0.9")).to_integral_value(rounding=ROUND_CEILING))
                )
        if requirements:
            recipes[int(product_id)] = (max(1, int(product.get("quantity", 1))), requirements)
    return recipes


def load_blueprints(conn, recipe_version="current", history=None):
    if recipe_version != "current":
        history = history or RecipeHistory()
        return normalize_blueprints(history.blueprint_rows(recipe_version))
    with conn.cursor() as cur:
        cur.execute("SELECT data FROM public.sde_blueprints")
        return normalize_blueprints(data for (data,) in cur)


def load_history_counts(conn):
    # zKill bounds its backwards calendar scan by the number of entries in
    # that item's complete market history, including dates after an old kill.
    with conn.cursor() as cur:
        cur.execute("""SELECT key::bigint,count(*)
                       FROM market.zkill_daily_prices p
                       CROSS JOIN LATERAL jsonb_object_keys(p.prices) k(key)
                       GROUP BY key""")
        return {type_id: int(count) for type_id, count in cur}


class QuoteCache:
    """Price/Build cache rules from zKill's dated source revisions.

    rules_at is the source/valuation era, never the kill date. A cold cache
    cannot recover the contents of zKill's old Redis instance.
    """

    MARKET_SWITCHES = (
        (datetime(2016, 1, 20, 21, 23, 29, tzinfo=UTC), True),
        (datetime(2016, 3, 28, 13, 5, 31, tzinfo=UTC), False),
        (datetime(2016, 3, 28, 13, 42, 36, tzinfo=UTC), True),
        (datetime(2016, 5, 6, 15, 59, 44, tzinfo=UTC), False),
        (datetime(2016, 5, 6, 22, 39, 11, tzinfo=UTC), True),
        (datetime(2018, 6, 19, 13, 39, 50, tzinfo=UTC), False),
        (datetime(2018, 7, 31, 4, 44, 15, tzinfo=UTC), True),
        (datetime(2019, 1, 13, 19, 46, 14, tzinfo=UTC), False),
        (datetime(2023, 10, 23, 22, 50, 14, tzinfo=UTC), True),
    )
    SPLIT_TTL = datetime(2025, 11, 21, 16, 34, 53, tzinfo=UTC)
    BUILD_RECALC = datetime(2024, 3, 2, 19, 56, 58, tzinfo=UTC)

    def __init__(self, clock=None, rules_at=None):
        self.clock = clock or (lambda: datetime.now(UTC))
        self.rules_at = rules_at
        self.market = OrderedDict()
        self.built = OrderedDict()

    def era(self):
        return self.rules_at or self.clock()

    def market_enabled(self):
        # Earlier implementations used a different price backend. Do not
        # assign the modern Redis policy to unsupported source revisions.
        enabled = False
        for start, active in self.MARKET_SWITCHES:
            if self.era() >= start:
                enabled = active
        return enabled

    def market_get(self, type_id, day):
        if not self.market_enabled():
            return None
        bucket = self.market.get(day)
        if bucket is None:
            return None
        expires, quotes = bucket
        if self.clock() >= expires:
            del self.market[day]
            return None
        self.market.move_to_end(day)
        return quotes.get(type_id)

    def market_put(self, type_id, day, result):
        if not self.market_enabled():
            return
        now = self.clock()
        bucket = self.market.get(day)
        quotes = bucket[1] if bucket is not None and now < bucket[0] else {}
        quotes[type_id] = result
        midnight = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        ttl = 86400
        if self.era() >= self.SPLIT_TTL and midnight < now - timedelta(days=3):
            ttl = 3600
        # zKill expires the entire date hash after HSET, not each item.
        self.market[day] = (now + timedelta(seconds=ttl), quotes)
        self.market.move_to_end(day)
        while len(self.market) > 64:
            self.market.popitem(last=False)

    def build_get(self, type_id, day, recalc=False):
        if recalc and self.era() >= self.BUILD_RECALC:
            return None
        key = (type_id, day)
        item = self.built.get(key)
        if item is None:
            return None
        expires, result = item
        if self.clock() >= expires:
            del self.built[key]
            return None
        self.built.move_to_end(key)
        return result

    def build_put(self, type_id, day, result):
        key = (type_id, day)
        self.built[key] = (self.clock() + timedelta(days=1), result)
        self.built.move_to_end(key)
        while len(self.built) > 50000:
            self.built.popitem(last=False)


class Pricing:
    """Local equivalent of zKill Price/Build, with no network or writes."""

    def __init__(self, metadata, recipes, counts, lookup=None, *, clock=None, rules_at=None,
                 valuation_mode="zkill", recipe_history=None):
        if valuation_mode not in {"zkill", "loss-day"}:
            raise ValueError("Unknown valuation mode")
        self.metadata = metadata
        self.recipes = recipes
        self.counts = counts
        self.histories = defaultdict(lambda: deque(maxlen=100))
        self.lookup = lookup
        self.cache = {}
        self.quotes = QuoteCache(clock, rules_at)
        self.valuation_mode = valuation_mode
        self.recipe_history = recipe_history
        self.recipe_day = None
        self.recipe_version = "fixed"

    def select_recipe_day(self, day):
        if self.recipe_history is None or self.recipe_day == day:
            return
        version = self.recipe_history.version_on(day)
        if version != self.recipe_version:
            self.recipes = load_blueprints(None, version, self.recipe_history) if version else {}
            self.recipe_version = version
            # Quotes that include construction must never cross recipe sets.
            self.quotes.market.clear()
            self.quotes.built.clear()
            self.cache.clear()
            LOG.info("RECIPE_DATE day=%s version=%s", day, version or "unavailable")
        self.recipe_day = day

    def add_day(self, day, payload):
        # Clear calculation traces, but keep unexpired Price/Build quotes:
        # a market-history refresh does not invalidate zKill's quote cache.
        self.cache.clear()
        if self.valuation_mode == "loss-day":
            # Retrospective daily prices use the complete imported snapshot,
            # not a quote frozen before that day's data was available.
            self.quotes.market.clear()
            self.quotes.built.clear()
        for key, value in (payload or {}).items():
            type_id = int(key)
            history = self.histories[type_id]
            if history and history[-1][0] == day:
                history.pop()
            elif history and history[-1][0] > day:
                # Replacing an older observation is unusual. The normal
                # chronological import remains O(1) per item observation.
                history = deque(((d, v) for d, v in history if d != day), maxlen=100)
                self.histories[type_id] = history
            if value is not None and Decimal(str(value)) > 0:
                history.append((day, Decimal(str(value))))

    def market(self, type_id, day):
        # The do/while in zKill inspects at least one calendar date.
        earliest = day - timedelta(days=max(1, self.counts.get(type_id, 0)) - 1)
        observations = self.lookup(type_id, day, earliest) if self.lookup else self.histories.get(type_id, ())
        observations = sorted(
            ((d, Decimal(str(v))) for d, v in observations if earliest <= d <= day and v > 0),
            reverse=True,
        )[:34]
        exact = next((v for d, v in observations if d == day), None)
        return [v for _d, v in observations], exact

    def build(self, type_id, day, stack, recalc=False):
        if " SKIN " in self.metadata.get(type_id, (None, None, ""))[2]:
            return ONE_CENT, False
        if self.recipe_history is not None and self.recipe_version is None:
            raise HistoricalRecipeUnavailable(f"No archived CCP recipe set covers {self.recipe_day}")
        cached = self.quotes.build_get(type_id, day, recalc)
        if cached is not None:
            return cached
        recipe = self.recipes.get(type_id)
        if not recipe:
            return ONE_CENT, True
        output, requirements = recipe
        total = Decimal(0)
        unknown = False
        for material, quantity in requirements.items():
            if material == type_id:
                continue
            material_day = day if self.valuation_mode == "loss-day" else day - timedelta(days=1)
            value, missing = self.price(material, day, material_day, True, stack)
            total += value * quantity
            unknown |= missing
        result = total / output, unknown
        self.quotes.build_put(type_id, day, result)
        return result

    def price(self, type_id, kill_day, market_day, date_only=False, stack=()):
        if self.valuation_mode == "loss-day":
            self.select_recipe_day(kill_day)
            market_day = kill_day
        key = (type_id, kill_day, market_day, date_only)
        # Recheck the real quote expiry on each request. A plain memo here
        # would bypass TTLs and the historical disabled-cache revisions.
        if type_id in stack or len(stack) >= 32:
            return ONE_CENT, True
        result = self._price(type_id, kill_day, market_day, date_only, stack + (type_id,))
        self.cache[key] = result
        return result

    def _price(self, type_id, kill_day, market_day, date_only, stack):
        group, category, _name = self.metadata.get(type_id, (None, None, ""))
        if category == 91:
            return ONE_CENT, False
        fixed = fixed_price(type_id, kill_day, date_only)
        if fixed is not None:
            return fixed, False
        if group == 29:
            return Decimal("10000"), False
        if group in (4513, 4759):
            return Decimal("200"), False
        if group in (30, 659):
            built = self.build(type_id, kill_day, stack)
            if built[0] > 1:
                return built
        if type_id == 2233:
            total = Decimal(0)
            unknown = False
            for material, quantity in ((3962, 1), (2867, 8), (2871, 8), (2876, 8), (2872, 8)):
                value, missing = self.price(material, kill_day, market_day, date_only, stack)
                total += value * quantity
                unknown |= missing
            return total, unknown
        if category == 66:
            built = self.build(type_id, kill_day, stack, recalc=True)
            if built[0] > ONE_CENT:
                return built
        cached = self.quotes.market_get(type_id, market_day)
        if cached is not None:
            return cached
        history, exact = self.market(type_id, market_day)
        value, unknown = rolling_price(history)
        if value <= ONE_CENT:
            value, unknown = self.build(type_id, market_day, stack)
        if exact is not None and exact < value:
            value, unknown = exact, False
        result = value, unknown
        self.quotes.market_put(type_id, market_day, result)
        return result


def load_config(path):
    cfg = json.loads(path.read_text())
    return dict(
        dbname=cfg["db_name"], user=cfg["db_user"], password=cfg["db_password"],
        host=cfg["db_host"], port=cfg.get("db_port", 5432),
    )


def valuation_timestamp(value):
    """An explicit source-era date or aware timestamp, normalized to UTC."""
    try:
        if len(value) == 10:
            return datetime.combine(date.fromisoformat(value), datetime.min.time(), tzinfo=UTC)
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("Include a timezone, for example 2022-08-01T12:00:00Z")
        return result.astimezone(UTC)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def relation_name(prefix, month):
    return f"{prefix}_{month.year}_{month.month:02d}"


def ensure_partition_exists(conn, name):
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f"rawkm.{name}",))
        return cur.fetchone()[0] is not None


def load_metadata(conn):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT t.sde_key::bigint,
                      NULLIF(t.data->>'groupID','')::bigint,
                      NULLIF(g.data->>'categoryID','')::bigint,
                      COALESCE(t.data->'name'->>'en',t.data->>'name','')
               FROM public.sde_types t
               LEFT JOIN public.sde_groups g ON g.sde_key=t.data->>'groupID'
               WHERE t.sde_key ~ '^[0-9]+$'"""
        )
        return {int(type_id): (group_id, category_id, name or "") for type_id, group_id, category_id, name in cur}


def price_coverage(conn, through):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT min(price_date),max(price_date),count(*),
                      (%s::date-%s::date+1)-count(*)
               FROM market.zkill_daily_prices
               WHERE price_date BETWEEN %s AND %s""",
            (through, START, START, through),
        )
        return cur.fetchone()


def month_counts(conn, month):
    table = relation_name("killmails", month)
    if not ensure_partition_exists(conn, table):
        return 0, 0
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT count(*),count(*) FILTER (WHERE estimated_value_cents IS NULL) FROM {}.{}")
            .format(sql.Identifier("rawkm"), sql.Identifier(table))
        )
        total, missing = cur.fetchone()
    return int(total), int(missing)


def pricing_date_sql(valuation_mode="zkill", alias=None):
    column = sql.Identifier(alias, "killmail_time") if alias else sql.Identifier("killmail_time")
    if valuation_mode == "loss-day":
        return sql.SQL("({} AT TIME ZONE 'UTC')::date").format(column)
    if valuation_mode == "zkill":
        return sql.SQL("(({}-interval '3601 seconds') AT TIME ZONE 'UTC')::date").format(column)
    raise ValueError("Unknown valuation mode")


def target_months(conn, valuation_mode="zkill"):
    query = sql.SQL(
        """SELECT date_trunc('month',killmail_time)::date,
                  max({price_day})
           FROM rawkm.killmails
           WHERE estimated_value_cents IS NULL
           GROUP BY 1 ORDER BY 1"""
    ).format(price_day=pricing_date_sql(valuation_mode))
    with conn.cursor() as cur:
        cur.execute(query)
        return [(month, price_day) for month, price_day in cur]


def needed_prices(conn, month, valuation_mode="zkill"):
    km = relation_name("killmails", month)
    items = relation_name("killmail_items", month)
    predicate = sql.SQL("WHERE k.estimated_value_cents IS NULL")
    query = sql.SQL(
        """SELECT price_day,kill_day,item_type_id FROM (
             SELECT DISTINCT {price_day} price_day,
                    (k.killmail_time AT TIME ZONE 'UTC')::date kill_day,
                    k.victim_ship_type_id item_type_id
             FROM {}.{} k {} AND k.victim_ship_type_id IS NOT NULL
             UNION
             SELECT DISTINCT {price_day},
                    (k.killmail_time AT TIME ZONE 'UTC')::date,
                    i.item_type_id
             FROM {}.{} k JOIN {}.{} i
               ON i.killmail_id=k.killmail_id AND i.killmail_time=k.killmail_time
             {} AND i.item_type_id IS NOT NULL
               AND COALESCE(i.singleton,0)<>2 AND COALESCE(i.flag,0)<>179
               AND NOT (i.item_type_id=33329 AND COALESCE(i.flag,0)=89)
           ) q ORDER BY price_day,kill_day,item_type_id"""
    ).format(
        sql.Identifier("rawkm"), sql.Identifier(km), predicate,
        sql.Identifier("rawkm"), sql.Identifier(km),
        sql.Identifier("rawkm"), sql.Identifier(items), predicate,
        price_day=pricing_date_sql(valuation_mode, "k"),
    )
    by_day = defaultdict(list)
    with conn.cursor() as cur:
        cur.execute(query)
        for price_day, kill_day, type_id in cur:
            by_day[price_day].append((kill_day, int(type_id)))
    return by_day


def advance_prices(
    price_cursor, pending_row, through, pricing, needed,
    stopped=None, on_day=None,
):
    rows = []
    last_day = None
    while pending_row is not None and pending_row[0] <= through:
        if stopped is not None and stopped.is_set():
            raise StopRequested()
        day, payload = pending_row
        if on_day is not None and day.day == 1:
            on_day(day)
        pricing.add_day(day, payload)
        rows.extend(boundary_prices(day, needed.get(day, ()), pricing))
        last_day = day
        pending_row = price_cursor.fetchone()
    return pending_row, rows, last_day


def boundary_prices(day, pairs, pricing):
    rows = []
    for kill_day, type_id in pairs:
        try:
            value, fallback = pricing.price(type_id, kill_day, day)
        except HistoricalRecipeUnavailable:
            # NULL signals an unavailable recipe, not a fictional 0.01 price.
            value, fallback = None, True
        rows.append((kill_day, day, type_id, value, fallback))
    return rows


def update_month(conn, month, price_rows, valuation_mode="zkill"):
    km = relation_name("killmails", month)
    items = relation_name("killmail_items", month)
    condition = sql.SQL("AND k.estimated_value_cents IS NULL")
    with conn.cursor() as cur:
        cur.execute(
            """CREATE TEMP TABLE valuation_prices(
                 kill_day date,price_day date,item_type_id bigint,unit_price_isk numeric,used_fallback boolean,
                 PRIMARY KEY(kill_day,price_day,item_type_id)) ON COMMIT DROP"""
        )
        if price_rows:
            execute_values(
                cur,
                "INSERT INTO valuation_prices VALUES %s ON CONFLICT DO NOTHING",
                price_rows,
                page_size=5000,
            )
        cur.execute(
            sql.SQL(
                """CREATE TEMP TABLE valuation_stage ON COMMIT DROP AS
                   SELECT k.killmail_id,k.killmail_time,
                     LEAST(%s::numeric,
                       round(100 * (COALESCE(h.unit_price_isk,0.01)::numeric+
                       COALESCE(sum(
                         (COALESCE(i.quantity_destroyed,0)+COALESCE(i.quantity_dropped,0))::numeric*
                         CASE
                           WHEN i.singleton=2 OR i.flag=179 OR (i.item_type_id=33329 AND i.flag=89)
                           THEN 0.01
                           ELSE COALESCE(p.unit_price_isk,0.01)
                         END
                       ),0)))
                     )::bigint AS value_cents
                   FROM {}.{} k
                   LEFT JOIN {}.{} i ON i.killmail_id=k.killmail_id AND i.killmail_time=k.killmail_time
                   LEFT JOIN valuation_prices h
                     ON h.price_day={price_day}
                    AND h.kill_day=(k.killmail_time AT TIME ZONE 'UTC')::date
                    AND h.item_type_id=k.victim_ship_type_id
                   LEFT JOIN valuation_prices p
                     ON p.price_day={price_day}
                    AND p.kill_day=(k.killmail_time AT TIME ZONE 'UTC')::date
                    AND p.item_type_id=i.item_type_id
                   WHERE TRUE {}
                   GROUP BY k.killmail_id,k.killmail_time,h.unit_price_isk
                   HAVING h.unit_price_isk IS NOT NULL AND count(*) FILTER (
                     WHERE i.item_type_id IS NOT NULL AND p.unit_price_isk IS NULL
                       AND COALESCE(i.singleton,0)<>2 AND COALESCE(i.flag,0)<>179
                       AND NOT (i.item_type_id=33329 AND COALESCE(i.flag,0)=89)
                   )=0"""
            ).format(
                sql.Identifier("rawkm"), sql.Identifier(km),
                sql.Identifier("rawkm"), sql.Identifier(items), condition,
                price_day=pricing_date_sql(valuation_mode, "k"),
            ),
            (MAX_BIGINT,),
        )
        cur.execute(
            sql.SQL(
                """UPDATE {}.{} k SET estimated_value_cents=s.value_cents
                   FROM valuation_stage s
                   WHERE k.killmail_id=s.killmail_id AND k.killmail_time=s.killmail_time
                     AND k.estimated_value_cents IS NULL"""
            ).format(sql.Identifier("rawkm"), sql.Identifier(km))
        )
        updated = cur.rowcount
        cur.execute("SELECT count(*) FROM valuation_prices WHERE used_fallback")
        fallback_pairs = int(cur.fetchone()[0])
    return int(updated), fallback_pairs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-config", type=Path, default=Path.home() / "eveosint/config/db.json")
    parser.add_argument("--progress", type=Path, default=Path.home() / "eveosint/data/market/killmail_values_progress.json")
    parser.add_argument("--min-free-gb", type=float, default=20.0)
    parser.add_argument("--schema-only", action="store_true")
    parser.add_argument("--valuation-mode", choices=("loss-day", "zkill"), default="loss-day",
                        help="Loss-day prices/recipes by default; zkill retains source replay date offsets")
    parser.add_argument("--recipe-version", default="auto",
                        help="auto selects recipes by loss date; current or an exact snapshot overrides it")
    parser.add_argument("--list-recipe-versions", action="store_true",
                        help="List bundled recipe snapshots and exit without connecting to the database")
    parser.add_argument("--cache-rules-at", type=valuation_timestamp,
                        help="Cache source era: YYYY-MM-DD or an ISO timestamp with timezone; not the loss date")
    args = parser.parse_args()
    if not 0 <= args.min_free_gb <= 500:
        parser.error("Invalid disk reserve")
    if args.valuation_mode == "zkill" and args.recipe_version == "auto" and not args.list_recipe_versions:
        parser.error("zKill replay requires an explicit --recipe-version")
    if args.cache_rules_at and args.valuation_mode != "zkill":
        parser.error("--cache-rules-at is only used with --valuation-mode zkill")
    history = None
    if args.list_recipe_versions or args.recipe_version != "current":
        try:
            history = RecipeHistory()
            if args.list_recipe_versions:
                print(json.dumps(history.manifest, indent=2))
                return 0
            if args.recipe_version != "auto":
                args.recipe_version = history.resolve(args.recipe_version)
                # Fail before schema changes or writes if the snapshot is corrupt.
                history.recipes(args.recipe_version)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stopped = threading.Event()
    conn = price_conn = None
    owns_lock = False
    state = {"pid": os.getpid(), "formula_version": FORMULA_VERSION, "months_processed": 0,
             "killmails_valued": 0, "fallback_price_pairs": 0,
             "valuation_mode": args.valuation_mode, "killmails_unvalued": 0,
             "unavailable_recipe_price_pairs": 0,
             "recipe_version": args.recipe_version,
             "cache_rules_at": args.cache_rules_at.isoformat() if args.cache_rules_at else "current"}
    if history is not None and args.recipe_version != "auto":
        snapshot = history.versions[args.recipe_version]
        state.update(recipe_snapshot_date=snapshot["snapshot_date"],
                     recipe_source=snapshot["source"], recipe_sha256=snapshot["recipes_sha256"])

    def progress(phase, **values):
        state.update(phase=phase, updated_at=datetime.now(UTC).isoformat(), **values)
        args.progress.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.progress.with_suffix(".tmp")
        temporary.write_text(json.dumps(state))
        temporary.replace(args.progress)

    def stop(*_):
        stopped.set()
        if conn is not None:
            try:
                conn.cancel()
            except Exception:
                pass

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    try:
        cfg = load_config(args.db_config)
        conn = psycopg2.connect(**cfg, application_name="killmail_valuation")
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (LOCK,))
            if not cur.fetchone()[0]:
                raise RuntimeError("Another killmail valuation is running")
            owns_lock = True
            cur.execute("SET lock_timeout='10s'; SET statement_timeout=0; SET TIME ZONE 'UTC'")
            cur.execute(SCHEMA.read_text())
        conn.commit()
        if args.schema_only:
            progress("schema_ready")
            return 0
        targets = target_months(conn, args.valuation_mode)
        if not targets:
            progress("completed", months_total=0, message="No killmail has a missing value.")
            return 0
        months = [month for month, _price_day in targets]
        target_price_through = max(price_day for _month, price_day in targets)
        with conn.cursor() as cur:
            cur.execute("SELECT max(price_date) FROM market.zkill_daily_prices")
            imported_through = cur.fetchone()[0]
        if imported_through is None:
            raise RuntimeError("Import zKill price history before calculating killmail values")
        price_through = min(target_price_through, imported_through)
        if args.valuation_mode == "loss-day":
            # Today's trading day is unfinished. Those kills stay NULL and
            # are picked up automatically once its daily prices are imported.
            price_through = min(price_through, datetime.now(UTC).date() - timedelta(days=1))
        state.update(price_available_through=price_through.isoformat(),
                     target_latest_price_day=target_price_through.isoformat())
        first_price, last_price, _days, missing_days = price_coverage(conn, price_through)
        if first_price != START or last_price != price_through or missing_days:
            raise RuntimeError(
                f"Complete zKill price history is required from {START} through {price_through}; "
                f"found {first_price or 'none'} through {last_price or 'none'} with {missing_days} missing day(s)."
            )
        progress("loading_pricing_rules")
        metadata = load_metadata(conn)
        automatic = args.recipe_version == "auto"
        pricing = Pricing(metadata, {} if automatic else load_blueprints(conn, args.recipe_version, history),
                          load_history_counts(conn), rules_at=args.cache_rules_at,
                          valuation_mode=args.valuation_mode, recipe_history=history if automatic else None)
        state["recipe_count"] = len(pricing.recipes)
        LOG.info("VALUATION_RULES formula=%s recipes=%s recipe_count=%s cache_rules_at=%s",
                 FORMULA_VERSION, args.recipe_version, len(pricing.recipes), state["cache_rules_at"])
        conn.commit()
        state.update(from_month=months[0].isoformat(), through_month=months[-1].isoformat(), months_total=len(months))
        with conn.cursor() as cur:
            cur.execute("SELECT pg_database_size(current_database())")
            state["database_bytes_start"] = int(cur.fetchone()[0])
        progress("warming_prices", current_month=months[0].isoformat())
        price_conn = psycopg2.connect(**cfg, application_name="killmail_valuation_prices")
        price_conn.set_session(readonly=True, autocommit=False)
        register_default_jsonb(
            price_conn,
            loads=lambda value: json.loads(
                value, parse_float=Decimal, parse_int=Decimal
            ),
        )
        price_cursor = price_conn.cursor(name="killmail_valuation_price_stream")
        price_cursor.itersize = 1
        price_cursor.execute(
            "SELECT price_date,prices FROM market.zkill_daily_prices WHERE price_date BETWEEN %s AND %s ORDER BY price_date",
            (START, price_through),
        )
        pending = price_cursor.fetchone()
        last_processed_day = None
        for index, month in enumerate(months, 1):
            if stopped.is_set():
                raise StopRequested()
            if shutil.disk_usage(args.progress.parent).free < args.min_free_gb * 1024**3:
                raise RuntimeError("Disk reserve reached before the next month; free space and resume.")
            total, missing = month_counts(conn, month)
            state.update(current_month=month.isoformat(), month_index=index, month_killmails=total,
                         month_missing=missing, phase="reading_items")
            progress("reading_items")
            should_write = bool(missing)
            needed = needed_prices(conn, month, args.valuation_mode) if should_write else {}
            # Do not keep a raw-killmail transaction open while historical
            # price snapshots are streamed and reduced in Python.
            conn.commit()
            rows = []
            if last_processed_day in needed:
                rows.extend(boundary_prices(last_processed_day, needed[last_processed_day], pricing))
            month_price_end = min(next_month(month) - timedelta(days=1), price_through)
            pending, new_rows, processed_day = advance_prices(
                price_cursor, pending, month_price_end, pricing, needed,
                stopped=stopped,
                on_day=lambda day: progress(
                    "reading_prices", current_price_day=day.isoformat()
                ),
            )
            rows.extend(new_rows)
            if processed_day is not None:
                last_processed_day = processed_day
            if not should_write:
                state["months_processed"] += 1
                progress("running", month_updated=0, month_fallback_pairs=0)
                continue
            if stopped.is_set():
                raise StopRequested()
            progress("writing_values", price_pairs=len(rows))
            try:
                updated, fallback = update_month(conn, month, rows, args.valuation_mode)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            state["months_processed"] += 1
            state["killmails_valued"] += updated
            state["fallback_price_pairs"] += fallback
            state["killmails_unvalued"] += max(0, missing - updated)
            state["unavailable_recipe_price_pairs"] += sum(value is None for _kd, _pd, _t, value, _f in rows)
            if automatic:
                version = pricing.recipe_version
                state.update(active_recipe_version=version or "unavailable",
                             recipe_snapshot_date=history.versions[version]["snapshot_date"] if version else None,
                             recipe_day=pricing.recipe_day.isoformat() if pricing.recipe_day else None,
                             recipe_count=len(pricing.recipes))
            LOG.info("VALUATION_MONTH month=%s updated=%s total=%s fallback_pairs=%s", month, updated, total, fallback)
            progress("running", month_updated=updated, month_fallback_pairs=fallback)
        with conn.cursor() as cur:
            cur.execute("SELECT pg_database_size(current_database())")
            final_size = int(cur.fetchone()[0])
        progress("completed", current_month=None, database_bytes_end=final_size,
                 database_bytes_added=final_size - state["database_bytes_start"])
        LOG.info("DONE %s", json.dumps(state))
        return 0
    except (StopRequested, QueryCanceled):
        if conn:
            conn.rollback()
        progress("paused", message="Stopped. Completed months are retained; resume skips valued killmails.")
        return 0
    except Exception as exc:
        if conn:
            conn.rollback()
        LOG.exception("Killmail valuation failed")
        if owns_lock or conn is None:
            progress("failed", error=str(exc))
        return 1
    finally:
        if price_conn:
            price_conn.close()
        if conn:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
