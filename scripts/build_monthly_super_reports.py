#!/usr/bin/env python3
"""
EVEOSINT — build super/titan report tables.

Modes:
  monthly:
    Creates one snapshot per month, on the configured day of month.
    Default tables:
      report_super.report_mensuel_character_YYYY_MM_DD
      report_super.report_mensuel_corporation_YYYY_MM_DD
      report_super.report_mensuel_alliance_YYYY_MM_DD

  daily:
    Creates one snapshot for --date and the 31 previous days, or all available daily snapshots with --all-daily.
    Default tables:
      report_super.report_daily_character_YYYY_MM_DD
      report_super.report_daily_corporation_YYYY_MM_DD
      report_super.report_daily_alliance_YYYY_MM_DD

No analysis/diff tables are generated here.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable

import psycopg2
from psycopg2 import sql

CONFIG_PATH = Path.home() / "eveosint" / "config" / "db.json"
BASE_DIR = Path.home() / "eveosint"
LOG_DIR = BASE_DIR / "data" / "logs"
LOG_FILE = LOG_DIR / "build_monthly_super_reports.log"

SUPER_SCHEMA = "superintel"
ENTITIES_SCHEMA = "entities"
DEFAULT_REPORT_SCHEMA = "report_super"
DEFAULT_DAY_OF_MONTH = 25
DEFAULT_HOUR_UTC = 11
DEFAULT_DAILY_LOOKBACK_DAYS = 31

TITAN_GROUP_ID = 30
SUPERCARRIER_GROUP_ID = 659

MODE_MONTHLY = "monthly"
MODE_DAILY = "daily"

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SUFFIX_RE = re.compile(r"^[0-9]{4}_[0-9]{2}_[0-9]{2}$")
DAILY_TABLE_RE = re.compile(r"^report_daily_(character|corporation|alliance)_([0-9]{4}_[0-9]{2}_[0-9]{2})$")

SHIP_COLUMNS = [
    ("Erebus", "erebus"),
    ("Revenant", "revenant"),
    ("Leviathan", "leviathan"),
    ("Avatar", "avatar"),
    ("Hel", "hel"),
    ("Ragnarok", "ragnarok"),
    ("Nyx", "nyx"),
    ("Wyvern", "wyvern"),
    ("Aeon", "aeon"),
    ("Vendetta", "vendetta"),
    ("Vanquisher", "vanquisher"),
    ("Molok", "molok"),
    ("Komodo", "komodo"),
    ("Azariel", "azariel"),
]


@dataclass(frozen=True)
class ReportPeriod:
    day: date
    ref_dt: datetime
    suffix: str
    mode: str


@dataclass(frozen=True)
class RebuildSpec:
    mode: str
    all_daily: bool = False
    all_monthly: bool = False


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


logger = logging.getLogger(__name__)


def load_db_config() -> dict:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"Missing DB config file: {CONFIG_PATH}")
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in ["db_name", "db_user", "db_password", "db_host", "db_port"] if k not in cfg]
    if missing:
        raise RuntimeError(f"Missing DB config keys: {', '.join(missing)}")
    return cfg


def db_connect():
    cfg = load_db_config()
    return psycopg2.connect(
        dbname=cfg["db_name"],
        user=cfg["db_user"],
        password=cfg["db_password"],
        host=cfg["db_host"],
        port=cfg["db_port"],
    )


def parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def validate_ident(value: str, label: str) -> str:
    if not IDENT_RE.match(value):
        raise RuntimeError(f"Invalid {label}: {value!r}")
    return value


def validate_suffix(value: str) -> str:
    if not SUFFIX_RE.match(value):
        raise RuntimeError(f"Invalid report suffix: {value!r}")
    return value


def table_exists(conn, schema: str, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema=%s AND table_name=%s
            )
            """,
            (schema, table),
        )
        return bool(cur.fetchone()[0])


def column_exists(conn, schema: str, table: str, column: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema=%s AND table_name=%s AND column_name=%s
            )
            """,
            (schema, table, column),
        )
        return bool(cur.fetchone()[0])


def ensure_table_columns(conn, schema: str, table: str, columns: Iterable[str]) -> None:
    if not table_exists(conn, schema, table):
        raise RuntimeError(f"Missing required table {schema}.{table}")
    missing = [c for c in columns if not column_exists(conn, schema, table, c)]
    if missing:
        raise RuntimeError(f"Missing columns in {schema}.{table}: {', '.join(missing)}")


def ensure_sources(conn) -> None:
    ensure_table_columns(conn, SUPER_SCHEMA, "events", ["killmail_time"])
    ensure_table_columns(conn, SUPER_SCHEMA, "ship_sessions", [
        "character_id", "ship_type_id", "session_id", "first_event_time", "death_time", "is_alive"
    ])
    ensure_table_columns(conn, SUPER_SCHEMA, "super_ship_types", [
        "ship_type_id", "ship_name", "group_id", "group_name"
    ])
    ensure_table_columns(conn, ENTITIES_SCHEMA, "characters", ["character_id", "name", "is_deleted"])
    ensure_table_columns(conn, ENTITIES_SCHEMA, "character_corporation_history", [
        "character_id", "record_id", "corporation_id", "start_date", "end_date", "is_deleted"
    ])
    ensure_table_columns(conn, ENTITIES_SCHEMA, "corporations", [
        "corporation_id", "name", "ticker", "is_deleted"
    ])
    ensure_table_columns(conn, ENTITIES_SCHEMA, "corporation_alliance_history", [
        "corporation_id", "record_id", "alliance_id", "start_date", "end_date", "is_deleted"
    ])
    ensure_table_columns(conn, ENTITIES_SCHEMA, "alliances", ["alliance_id", "name", "ticker", "is_deleted"])


def ensure_report_schema(conn, schema: str) -> None:
    validate_ident(schema, "report schema")
    with conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
    conn.commit()


def month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def next_month(d: date) -> date:
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def months_between(start_month: date, end_month: date) -> Iterable[date]:
    current = month_start(start_month)
    end = month_start(end_month)
    while current <= end:
        yield current
        current = next_month(current)


def days_between(start_day: date, end_day: date) -> Iterable[date]:
    current = start_day
    while current <= end_day:
        yield current
        current += timedelta(days=1)


def get_event_date_bounds(conn) -> tuple[date, date]:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT MIN(killmail_time)::date, MAX(killmail_time)::date FROM {}.events").format(sql.Identifier(SUPER_SCHEMA)))
        row = cur.fetchone()
    if not row or row[0] is None or row[1] is None:
        raise RuntimeError("No rows found in superintel.events")
    return row[0], row[1]


def make_report_period(day_value: date, mode: str, hour_utc: int) -> ReportPeriod:
    if hour_utc < 0 or hour_utc > 23:
        raise RuntimeError("hour_utc must be between 0 and 23")
    if mode not in (MODE_MONTHLY, MODE_DAILY):
        raise RuntimeError(f"Invalid report mode: {mode}")
    return ReportPeriod(
        day=day_value,
        ref_dt=datetime.combine(day_value, time(hour_utc, 0, 0), tzinfo=timezone.utc),
        suffix=day_value.strftime("%Y_%m_%d"),
        mode=mode,
    )


def make_monthly_period(month: date, day_of_month: int, hour_utc: int) -> ReportPeriod:
    if day_of_month < 1 or day_of_month > 28:
        raise RuntimeError("day_of_month must be between 1 and 28")
    d = date(month.year, month.month, day_of_month)
    return make_report_period(d, MODE_MONTHLY, hour_utc)


def table_prefix(mode: str) -> str:
    if mode == MODE_MONTHLY:
        return "report_mensuel"
    if mode == MODE_DAILY:
        return "report_daily"
    raise RuntimeError(f"Invalid report mode: {mode}")


def report_table(kind: str, suffix: str, mode: str) -> str:
    validate_ident(kind, "report kind")
    validate_suffix(suffix)
    return f"{table_prefix(mode)}_{kind}_{suffix}"


def prepare_target(conn, schema: str, table: str, replace: bool) -> bool:
    exists = table_exists(conn, schema, table)
    if exists and not replace:
        logger.info("SKIP existing table %s.%s", schema, table)
        return False
    if exists and replace:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {}.{}").format(sql.Identifier(schema), sql.Identifier(table)))
        conn.commit()
    return True


def build_character_report(conn, schema: str, period: ReportPeriod, replace: bool) -> bool:
    table = report_table("character", period.suffix, period.mode)
    if not prepare_target(conn, schema, table, replace):
        return False

    ship_counts = [
        sql.SQL("COUNT(*) FILTER (WHERE sst.ship_name = {ship})::int AS {col}").format(
            ship=sql.Literal(ship_name), col=sql.Identifier(col)
        )
        for ship_name, col in SHIP_COLUMNS
    ]
    ship_out = [
        sql.SQL("COALESCE(sa.{col}, 0)::int AS {col}").format(col=sql.Identifier(col))
        for _, col in SHIP_COLUMNS
    ]

    with conn.cursor() as cur:
        cur.execute(sql.SQL("""
            CREATE TABLE {dst} AS
            WITH alive AS (
                SELECT ss.character_id, ss.ship_type_id
                FROM {sup}.ship_sessions ss
                WHERE ss.first_event_time <= %s
                  AND (ss.death_time IS NULL OR ss.death_time > %s)
            ),
            ship_agg AS (
                SELECT
                    a.character_id,
                    {ship_counts},
                    COUNT(*) FILTER (WHERE sst.group_id = {supercarrier_group})::int AS total_super,
                    COUNT(*) FILTER (WHERE sst.group_id = {titan_group})::int AS total_titan
                FROM alive a
                JOIN {sup}.super_ship_types sst ON sst.ship_type_id = a.ship_type_id
                GROUP BY a.character_id
            )
            SELECT
                %s::date AS jour,
                sa.character_id::bigint AS character_id,
                COALESCE(ch.name, 'UNKNOWN_' || sa.character_id::text) AS character_name,
                {ship_out},
                COALESCE(cch.corporation_id, 0)::bigint AS corporation_id,
                COALESCE(corp.name, 'UNKNOWN_CORP') AS corporation_name,
                COALESCE(corp.ticker, '') AS corporation_ticker,
                cah.alliance_id::bigint AS alliance_id,
                COALESCE(al.name, 'NO_ALLIANCE') AS alliance_name,
                COALESCE(al.ticker, 'NONE') AS alliance_ticker,
                COALESCE(sa.total_super, 0)::int AS total_super,
                COALESCE(sa.total_titan, 0)::int AS total_titan
            FROM ship_agg sa
            LEFT JOIN {ent}.characters ch
              ON ch.character_id = sa.character_id AND ch.is_deleted = FALSE
            LEFT JOIN LATERAL (
                SELECT h.corporation_id
                FROM {ent}.character_corporation_history h
                WHERE h.character_id = sa.character_id
                  AND h.start_date <= %s
                  AND (h.end_date IS NULL OR %s < h.end_date)
                  AND h.is_deleted = FALSE
                ORDER BY h.start_date DESC, h.record_id DESC
                LIMIT 1
            ) cch ON TRUE
            LEFT JOIN {ent}.corporations corp
              ON corp.corporation_id = cch.corporation_id AND corp.is_deleted = FALSE
            LEFT JOIN LATERAL (
                SELECT h2.alliance_id
                FROM {ent}.corporation_alliance_history h2
                WHERE h2.corporation_id = cch.corporation_id
                  AND h2.start_date <= %s
                  AND (h2.end_date IS NULL OR %s < h2.end_date)
                  AND h2.is_deleted = FALSE
                ORDER BY h2.start_date DESC, h2.record_id DESC
                LIMIT 1
            ) cah ON TRUE
            LEFT JOIN {ent}.alliances al
              ON al.alliance_id = cah.alliance_id AND al.is_deleted = FALSE;
        """).format(
            dst=sql.Identifier(schema, table),
            sup=sql.Identifier(SUPER_SCHEMA),
            ent=sql.Identifier(ENTITIES_SCHEMA),
            ship_counts=sql.SQL(",\n                    ").join(ship_counts),
            ship_out=sql.SQL(",\n                ").join(ship_out),
            supercarrier_group=sql.Literal(SUPERCARRIER_GROUP_ID),
            titan_group=sql.Literal(TITAN_GROUP_ID),
        ), (period.ref_dt, period.ref_dt, period.day, period.ref_dt, period.ref_dt, period.ref_dt, period.ref_dt))

        for col in ["character_id", "corporation_id", "alliance_id"]:
            cur.execute(sql.SQL("CREATE INDEX {idx} ON {dst} ({col})").format(
                idx=sql.Identifier(f"{table}_{col}_idx"),
                dst=sql.Identifier(schema, table),
                col=sql.Identifier(col),
            ))
        cur.execute(sql.SQL("ANALYZE {dst}").format(dst=sql.Identifier(schema, table)))
    conn.commit()
    logger.info("BUILT %s.%s", schema, table)
    return True


def build_corporation_report(conn, schema: str, period: ReportPeriod, replace: bool) -> bool:
    src = report_table("character", period.suffix, period.mode)
    dst = report_table("corporation", period.suffix, period.mode)
    if not table_exists(conn, schema, src):
        raise RuntimeError(f"Missing source table {schema}.{src}")
    if not prepare_target(conn, schema, dst, replace):
        return False

    ship_sums = [sql.SQL("SUM({c})::int AS {c}").format(c=sql.Identifier(col)) for _, col in SHIP_COLUMNS]

    with conn.cursor() as cur:
        cur.execute(sql.SQL("""
            CREATE TABLE {dst} AS
            SELECT
                jour,
                corporation_id,
                corporation_name,
                corporation_ticker,
                alliance_id,
                alliance_name,
                alliance_ticker,
                COUNT(DISTINCT character_id)::int AS nb_characters,
                {ship_sums},
                SUM(total_super)::int AS total_super,
                SUM(total_titan)::int AS total_titan
            FROM {src}
            GROUP BY jour, corporation_id, corporation_name, corporation_ticker, alliance_id, alliance_name, alliance_ticker;
        """).format(
            dst=sql.Identifier(schema, dst),
            src=sql.Identifier(schema, src),
            ship_sums=sql.SQL(",\n                ").join(ship_sums),
        ))
        cur.execute(sql.SQL("CREATE INDEX {idx} ON {dst} (corporation_id)").format(
            idx=sql.Identifier(f"{dst}_corporation_id_idx"), dst=sql.Identifier(schema, dst)
        ))
        cur.execute(sql.SQL("ANALYZE {dst}").format(dst=sql.Identifier(schema, dst)))
    conn.commit()
    logger.info("BUILT %s.%s", schema, dst)
    return True


def build_alliance_report(conn, schema: str, period: ReportPeriod, replace: bool) -> bool:
    src = report_table("character", period.suffix, period.mode)
    dst = report_table("alliance", period.suffix, period.mode)
    if not table_exists(conn, schema, src):
        raise RuntimeError(f"Missing source table {schema}.{src}")
    if not prepare_target(conn, schema, dst, replace):
        return False

    ship_sums = [sql.SQL("SUM({c})::int AS {c}").format(c=sql.Identifier(col)) for _, col in SHIP_COLUMNS]

    with conn.cursor() as cur:
        cur.execute(sql.SQL("""
            CREATE TABLE {dst} AS
            SELECT
                jour,
                alliance_id,
                alliance_name,
                alliance_ticker,
                COUNT(DISTINCT character_id)::int AS nb_characters,
                COUNT(DISTINCT corporation_id)::int AS nb_corporations,
                {ship_sums},
                SUM(total_super)::int AS total_super,
                SUM(total_titan)::int AS total_titan
            FROM {src}
            GROUP BY jour, alliance_id, alliance_name, alliance_ticker;
        """).format(
            dst=sql.Identifier(schema, dst),
            src=sql.Identifier(schema, src),
            ship_sums=sql.SQL(",\n                ").join(ship_sums),
        ))
        cur.execute(sql.SQL("CREATE INDEX {idx} ON {dst} (alliance_id)").format(
            idx=sql.Identifier(f"{dst}_alliance_id_idx"), dst=sql.Identifier(schema, dst)
        ))
        cur.execute(sql.SQL("ANALYZE {dst}").format(dst=sql.Identifier(schema, dst)))
    conn.commit()
    logger.info("BUILT %s.%s", schema, dst)
    return True


def resolve_monthly_periods(conn, args) -> list[ReportPeriod]:
    db_start_day, db_end_day = get_event_date_bounds(conn)
    db_start = month_start(db_start_day)
    db_end = month_start(db_end_day)
    start = month_start(args.from_date) if args.from_date else db_start
    end = month_start(args.to_date) if args.to_date else db_end

    if end < start:
        raise RuntimeError("--to cannot be before --from")

    today_utc = datetime.now(timezone.utc).date()
    periods: list[ReportPeriod] = []

    for month in months_between(start, end):
        period = make_monthly_period(month, args.day_of_month, args.hour_utc)

        if not args.include_future and period.day > today_utc:
            logger.info("SKIP future report date %s because today_utc=%s", period.day, today_utc)
            continue

        periods.append(period)

    return periods


def existing_daily_suffixes(conn, schema: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_name LIKE 'report_daily_character_%%'
            ORDER BY table_name
            """,
            (schema,),
        )
        rows = [r[0] for r in cur.fetchall()]

    suffixes: list[str] = []
    for table in rows:
        match = DAILY_TABLE_RE.match(table)
        if not match:
            continue
        suffixes.append(match.group(2))

    return suffixes


def suffix_to_date(suffix: str) -> date:
    validate_suffix(suffix)
    return datetime.strptime(suffix, "%Y_%m_%d").date()


def resolve_daily_periods(conn, args, schema: str) -> tuple[list[ReportPeriod], bool]:
    today_utc = datetime.now(timezone.utc).date()

    if args.all_daily:
        db_start_day, db_end_day = get_event_date_bounds(conn)
        end = db_end_day
        if not args.include_future and end > today_utc:
            end = today_utc
        if end < db_start_day:
            logger.info("No eligible full daily range: start=%s end=%s today_utc=%s", db_start_day, end, today_utc)
            return [], args.replace
        periods = [
            make_report_period(day_value, MODE_DAILY, args.hour_utc)
            for day_value in days_between(db_start_day, end)
        ]
        return periods, args.replace

    if args.rebuild_all:
        suffixes = existing_daily_suffixes(conn, schema)
        if not suffixes:
            logger.info("No existing daily character report found in %s", schema)
            return [], True
        periods = [make_report_period(suffix_to_date(s), MODE_DAILY, args.hour_utc) for s in suffixes]
        if not args.include_future:
            periods = [p for p in periods if p.day <= today_utc]
        return periods, True

    if args.date is None:
        raise RuntimeError("--mode daily requires --date YYYY-MM-DD unless --all-daily or --rebuild-all is used")

    db_start_day, _ = get_event_date_bounds(conn)
    requested = args.date
    start = requested - timedelta(days=args.lookback_days)
    end = requested

    if end < db_start_day:
        logger.info("Requested daily window ends before first event date: end=%s first_event=%s", end, db_start_day)
        return [], args.replace

    if start < db_start_day:
        start = db_start_day

    periods = []
    for day_value in days_between(start, end):
        if not args.include_future and day_value > today_utc:
            logger.info("SKIP future report date %s because today_utc=%s", day_value, today_utc)
            continue
        periods.append(make_report_period(day_value, MODE_DAILY, args.hour_utc))

    return periods, args.replace


def build_periods(conn, schema: str, periods: list[ReportPeriod], replace: bool, dry_run: bool) -> None:
    if not periods:
        logger.info("No eligible report period to build")
        return

    logger.info("Periods: %s -> %s count=%s mode=%s", periods[0].day, periods[-1].day, len(periods), periods[0].mode)

    if dry_run:
        for period in periods:
            logger.info("DRY %s.%s", schema, report_table("character", period.suffix, period.mode))
            logger.info("DRY %s.%s", schema, report_table("corporation", period.suffix, period.mode))
            logger.info("DRY %s.%s", schema, report_table("alliance", period.suffix, period.mode))
        return

    built_char = built_corp = built_alli = 0
    for period in periods:
        logger.info("PERIOD %s mode=%s ref_dt=%s replace=%s", period.day, period.mode, period.ref_dt.isoformat(), replace)
        if build_character_report(conn, schema, period, replace):
            built_char += 1
        if build_corporation_report(conn, schema, period, replace):
            built_corp += 1
        if build_alliance_report(conn, schema, period, replace):
            built_alli += 1

    logger.info("DONE character=%s corporation=%s alliance=%s", built_char, built_corp, built_alli)


def build(args) -> None:
    report_schema = validate_ident(args.schema, "report schema")
    logger.info(
        "START build_super_reports schema=%s mode=%s replace=%s rebuild_all=%s",
        report_schema,
        args.mode,
        args.replace,
        args.rebuild_all,
    )

    if args.mode == MODE_MONTHLY and args.rebuild_all:
        raise RuntimeError("--rebuild-all is only supported for --mode daily")

    conn = db_connect()
    try:
        ensure_sources(conn)
        ensure_report_schema(conn, report_schema)

        if args.mode == MODE_MONTHLY:
            periods = resolve_monthly_periods(conn, args)
            replace = args.replace
        elif args.mode == MODE_DAILY:
            periods, replace = resolve_daily_periods(conn, args, report_schema)
        else:
            raise RuntimeError(f"Invalid mode: {args.mode}")

        build_periods(conn, report_schema, periods, replace, args.dry_run)
    finally:
        conn.close()


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Build super/titan report SQL tables.")
    parser.add_argument("--schema", default=DEFAULT_REPORT_SCHEMA)
    parser.add_argument("--mode", choices=[MODE_MONTHLY, MODE_DAILY], default=MODE_MONTHLY)

    # Monthly range options.
    parser.add_argument("--from", dest="from_date", type=parse_date, help="monthly mode: YYYY-MM-DD, month only")
    parser.add_argument("--to", dest="to_date", type=parse_date, help="monthly mode: YYYY-MM-DD, month only")
    parser.add_argument("--day-of-month", type=int, default=DEFAULT_DAY_OF_MONTH)

    # Daily options.
    parser.add_argument("--date", type=parse_date, help="daily mode: YYYY-MM-DD. Builds this date and previous 31 days by default")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_DAILY_LOOKBACK_DAYS)
    parser.add_argument("--all-daily", action="store_true", help="daily mode only: build every daily report from first event to last available event, skipping existing tables unless --replace is set")
    parser.add_argument("--rebuild-all", action="store_true", help="daily mode only: rebuild all existing daily report dates")

    # Shared options.
    parser.add_argument("--hour-utc", type=int, default=DEFAULT_HOUR_UTC)
    parser.add_argument("--replace", action="store_true", help="replace existing generated report tables in the selected period")
    parser.add_argument("--include-future", action="store_true", help="also build report dates after today, not recommended")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.lookback_days < 0:
        raise RuntimeError("--lookback-days must be >= 0")
    if args.all_daily and args.rebuild_all:
        raise RuntimeError("Use either --all-daily or --rebuild-all, not both")
    if args.all_daily and args.date is not None:
        raise RuntimeError("Use either --all-daily or --date, not both")

    build(args)


if __name__ == "__main__":
    main()
