#!/usr/bin/env python3
"""
build_superintel_events.py

EVEOSINT — construit les tables superintel depuis rawkm normalisé.

RÈGLE IMPORTANTE :
  Ce script ne supprime jamais les données métier.
  Aucun TRUNCATE / DELETE / DROP sur superintel.
  Tous les modes sont append-only / idempotents.

Sources :
  rawkm.killmails
  rawkm.killmail_attackers
  public.sde_groups.data
  public.sde_types.data

Sorties :
  superintel.super_ship_types
  superintel.events
  superintel.super_pilots
  superintel.pilot_last_activity
  superintel.ship_sessions
  superintel.build_state
  superintel.build_runs

Notes sessions :
  - role = attacker : ouvre/continue une session.
  - role = victim : ferme la session ouverte ; si aucune session ouverte, crée une session instantanée morte.
  - sécurité temporelle : un attacker du même character_id + ship_type_id dans les 24h après une victim
    précédente est ignoré pour éviter une fausse nouvelle session.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg2


# ============================================================
# CONFIG
# ============================================================

CONFIG_PATH = Path.home() / "eveosint" / "config" / "db.json"
BASE_DIR = Path.home() / "eveosint"
LOG_DIR = BASE_DIR / "data" / "logs"
LOG_FILE = LOG_DIR / "build_superintel_events.log"

RAW_SCHEMA = "rawkm"
RAW_KILLMAILS_TABLE = "killmails"
RAW_ATTACKERS_TABLE = "killmail_attackers"

SUPER_SCHEMA = "superintel"
SDE_SCHEMA = "public"
SDE_GROUPS_TABLE = "sde_groups"
SDE_TYPES_TABLE = "sde_types"

TITAN_GROUP_ID = 30
SUPERCARRIER_GROUP_ID = 659
SUPER_GROUP_IDS = [TITAN_GROUP_ID, SUPERCARRIER_GROUP_ID]

DEFAULT_BATCH_SIZE = 50000
PIPELINE_NAME = "build_superintel_events"

ROLE_VICTIM = "victim"
ROLE_ATTACKER = "attacker"

SESSION_SAFETY_HOURS = 24


# ============================================================
# LOGGING
# ============================================================


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


# ============================================================
# TYPES
# ============================================================


@dataclass
class Stats:
    scanned_killmails: int = 0
    inserted_victim_events: int = 0
    inserted_attacker_events: int = 0
    inserted_events_total: int = 0
    super_killmails: int = 0
    refreshed_super_ship_types: int = 0
    upserted_super_pilots: int = 0
    upserted_last_activity: int = 0
    upserted_ship_sessions: int = 0
    checkpoint_killmail_id: int | None = None
    checkpoint_killmail_time: datetime | None = None


@dataclass
class Mode:
    name: str
    start_dt: datetime | None = None
    end_dt_exclusive: datetime | None = None


# ============================================================
# DB HELPERS
# ============================================================


def load_db_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"Missing DB config file: {CONFIG_PATH}")

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = json.load(f)

    required = ["db_name", "db_user", "db_password", "db_host", "db_port"]
    missing = [k for k in required if k not in cfg]
    if missing:
        raise RuntimeError(f"Missing keys in {CONFIG_PATH}: {', '.join(missing)}")

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


def qname(schema: str, table: str) -> str:
    return f'"{schema}"."{table}"'


def table_exists(conn, schema: str, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema=%s
                  AND table_name=%s
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
                WHERE table_schema=%s
                  AND table_name=%s
                  AND column_name=%s
            )
            """,
            (schema, table, column),
        )
        return bool(cur.fetchone()[0])


def ensure_column(conn, schema: str, table: str, column: str, ddl_type: str) -> None:
    if column_exists(conn, schema, table, column):
        return
    with conn.cursor() as cur:
        cur.execute(f'ALTER TABLE {qname(schema, table)} ADD COLUMN "{column}" {ddl_type};')
    conn.commit()


def get_obsolete_columns(conn, schema: str, table: str, columns: list[str]) -> list[str]:
    found: list[str] = []
    for col in columns:
        if column_exists(conn, schema, table, col):
            found.append(col)
    return found


def fail_if_obsolete_columns_exist(conn) -> None:
    """
    On ne DROP pas automatiquement.
    Si les anciennes colonnes foireuses existent encore, le script stoppe clairement.
    L'utilisateur les supprime à la main avec le SQL validé.
    """
    checks = {
        "events": ["final_blow"],
        "super_pilots": [
            "first_seen_time",
            "first_seen_killmail_id",
            "last_seen_killmail_id",
            "first_seen_role",
            "last_seen_role",
            "first_seen_ship_type_id",
            "last_seen_ship_type_id",
            "event_count",
        ],
        "pilot_last_activity": ["final_blow"],
    }

    problems: list[str] = []
    for table, columns in checks.items():
        if not table_exists(conn, SUPER_SCHEMA, table):
            continue
        found = get_obsolete_columns(conn, SUPER_SCHEMA, table, columns)
        if found:
            problems.append(f"{SUPER_SCHEMA}.{table}: {', '.join(found)}")

    if problems:
        raise RuntimeError(
            "Obsolete columns still exist. Drop them manually before running this script: "
            + " | ".join(problems)
        )


def ensure_required_raw_tables(conn) -> None:
    required = {
        RAW_KILLMAILS_TABLE: [
            "killmail_id",
            "killmail_time",
            "victim_character_id",
            "victim_corporation_id",
            "victim_alliance_id",
            "victim_ship_type_id",
        ],
        RAW_ATTACKERS_TABLE: [
            "killmail_id",
            "killmail_time",
            "attacker_index",
            "character_id",
            "corporation_id",
            "alliance_id",
            "ship_type_id",
        ],
    }

    for table, columns in required.items():
        if not table_exists(conn, RAW_SCHEMA, table):
            raise RuntimeError(f"Missing source table {RAW_SCHEMA}.{table}")

        missing = [c for c in columns if not column_exists(conn, RAW_SCHEMA, table, c)]
        if missing:
            raise RuntimeError(f"Missing columns in {RAW_SCHEMA}.{table}: {', '.join(missing)}")


def ensure_required_sde_tables(conn) -> None:
    for table in (SDE_GROUPS_TABLE, SDE_TYPES_TABLE):
        if not table_exists(conn, SDE_SCHEMA, table):
            raise RuntimeError(f"Missing SDE table {SDE_SCHEMA}.{table}")
        if not column_exists(conn, SDE_SCHEMA, table, "data"):
            raise RuntimeError(f"Missing column {SDE_SCHEMA}.{table}.data")


# ============================================================
# SCHEMA / TABLES
# ============================================================


def ensure_superintel_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{SUPER_SCHEMA}";')

        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {qname(SUPER_SCHEMA, 'super_ship_types')} (
            ship_type_id BIGINT PRIMARY KEY,
            ship_name TEXT NOT NULL,
            group_id BIGINT NOT NULL,
            group_name TEXT NOT NULL,
            refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)

        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {qname(SUPER_SCHEMA, 'events')} (
            killmail_id BIGINT NOT NULL,
            killmail_time TIMESTAMPTZ NOT NULL,
            role TEXT NOT NULL,
            source_index INTEGER NOT NULL,
            character_id BIGINT NOT NULL,
            corporation_id BIGINT,
            alliance_id BIGINT,
            ship_type_id BIGINT NOT NULL,
            detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (killmail_id, killmail_time, role, source_index)
        );
        """)

        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {qname(SUPER_SCHEMA, 'super_pilots')} (
            character_id BIGINT PRIMARY KEY,
            last_seen_time TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)

        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {qname(SUPER_SCHEMA, 'pilot_last_activity')} (
            character_id BIGINT PRIMARY KEY,
            last_killmail_id BIGINT NOT NULL,
            last_killmail_time TIMESTAMPTZ NOT NULL,
            last_role TEXT NOT NULL,
            corporation_id BIGINT,
            alliance_id BIGINT,
            ship_type_id BIGINT,
            refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)

        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {qname(SUPER_SCHEMA, 'ship_sessions')} (
            character_id BIGINT NOT NULL,
            ship_type_id BIGINT NOT NULL,
            session_id BIGINT NOT NULL,
            first_event_time TIMESTAMPTZ NOT NULL,
            last_event_time TIMESTAMPTZ NOT NULL,
            death_time TIMESTAMPTZ,
            is_alive BOOLEAN NOT NULL,
            event_count BIGINT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (character_id, ship_type_id, session_id)
        );
        """)

        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {qname(SUPER_SCHEMA, 'build_state')} (
            pipeline_name TEXT PRIMARY KEY,
            last_killmail_id BIGINT,
            last_killmail_time TIMESTAMPTZ,
            last_success_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)

        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {qname(SUPER_SCHEMA, 'build_runs')} (
            run_id UUID PRIMARY KEY,
            pipeline_name TEXT NOT NULL,
            mode TEXT NOT NULL,
            from_time TIMESTAMPTZ,
            to_time TIMESTAMPTZ,
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at TIMESTAMPTZ,
            status TEXT NOT NULL,
            scanned_killmails BIGINT NOT NULL DEFAULT 0,
            inserted_victim_events BIGINT NOT NULL DEFAULT 0,
            inserted_attacker_events BIGINT NOT NULL DEFAULT 0,
            inserted_events_total BIGINT NOT NULL DEFAULT 0,
            super_killmails BIGINT NOT NULL DEFAULT 0,
            refreshed_super_ship_types BIGINT NOT NULL DEFAULT 0,
            upserted_super_pilots BIGINT NOT NULL DEFAULT 0,
            upserted_last_activity BIGINT NOT NULL DEFAULT 0,
            upserted_ship_sessions BIGINT NOT NULL DEFAULT 0,
            checkpoint_killmail_id BIGINT,
            checkpoint_killmail_time TIMESTAMPTZ,
            error TEXT
        );
        """)

        cur.execute(f"CREATE INDEX IF NOT EXISTS superintel_events_character_idx ON {qname(SUPER_SCHEMA, 'events')} (character_id);")
        cur.execute(f"CREATE INDEX IF NOT EXISTS superintel_events_time_idx ON {qname(SUPER_SCHEMA, 'events')} (killmail_time);")
        cur.execute(f"CREATE INDEX IF NOT EXISTS superintel_events_ship_idx ON {qname(SUPER_SCHEMA, 'events')} (ship_type_id);")
        cur.execute(f"CREATE INDEX IF NOT EXISTS superintel_events_corp_idx ON {qname(SUPER_SCHEMA, 'events')} (corporation_id);")
        cur.execute(f"CREATE INDEX IF NOT EXISTS superintel_events_alliance_idx ON {qname(SUPER_SCHEMA, 'events')} (alliance_id);")
        cur.execute(f"CREATE INDEX IF NOT EXISTS superintel_ship_sessions_alive_idx ON {qname(SUPER_SCHEMA, 'ship_sessions')} (is_alive, ship_type_id);")
        cur.execute(f"CREATE INDEX IF NOT EXISTS superintel_ship_sessions_char_idx ON {qname(SUPER_SCHEMA, 'ship_sessions')} (character_id);")
        cur.execute(f"CREATE INDEX IF NOT EXISTS superintel_ship_sessions_time_idx ON {qname(SUPER_SCHEMA, 'ship_sessions')} (first_event_time, death_time);")

    conn.commit()

    # Migration non destructive pour build_runs uniquement.
    for col, typ in [
        ("from_time", "TIMESTAMPTZ"),
        ("to_time", "TIMESTAMPTZ"),
        ("finished_at", "TIMESTAMPTZ"),
        ("scanned_killmails", "BIGINT NOT NULL DEFAULT 0"),
        ("inserted_victim_events", "BIGINT NOT NULL DEFAULT 0"),
        ("inserted_attacker_events", "BIGINT NOT NULL DEFAULT 0"),
        ("inserted_events_total", "BIGINT NOT NULL DEFAULT 0"),
        ("super_killmails", "BIGINT NOT NULL DEFAULT 0"),
        ("refreshed_super_ship_types", "BIGINT NOT NULL DEFAULT 0"),
        ("upserted_super_pilots", "BIGINT NOT NULL DEFAULT 0"),
        ("upserted_last_activity", "BIGINT NOT NULL DEFAULT 0"),
        ("upserted_ship_sessions", "BIGINT NOT NULL DEFAULT 0"),
        ("checkpoint_killmail_id", "BIGINT"),
        ("checkpoint_killmail_time", "TIMESTAMPTZ"),
        ("error", "TEXT"),
    ]:
        ensure_column(conn, SUPER_SCHEMA, "build_runs", col, typ)

    # Colonnes utiles ajoutées sans destruction si table créée par une ancienne version.
    for col, typ in [
        ("last_seen_time", "TIMESTAMPTZ"),
        ("created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
        ("updated_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
    ]:
        ensure_column(conn, SUPER_SCHEMA, "super_pilots", col, typ)

    for col, typ in [
        ("refreshed_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
    ]:
        ensure_column(conn, SUPER_SCHEMA, "pilot_last_activity", col, typ)


def start_run(conn, run_id: str, mode: Mode) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {qname(SUPER_SCHEMA, 'build_runs')}
                (run_id, pipeline_name, mode, from_time, to_time, status)
            VALUES (%s, %s, %s, %s, %s, 'running')
            ON CONFLICT (run_id) DO NOTHING
            """,
            (run_id, PIPELINE_NAME, mode.name, mode.start_dt, mode.end_dt_exclusive),
        )
    conn.commit()


def finish_run(conn, run_id: str, status: str, stats: Stats, error: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {qname(SUPER_SCHEMA, 'build_runs')}
            SET
                finished_at = NOW(),
                status = %s,
                scanned_killmails = %s,
                inserted_victim_events = %s,
                inserted_attacker_events = %s,
                inserted_events_total = %s,
                super_killmails = %s,
                refreshed_super_ship_types = %s,
                upserted_super_pilots = %s,
                upserted_last_activity = %s,
                upserted_ship_sessions = %s,
                checkpoint_killmail_id = %s,
                checkpoint_killmail_time = %s,
                error = %s
            WHERE run_id = %s
            """,
            (
                status,
                stats.scanned_killmails,
                stats.inserted_victim_events,
                stats.inserted_attacker_events,
                stats.inserted_events_total,
                stats.super_killmails,
                stats.refreshed_super_ship_types,
                stats.upserted_super_pilots,
                stats.upserted_last_activity,
                stats.upserted_ship_sessions,
                stats.checkpoint_killmail_id,
                stats.checkpoint_killmail_time,
                error,
                run_id,
            ),
        )
    conn.commit()


# ============================================================
# MODE / ARGUMENTS
# ============================================================


def parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def utc_day_start(d: date) -> datetime:
    return datetime.combine(d, time(0, 0, 0), tzinfo=timezone.utc)


def resolve_mode(args) -> Mode:
    selected = sum(
        1
        for cond in (
            args.rebuild,
            args.months is not None,
            args.from_date is not None or args.to_date is not None,
        )
        if cond
    )
    if selected > 1:
        raise RuntimeError("Use only one mode among --rebuild, --months, or --from/--to")

    if args.rebuild:
        return Mode(name="rebuild")

    if args.months is not None:
        if args.months <= 0:
            raise RuntimeError("--months must be greater than 0")
        today = datetime.now(timezone.utc).date()
        approx_start = today - timedelta(days=args.months * 31)
        return Mode(
            name=f"months_{args.months}",
            start_dt=utc_day_start(approx_start),
            end_dt_exclusive=utc_day_start(today + timedelta(days=1)),
        )

    if args.from_date is not None or args.to_date is not None:
        if args.from_date is None or args.to_date is None:
            raise RuntimeError("Use --from and --to together")
        if args.to_date < args.from_date:
            raise RuntimeError("--to must be greater than or equal to --from")
        return Mode(
            name="date_range",
            start_dt=utc_day_start(args.from_date),
            end_dt_exclusive=utc_day_start(args.to_date + timedelta(days=1)),
        )

    return Mode(name="incremental")


# ============================================================
# PIPELINE SQL
# ============================================================


def refresh_super_ship_types_from_sde(conn) -> int:
    logger.info("Refreshing/upserting super_ship_types from public.sde_groups/public.sde_types")

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {qname(SUPER_SCHEMA, 'super_ship_types')}
                (ship_type_id, ship_name, group_id, group_name, refreshed_at)
            SELECT
                (t.data->>'_key')::BIGINT AS ship_type_id,
                COALESCE(t.data->'name'->>'en', 'UNKNOWN_' || (t.data->>'_key')) AS ship_name,
                (g.data->>'_key')::BIGINT AS group_id,
                COALESCE(g.data->'name'->>'en', 'UNKNOWN_GROUP_' || (g.data->>'_key')) AS group_name,
                NOW() AS refreshed_at
            FROM {qname(SDE_SCHEMA, SDE_TYPES_TABLE)} t
            JOIN {qname(SDE_SCHEMA, SDE_GROUPS_TABLE)} g
              ON (g.data->>'_key')::BIGINT = (t.data->>'groupID')::BIGINT
            WHERE (g.data->>'_key')::BIGINT = ANY(%s)
              AND COALESCE((t.data->>'published')::BOOLEAN, TRUE) = TRUE
            ON CONFLICT (ship_type_id)
            DO UPDATE SET
                ship_name = EXCLUDED.ship_name,
                group_id = EXCLUDED.group_id,
                group_name = EXCLUDED.group_name,
                refreshed_at = NOW();
            """,
            (SUPER_GROUP_IDS,),
        )
        affected = cur.rowcount

        cur.execute(
            f"SELECT COUNT(*) FROM {qname(SUPER_SCHEMA, 'super_ship_types')} WHERE group_id = ANY(%s);",
            (SUPER_GROUP_IDS,),
        )
        current_count = int(cur.fetchone()[0])
        if current_count <= 0:
            raise RuntimeError(
                "SDE lookup returned zero Titan/Super ship types from "
                "public.sde_groups/public.sde_types with groups 30/659"
            )

    conn.commit()
    logger.info("Super ship types current rows=%s affected=%s", current_count, affected)
    return affected


def get_checkpoint(conn) -> tuple[int | None, datetime | None]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT last_killmail_id, last_killmail_time
            FROM {qname(SUPER_SCHEMA, 'build_state')}
            WHERE pipeline_name = %s
            """,
            (PIPELINE_NAME,),
        )
        row = cur.fetchone()
    if row is None:
        return None, None
    return row[0], row[1]


def create_todo_table(conn, mode: Mode, batch_size: int) -> int:
    last_id, last_time = get_checkpoint(conn)

    with conn.cursor() as cur:
        # Table temporaire uniquement : aucun impact métier.
        cur.execute("DROP TABLE IF EXISTS tmp_superintel_todo;")
        cur.execute(
            """
            CREATE TEMP TABLE tmp_superintel_todo (
                killmail_id BIGINT NOT NULL,
                killmail_time TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (killmail_id, killmail_time)
            ) ON COMMIT PRESERVE ROWS;
            """
        )

        if mode.name == "incremental":
            if last_id is None or last_time is None:
                logger.info("Incremental without checkpoint: first batch batch_size=%s", batch_size)
                cur.execute(
                    f"""
                    INSERT INTO tmp_superintel_todo (killmail_id, killmail_time)
                    SELECT killmail_id, killmail_time
                    FROM {qname(RAW_SCHEMA, RAW_KILLMAILS_TABLE)}
                    ORDER BY killmail_time, killmail_id
                    LIMIT %s
                    """,
                    (batch_size,),
                )
            else:
                logger.info(
                    "Incremental checkpoint killmail_time=%s killmail_id=%s batch_size=%s",
                    last_time,
                    last_id,
                    batch_size,
                )
                cur.execute(
                    f"""
                    INSERT INTO tmp_superintel_todo (killmail_id, killmail_time)
                    SELECT killmail_id, killmail_time
                    FROM {qname(RAW_SCHEMA, RAW_KILLMAILS_TABLE)}
                    WHERE (killmail_time, killmail_id) > (%s, %s)
                    ORDER BY killmail_time, killmail_id
                    LIMIT %s
                    """,
                    (last_time, last_id, batch_size),
                )

        elif mode.name == "rebuild":
            logger.info("Rebuild mode is NON DESTRUCTIVE: no truncate/delete, only insert/upsert")
            cur.execute(
                f"""
                INSERT INTO tmp_superintel_todo (killmail_id, killmail_time)
                SELECT killmail_id, killmail_time
                FROM {qname(RAW_SCHEMA, RAW_KILLMAILS_TABLE)}
                """
            )

        else:
            if mode.start_dt is None or mode.end_dt_exclusive is None:
                raise RuntimeError("Internal error: range mode missing dates")
            logger.info("Range mode is NON DESTRUCTIVE: no delete, only insert/upsert")
            cur.execute(
                f"""
                INSERT INTO tmp_superintel_todo (killmail_id, killmail_time)
                SELECT killmail_id, killmail_time
                FROM {qname(RAW_SCHEMA, RAW_KILLMAILS_TABLE)}
                WHERE killmail_time >= %s
                  AND killmail_time < %s
                """,
                (mode.start_dt, mode.end_dt_exclusive),
            )

        inserted = cur.rowcount

    conn.commit()
    logger.info("Todo killmails=%s", inserted)
    return inserted


def insert_victim_events(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {qname(SUPER_SCHEMA, 'events')} (
                killmail_id,
                killmail_time,
                role,
                source_index,
                character_id,
                corporation_id,
                alliance_id,
                ship_type_id
            )
            SELECT
                km.killmail_id,
                km.killmail_time,
                %s AS role,
                -1 AS source_index,
                km.victim_character_id AS character_id,
                km.victim_corporation_id AS corporation_id,
                km.victim_alliance_id AS alliance_id,
                km.victim_ship_type_id AS ship_type_id
            FROM tmp_superintel_todo todo
            JOIN {qname(RAW_SCHEMA, RAW_KILLMAILS_TABLE)} km
              ON km.killmail_id = todo.killmail_id
             AND km.killmail_time = todo.killmail_time
            JOIN {qname(SUPER_SCHEMA, 'super_ship_types')} sst
              ON sst.ship_type_id = km.victim_ship_type_id
            WHERE km.victim_character_id IS NOT NULL
            ON CONFLICT (killmail_id, killmail_time, role, source_index)
            DO NOTHING;
            """,
            (ROLE_VICTIM,),
        )
        rows = cur.rowcount

    conn.commit()
    logger.info("Victim events inserted=%s", rows)
    return rows


def insert_attacker_events(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {qname(SUPER_SCHEMA, 'events')} (
                killmail_id,
                killmail_time,
                role,
                source_index,
                character_id,
                corporation_id,
                alliance_id,
                ship_type_id
            )
            SELECT
                atk.killmail_id,
                atk.killmail_time,
                %s AS role,
                atk.attacker_index AS source_index,
                atk.character_id,
                atk.corporation_id,
                atk.alliance_id,
                atk.ship_type_id
            FROM tmp_superintel_todo todo
            JOIN {qname(RAW_SCHEMA, RAW_ATTACKERS_TABLE)} atk
              ON atk.killmail_id = todo.killmail_id
             AND atk.killmail_time = todo.killmail_time
            JOIN {qname(SUPER_SCHEMA, 'super_ship_types')} sst
              ON sst.ship_type_id = atk.ship_type_id
            WHERE atk.character_id IS NOT NULL
            ON CONFLICT (killmail_id, killmail_time, role, source_index)
            DO NOTHING;
            """,
            (ROLE_ATTACKER,),
        )
        rows = cur.rowcount

    conn.commit()
    logger.info("Attacker events inserted=%s", rows)
    return rows


def count_super_killmails_in_todo(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*)
            FROM (
                SELECT DISTINCT e.killmail_id, e.killmail_time
                FROM {qname(SUPER_SCHEMA, 'events')} e
                JOIN tmp_superintel_todo todo
                  ON todo.killmail_id = e.killmail_id
                 AND todo.killmail_time = e.killmail_time
            ) x
            """
        )
        return int(cur.fetchone()[0])


def upsert_super_pilots(conn) -> int:
    logger.info("Upserting super_pilots from superintel.events")

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {qname(SUPER_SCHEMA, 'super_pilots')} (
                character_id,
                last_seen_time,
                created_at,
                updated_at
            )
            SELECT DISTINCT ON (e.character_id)
                e.character_id,
                e.killmail_time AS last_seen_time,
                NOW() AS created_at,
                NOW() AS updated_at
            FROM {qname(SUPER_SCHEMA, 'events')} e
            ORDER BY e.character_id, e.killmail_time DESC, e.killmail_id DESC, e.source_index DESC
            ON CONFLICT (character_id)
            DO UPDATE SET
                last_seen_time = GREATEST({qname(SUPER_SCHEMA, 'super_pilots')}.last_seen_time, EXCLUDED.last_seen_time),
                updated_at = NOW();
            """
        )
        rows = cur.rowcount

    conn.commit()
    logger.info("super_pilots upsert affected=%s", rows)
    return rows


def upsert_pilot_last_activity(conn) -> int:
    logger.info("Upserting pilot_last_activity for known super pilots from normalized raw killmails")

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {qname(SUPER_SCHEMA, 'pilot_last_activity')} (
                character_id,
                last_killmail_id,
                last_killmail_time,
                last_role,
                corporation_id,
                alliance_id,
                ship_type_id,
                refreshed_at
            )
            WITH pilots AS (
                SELECT character_id
                FROM {qname(SUPER_SCHEMA, 'super_pilots')}
            ),
            activity AS (
                SELECT
                    km.victim_character_id AS character_id,
                    km.killmail_id,
                    km.killmail_time,
                    %s::TEXT AS role,
                    km.victim_corporation_id AS corporation_id,
                    km.victim_alliance_id AS alliance_id,
                    km.victim_ship_type_id AS ship_type_id,
                    -1 AS source_index
                FROM {qname(RAW_SCHEMA, RAW_KILLMAILS_TABLE)} km
                JOIN pilots p
                  ON p.character_id = km.victim_character_id

                UNION ALL

                SELECT
                    atk.character_id,
                    atk.killmail_id,
                    atk.killmail_time,
                    %s::TEXT AS role,
                    atk.corporation_id,
                    atk.alliance_id,
                    atk.ship_type_id,
                    atk.attacker_index AS source_index
                FROM {qname(RAW_SCHEMA, RAW_ATTACKERS_TABLE)} atk
                JOIN pilots p
                  ON p.character_id = atk.character_id
            ),
            latest AS (
                SELECT DISTINCT ON (character_id)
                    character_id,
                    killmail_id,
                    killmail_time,
                    role,
                    corporation_id,
                    alliance_id,
                    ship_type_id
                FROM activity
                ORDER BY character_id, killmail_time DESC, killmail_id DESC, source_index DESC
            )
            SELECT
                character_id,
                killmail_id,
                killmail_time,
                role,
                corporation_id,
                alliance_id,
                ship_type_id,
                NOW()
            FROM latest
            ON CONFLICT (character_id)
            DO UPDATE SET
                last_killmail_id = EXCLUDED.last_killmail_id,
                last_killmail_time = EXCLUDED.last_killmail_time,
                last_role = EXCLUDED.last_role,
                corporation_id = EXCLUDED.corporation_id,
                alliance_id = EXCLUDED.alliance_id,
                ship_type_id = EXCLUDED.ship_type_id,
                refreshed_at = NOW();
            """,
            (ROLE_VICTIM, ROLE_ATTACKER),
        )
        rows = cur.rowcount

    conn.commit()
    logger.info("pilot_last_activity upsert affected=%s", rows)
    return rows


def upsert_ship_sessions(conn) -> int:
    logger.info(
        "Building/upserting ship_sessions with %sh victim safety window",
        SESSION_SAFETY_HOURS,
    )

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {qname(SUPER_SCHEMA, 'ship_sessions')} (
                character_id,
                ship_type_id,
                session_id,
                first_event_time,
                last_event_time,
                death_time,
                is_alive,
                event_count,
                updated_at
            )
            WITH ordered_all AS (
                SELECT
                    e.character_id,
                    e.ship_type_id,
                    e.killmail_id,
                    e.killmail_time,
                    e.role,
                    e.source_index,
                    MAX(CASE WHEN e.role = %s THEN e.killmail_time END) OVER (
                        PARTITION BY e.character_id, e.ship_type_id
                        ORDER BY e.killmail_time, e.killmail_id, CASE WHEN e.role = %s THEN 0 ELSE 1 END, e.source_index
                        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                    ) AS previous_victim_time
                FROM {qname(SUPER_SCHEMA, 'events')} e
            ),
            filtered AS (
                SELECT *
                FROM ordered_all
                WHERE NOT (
                    role = %s
                    AND previous_victim_time IS NOT NULL
                    AND killmail_time <= previous_victim_time + (%s::TEXT || ' hours')::INTERVAL
                )
            ),
            tagged AS (
                SELECT
                    f.*,
                    CASE
                        WHEN LAG(role) OVER (
                            PARTITION BY character_id, ship_type_id
                            ORDER BY killmail_time, killmail_id, CASE WHEN role = %s THEN 0 ELSE 1 END, source_index
                        ) IS NULL THEN 1
                        WHEN LAG(role) OVER (
                            PARTITION BY character_id, ship_type_id
                            ORDER BY killmail_time, killmail_id, CASE WHEN role = %s THEN 0 ELSE 1 END, source_index
                        ) = %s THEN 1
                        ELSE 0
                    END AS new_session
                FROM filtered f
            ),
            sessionized AS (
                SELECT
                    t.*,
                    SUM(new_session) OVER (
                        PARTITION BY character_id, ship_type_id
                        ORDER BY killmail_time, killmail_id, CASE WHEN role = %s THEN 0 ELSE 1 END, source_index
                        ROWS UNBOUNDED PRECEDING
                    ) AS session_id
                FROM tagged t
            ),
            aggregated AS (
                SELECT
                    character_id,
                    ship_type_id,
                    session_id,
                    MIN(killmail_time) AS first_event_time,
                    MAX(killmail_time) AS last_event_time,
                    MAX(killmail_time) FILTER (WHERE role = %s) AS death_time,
                    COUNT(*)::BIGINT AS event_count
                FROM sessionized
                GROUP BY character_id, ship_type_id, session_id
            )
            SELECT
                character_id,
                ship_type_id,
                session_id,
                first_event_time,
                last_event_time,
                death_time,
                (death_time IS NULL) AS is_alive,
                event_count,
                NOW()
            FROM aggregated
            ON CONFLICT (character_id, ship_type_id, session_id)
            DO UPDATE SET
                first_event_time = EXCLUDED.first_event_time,
                last_event_time = EXCLUDED.last_event_time,
                death_time = EXCLUDED.death_time,
                is_alive = EXCLUDED.is_alive,
                event_count = EXCLUDED.event_count,
                updated_at = NOW();
            """,
            (
                ROLE_VICTIM,
                ROLE_ATTACKER,
                ROLE_ATTACKER,
                str(SESSION_SAFETY_HOURS),
                ROLE_ATTACKER,
                ROLE_ATTACKER,
                ROLE_VICTIM,
                ROLE_ATTACKER,
                ROLE_VICTIM,
            ),
        )
        rows = cur.rowcount

    conn.commit()
    logger.info("ship_sessions upsert affected=%s", rows)
    return rows


def get_todo_checkpoint(conn) -> tuple[int | None, datetime | None]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT killmail_id, killmail_time
            FROM tmp_superintel_todo
            ORDER BY killmail_time DESC, killmail_id DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
    if row is None:
        return None, None
    return int(row[0]), row[1]


def update_checkpoint(conn, killmail_id: int | None, killmail_time: datetime | None) -> None:
    if killmail_id is None or killmail_time is None:
        return

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {qname(SUPER_SCHEMA, 'build_state')} (
                pipeline_name,
                last_killmail_id,
                last_killmail_time,
                last_success_at,
                updated_at
            )
            VALUES (%s, %s, %s, NOW(), NOW())
            ON CONFLICT (pipeline_name)
            DO UPDATE SET
                last_killmail_id = CASE
                    WHEN {qname(SUPER_SCHEMA, 'build_state')}.last_killmail_time IS NULL
                      OR EXCLUDED.last_killmail_time > {qname(SUPER_SCHEMA, 'build_state')}.last_killmail_time
                      OR (
                          EXCLUDED.last_killmail_time = {qname(SUPER_SCHEMA, 'build_state')}.last_killmail_time
                          AND EXCLUDED.last_killmail_id > {qname(SUPER_SCHEMA, 'build_state')}.last_killmail_id
                      )
                    THEN EXCLUDED.last_killmail_id
                    ELSE {qname(SUPER_SCHEMA, 'build_state')}.last_killmail_id
                END,
                last_killmail_time = GREATEST({qname(SUPER_SCHEMA, 'build_state')}.last_killmail_time, EXCLUDED.last_killmail_time),
                last_success_at = EXCLUDED.last_success_at,
                updated_at = NOW()
            """,
            (PIPELINE_NAME, killmail_id, killmail_time),
        )

    conn.commit()
    logger.info("Checkpoint updated candidate killmail_time=%s killmail_id=%s", killmail_time, killmail_id)


# ============================================================
# BUILD
# ============================================================


def build(args) -> None:
    run_id = str(uuid.uuid4())
    mode = resolve_mode(args)
    stats = Stats()

    logger.info("Starting build_superintel_events run_id=%s mode=%s", run_id, mode.name)
    logger.info("Log file: %s", LOG_FILE)
    logger.info("NON-DESTRUCTIVE MODE: no business TRUNCATE/DELETE/DROP will be executed")

    conn = db_connect()
    try:
        ensure_superintel_tables(conn)
        fail_if_obsolete_columns_exist(conn)
        start_run(conn, run_id, mode)

        ensure_required_raw_tables(conn)
        ensure_required_sde_tables(conn)

        stats.refreshed_super_ship_types = refresh_super_ship_types_from_sde(conn)
        stats.scanned_killmails = create_todo_table(conn, mode, args.batch_size)

        if stats.scanned_killmails > 0:
            stats.inserted_victim_events = insert_victim_events(conn)
            stats.inserted_attacker_events = insert_attacker_events(conn)
            stats.inserted_events_total = stats.inserted_victim_events + stats.inserted_attacker_events
            stats.super_killmails = count_super_killmails_in_todo(conn)

            ck_id, ck_time = get_todo_checkpoint(conn)
            stats.checkpoint_killmail_id = ck_id
            stats.checkpoint_killmail_time = ck_time
        else:
            logger.info("No raw killmails to process for this mode")

        stats.upserted_super_pilots = upsert_super_pilots(conn)
        stats.upserted_last_activity = upsert_pilot_last_activity(conn)
        stats.upserted_ship_sessions = upsert_ship_sessions(conn)

        if mode.name in ("incremental", "rebuild"):
            update_checkpoint(conn, stats.checkpoint_killmail_id, stats.checkpoint_killmail_time)

        finish_run(conn, run_id, "success", stats)

        logger.info(
            "SUCCESS run_id=%s scanned=%s inserted_events=%s super_killmails=%s super_pilots_affected=%s last_activity_affected=%s ship_sessions_affected=%s",
            run_id,
            stats.scanned_killmails,
            stats.inserted_events_total,
            stats.super_killmails,
            stats.upserted_super_pilots,
            stats.upserted_last_activity,
            stats.upserted_ship_sessions,
        )

    except Exception as exc:
        conn.rollback()
        logger.exception("FAILED run_id=%s error=%s", run_id, exc)
        try:
            finish_run(conn, run_id, "failed", stats, str(exc))
        except Exception:
            logger.exception("Could not write failed run status")
        raise
    finally:
        conn.close()


# ============================================================
# CLI
# ============================================================


def main() -> None:
    configure_logging()

    parser = argparse.ArgumentParser(description="Build superintel tables from normalized rawkm killmails.")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Scan all rawkm.killmails. Non destructive: no truncate/delete/drop.",
    )
    parser.add_argument(
        "--from",
        dest="from_date",
        type=parse_date,
        help="Process from YYYY-MM-DD inclusive. Non destructive.",
    )
    parser.add_argument(
        "--to",
        dest="to_date",
        type=parse_date,
        help="Process to YYYY-MM-DD inclusive. Non destructive.",
    )
    parser.add_argument(
        "--months",
        type=int,
        help="Process approximately last N months. Non destructive.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Incremental batch size. Default: {DEFAULT_BATCH_SIZE}.",
    )

    args = parser.parse_args()
    if args.batch_size <= 0:
        raise RuntimeError("--batch-size must be greater than 0")
    build(args)


if __name__ == "__main__":
    main()
