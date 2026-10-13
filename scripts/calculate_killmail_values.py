#!/usr/bin/env python3
"""Calculate and store zKill-like values directly on rawkm.killmails."""

import argparse
import json
import logging
import os
import shutil
import signal
import threading
from collections import defaultdict, deque
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import psycopg2
from psycopg2 import sql
from psycopg2.errors import QueryCanceled
from psycopg2.extras import execute_values, register_default_jsonb

START = date(2003, 1, 1)
LOCK = 79130423
FORMULA_VERSION = "zkill-like-v1"
SCHEMA = Path(__file__).with_name("killmail_values_schema.sql")
LOG = logging.getLogger("killmail_values")
ONE_CENT = Decimal("0.01")
MAX_BIGINT = 9223372036854775807

# Price::getSpecialPriceTypeIDs(), used by the public daily price endpoint.
SPECIAL_TYPES = {
    635, 2233, 2834, 2836, 3514, 3516, 3518, 9860, 11011, 11019,
    11375, 11936, 11938, 11940, 11942, 12478, 13202, 25560, 26840,
    26842, 32209, 32788, 32790, 33395, 33397, 33673, 33675, 34556,
    34557, 34558, 34559, 34560, 35779, 35781, 36902, 42124, 42125,
    42126, 42241, 42242, 42243, 42245, 42246, 44264, 44265, 45530,
    45531, 45645, 45647, 45649, 47512, 47514, 48635, 48636, 55511,
    60764, 60765, 74141, 74316, 77726, 78414, 78576, 85062, 85229,
    85236, 87381, 88001, 89807, 89808,
}

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


def next_month(value):
    return date(value.year + (value.month == 12), value.month % 12 + 1, 1)


def cents(value):
    return int((Decimal(value) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fixed_price(type_id, day):
    if type_id == 88001 and day <= date(2025, 6, 1):
        return Decimal("10000000000")
    if type_id == 3514:
        return Decimal("100000000000") if day <= date(2023, 12, 1) else Decimal("250000000000")
    if type_id == 42241:
        return Decimal("350000000000") if day <= date(2019, 7, 1) else Decimal("650000000000")
    if type_id == 11940:
        return Decimal("500000000") if day <= date(2020, 1, 25) else Decimal("3400000000000")
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


def effective_price(type_id, day, history, exact, metadata):
    group_id, category_id, _name = metadata.get(type_id, (None, None, ""))
    if category_id == 91:  # SKINs
        return ONE_CENT, False
    value = fixed_price(type_id, day)
    if value is not None:
        return value, False
    if group_id == 29:  # Capsules
        return Decimal("10000"), False
    if group_id in (4513, 4759):
        return Decimal("200"), False
    # The daily endpoint explicitly supplies calculated/special and capital
    # build prices. Use the exact-day value, as zKill does before market history.
    if (type_id in SPECIAL_TYPES or group_id in (30, 659)) and exact is not None:
        return exact, False
    return rolling_price(history, exact)


def load_config(path):
    cfg = json.loads(path.read_text())
    return dict(
        dbname=cfg["db_name"], user=cfg["db_user"], password=cfg["db_password"],
        host=cfg["db_host"], port=cfg.get("db_port", 5432),
    )


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


def target_months(conn):
    query = sql.SQL(
        """SELECT date_trunc('month',killmail_time)::date,
                  max(((killmail_time-interval '1 hour') AT TIME ZONE 'UTC')::date)
           FROM rawkm.killmails
           WHERE estimated_value_cents IS NULL
           GROUP BY 1 ORDER BY 1"""
    )
    with conn.cursor() as cur:
        cur.execute(query)
        return [(month, price_day) for month, price_day in cur]


def needed_prices(conn, month):
    km = relation_name("killmails", month)
    items = relation_name("killmail_items", month)
    predicate = sql.SQL("WHERE k.estimated_value_cents IS NULL")
    query = sql.SQL(
        """SELECT price_day,item_type_id FROM (
             SELECT DISTINCT ((k.killmail_time-interval '1 hour') AT TIME ZONE 'UTC')::date price_day,
                    k.victim_ship_type_id item_type_id
             FROM {}.{} k {} AND k.victim_ship_type_id IS NOT NULL
             UNION
             SELECT DISTINCT ((k.killmail_time-interval '1 hour') AT TIME ZONE 'UTC')::date,
                    i.item_type_id
             FROM {}.{} k JOIN {}.{} i
               ON i.killmail_id=k.killmail_id AND i.killmail_time=k.killmail_time
             {} AND i.item_type_id IS NOT NULL
           ) q ORDER BY price_day,item_type_id"""
    ).format(
        sql.Identifier("rawkm"), sql.Identifier(km), predicate,
        sql.Identifier("rawkm"), sql.Identifier(km),
        sql.Identifier("rawkm"), sql.Identifier(items), predicate,
    )
    by_day = defaultdict(list)
    with conn.cursor() as cur:
        cur.execute(query)
        for price_day, type_id in cur:
            by_day[price_day].append(int(type_id))
    return by_day


def advance_prices(
    price_cursor, pending_row, through, histories, needed, metadata,
    stopped=None, on_day=None,
):
    rows = []
    last_day = None
    last_exact = {}
    while pending_row is not None and pending_row[0] <= through:
        if stopped is not None and stopped.is_set():
            raise StopRequested()
        day, payload = pending_row
        if on_day is not None and day.day == 1:
            on_day(day)
        exact = {int(key): Decimal(str(value)) for key, value in (payload or {}).items()}
        for type_id, value in exact.items():
            histories[type_id].append(value)
        for type_id in needed.get(day, ()):
            value, fallback = effective_price(
                type_id, day, histories.get(type_id, ()), exact.get(type_id), metadata
            )
            rows.append((day, type_id, cents(value), fallback))
        last_day, last_exact = day, exact
        pending_row = price_cursor.fetchone()
    return pending_row, rows, last_day, last_exact


def boundary_prices(day, type_ids, histories, exact, metadata):
    rows = []
    for type_id in type_ids:
        value, fallback = effective_price(
            type_id, day, histories.get(type_id, ()), exact.get(type_id), metadata
        )
        rows.append((day, type_id, cents(value), fallback))
    return rows


def update_month(conn, month, price_rows):
    km = relation_name("killmails", month)
    items = relation_name("killmail_items", month)
    condition = sql.SQL("AND k.estimated_value_cents IS NULL")
    with conn.cursor() as cur:
        cur.execute(
            """CREATE TEMP TABLE valuation_prices(
                 price_day date,item_type_id bigint,unit_price_cents bigint,used_fallback boolean,
                 PRIMARY KEY(price_day,item_type_id)) ON COMMIT DROP"""
        )
        if price_rows:
            execute_values(
                cur,
                "INSERT INTO valuation_prices VALUES %s ON CONFLICT DO NOTHING",
                price_rows,
                page_size=5000,
            )
        cur.execute(
            """CREATE TEMP TABLE valuation_blueprints(type_id bigint PRIMARY KEY) ON COMMIT DROP;
               INSERT INTO valuation_blueprints
               SELECT sde_key::bigint FROM public.sde_types
               WHERE sde_key ~ '^[0-9]+$'
                 AND lower(COALESCE(data->'name'->>'en',data->>'name','')) LIKE '%blueprint%'"""
        )
        cur.execute(
            sql.SQL(
                """CREATE TEMP TABLE valuation_stage ON COMMIT DROP AS
                   SELECT k.killmail_id,k.killmail_time,
                     LEAST(%s::numeric,
                       COALESCE(h.unit_price_cents,1)::numeric+
                       COALESCE(sum(
                         (COALESCE(i.quantity_destroyed,0)+COALESCE(i.quantity_dropped,0))::numeric*
                         CASE
                           WHEN i.singleton=2 OR i.flag=179 OR (i.item_type_id=33329 AND i.flag=89)
                             OR (k.killmail_id<21112472 AND i.flag=5 AND b.type_id IS NOT NULL)
                           THEN 1
                           ELSE COALESCE(p.unit_price_cents,1)
                         END
                       ),0)
                     )::bigint AS value_cents
                   FROM {}.{} k
                   LEFT JOIN {}.{} i ON i.killmail_id=k.killmail_id AND i.killmail_time=k.killmail_time
                   LEFT JOIN valuation_prices h
                     ON h.price_day=((k.killmail_time-interval '1 hour') AT TIME ZONE 'UTC')::date
                    AND h.item_type_id=k.victim_ship_type_id
                   LEFT JOIN valuation_prices p
                     ON p.price_day=((k.killmail_time-interval '1 hour') AT TIME ZONE 'UTC')::date
                    AND p.item_type_id=i.item_type_id
                   LEFT JOIN valuation_blueprints b ON b.type_id=i.item_type_id
                   WHERE TRUE {}
                   GROUP BY k.killmail_id,k.killmail_time,h.unit_price_cents"""
            ).format(
                sql.Identifier("rawkm"), sql.Identifier(km),
                sql.Identifier("rawkm"), sql.Identifier(items), condition,
            ),
            (MAX_BIGINT,),
        )
        cur.execute(
            sql.SQL(
                """UPDATE {}.{} k SET estimated_value_cents=s.value_cents
                   FROM valuation_stage s
                   WHERE k.killmail_id=s.killmail_id AND k.killmail_time=s.killmail_time"""
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
    args = parser.parse_args()
    if not 0 <= args.min_free_gb <= 500:
        parser.error("Invalid disk reserve")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stopped = threading.Event()
    conn = price_conn = None
    owns_lock = False
    state = {"pid": os.getpid(), "formula_version": FORMULA_VERSION, "months_processed": 0,
             "killmails_valued": 0, "fallback_price_pairs": 0}

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
            cur.execute("SET lock_timeout='10s'; SET statement_timeout=0")
            cur.execute(SCHEMA.read_text())
        conn.commit()
        if args.schema_only:
            progress("schema_ready")
            return 0
        targets = target_months(conn)
        if not targets:
            progress("completed", months_total=0, message="No killmail has a missing value.")
            return 0
        months = [month for month, _price_day in targets]
        price_through = max(price_day for _month, price_day in targets)
        first_price, last_price, _days, missing_days = price_coverage(conn, price_through)
        if first_price != START or last_price != price_through or missing_days:
            raise RuntimeError(
                f"Complete zKill price history is required from {START} through {price_through}; "
                f"found {first_price or 'none'} through {last_price or 'none'} with {missing_days} missing day(s)."
            )
        metadata = load_metadata(conn)
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
        histories = defaultdict(lambda: deque(maxlen=34))
        last_processed_day = None
        last_exact = {}
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
            needed = needed_prices(conn, month) if should_write else {}
            # Do not keep a raw-killmail transaction open while historical
            # price snapshots are streamed and reduced in Python.
            conn.commit()
            rows = []
            if last_processed_day in needed:
                rows.extend(boundary_prices(last_processed_day, needed[last_processed_day], histories, last_exact, metadata))
            month_price_end = min(next_month(month) - timedelta(days=1), price_through)
            pending, new_rows, processed_day, exact = advance_prices(
                price_cursor, pending, month_price_end, histories, needed, metadata,
                stopped=stopped,
                on_day=lambda day: progress(
                    "reading_prices", current_price_day=day.isoformat()
                ),
            )
            rows.extend(new_rows)
            if processed_day is not None:
                last_processed_day, last_exact = processed_day, exact
            if not should_write:
                state["months_processed"] += 1
                progress("running", month_updated=0, month_fallback_pairs=0)
                continue
            if stopped.is_set():
                raise StopRequested()
            progress("writing_values", price_pairs=len(rows))
            try:
                updated, fallback = update_month(conn, month, rows)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            state["months_processed"] += 1
            state["killmails_valued"] += updated
            state["fallback_price_pairs"] += fallback
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
