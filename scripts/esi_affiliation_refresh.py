#!/usr/bin/env python3
"""
esi_affiliation_refresh.py

EVEOSINT — pipeline ESI characters / affiliations / corpos / alliances.

Version TABLE-FIRST.

Règle forte :
  - Les gros worksets métier ne vivent pas en liste Python.
  - Les worksets vivent en tables SQL persistantes dans entities.*.
  - Python ne prend que de petits lots transitoires pour appeler ESI.
  - Le run est auditable et reprenable conceptuellement via run_id/status.
  - Rien dans superintel sauf lecture source.
  - entities.* = métier / référentiels / work tables persistantes.
  - esi.* = cache + logs techniques.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values
import requests


# ============================================================
# CONFIG
# ============================================================

CONFIG_PATH = Path.home() / "eveosint" / "config" / "db.json"
BASE_DIR = Path.home() / "eveosint"
LOG_DIR = BASE_DIR / "data" / "logs"
LOG_FILE = LOG_DIR / "esi_affiliation_refresh.log"

ENTITIES_SCHEMA = "entities"
ESI_SCHEMA = "esi"

ESI_BASE = "https://esi.evetech.net/latest"
DATASOURCE = "tranquility"

ESI_USER_AGENT = (
    "EveOsint/1.0.0 "
    "(source: https://github.com/DictateurImperator/EVEOSINT; "
    "discord: 206798315935760386 / old name DictateurImperator#0447; "
    "eve-character: Dictateur Imperator)"
)

ESI_COMPATIBILITY_DATE = "2026-05-07"

DEFAULT_SOURCE_TABLE = "superintel.super_pilots"
DEFAULT_SOURCE_COLUMN = "character_id"
DEFAULT_WORKERS = 4
DEFAULT_BATCH_SIZE = 999
DEFAULT_PROCESS_BATCH_SIZE = 2000

# Hard safety below CCP/requested limit. 280/min leaves margin under 300/min.
DEFAULT_ESI_MAX_CALLS_PER_MINUTE = 280

# Internal synthetic corporation used for terminal affiliation of deleted characters.
# The corporation row itself is created/backfilled once via SQL, not by this script.
DELETED_CORPORATION_ID = -1
DELETED_HISTORY_RECORD_ID = -1

REQUEST_TIMEOUT = (3.05, 30)
MAX_ATTEMPTS = 6

BACKOFF_START_SECONDS = 5.0
BACKOFF_MAX_SECONDS = 900.0
RATE_LIMIT_DEFAULT_SLEEP_SECONDS = 900.0

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
class HttpResult:
    status_code: int
    payload: Any | None
    etag: str | None
    expires_at: datetime | None
    last_modified: datetime | None
    error_limit_remain: int | None
    error_limit_reset: int | None


@dataclass
class EndpointCache:
    endpoint: str
    etag: str | None
    fetched_at: datetime | None
    expires_at: datetime | None
    last_status: int | None


@dataclass
class Stats:
    source_ids: int = 0

    public_character_skipped: int = 0
    public_character_loaded: int = 0
    public_character_deleted: int = 0
    public_character_failed: int = 0

    without_history: int = 0
    with_history: int = 0
    initial_character_history_loaded: int = 0
    initial_character_history_failed: int = 0

    affiliation_batches: int = 0
    affiliation_304: int = 0
    affiliation_200: int = 0
    affiliation_failed: int = 0
    affiliation_rows: int = 0

    queued_character_history: int = 0
    refreshed_character_history: int = 0
    refreshed_character_history_failed: int = 0

    unknown_corporations: int = 0
    public_corporations_loaded: int = 0
    public_corporations_deleted: int = 0
    corporation_history_loaded: int = 0
    corporation_history_failed: int = 0

    queued_corporation_history: int = 0
    refreshed_corporation_history: int = 0
    refreshed_corporation_history_failed: int = 0

    unknown_alliances: int = 0
    public_alliances_loaded: int = 0
    public_alliances_deleted: int = 0
    public_alliances_failed: int = 0


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


def parse_table_ref(value: str) -> tuple[str, str]:
    parts = value.split(".")
    if len(parts) != 2:
        raise RuntimeError("Table reference must be schema.table")
    schema, table = parts
    if not IDENT_RE.match(schema) or not IDENT_RE.match(table):
        raise RuntimeError(f"Invalid table reference: {value}")
    return schema, table


def validate_identifier(value: str, label: str) -> str:
    if not IDENT_RE.match(value):
        raise RuntimeError(f"Invalid {label}: {value}")
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


def ensure_source(conn, source_table: str, source_column: str) -> tuple[str, str, str]:
    schema, table = parse_table_ref(source_table)
    column = validate_identifier(source_column, "source column")

    if not table_exists(conn, schema, table):
        raise RuntimeError(f"Missing source table {schema}.{table}")
    if not column_exists(conn, schema, table, column):
        raise RuntimeError(f"Missing source column {schema}.{table}.{column}")

    return schema, table, column


def ensure_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(ENTITIES_SCHEMA)))
        cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(ESI_SCHEMA)))

        # -------------------------
        # Business tables
        # -------------------------

        cur.execute(sql.SQL("""
        CREATE TABLE IF NOT EXISTS {}.characters (
            character_id BIGINT PRIMARY KEY,
            name TEXT,
            birthday TIMESTAMPTZ,
            gender TEXT,
            race_id BIGINT,
            bloodline_id BIGINT,
            is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
            fetched_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """).format(sql.Identifier(ENTITIES_SCHEMA)))

        cur.execute(sql.SQL("""
        CREATE TABLE IF NOT EXISTS {}.character_corporation_history (
            character_id BIGINT NOT NULL,
            record_id BIGINT NOT NULL,
            corporation_id BIGINT NOT NULL,
            start_date TIMESTAMPTZ NOT NULL,
            end_date TIMESTAMPTZ,
            is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
            fetched_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (character_id, record_id)
        );
        """).format(sql.Identifier(ENTITIES_SCHEMA)))

        cur.execute(sql.SQL("""
        CREATE TABLE IF NOT EXISTS {}.character_current_affiliation (
            character_id BIGINT PRIMARY KEY,
            corporation_id BIGINT,
            alliance_id BIGINT,
            source_start_date TIMESTAMPTZ,
            source_history_fetched_at TIMESTAMPTZ,
            affiliation_checked_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """).format(sql.Identifier(ENTITIES_SCHEMA)))

        cur.execute(sql.SQL("""
        CREATE TABLE IF NOT EXISTS {}.corporations (
            corporation_id BIGINT PRIMARY KEY,
            name TEXT,
            ticker TEXT,
            date_founded TIMESTAMPTZ,
            is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
            fetched_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """).format(sql.Identifier(ENTITIES_SCHEMA)))

        cur.execute(sql.SQL("""
        CREATE TABLE IF NOT EXISTS {}.corporation_alliance_history (
            corporation_id BIGINT NOT NULL,
            record_id BIGINT NOT NULL,
            alliance_id BIGINT,
            start_date TIMESTAMPTZ NOT NULL,
            end_date TIMESTAMPTZ,
            is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
            fetched_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (corporation_id, record_id)
        );
        """).format(sql.Identifier(ENTITIES_SCHEMA)))

        cur.execute(sql.SQL("""
        CREATE TABLE IF NOT EXISTS {}.corporation_current_affiliation (
            corporation_id BIGINT PRIMARY KEY,
            alliance_id BIGINT,
            source_start_date TIMESTAMPTZ,
            source_history_fetched_at TIMESTAMPTZ,
            affiliation_checked_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """).format(sql.Identifier(ENTITIES_SCHEMA)))

        cur.execute(sql.SQL("""
        CREATE TABLE IF NOT EXISTS {}.alliances (
            alliance_id BIGINT PRIMARY KEY,
            name TEXT,
            ticker TEXT,
            date_founded TIMESTAMPTZ,
            is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
            fetched_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """).format(sql.Identifier(ENTITIES_SCHEMA)))

        # Existing queue tables kept for compatibility.
        cur.execute(sql.SQL("""
        CREATE TABLE IF NOT EXISTS {}.character_history_refresh_queue (
            character_id BIGINT PRIMARY KEY,
            reason TEXT NOT NULL,
            detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            known_corporation_id BIGINT,
            current_corporation_id BIGINT,
            current_alliance_id BIGINT,
            processed_at TIMESTAMPTZ
        );
        """).format(sql.Identifier(ENTITIES_SCHEMA)))

        cur.execute(sql.SQL("""
        CREATE TABLE IF NOT EXISTS {}.corporation_alliance_history_refresh_queue (
            corporation_id BIGINT PRIMARY KEY,
            reason TEXT NOT NULL,
            detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            known_alliance_id BIGINT,
            current_alliance_id BIGINT,
            processed_at TIMESTAMPTZ
        );
        """).format(sql.Identifier(ENTITIES_SCHEMA)))

        # -------------------------
        # Persistent work tables
        # -------------------------

        cur.execute(sql.SQL("""
        CREATE TABLE IF NOT EXISTS {}.esi_affiliation_run (
            run_id UUID PRIMARY KEY,
            source_table TEXT NOT NULL,
            source_column TEXT NOT NULL,
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at TIMESTAMPTZ,
            status TEXT NOT NULL DEFAULT 'running',
            error TEXT
        );
        """).format(sql.Identifier(ENTITIES_SCHEMA)))

        cur.execute(sql.SQL("""
        CREATE TABLE IF NOT EXISTS {}.esi_affiliation_character_work (
            run_id UUID NOT NULL,
            character_id BIGINT NOT NULL,
            has_history BOOLEAN,
            affiliation_checked_at TIMESTAMPTZ,
            source_history_fetched_at TIMESTAMPTZ,

            public_status TEXT NOT NULL DEFAULT 'pending',
            public_http_status INTEGER,
            public_attempts INTEGER NOT NULL DEFAULT 0,
            public_error TEXT,
            public_processed_at TIMESTAMPTZ,

            initial_history_status TEXT NOT NULL DEFAULT 'not_applicable',
            initial_history_http_status INTEGER,
            initial_history_attempts INTEGER NOT NULL DEFAULT 0,
            initial_history_error TEXT,
            initial_history_processed_at TIMESTAMPTZ,

            affiliation_batch_no INTEGER,
            affiliation_status TEXT NOT NULL DEFAULT 'not_applicable',
            affiliation_http_status INTEGER,
            affiliation_error TEXT,
            affiliation_processed_at TIMESTAMPTZ,

            history_refresh_status TEXT NOT NULL DEFAULT 'not_applicable',
            history_refresh_http_status INTEGER,
            history_refresh_attempts INTEGER NOT NULL DEFAULT 0,
            history_refresh_error TEXT,
            history_refresh_processed_at TIMESTAMPTZ,

            known_corporation_id BIGINT,
            current_corporation_id BIGINT,
            current_alliance_id BIGINT,

            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (run_id, character_id)
        );
        """).format(sql.Identifier(ENTITIES_SCHEMA)))

        cur.execute(sql.SQL("""
        CREATE TABLE IF NOT EXISTS {}.esi_affiliation_result_work (
            run_id UUID NOT NULL,
            character_id BIGINT NOT NULL,
            corporation_id BIGINT NOT NULL,
            alliance_id BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (run_id, character_id)
        );
        """).format(sql.Identifier(ENTITIES_SCHEMA)))

        cur.execute(sql.SQL("""
        CREATE TABLE IF NOT EXISTS {}.esi_affiliation_corporation_work (
            run_id UUID NOT NULL,
            corporation_id BIGINT NOT NULL,
            alliance_id BIGINT,

            public_status TEXT NOT NULL DEFAULT 'pending',
            public_http_status INTEGER,
            public_attempts INTEGER NOT NULL DEFAULT 0,
            public_error TEXT,
            public_processed_at TIMESTAMPTZ,

            initial_history_status TEXT NOT NULL DEFAULT 'pending',
            initial_history_http_status INTEGER,
            initial_history_attempts INTEGER NOT NULL DEFAULT 0,
            initial_history_error TEXT,
            initial_history_processed_at TIMESTAMPTZ,

            refresh_status TEXT NOT NULL DEFAULT 'not_applicable',
            refresh_http_status INTEGER,
            refresh_attempts INTEGER NOT NULL DEFAULT 0,
            refresh_error TEXT,
            refresh_processed_at TIMESTAMPTZ,

            known_alliance_id BIGINT,
            current_alliance_id BIGINT,

            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (run_id, corporation_id)
        );
        """).format(sql.Identifier(ENTITIES_SCHEMA)))

        cur.execute(sql.SQL("""
        CREATE TABLE IF NOT EXISTS {}.esi_affiliation_alliance_work (
            run_id UUID NOT NULL,
            alliance_id BIGINT NOT NULL,

            public_status TEXT NOT NULL DEFAULT 'pending',
            public_http_status INTEGER,
            public_attempts INTEGER NOT NULL DEFAULT 0,
            public_error TEXT,
            public_processed_at TIMESTAMPTZ,

            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (run_id, alliance_id)
        );
        """).format(sql.Identifier(ENTITIES_SCHEMA)))

        # -------------------------
        # ESI technical tables
        # -------------------------

        cur.execute(sql.SQL("""
        CREATE TABLE IF NOT EXISTS {}.endpoint_cache (
            endpoint TEXT PRIMARY KEY,
            entity_type TEXT NOT NULL,
            entity_id BIGINT,
            etag TEXT,
            fetched_at TIMESTAMPTZ,
            expires_at TIMESTAMPTZ,
            last_status INTEGER,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """).format(sql.Identifier(ESI_SCHEMA)))

        cur.execute(sql.SQL("""
        CREATE TABLE IF NOT EXISTS {}.call_log (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            run_id UUID NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id BIGINT,
            endpoint TEXT NOT NULL,
            url TEXT NOT NULL,
            method TEXT NOT NULL,
            called_at TIMESTAMPTZ NOT NULL,
            finished_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            http_status INTEGER,
            etag TEXT,
            expires_at TIMESTAMPTZ,
            error_limit_remain INTEGER,
            error_limit_reset INTEGER,
            compatibility_date TEXT NOT NULL,
            user_agent TEXT NOT NULL,
            error TEXT
        );
        """).format(sql.Identifier(ESI_SCHEMA)))

        # -------------------------
        # Indexes
        # -------------------------

        cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS characters_deleted_idx ON {}.characters (is_deleted);").format(sql.Identifier(ENTITIES_SCHEMA)))
        cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS char_hist_char_start_idx ON {}.character_corporation_history (character_id, start_date);").format(sql.Identifier(ENTITIES_SCHEMA)))
        cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS corp_hist_corp_start_idx ON {}.corporation_alliance_history (corporation_id, start_date);").format(sql.Identifier(ENTITIES_SCHEMA)))
        cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS esi_cache_entity_idx ON {}.endpoint_cache (entity_type, entity_id);").format(sql.Identifier(ESI_SCHEMA)))
        cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS esi_call_log_entity_idx ON {}.call_log (entity_type, entity_id);").format(sql.Identifier(ESI_SCHEMA)))

        cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS esi_aff_char_public_status_idx ON {}.esi_affiliation_character_work (run_id, public_status, character_id);").format(sql.Identifier(ENTITIES_SCHEMA)))
        cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS esi_aff_char_initial_status_idx ON {}.esi_affiliation_character_work (run_id, initial_history_status, character_id);").format(sql.Identifier(ENTITIES_SCHEMA)))
        cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS esi_aff_char_aff_status_idx ON {}.esi_affiliation_character_work (run_id, affiliation_status, affiliation_checked_at, source_history_fetched_at, character_id);").format(sql.Identifier(ENTITIES_SCHEMA)))
        cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS esi_aff_char_refresh_status_idx ON {}.esi_affiliation_character_work (run_id, history_refresh_status, character_id);").format(sql.Identifier(ENTITIES_SCHEMA)))
        cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS esi_aff_result_corp_idx ON {}.esi_affiliation_result_work (run_id, corporation_id);").format(sql.Identifier(ENTITIES_SCHEMA)))
        cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS esi_aff_corp_public_status_idx ON {}.esi_affiliation_corporation_work (run_id, public_status, corporation_id);").format(sql.Identifier(ENTITIES_SCHEMA)))
        cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS esi_aff_corp_initial_status_idx ON {}.esi_affiliation_corporation_work (run_id, initial_history_status, corporation_id);").format(sql.Identifier(ENTITIES_SCHEMA)))
        cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS esi_aff_corp_refresh_status_idx ON {}.esi_affiliation_corporation_work (run_id, refresh_status, corporation_id);").format(sql.Identifier(ENTITIES_SCHEMA)))
        cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS esi_aff_alliance_public_status_idx ON {}.esi_affiliation_alliance_work (run_id, public_status, alliance_id);").format(sql.Identifier(ENTITIES_SCHEMA)))

    conn.commit()


# ============================================================
# CACHE / LOG
# ============================================================


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc)
    except Exception:
        return None


def parse_int_header(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def http_date(value: datetime) -> str:
    return format_datetime(value.astimezone(timezone.utc), usegmt=True)


def get_cache(conn, endpoint: str) -> EndpointCache | None:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            SELECT endpoint, etag, fetched_at, expires_at, last_status
            FROM {}.endpoint_cache
            WHERE endpoint=%s
            """).format(sql.Identifier(ESI_SCHEMA)),
            (endpoint,),
        )
        row = cur.fetchone()

    if row is None:
        return None

    return EndpointCache(
        endpoint=row[0],
        etag=row[1],
        fetched_at=row[2],
        expires_at=row[3],
        last_status=row[4],
    )


def cache_is_fresh(cache: EndpointCache | None) -> bool:
    return cache is not None and cache.expires_at is not None and utc_now() < cache.expires_at


def upsert_cache(
    conn,
    *,
    endpoint: str,
    entity_type: str,
    entity_id: int | None,
    etag: str | None,
    fetched_at: datetime | None,
    expires_at: datetime | None,
    status: int | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            INSERT INTO {}.endpoint_cache (
                endpoint, entity_type, entity_id, etag, fetched_at, expires_at, last_status, updated_at
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (endpoint)
            DO UPDATE SET
                entity_type = EXCLUDED.entity_type,
                entity_id = EXCLUDED.entity_id,
                etag = EXCLUDED.etag,
                fetched_at = EXCLUDED.fetched_at,
                expires_at = EXCLUDED.expires_at,
                last_status = EXCLUDED.last_status,
                updated_at = NOW()
            """).format(sql.Identifier(ESI_SCHEMA)),
            (endpoint, entity_type, entity_id, etag, fetched_at, expires_at, status),
        )
    conn.commit()


def delete_cache(conn, endpoint: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("DELETE FROM {}.endpoint_cache WHERE endpoint=%s").format(sql.Identifier(ESI_SCHEMA)),
            (endpoint,),
        )
    conn.commit()


def insert_call_log(
    conn,
    *,
    run_id: str,
    entity_type: str,
    entity_id: int | None,
    endpoint: str,
    url: str,
    method: str,
    called_at: datetime,
    http_status: int | None,
    etag: str | None,
    expires_at: datetime | None,
    error_limit_remain: int | None,
    error_limit_reset: int | None,
    error: str | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            INSERT INTO {}.call_log (
                run_id, entity_type, entity_id, endpoint, url, method,
                called_at, finished_at, http_status, etag, expires_at,
                error_limit_remain, error_limit_reset,
                compatibility_date, user_agent, error
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,NOW(),%s,%s,%s,%s,%s,%s,%s,%s)
            """).format(sql.Identifier(ESI_SCHEMA)),
            (
                run_id, entity_type, entity_id, endpoint, url, method,
                called_at, http_status, etag, expires_at,
                error_limit_remain, error_limit_reset,
                ESI_COMPATIBILITY_DATE, ESI_USER_AGENT, error,
            ),
        )
    conn.commit()


# ============================================================
# HTTP ESI
# ============================================================


class GlobalRateLimiter:
    """Process-wide ESI call limiter shared by all worker threads."""

    def __init__(self, max_calls_per_minute: int) -> None:
        if max_calls_per_minute <= 0:
            raise RuntimeError("max_calls_per_minute must be greater than 0")
        self.max_calls_per_minute = max_calls_per_minute
        self.min_interval_seconds = 60.0 / float(max_calls_per_minute)
        self._lock = threading.Lock()
        self._next_allowed_at = time.monotonic()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            allowed_at = max(now, self._next_allowed_at)
            self._next_allowed_at = allowed_at + self.min_interval_seconds
            sleep_seconds = allowed_at - now

        if sleep_seconds > 0:
            time.sleep(sleep_seconds)


ESI_RATE_LIMITER = GlobalRateLimiter(DEFAULT_ESI_MAX_CALLS_PER_MINUTE)


def configure_esi_rate_limiter(max_calls_per_minute: int) -> None:
    global ESI_RATE_LIMITER
    ESI_RATE_LIMITER = GlobalRateLimiter(max_calls_per_minute)
    logger.info(
        "ESI global rate limit enabled: max_calls_per_minute=%s min_interval=%.3fs",
        max_calls_per_minute,
        ESI_RATE_LIMITER.min_interval_seconds,
    )


def build_headers(etag: str | None = None, if_modified_since: datetime | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": ESI_USER_AGENT,
        "Accept": "application/json",
        "X-Compatibility-Date": ESI_COMPATIBILITY_DATE,
    }
    if etag:
        headers["If-None-Match"] = etag
    if if_modified_since is not None:
        headers["If-Modified-Since"] = http_date(if_modified_since)
    return headers


def throttle_sleep(reason: str, attempt: int, reset: int | None = None, retry_after: int | None = None) -> None:
    if reason in ("http_420", "http_429"):
        if retry_after is not None and retry_after > 0:
            seconds = float(retry_after)
        elif reset is not None and reset > 0:
            seconds = float(reset + 1)
        else:
            seconds = RATE_LIMIT_DEFAULT_SLEEP_SECONDS
    else:
        seconds = min(BACKOFF_START_SECONDS * (2 ** max(attempt - 1, 0)), BACKOFF_MAX_SECONDS)

    logger.warning(
        "ESI throttle/backoff reason=%s attempt=%s sleeping=%.1fs reset=%s retry_after=%s",
        reason, attempt, seconds, reset, retry_after,
    )
    time.sleep(seconds)


def esi_request(
    *,
    conn,
    session: requests.Session,
    run_id: str,
    method: str,
    endpoint: str,
    entity_type: str,
    entity_id: int | None,
    etag: str | None = None,
    if_modified_since: datetime | None = None,
    json_body: Any | None = None,
) -> HttpResult:
    url = f"{ESI_BASE}{endpoint}"
    params = {"datasource": DATASOURCE}

    for attempt in range(1, MAX_ATTEMPTS + 1):
        ESI_RATE_LIMITER.wait()
        called_at = utc_now()
        started_monotonic = time.monotonic()
        headers = build_headers(etag=etag, if_modified_since=if_modified_since)

        try:
            response = session.request(
                method,
                url,
                params=params,
                headers=headers,
                json=json_body,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            elapsed_ms = int((time.monotonic() - started_monotonic) * 1000)
            logger.warning(
                "CALL_FAILED method=%s endpoint=%s entity_type=%s entity_id=%s elapsed_ms=%s error=%s",
                method, endpoint, entity_type, entity_id, elapsed_ms, type(exc).__name__,
            )
            insert_call_log(
                conn,
                run_id=run_id,
                entity_type=entity_type,
                entity_id=entity_id,
                endpoint=endpoint,
                url=url,
                method=method,
                called_at=called_at,
                http_status=None,
                etag=None,
                expires_at=None,
                error_limit_remain=None,
                error_limit_reset=None,
                error=type(exc).__name__,
            )
            throttle_sleep(type(exc).__name__, attempt)
            continue

        elapsed_ms = int((time.monotonic() - started_monotonic) * 1000)

        status = response.status_code
        new_etag = response.headers.get("ETag")
        expires_at = parse_http_date(response.headers.get("Expires"))
        last_modified = parse_http_date(response.headers.get("Last-Modified"))
        remain = parse_int_header(response.headers.get("X-ESI-Error-Limit-Remain"))
        reset = parse_int_header(response.headers.get("X-ESI-Error-Limit-Reset"))

        insert_call_log(
            conn,
            run_id=run_id,
            entity_type=entity_type,
            entity_id=entity_id,
            endpoint=endpoint,
            url=url,
            method=method,
            called_at=called_at,
            http_status=status,
            etag=new_etag,
            expires_at=expires_at,
            error_limit_remain=remain,
            error_limit_reset=reset,
            error=None,
        )

        logger.info(
            "CALL method=%s endpoint=%s entity_type=%s entity_id=%s status=%s elapsed_ms=%s remain=%s reset=%s",
            method, endpoint, entity_type, entity_id, status, elapsed_ms, remain, reset,
        )

        if status == 304:
            return HttpResult(status, None, new_etag or etag, expires_at, last_modified, remain, reset)

        if 200 <= status < 300:
            try:
                payload = response.json()
            except Exception as exc:
                raise RuntimeError(f"Invalid JSON endpoint={endpoint} status={status}") from exc
            return HttpResult(status, payload, new_etag, expires_at, last_modified, remain, reset)

        if status in (420, 429):
            retry_after = parse_int_header(response.headers.get("Retry-After"))
            throttle_sleep(f"http_{status}", attempt, reset=reset, retry_after=retry_after)
            continue

        if status in (500, 502, 503, 504):
            throttle_sleep(f"http_{status}", attempt, reset=reset)
            continue

        if 400 <= status < 500:
            try:
                payload = response.json()
            except Exception:
                payload = None
            return HttpResult(status, payload, new_etag, expires_at, last_modified, remain, reset)

        throttle_sleep(f"http_{status}", attempt, reset=reset)

    raise RuntimeError(f"Max attempts reached method={method} endpoint={endpoint}")


# ============================================================
# STATUS / CURRENT HELPERS
# ============================================================


def character_exists(conn, character_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT EXISTS (SELECT 1 FROM {}.characters WHERE character_id=%s)").format(sql.Identifier(ENTITIES_SCHEMA)),
            (character_id,),
        )
        return bool(cur.fetchone()[0])


def character_has_history(conn, character_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT EXISTS (SELECT 1 FROM {}.character_corporation_history WHERE character_id=%s)").format(sql.Identifier(ENTITIES_SCHEMA)),
            (character_id,),
        )
        return bool(cur.fetchone()[0])


def character_is_deleted(conn, character_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT is_deleted FROM {}.characters WHERE character_id=%s").format(sql.Identifier(ENTITIES_SCHEMA)),
            (character_id,),
        )
        row = cur.fetchone()
    return bool(row[0]) if row else False


def corporation_is_deleted(conn, corporation_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT is_deleted FROM {}.corporations WHERE corporation_id=%s").format(sql.Identifier(ENTITIES_SCHEMA)),
            (corporation_id,),
        )
        row = cur.fetchone()
    return bool(row[0]) if row else False


def corporation_exists(conn, corporation_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT EXISTS (SELECT 1 FROM {}.corporations WHERE corporation_id=%s)").format(sql.Identifier(ENTITIES_SCHEMA)),
            (corporation_id,),
        )
        return bool(cur.fetchone()[0])


def corporation_has_history(conn, corporation_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT EXISTS (SELECT 1 FROM {}.corporation_alliance_history WHERE corporation_id=%s)").format(sql.Identifier(ENTITIES_SCHEMA)),
            (corporation_id,),
        )
        return bool(cur.fetchone()[0])


def alliance_is_deleted(conn, alliance_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT is_deleted FROM {}.alliances WHERE alliance_id=%s").format(sql.Identifier(ENTITIES_SCHEMA)),
            (alliance_id,),
        )
        row = cur.fetchone()
    return bool(row[0]) if row else False


def alliance_exists(conn, alliance_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT EXISTS (SELECT 1 FROM {}.alliances WHERE alliance_id=%s)").format(sql.Identifier(ENTITIES_SCHEMA)),
            (alliance_id,),
        )
        return bool(cur.fetchone()[0])


def rebuild_character_current(conn, character_ids: list[int]) -> int:
    if not character_ids:
        return 0

    # Petite liste transitoire pour une requête SQL ciblée.
    # Le stockage de travail reste en table.
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            INSERT INTO {}.character_current_affiliation (
                character_id,
                corporation_id,
                alliance_id,
                source_start_date,
                source_history_fetched_at,
                affiliation_checked_at,
                updated_at
            )
            WITH latest AS (
                SELECT DISTINCT ON (h.character_id)
                    h.character_id,
                    h.corporation_id,
                    h.start_date,
                    h.fetched_at
                FROM {}.character_corporation_history h
                WHERE h.character_id = ANY(%s)
                  AND h.is_deleted = FALSE
                ORDER BY h.character_id, h.start_date DESC, h.record_id DESC
            )
            SELECT
                l.character_id,
                l.corporation_id,
                old.alliance_id,
                l.start_date,
                l.fetched_at,
                old.affiliation_checked_at,
                NOW()
            FROM latest l
            LEFT JOIN {}.character_current_affiliation old
              ON old.character_id = l.character_id
            ON CONFLICT (character_id)
            DO UPDATE SET
                corporation_id = EXCLUDED.corporation_id,
                alliance_id = EXCLUDED.alliance_id,
                source_start_date = EXCLUDED.source_start_date,
                source_history_fetched_at = EXCLUDED.source_history_fetched_at,
                updated_at = NOW()
            """).format(
                sql.Identifier(ENTITIES_SCHEMA),
                sql.Identifier(ENTITIES_SCHEMA),
                sql.Identifier(ENTITIES_SCHEMA),
            ),
            (character_ids,),
        )
        rows = cur.rowcount
    conn.commit()
    return rows


def rebuild_corporation_current(conn, corporation_ids: list[int]) -> int:
    if not corporation_ids:
        return 0

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            INSERT INTO {}.corporation_current_affiliation (
                corporation_id,
                alliance_id,
                source_start_date,
                source_history_fetched_at,
                affiliation_checked_at,
                updated_at
            )
            WITH latest AS (
                SELECT DISTINCT ON (h.corporation_id)
                    h.corporation_id,
                    h.alliance_id,
                    h.start_date,
                    h.fetched_at
                FROM {}.corporation_alliance_history h
                WHERE h.corporation_id = ANY(%s)
                  AND h.is_deleted = FALSE
                ORDER BY h.corporation_id, h.start_date DESC, h.record_id DESC
            )
            SELECT
                l.corporation_id,
                l.alliance_id,
                l.start_date,
                l.fetched_at,
                old.affiliation_checked_at,
                NOW()
            FROM latest l
            LEFT JOIN {}.corporation_current_affiliation old
              ON old.corporation_id = l.corporation_id
            ON CONFLICT (corporation_id)
            DO UPDATE SET
                alliance_id = EXCLUDED.alliance_id,
                source_start_date = EXCLUDED.source_start_date,
                source_history_fetched_at = EXCLUDED.source_history_fetched_at,
                updated_at = NOW()
            """).format(
                sql.Identifier(ENTITIES_SCHEMA),
                sql.Identifier(ENTITIES_SCHEMA),
                sql.Identifier(ENTITIES_SCHEMA),
            ),
            (corporation_ids,),
        )
        rows = cur.rowcount
    conn.commit()
    return rows


def mark_affiliation_checked(conn, character_ids: list[int]) -> None:
    if not character_ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            UPDATE {}.character_current_affiliation
            SET affiliation_checked_at = NOW(),
                updated_at = NOW()
            WHERE character_id = ANY(%s)
            """).format(sql.Identifier(ENTITIES_SCHEMA)),
            (character_ids,),
        )
    conn.commit()


# ============================================================
# WRITE ENTITIES
# ============================================================


def parse_esi_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def upsert_character(conn, character_id: int, payload: dict[str, Any], fetched_at: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            INSERT INTO {}.characters (
                character_id, name, birthday, gender, race_id, bloodline_id,
                is_deleted, fetched_at, updated_at
            )
            VALUES (%s,%s,%s,%s,%s,%s,FALSE,%s,NOW())
            ON CONFLICT (character_id)
            DO UPDATE SET
                name = EXCLUDED.name,
                birthday = EXCLUDED.birthday,
                gender = EXCLUDED.gender,
                race_id = EXCLUDED.race_id,
                bloodline_id = EXCLUDED.bloodline_id,
                is_deleted = FALSE,
                fetched_at = EXCLUDED.fetched_at,
                updated_at = NOW()
            """).format(sql.Identifier(ENTITIES_SCHEMA)),
            (
                character_id,
                payload.get("name"),
                parse_esi_datetime(payload.get("birthday")),
                payload.get("gender"),
                payload.get("race_id"),
                payload.get("bloodline_id"),
                fetched_at,
            ),
        )
    conn.commit()


def mark_character_deleted(conn, character_id: int, fetched_at: datetime) -> None:
    """
    Mark a character deleted and create its terminal synthetic affiliation.

    DELETED_CORPORATION_ID is an internal EVEOSINT entity. Its corporation row
    is deliberately managed by the one-shot SQL backfill, not created here.
    The synthetic history record is active (is_deleted=FALSE): the character is
    deleted, not the affiliation record itself.
    """
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            INSERT INTO {}.characters (
                character_id, name, birthday, gender, race_id, bloodline_id,
                is_deleted, fetched_at, updated_at
            )
            VALUES (%s,%s,NULL,NULL,NULL,NULL,TRUE,%s,NOW())
            ON CONFLICT (character_id)
            DO UPDATE SET
                name = EXCLUDED.name,
                birthday = NULL,
                gender = NULL,
                race_id = NULL,
                bloodline_id = NULL,
                is_deleted = TRUE,
                fetched_at = EXCLUDED.fetched_at,
                updated_at = NOW()
            """).format(sql.Identifier(ENTITIES_SCHEMA)),
            (character_id, f"DELETED_{character_id}", fetched_at),
        )

        # Keep the first deletion-detection timestamp if a synthetic terminal
        # record already exists. A deleted character must have exactly one
        # terminal transition to the internal Deleted corporation.
        cur.execute(
            sql.SQL("""
            INSERT INTO {}.character_corporation_history (
                character_id, record_id, corporation_id,
                start_date, end_date, is_deleted, fetched_at, updated_at
            )
            VALUES (%s,%s,%s,%s,NULL,FALSE,%s,NOW())
            ON CONFLICT (character_id, record_id) DO NOTHING
            """).format(sql.Identifier(ENTITIES_SCHEMA)),
            (
                character_id,
                DELETED_HISTORY_RECORD_ID,
                DELETED_CORPORATION_ID,
                fetched_at,
                fetched_at,
            ),
        )

        cur.execute(
            sql.SQL("""
            SELECT start_date, fetched_at
            FROM {}.character_corporation_history
            WHERE character_id=%s
              AND record_id=%s
              AND corporation_id=%s
            """).format(sql.Identifier(ENTITIES_SCHEMA)),
            (character_id, DELETED_HISTORY_RECORD_ID, DELETED_CORPORATION_ID),
        )
        terminal = cur.fetchone()
        if terminal is None:
            raise RuntimeError(f"Failed to create Deleted history affiliation character_id={character_id}")
        deleted_at, history_fetched_at = terminal

        # Close the last real corporation membership at the deletion timestamp.
        cur.execute(
            sql.SQL("""
            WITH previous AS (
                SELECT record_id
                FROM {}.character_corporation_history
                WHERE character_id=%s
                  AND record_id <> %s
                  AND corporation_id <> %s
                  AND start_date <= %s
                ORDER BY start_date DESC, record_id DESC
                LIMIT 1
            )
            UPDATE {}.character_corporation_history h
            SET end_date=%s,
                updated_at=NOW()
            FROM previous p
            WHERE h.character_id=%s
              AND h.record_id=p.record_id
              AND (h.end_date IS NULL OR h.end_date > %s)
            """).format(
                sql.Identifier(ENTITIES_SCHEMA),
                sql.Identifier(ENTITIES_SCHEMA),
            ),
            (
                character_id,
                DELETED_HISTORY_RECORD_ID,
                DELETED_CORPORATION_ID,
                deleted_at,
                deleted_at,
                character_id,
                deleted_at,
            ),
        )

        # HISTORY remains the source of truth: current affiliation is the latest
        # synthetic history row, with no alliance.
        cur.execute(
            sql.SQL("""
            INSERT INTO {}.character_current_affiliation (
                character_id, corporation_id, alliance_id,
                source_start_date, source_history_fetched_at,
                affiliation_checked_at, updated_at
            )
            VALUES (%s,%s,NULL,%s,%s,%s,NOW())
            ON CONFLICT (character_id)
            DO UPDATE SET
                corporation_id = EXCLUDED.corporation_id,
                alliance_id = NULL,
                source_start_date = EXCLUDED.source_start_date,
                source_history_fetched_at = EXCLUDED.source_history_fetched_at,
                affiliation_checked_at = EXCLUDED.affiliation_checked_at,
                updated_at = NOW()
            """).format(sql.Identifier(ENTITIES_SCHEMA)),
            (
                character_id,
                DELETED_CORPORATION_ID,
                deleted_at,
                history_fetched_at,
                deleted_at,
            ),
        )

        # Any old mismatch queue entry is terminally resolved.
        cur.execute(
            sql.SQL("""
            UPDATE {}.character_history_refresh_queue
            SET processed_at=NOW()
            WHERE character_id=%s
            """).format(sql.Identifier(ENTITIES_SCHEMA)),
            (character_id,),
        )

    conn.commit()


def upsert_character_history(conn, character_id: int, history: list[dict[str, Any]], fetched_at: datetime) -> int:
    if not isinstance(history, list):
        raise RuntimeError(f"Invalid character history payload for character_id={character_id}")

    rows = sorted(history, key=lambda r: parse_esi_datetime(r["start_date"]))

    affected = 0
    with conn.cursor() as cur:
        for i, row in enumerate(rows):
            if "record_id" not in row or "corporation_id" not in row or "start_date" not in row:
                raise RuntimeError(f"Missing field in character corporation history character_id={character_id}")

            start_dt = parse_esi_datetime(row["start_date"])
            end_dt = parse_esi_datetime(rows[i + 1]["start_date"]) if i + 1 < len(rows) else None

            cur.execute(
                sql.SQL("""
                INSERT INTO {}.character_corporation_history (
                    character_id, record_id, corporation_id,
                    start_date, end_date, is_deleted, fetched_at, updated_at
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT (character_id, record_id)
                DO UPDATE SET
                    corporation_id = EXCLUDED.corporation_id,
                    start_date = EXCLUDED.start_date,
                    end_date = EXCLUDED.end_date,
                    is_deleted = EXCLUDED.is_deleted,
                    fetched_at = EXCLUDED.fetched_at,
                    updated_at = NOW()
                """).format(sql.Identifier(ENTITIES_SCHEMA)),
                (
                    character_id,
                    int(row["record_id"]),
                    int(row["corporation_id"]),
                    start_dt,
                    end_dt,
                    bool(row.get("is_deleted", False)),
                    fetched_at,
                ),
            )
            affected += cur.rowcount

    conn.commit()

    rebuild_character_current(conn, [character_id])
    return affected


def upsert_corporation(conn, corporation_id: int, payload: dict[str, Any], fetched_at: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            INSERT INTO {}.corporations (
                corporation_id, name, ticker, date_founded,
                is_deleted, fetched_at, updated_at
            )
            VALUES (%s,%s,%s,%s,FALSE,%s,NOW())
            ON CONFLICT (corporation_id)
            DO UPDATE SET
                name = EXCLUDED.name,
                ticker = EXCLUDED.ticker,
                date_founded = EXCLUDED.date_founded,
                is_deleted = FALSE,
                fetched_at = EXCLUDED.fetched_at,
                updated_at = NOW()
            """).format(sql.Identifier(ENTITIES_SCHEMA)),
            (
                corporation_id,
                payload.get("name"),
                payload.get("ticker"),
                parse_esi_datetime(payload.get("date_founded")),
                fetched_at,
            ),
        )
    conn.commit()


def mark_corporation_deleted(conn, corporation_id: int, fetched_at: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            INSERT INTO {}.corporations (
                corporation_id, name, ticker, date_founded,
                is_deleted, fetched_at, updated_at
            )
            VALUES (%s,%s,NULL,NULL,TRUE,%s,NOW())
            ON CONFLICT (corporation_id)
            DO UPDATE SET
                name = EXCLUDED.name,
                ticker = NULL,
                date_founded = NULL,
                is_deleted = TRUE,
                fetched_at = EXCLUDED.fetched_at,
                updated_at = NOW()
            """).format(sql.Identifier(ENTITIES_SCHEMA)),
            (corporation_id, f"DELETED_{corporation_id}", fetched_at),
        )
    conn.commit()


def upsert_corporation_history(conn, corporation_id: int, history: list[dict[str, Any]], fetched_at: datetime) -> int:
    if not isinstance(history, list):
        raise RuntimeError(f"Invalid corporation alliance history payload corporation_id={corporation_id}")

    rows = sorted(history, key=lambda r: parse_esi_datetime(r["start_date"]))

    affected = 0
    with conn.cursor() as cur:
        for i, row in enumerate(rows):
            if "start_date" not in row:
                raise RuntimeError(f"Missing start_date in corporation alliance history corporation_id={corporation_id}")

            record_id = int(row.get("record_id", i))
            start_dt = parse_esi_datetime(row["start_date"])
            end_dt = parse_esi_datetime(rows[i + 1]["start_date"]) if i + 1 < len(rows) else None

            cur.execute(
                sql.SQL("""
                INSERT INTO {}.corporation_alliance_history (
                    corporation_id, record_id, alliance_id,
                    start_date, end_date, is_deleted, fetched_at, updated_at
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT (corporation_id, record_id)
                DO UPDATE SET
                    alliance_id = EXCLUDED.alliance_id,
                    start_date = EXCLUDED.start_date,
                    end_date = EXCLUDED.end_date,
                    is_deleted = EXCLUDED.is_deleted,
                    fetched_at = EXCLUDED.fetched_at,
                    updated_at = NOW()
                """).format(sql.Identifier(ENTITIES_SCHEMA)),
                (
                    corporation_id,
                    record_id,
                    row.get("alliance_id"),
                    start_dt,
                    end_dt,
                    bool(row.get("is_deleted", False)),
                    fetched_at,
                ),
            )
            affected += cur.rowcount

    conn.commit()

    rebuild_corporation_current(conn, [corporation_id])
    return affected


def upsert_alliance(conn, alliance_id: int, payload: dict[str, Any], fetched_at: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            INSERT INTO {}.alliances (
                alliance_id, name, ticker, date_founded,
                is_deleted, fetched_at, updated_at
            )
            VALUES (%s,%s,%s,%s,FALSE,%s,NOW())
            ON CONFLICT (alliance_id)
            DO UPDATE SET
                name = EXCLUDED.name,
                ticker = EXCLUDED.ticker,
                date_founded = EXCLUDED.date_founded,
                is_deleted = FALSE,
                fetched_at = EXCLUDED.fetched_at,
                updated_at = NOW()
            """).format(sql.Identifier(ENTITIES_SCHEMA)),
            (
                alliance_id,
                payload.get("name"),
                payload.get("ticker"),
                parse_esi_datetime(payload.get("date_founded")),
                fetched_at,
            ),
        )
    conn.commit()


def mark_alliance_deleted(conn, alliance_id: int, fetched_at: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            INSERT INTO {}.alliances (
                alliance_id, name, ticker, date_founded,
                is_deleted, fetched_at, updated_at
            )
            VALUES (%s,%s,NULL,NULL,TRUE,%s,NOW())
            ON CONFLICT (alliance_id)
            DO UPDATE SET
                name = EXCLUDED.name,
                ticker = NULL,
                date_founded = NULL,
                is_deleted = TRUE,
                fetched_at = EXCLUDED.fetched_at,
                updated_at = NOW()
            """).format(sql.Identifier(ENTITIES_SCHEMA)),
            (alliance_id, f"DELETED_{alliance_id}", fetched_at),
        )
    conn.commit()


# ============================================================
# ESI DOMAIN CALLS
# ============================================================


def is_deleted_character_response(result: HttpResult) -> bool:
    return (
        result.status_code == 404
        and isinstance(result.payload, dict)
        and result.payload.get("error") == "Character has been deleted!"
    )


def fetch_public_character(run_id: str, character_id: int, force: bool = False) -> str:
    conn = db_connect()
    session = requests.Session()
    try:
        exists = character_exists(conn, character_id)

        # Terminal state: once a character is known deleted, never query ESI for
        # that character again.
        if exists and character_is_deleted(conn, character_id):
            return "deleted_skip"

        if exists and not force:
            return "skipped"

        endpoint = f"/characters/{character_id}/"
        cache = get_cache(conn, endpoint)

        if not exists and cache_is_fresh(cache):
            logger.warning(
                "REPAIR stale public character cache without entity character_id=%s endpoint=%s",
                character_id, endpoint,
            )
            delete_cache(conn, endpoint)
            cache = None

        if force:
            logger.info(
                "FORCE public character verification after history refresh character_id=%s",
                character_id,
            )

        result = esi_request(
            conn=conn,
            session=session,
            run_id=run_id,
            method="GET",
            endpoint=endpoint,
            entity_type="character",
            entity_id=character_id,
            etag=None if force else (cache.etag if cache else None),
        )
        now = utc_now()
        upsert_cache(
            conn,
            endpoint=endpoint,
            entity_type="character",
            entity_id=character_id,
            etag=result.etag,
            fetched_at=now,
            expires_at=result.expires_at,
            status=result.status_code,
        )

        if result.status_code == 200:
            if not isinstance(result.payload, dict):
                raise RuntimeError(f"Invalid public character payload character_id={character_id}")
            upsert_character(conn, character_id, result.payload, now)
            return "loaded" if force or not exists else "skipped"

        if is_deleted_character_response(result) or result.status_code == 404:
            mark_character_deleted(conn, character_id, now)
            logger.info(
                "CHARACTER DELETED terminal affiliation character_id=%s corporation_id=%s",
                character_id,
                DELETED_CORPORATION_ID,
            )
            return "deleted"

        if result.status_code == 304:
            if force:
                raise RuntimeError(
                    f"Unexpected 304 on forced public character verification character_id={character_id}"
                )
            raise RuntimeError(f"304 public character but entity missing character_id={character_id}")

        raise RuntimeError(f"Unexpected public character status character_id={character_id} status={result.status_code} payload={result.payload}")

    finally:
        session.close()
        conn.close()


def fetch_character_history(run_id: str, character_id: int, force: bool = False) -> str:
    conn = db_connect()
    session = requests.Session()
    try:
        # Deleted is a terminal state. The synthetic Deleted history row is the
        # final affiliation and must never be refreshed from ESI again.
        if character_is_deleted(conn, character_id):
            logger.info(
                "SKIP character history for deleted character_id=%s",
                character_id,
            )
            return "deleted_skip"

        endpoint = f"/characters/{character_id}/corporationhistory/"
        cache = get_cache(conn, endpoint)

        if not character_has_history(conn, character_id) and cache_is_fresh(cache):
            logger.warning(
                "REPAIR stale character history cache without history character_id=%s endpoint=%s",
                character_id, endpoint,
            )
            delete_cache(conn, endpoint)
            cache = None

        # Normal refreshes keep using ESI cache/ETag.
        # A targeted affiliation mismatch must obtain a real history payload:
        # bypass the fresh-cache shortcut and do not send If-None-Match.
        if not force and cache_is_fresh(cache) and character_has_history(conn, character_id):
            return "skipped_cache"

        if force:
            logger.info(
                "FORCE character history refresh after affiliation mismatch character_id=%s",
                character_id,
            )

        result = esi_request(
            conn=conn,
            session=session,
            run_id=run_id,
            method="GET",
            endpoint=endpoint,
            entity_type="character",
            entity_id=character_id,
            etag=None if force else (cache.etag if cache else None),
        )
        now = utc_now()
        upsert_cache(
            conn,
            endpoint=endpoint,
            entity_type="character",
            entity_id=character_id,
            etag=result.etag,
            fetched_at=now,
            expires_at=result.expires_at,
            status=result.status_code,
        )

        if result.status_code == 304:
            if force:
                raise RuntimeError(
                    f"Unexpected 304 on forced character history refresh character_id={character_id}"
                )
            history_status = "unchanged_304"
        elif result.status_code == 200:
            if not isinstance(result.payload, list):
                raise RuntimeError(f"Invalid character history payload character_id={character_id}")
            upsert_character_history(conn, character_id, result.payload, now)
            history_status = "loaded"
        elif result.status_code == 404:
            logger.warning("Character history 404 character_id=%s payload=%s", character_id, result.payload)
            history_status = "not_found"
        else:
            raise RuntimeError(f"Unexpected character history status character_id={character_id} status={result.status_code} payload={result.payload}")

        # Every targeted FORCE refresh is followed by an unconditional public
        # character verification. A new deletion becomes terminal immediately:
        # mark is_deleted, append synthetic corp -1 history, set current=-1.
        if force:
            public_status = fetch_public_character(run_id, character_id, force=True)
            if public_status in ("deleted", "deleted_skip"):
                return "deleted"

        return history_status

    finally:
        session.close()
        conn.close()


def fetch_public_corporation(run_id: str, corporation_id: int) -> str:
    conn = db_connect()
    session = requests.Session()
    try:

        if corporation_exists(conn, corporation_id):
            if corporation_is_deleted(conn, corporation_id):
                return "deleted_skip"
            return "skipped"

        endpoint = f"/corporations/{corporation_id}/"
        cache = get_cache(conn, endpoint)

        if cache and cache.last_status == 404:
            mark_corporation_deleted(conn, corporation_id, utc_now())
            return "deleted"

        result = esi_request(
            conn=conn,
            session=session,
            run_id=run_id,
            method="GET",
            endpoint=endpoint,
            entity_type="corporation",
            entity_id=corporation_id,
            etag=cache.etag if cache else None,
        )
        now = utc_now()
        upsert_cache(
            conn,
            endpoint=endpoint,
            entity_type="corporation",
            entity_id=corporation_id,
            etag=result.etag,
            fetched_at=now,
            expires_at=result.expires_at,
            status=result.status_code,
        )

        if result.status_code == 200:
            if not isinstance(result.payload, dict):
                raise RuntimeError(f"Invalid corporation payload corporation_id={corporation_id}")
            upsert_corporation(conn, corporation_id, result.payload, now)
            return "loaded"

        if result.status_code == 404:
            mark_corporation_deleted(conn, corporation_id, now)
            return "deleted"

        if result.status_code == 304:
            return "unchanged_304"

        raise RuntimeError(f"Unexpected corporation status corporation_id={corporation_id} status={result.status_code} payload={result.payload}")

    finally:
        session.close()
        conn.close()


def fetch_corporation_history(run_id: str, corporation_id: int, force: bool = False) -> str:
    conn = db_connect()
    session = requests.Session()
    try:

        if corporation_is_deleted(conn, corporation_id):
            return "deleted_skip"

        endpoint = f"/corporations/{corporation_id}/alliancehistory/"
        cache = get_cache(conn, endpoint)

        if not force and cache_is_fresh(cache) and corporation_has_history(conn, corporation_id):
            return "skipped_cache"

        if not corporation_has_history(conn, corporation_id) and cache_is_fresh(cache):
            logger.warning(
                "REPAIR stale corporation history cache without history corporation_id=%s endpoint=%s",
                corporation_id, endpoint,
            )
            delete_cache(conn, endpoint)
            cache = None

        if force:
            logger.info(
                "FORCE corporation alliance history refresh after alliance mismatch corporation_id=%s",
                corporation_id,
            )

        result = esi_request(
            conn=conn,
            session=session,
            run_id=run_id,
            method="GET",
            endpoint=endpoint,
            entity_type="corporation",
            entity_id=corporation_id,
            etag=None if force else (cache.etag if cache else None),
        )
        now = utc_now()
        upsert_cache(
            conn,
            endpoint=endpoint,
            entity_type="corporation",
            entity_id=corporation_id,
            etag=result.etag,
            fetched_at=now,
            expires_at=result.expires_at,
            status=result.status_code,
        )

        if result.status_code == 304:
            if force:
                raise RuntimeError(
                    f"Unexpected 304 on forced corporation alliance history refresh corporation_id={corporation_id}"
                )
            return "unchanged_304"

        if result.status_code == 200:
            if not isinstance(result.payload, list):
                raise RuntimeError(f"Invalid corporation history payload corporation_id={corporation_id}")
            upsert_corporation_history(conn, corporation_id, result.payload, now)
            return "loaded"

        if result.status_code == 404:
            logger.warning("Corporation history 404 corporation_id=%s payload=%s", corporation_id, result.payload)
            return "not_found"

        raise RuntimeError(f"Unexpected corporation history status corporation_id={corporation_id} status={result.status_code} payload={result.payload}")

    finally:
        session.close()
        conn.close()


def fetch_public_alliance(run_id: str, alliance_id: int) -> str:
    conn = db_connect()
    session = requests.Session()
    try:

        if alliance_exists(conn, alliance_id):
            if alliance_is_deleted(conn, alliance_id):
                return "deleted_skip"
            return "skipped"

        endpoint = f"/alliances/{alliance_id}/"
        cache = get_cache(conn, endpoint)

        if cache and cache.last_status == 404:
            mark_alliance_deleted(conn, alliance_id, utc_now())
            return "deleted"

        result = esi_request(
            conn=conn,
            session=session,
            run_id=run_id,
            method="GET",
            endpoint=endpoint,
            entity_type="alliance",
            entity_id=alliance_id,
            etag=cache.etag if cache else None,
        )
        now = utc_now()
        upsert_cache(
            conn,
            endpoint=endpoint,
            entity_type="alliance",
            entity_id=alliance_id,
            etag=result.etag,
            fetched_at=now,
            expires_at=result.expires_at,
            status=result.status_code,
        )

        if result.status_code == 200:
            if not isinstance(result.payload, dict):
                raise RuntimeError(f"Invalid alliance payload alliance_id={alliance_id}")
            upsert_alliance(conn, alliance_id, result.payload, now)
            return "loaded"

        if result.status_code == 404:
            mark_alliance_deleted(conn, alliance_id, now)
            return "deleted"

        if result.status_code == 304:
            return "unchanged_304"

        raise RuntimeError(f"Unexpected alliance status alliance_id={alliance_id} status={result.status_code} payload={result.payload}")

    finally:
        session.close()
        conn.close()


# ============================================================
# WORK TABLE HELPERS
# ============================================================


def create_run_row(conn, run_id: str, source_table: str, source_column: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            INSERT INTO {}.esi_affiliation_run (run_id, source_table, source_column, started_at, status)
            VALUES (%s, %s, %s, NOW(), 'running')
            ON CONFLICT (run_id)
            DO UPDATE SET
                source_table = EXCLUDED.source_table,
                source_column = EXCLUDED.source_column,
                started_at = NOW(),
                finished_at = NULL,
                status = 'running',
                error = NULL
            """).format(sql.Identifier(ENTITIES_SCHEMA)),
            (run_id, source_table, source_column),
        )
    conn.commit()


def finish_run_row(conn, run_id: str, status: str, error: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            UPDATE {}.esi_affiliation_run
            SET status=%s,
                error=%s,
                finished_at=NOW()
            WHERE run_id=%s
            """).format(sql.Identifier(ENTITIES_SCHEMA)),
            (status, error, run_id),
        )
    conn.commit()


def purge_work_tables(conn) -> None:
    """
    Purge les tables de travail persistantes avant un nouveau run.
    Ce ne sont pas des tables métier finales : elles servent uniquement d'état de traitement.
    """
    with conn.cursor() as cur:
        cur.execute(sql.SQL("""
            TRUNCATE TABLE
                {}.esi_affiliation_character_work,
                {}.esi_affiliation_result_work,
                {}.esi_affiliation_corporation_work,
                {}.esi_affiliation_alliance_work,
                {}.esi_affiliation_run
        """).format(
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
        ))
    conn.commit()


def populate_character_work(conn, run_id: str, source_table: str, source_column: str, limit: int | None) -> int:
    """Materialize the source workset in one SQL pass.

    This replaces the old INSERT + several full-table UPDATE passes. Existing
    public/deleted/history/current state is classified while the row is created,
    so a multi-million-character run does not rewrite the work table several
    times before the first ESI call.
    """
    schema, table, column = ensure_source(conn, source_table, source_column)

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("DELETE FROM {}.esi_affiliation_character_work WHERE run_id=%s").format(sql.Identifier(ENTITIES_SCHEMA)),
            (run_id,),
        )
        cur.execute(
            sql.SQL("DELETE FROM {}.esi_affiliation_result_work WHERE run_id=%s").format(sql.Identifier(ENTITIES_SCHEMA)),
            (run_id,),
        )
        cur.execute(
            sql.SQL("DELETE FROM {}.esi_affiliation_corporation_work WHERE run_id=%s").format(sql.Identifier(ENTITIES_SCHEMA)),
            (run_id,),
        )
        cur.execute(
            sql.SQL("DELETE FROM {}.esi_affiliation_alliance_work WHERE run_id=%s").format(sql.Identifier(ENTITIES_SCHEMA)),
            (run_id,),
        )

        limit_sql = sql.SQL("")
        params: list[Any] = []
        if limit is not None:
            limit_sql = sql.SQL(" LIMIT %s")
            params.append(limit)
        params.append(run_id)

        query = sql.SQL("""
            WITH src AS (
                SELECT DISTINCT {column}::BIGINT AS character_id
                FROM {schema}.{table}
                WHERE {column} IS NOT NULL
                ORDER BY {column}::BIGINT
                {limit_sql}
            )
            INSERT INTO {entities}.esi_affiliation_character_work (
                run_id,
                character_id,
                has_history,
                affiliation_checked_at,
                source_history_fetched_at,
                public_status,
                public_processed_at,
                initial_history_status,
                affiliation_status,
                history_refresh_status
            )
            SELECT
                %s::uuid,
                src.character_id,
                (h.character_id IS NOT NULL) AS has_history,
                cca.affiliation_checked_at,
                cca.source_history_fetched_at,
                CASE
                    WHEN c.character_id IS NULL THEN 'pending'
                    ELSE 'skipped'
                END AS public_status,
                CASE
                    WHEN c.character_id IS NULL THEN NULL
                    ELSE NOW()
                END AS public_processed_at,
                CASE
                    WHEN COALESCE(c.is_deleted, FALSE) = TRUE THEN 'not_applicable'
                    WHEN h.character_id IS NOT NULL THEN 'not_applicable'
                    ELSE 'pending'
                END AS initial_history_status,
                CASE
                    WHEN COALESCE(c.is_deleted, FALSE) = TRUE THEN 'not_applicable'
                    WHEN h.character_id IS NOT NULL THEN 'pending'
                    ELSE 'not_applicable'
                END AS affiliation_status,
                'not_applicable' AS history_refresh_status
            FROM src
            LEFT JOIN {entities}.characters c
              ON c.character_id = src.character_id
            LEFT JOIN {entities}.character_current_affiliation cca
              ON cca.character_id = src.character_id
            LEFT JOIN LATERAL (
                SELECT h.character_id
                FROM {entities}.character_corporation_history h
                WHERE h.character_id = src.character_id
                LIMIT 1
            ) h ON TRUE
            ON CONFLICT (run_id, character_id) DO NOTHING
        """).format(
            column=sql.Identifier(column),
            schema=sql.Identifier(schema),
            table=sql.Identifier(table),
            limit_sql=limit_sql,
            entities=sql.Identifier(ENTITIES_SCHEMA),
        )
        cur.execute(query, tuple(params))
        rows = int(cur.rowcount)

    conn.commit()
    return rows


def prepare_public_character_work(conn, run_id: str) -> int:
    """Return only the public profiles still missing after one-pass materialization."""
    with conn.cursor() as cur:
        cur.execute(sql.SQL("""
            SELECT COUNT(*)
            FROM {}.esi_affiliation_character_work
            WHERE run_id=%s
              AND public_status='pending'
        """).format(sql.Identifier(ENTITIES_SCHEMA)), (run_id,))
        pending = int(cur.fetchone()[0])
    return pending


def refresh_character_history_flags(conn, run_id: str) -> tuple[int, int]:
    """Refresh only rows that may have changed during this run, then count.

    The initial one-pass INSERT already classified the millions of pre-existing
    characters. Only freshly fetched public profiles and initial-history rows
    can have changed afterwards.
    """
    with conn.cursor() as cur:
        cur.execute(sql.SQL("""
            UPDATE {}.esi_affiliation_character_work w
            SET has_history = EXISTS (
                    SELECT 1
                    FROM {}.character_corporation_history h
                    WHERE h.character_id = w.character_id
                ),
                affiliation_checked_at = (
                    SELECT cca.affiliation_checked_at
                    FROM {}.character_current_affiliation cca
                    WHERE cca.character_id = w.character_id
                ),
                source_history_fetched_at = (
                    SELECT cca.source_history_fetched_at
                    FROM {}.character_current_affiliation cca
                    WHERE cca.character_id = w.character_id
                ),
                initial_history_status = CASE
                    WHEN w.initial_history_processed_at IS NOT NULL
                        THEN w.initial_history_status
                    WHEN COALESCE((
                        SELECT c.is_deleted
                        FROM {}.characters c
                        WHERE c.character_id = w.character_id
                    ), FALSE) = TRUE
                        THEN 'not_applicable'
                    WHEN EXISTS (
                        SELECT 1
                        FROM {}.character_corporation_history h
                        WHERE h.character_id = w.character_id
                    )
                        THEN 'not_applicable'
                    ELSE 'pending'
                END,
                affiliation_status = CASE
                    WHEN COALESCE((
                        SELECT c.is_deleted
                        FROM {}.characters c
                        WHERE c.character_id = w.character_id
                    ), FALSE) = TRUE
                        THEN 'not_applicable'
                    WHEN EXISTS (
                        SELECT 1
                        FROM {}.character_corporation_history h
                        WHERE h.character_id = w.character_id
                    )
                        THEN 'pending'
                    ELSE 'not_applicable'
                END,
                history_refresh_status = 'not_applicable',
                updated_at = NOW()
            WHERE w.run_id=%s
              AND (
                    w.public_status <> 'skipped'
                 OR w.initial_history_status = 'pending'
                 OR w.initial_history_processed_at IS NOT NULL
              )
        """).format(
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
        ), (run_id,))

        cur.execute(sql.SQL("""
            SELECT
                COUNT(*) FILTER (
                    WHERE w.has_history IS TRUE
                      AND COALESCE(c.is_deleted, FALSE) = FALSE
                ),
                COUNT(*) FILTER (
                    WHERE w.has_history IS FALSE
                      AND COALESCE(c.is_deleted, FALSE) = FALSE
                )
            FROM {}.esi_affiliation_character_work w
            LEFT JOIN {}.characters c
              ON c.character_id=w.character_id
            WHERE w.run_id=%s
        """).format(
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
        ), (run_id,))
        with_history, without_history = cur.fetchone()

    conn.commit()
    return int(with_history or 0), int(without_history or 0)


def pop_work_ids(
    conn,
    *,
    table: str,
    id_column: str,
    status_column: str,
    run_id: str,
    batch_size: int,
    order_by: str | None = None,
) -> list[int]:
    validate_identifier(table, "work table")
    validate_identifier(id_column, "id column")
    validate_identifier(status_column, "status column")

    if order_by:
        order_sql = sql.SQL(order_by)
    else:
        order_sql = sql.SQL("{}").format(sql.Identifier(id_column))

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            WITH picked AS (
                SELECT {id_column}
                FROM {schema}.{table}
                WHERE run_id=%s
                  AND {status_column}='pending'
                ORDER BY {order_sql}
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            UPDATE {schema}.{table} w
            SET {status_column}='running',
                updated_at=NOW()
            FROM picked p
            WHERE w.run_id=%s
              AND w.{id_column}=p.{id_column}
            RETURNING w.{id_column}
            """).format(
                schema=sql.Identifier(ENTITIES_SCHEMA),
                table=sql.Identifier(table),
                id_column=sql.Identifier(id_column),
                status_column=sql.Identifier(status_column),
                order_sql=order_sql,
            ),
            (run_id, batch_size, run_id),
        )
        ids = [int(r[0]) for r in cur.fetchall()]
    conn.commit()
    return ids


def update_character_work_status(
    conn,
    *,
    run_id: str,
    character_id: int,
    status_column: str,
    status: str,
    http_status_column: str | None = None,
    http_status: int | None = None,
    error_column: str | None = None,
    error: str | None = None,
    processed_column: str | None = None,
) -> None:
    validate_identifier(status_column, "status column")
    assignments = [sql.SQL("{}=%s").format(sql.Identifier(status_column)), sql.SQL("updated_at=NOW()")]
    params: list[Any] = [status]

    if http_status_column:
        validate_identifier(http_status_column, "http status column")
        assignments.append(sql.SQL("{}=%s").format(sql.Identifier(http_status_column)))
        params.append(http_status)
    if error_column:
        validate_identifier(error_column, "error column")
        assignments.append(sql.SQL("{}=%s").format(sql.Identifier(error_column)))
        params.append(error)
    if processed_column:
        validate_identifier(processed_column, "processed column")
        assignments.append(sql.SQL("{}=NOW()").format(sql.Identifier(processed_column)))

    params.extend([run_id, character_id])

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("UPDATE {}.esi_affiliation_character_work SET {} WHERE run_id=%s AND character_id=%s").format(
                sql.Identifier(ENTITIES_SCHEMA),
                sql.SQL(", ").join(assignments),
            ),
            tuple(params),
        )
    conn.commit()


def update_corporation_work_status(
    conn,
    *,
    run_id: str,
    corporation_id: int,
    status_column: str,
    status: str,
    http_status_column: str | None = None,
    http_status: int | None = None,
    error_column: str | None = None,
    error: str | None = None,
    processed_column: str | None = None,
) -> None:
    validate_identifier(status_column, "status column")
    assignments = [sql.SQL("{}=%s").format(sql.Identifier(status_column)), sql.SQL("updated_at=NOW()")]
    params: list[Any] = [status]

    if http_status_column:
        validate_identifier(http_status_column, "http status column")
        assignments.append(sql.SQL("{}=%s").format(sql.Identifier(http_status_column)))
        params.append(http_status)
    if error_column:
        validate_identifier(error_column, "error column")
        assignments.append(sql.SQL("{}=%s").format(sql.Identifier(error_column)))
        params.append(error)
    if processed_column:
        validate_identifier(processed_column, "processed column")
        assignments.append(sql.SQL("{}=NOW()").format(sql.Identifier(processed_column)))

    params.extend([run_id, corporation_id])

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("UPDATE {}.esi_affiliation_corporation_work SET {} WHERE run_id=%s AND corporation_id=%s").format(
                sql.Identifier(ENTITIES_SCHEMA),
                sql.SQL(", ").join(assignments),
            ),
            tuple(params),
        )
    conn.commit()


def update_alliance_work_status(
    conn,
    *,
    run_id: str,
    alliance_id: int,
    status: str,
    http_status: int | None = None,
    error: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            UPDATE {}.esi_affiliation_alliance_work
            SET public_status=%s,
                public_http_status=%s,
                public_error=%s,
                public_processed_at=NOW(),
                updated_at=NOW()
            WHERE run_id=%s AND alliance_id=%s
            """).format(sql.Identifier(ENTITIES_SCHEMA)),
            (status, http_status, error, run_id, alliance_id),
        )
    conn.commit()


def count_status(conn, table: str, run_id: str, status_column: str, status: str) -> int:
    validate_identifier(table, "table")
    validate_identifier(status_column, "status column")
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            SELECT COUNT(*)
            FROM {}.{}
            WHERE run_id=%s AND {}=%s
            """).format(
                sql.Identifier(ENTITIES_SCHEMA),
                sql.Identifier(table),
                sql.Identifier(status_column),
            ),
            (run_id, status),
        )
        return int(cur.fetchone()[0])


def status_summary(conn, table: str, run_id: str, status_column: str) -> dict[str, int]:
    validate_identifier(table, "table")
    validate_identifier(status_column, "status column")
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            SELECT {}, COUNT(*)
            FROM {}.{}
            WHERE run_id=%s
            GROUP BY {}
            """).format(
                sql.Identifier(status_column),
                sql.Identifier(ENTITIES_SCHEMA),
                sql.Identifier(table),
                sql.Identifier(status_column),
            ),
            (run_id,),
        )
        return {str(k): int(v) for k, v in cur.fetchall()}



def bulk_update_character_work_status(
    conn,
    *,
    run_id: str,
    status_column: str,
    status_to_ids: dict[str, list[int]],
    http_status_column: str | None = None,
    error_column: str | None = None,
    processed_column: str | None = None,
) -> None:
    validate_identifier(status_column, "status column")
    if http_status_column:
        validate_identifier(http_status_column, "http status column")
    if error_column:
        validate_identifier(error_column, "error column")
    if processed_column:
        validate_identifier(processed_column, "processed column")

    with conn.cursor() as cur:
        for status, ids in status_to_ids.items():
            if not ids:
                continue

            assignments = [
                sql.SQL("{}=%s").format(sql.Identifier(status_column)),
                sql.SQL("updated_at=NOW()"),
            ]
            params: list[Any] = [status]

            if http_status_column:
                assignments.append(sql.SQL("{}=%s").format(sql.Identifier(http_status_column)))
                params.append(None)

            if error_column:
                assignments.append(sql.SQL("{}=%s").format(sql.Identifier(error_column)))
                params.append(None if status != "failed" else "see log")

            if processed_column:
                assignments.append(sql.SQL("{}=NOW()").format(sql.Identifier(processed_column)))

            params.extend([run_id, ids])

            cur.execute(
                sql.SQL("""
                UPDATE {}.esi_affiliation_character_work
                SET {}
                WHERE run_id=%s
                  AND character_id = ANY(%s)
                """).format(
                    sql.Identifier(ENTITIES_SCHEMA),
                    sql.SQL(", ").join(assignments),
                ),
                tuple(params),
            )

    conn.commit()


def bulk_update_corporation_work_status(
    conn,
    *,
    run_id: str,
    status_column: str,
    status_to_ids: dict[str, list[int]],
    http_status_column: str | None = None,
    error_column: str | None = None,
    processed_column: str | None = None,
) -> None:
    validate_identifier(status_column, "status column")
    if http_status_column:
        validate_identifier(http_status_column, "http status column")
    if error_column:
        validate_identifier(error_column, "error column")
    if processed_column:
        validate_identifier(processed_column, "processed column")

    with conn.cursor() as cur:
        for status, ids in status_to_ids.items():
            if not ids:
                continue

            assignments = [
                sql.SQL("{}=%s").format(sql.Identifier(status_column)),
                sql.SQL("updated_at=NOW()"),
            ]
            params: list[Any] = [status]

            if http_status_column:
                assignments.append(sql.SQL("{}=%s").format(sql.Identifier(http_status_column)))
                params.append(None)

            if error_column:
                assignments.append(sql.SQL("{}=%s").format(sql.Identifier(error_column)))
                params.append(None if status != "failed" else "see log")

            if processed_column:
                assignments.append(sql.SQL("{}=NOW()").format(sql.Identifier(processed_column)))

            params.extend([run_id, ids])

            cur.execute(
                sql.SQL("""
                UPDATE {}.esi_affiliation_corporation_work
                SET {}
                WHERE run_id=%s
                  AND corporation_id = ANY(%s)
                """).format(
                    sql.Identifier(ENTITIES_SCHEMA),
                    sql.SQL(", ").join(assignments),
                ),
                tuple(params),
            )

    conn.commit()


def bulk_update_alliance_work_status(
    conn,
    *,
    run_id: str,
    status_to_ids: dict[str, list[int]],
) -> None:
    with conn.cursor() as cur:
        for status, ids in status_to_ids.items():
            if not ids:
                continue

            cur.execute(
                sql.SQL("""
                UPDATE {}.esi_affiliation_alliance_work
                SET public_status=%s,
                    public_http_status=NULL,
                    public_error=%s,
                    public_processed_at=NOW(),
                    updated_at=NOW()
                WHERE run_id=%s
                  AND alliance_id = ANY(%s)
                """).format(sql.Identifier(ENTITIES_SCHEMA)),
                (status, None if status != "failed" else "see log", run_id, ids),
            )

    conn.commit()


def group_results_by_status(results: dict[int, str]) -> dict[str, list[int]]:
    grouped: dict[str, list[int]] = {}
    for entity_id, status in results.items():
        grouped.setdefault(status, []).append(entity_id)
    return grouped


# ============================================================
# PARALLEL WORKERS
# ============================================================


def run_parallel_ids(ids: list[int], workers: int, fn, label: str) -> dict[int, str]:
    results: dict[int, str] = {}
    if not ids:
        return results

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(fn, item): item for item in ids}
        try:
            for fut in as_completed(future_map):
                item = future_map[fut]
                try:
                    results[item] = fut.result()
                except Exception as exc:
                    logger.exception("FAILED %s id=%s error=%s", label, item, exc)
                    results[item] = "failed"
        except KeyboardInterrupt:
            logger.warning("KeyboardInterrupt: cancelling pending %s futures", label)
            for fut in future_map:
                fut.cancel()
            raise

    return results


def run_character_stage_from_table(
    conn,
    *,
    run_id: str,
    status_column: str,
    process_batch_size: int,
    workers: int,
    fn,
    label: str,
    http_status_column: str,
    error_column: str,
    processed_column: str,
    order_by: str | None = None,
    progress_total: int | None = None,
) -> dict[str, int]:
    summary: dict[str, int] = {}

    while True:
        ids = pop_work_ids(
            conn,
            table="esi_affiliation_character_work",
            id_column="character_id",
            status_column=status_column,
            run_id=run_id,
            batch_size=process_batch_size,
            order_by=order_by,
        )
        if not ids:
            break

        logger.info("Processing %s chunk size=%s", label, len(ids))
        results = run_parallel_ids(ids, workers, fn, label)
        for status in results.values():
            summary[status] = summary.get(status, 0) + 1

        bulk_update_character_work_status(
            conn,
            run_id=run_id,
            status_column=status_column,
            status_to_ids=group_results_by_status(results),
            http_status_column=http_status_column,
            error_column=error_column,
            processed_column=processed_column,
        )

        processed_count = sum(summary.values())
        total_count = int(progress_total) if progress_total is not None else processed_count
        logger.info(
            "PROGRESS stage=%s processed=%s total=%s",
            label,
            processed_count,
            total_count,
        )

    return summary


def run_corporation_stage_from_table(
    conn,
    *,
    run_id: str,
    status_column: str,
    process_batch_size: int,
    workers: int,
    fn,
    label: str,
    http_status_column: str,
    error_column: str,
    processed_column: str,
) -> dict[str, int]:
    summary: dict[str, int] = {}

    while True:
        ids = pop_work_ids(
            conn,
            table="esi_affiliation_corporation_work",
            id_column="corporation_id",
            status_column=status_column,
            run_id=run_id,
            batch_size=process_batch_size,
        )
        if not ids:
            break

        logger.info("Processing %s chunk size=%s", label, len(ids))
        results = run_parallel_ids(ids, workers, fn, label)
        for status in results.values():
            summary[status] = summary.get(status, 0) + 1

        bulk_update_corporation_work_status(
            conn,
            run_id=run_id,
            status_column=status_column,
            status_to_ids=group_results_by_status(results),
            http_status_column=http_status_column,
            error_column=error_column,
            processed_column=processed_column,
        )

    return summary


def run_alliance_stage_from_table(
    conn,
    *,
    run_id: str,
    process_batch_size: int,
    workers: int,
    fn,
    label: str,
) -> dict[str, int]:
    summary: dict[str, int] = {}

    while True:
        ids = pop_work_ids(
            conn,
            table="esi_affiliation_alliance_work",
            id_column="alliance_id",
            status_column="public_status",
            run_id=run_id,
            batch_size=process_batch_size,
        )
        if not ids:
            break

        logger.info("Processing %s chunk size=%s", label, len(ids))
        results = run_parallel_ids(ids, workers, fn, label)
        for status in results.values():
            summary[status] = summary.get(status, 0) + 1

        bulk_update_alliance_work_status(
            conn,
            run_id=run_id,
            status_to_ids=group_results_by_status(results),
        )

    return summary


# ============================================================
# AFFILIATION BULK
# ============================================================


def batch_hash(ids: list[int]) -> str:
    normalized = ",".join(str(i) for i in sorted(ids))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def get_batch_since(conn, ids: list[int]) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            SELECT
                COUNT(*) FILTER (WHERE affiliation_checked_at IS NULL) AS null_checks,
                MIN(affiliation_checked_at) AS min_checked_at
            FROM {}.character_current_affiliation
            WHERE character_id = ANY(%s)
            """).format(sql.Identifier(ENTITIES_SCHEMA)),
            (ids,),
        )
        row = cur.fetchone()

    if not row:
        return None

    null_checks = int(row[0] or 0)
    min_checked_at = row[1]

    if null_checks > 0:
        return None

    return min_checked_at


def post_affiliation_batch(run_id: str, ids: list[int]) -> tuple[str, list[dict[str, Any]]]:
    conn = db_connect()
    session = requests.Session()
    try:
        ensure_tables(conn)

        bh = batch_hash(ids)
        endpoint = f"/characters/affiliation/?batch={bh}"
        real_endpoint = "/characters/affiliation/"
        cache = get_cache(conn, endpoint)
        since = get_batch_since(conn, ids)

        logger.info(
            "AFFILIATION BATCH size=%s since=%s",
            len(ids),
            since.isoformat() if since else None,
        )

        result = esi_request(
            conn=conn,
            session=session,
            run_id=run_id,
            method="POST",
            endpoint=real_endpoint,
            entity_type="character_affiliation_batch",
            entity_id=None,
            etag=cache.etag if cache else None,
            if_modified_since=since,
            json_body=ids,
        )
        now = utc_now()

        upsert_cache(
            conn,
            endpoint=endpoint,
            entity_type="character_affiliation_batch",
            entity_id=None,
            etag=result.etag,
            fetched_at=now,
            expires_at=result.expires_at,
            status=result.status_code,
        )

        if result.status_code == 304:
            mark_affiliation_checked(conn, ids)
            return "304", []

        if result.status_code == 200:
            if not isinstance(result.payload, list):
                raise RuntimeError("Invalid affiliation payload: expected list")
            return "200", result.payload

        raise RuntimeError(f"Unexpected affiliation status={result.status_code} payload={result.payload}")

    finally:
        session.close()
        conn.close()


def pop_affiliation_batch(conn, run_id: str, batch_size: int, batch_no: int) -> list[int]:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            WITH picked AS (
                SELECT character_id
                FROM {}.esi_affiliation_character_work
                WHERE run_id=%s
                  AND affiliation_status='pending'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM {}.characters c
                      WHERE c.character_id = esi_affiliation_character_work.character_id
                        AND c.is_deleted = TRUE
                  )
                ORDER BY
                    affiliation_checked_at ASC NULLS FIRST,
                    source_history_fetched_at ASC NULLS FIRST,
                    character_id ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            UPDATE {}.esi_affiliation_character_work w
            SET affiliation_status='running',
                affiliation_batch_no=%s,
                updated_at=NOW()
            FROM picked p
            WHERE w.run_id=%s
              AND w.character_id=p.character_id
            RETURNING w.character_id
            """).format(
                sql.Identifier(ENTITIES_SCHEMA),
                sql.Identifier(ENTITIES_SCHEMA),
                sql.Identifier(ENTITIES_SCHEMA),
            ),
            (run_id, batch_size, batch_no, run_id),
        )
        ids = [int(r[0]) for r in cur.fetchall()]
    conn.commit()
    return ids


def mark_affiliation_batch_status(
    conn,
    *,
    run_id: str,
    ids: list[int],
    status: str,
    http_status: int | None,
    error: str | None = None,
) -> None:
    if not ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            UPDATE {}.esi_affiliation_character_work
            SET affiliation_status=%s,
                affiliation_http_status=%s,
                affiliation_error=%s,
                affiliation_processed_at=NOW(),
                updated_at=NOW()
            WHERE run_id=%s
              AND character_id = ANY(%s)
            """).format(sql.Identifier(ENTITIES_SCHEMA)),
            (status, http_status, error, run_id, ids),
        )
    conn.commit()


def insert_affiliation_rows(conn, run_id: str, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0

    values: list[tuple[str, int, int, int | None]] = []
    for row in rows:
        character_id = int(row["character_id"])
        corporation_id = int(row["corporation_id"])
        alliance_raw = row.get("alliance_id")
        alliance_id = int(alliance_raw) if alliance_raw is not None else None
        values.append((run_id, character_id, corporation_id, alliance_id))

    with conn.cursor() as cur:
        execute_values(
            cur,
            f"""
            INSERT INTO {ENTITIES_SCHEMA}.esi_affiliation_result_work (
                run_id, character_id, corporation_id, alliance_id
            )
            VALUES %s
            ON CONFLICT (run_id, character_id)
            DO UPDATE SET
                corporation_id = EXCLUDED.corporation_id,
                alliance_id = EXCLUDED.alliance_id
            """,
            values,
            template="(%s::uuid, %s, %s, %s)",
            page_size=5000,
        )
    conn.commit()
    return len(values)



_AFFILIATION_BULK_THREAD_LOCAL = threading.local()


def affiliation_bulk_worker_connection():
    conn = getattr(_AFFILIATION_BULK_THREAD_LOCAL, "conn", None)
    if conn is None or conn.closed:
        conn = db_connect()
        _AFFILIATION_BULK_THREAD_LOCAL.conn = conn
    return conn


def affiliation_bulk_worker_session() -> requests.Session:
    session = getattr(_AFFILIATION_BULK_THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        _AFFILIATION_BULK_THREAD_LOCAL.session = session
    return session


def open_affiliation_candidate_cursor(run_id: str):
    """Open an isolated server-side cursor over the fixed STEP 4 candidate set."""
    stream_conn = db_connect()
    cursor_name = f"esi_aff_bulk_{uuid.uuid4().hex[:12]}"
    cur = stream_conn.cursor(name=cursor_name)
    cur.itersize = 5000
    cur.execute(sql.SQL("""
        SELECT character_id, affiliation_checked_at, source_history_fetched_at
        FROM {}.esi_affiliation_character_work
        WHERE run_id=%s
          AND affiliation_status='pending'
        ORDER BY affiliation_checked_at ASC NULLS FIRST,
                 source_history_fetched_at ASC NULLS FIRST,
                 character_id ASC
    """).format(sql.Identifier(ENTITIES_SCHEMA)), (run_id,))
    return stream_conn, cur


def make_affiliation_batch(rows: list[tuple[Any, Any, Any]]) -> tuple[list[int], datetime | None]:
    ids = [int(row[0]) for row in rows]
    checks = [row[1] for row in rows]
    since = None if any(value is None for value in checks) else (min(checks) if checks else None)
    return ids, since


def post_affiliation_batch_optimized(run_id: str, ids: list[int], since: datetime | None) -> tuple[str, list[dict[str, Any]], int | None]:
    """Persistent worker connection/session; no ensure_tables() per batch."""
    conn = affiliation_bulk_worker_connection()
    session = affiliation_bulk_worker_session()
    try:
        bh = batch_hash(ids)
        endpoint = f"/characters/affiliation/?batch={bh}"
        real_endpoint = "/characters/affiliation/"
        cache = get_cache(conn, endpoint)

        logger.info(
            "AFFILIATION BATCH size=%s since=%s",
            len(ids),
            since.isoformat() if since else None,
        )

        result = esi_request(
            conn=conn,
            session=session,
            run_id=run_id,
            method="POST",
            endpoint=real_endpoint,
            entity_type="character_affiliation_batch",
            entity_id=None,
            etag=cache.etag if cache else None,
            if_modified_since=since,
            json_body=ids,
        )
        now = utc_now()
        upsert_cache(
            conn,
            endpoint=endpoint,
            entity_type="character_affiliation_batch",
            entity_id=None,
            etag=result.etag,
            fetched_at=now,
            expires_at=result.expires_at,
            status=result.status_code,
        )

        if result.status_code == 304:
            return "304", [], 304
        if result.status_code == 200:
            if not isinstance(result.payload, list):
                raise RuntimeError("Invalid affiliation payload: expected list")
            return "200", result.payload, 200
        raise RuntimeError(f"Unexpected affiliation status={result.status_code} payload={result.payload}")
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise


def apply_affiliation_batch_optimized(
    conn,
    *,
    run_id: str,
    batch_no: int,
    ids: list[int],
    status: str,
    rows: list[dict[str, Any]],
    http_status: int | None,
    error: str | None = None,
) -> int:
    """Apply one bulk response in a single production DB transaction."""
    inserted = 0
    with conn.cursor() as cur:
        if status == "200" and rows:
            values: list[tuple[Any, ...]] = []
            for item in rows:
                alliance_id = item.get("alliance_id")
                values.append((
                    run_id,
                    int(item["character_id"]),
                    int(item["corporation_id"]),
                    int(alliance_id) if alliance_id is not None else None,
                ))
            execute_values(
                cur,
                f"""
                INSERT INTO {ENTITIES_SCHEMA}.esi_affiliation_result_work (
                    run_id, character_id, corporation_id, alliance_id
                ) VALUES %s
                ON CONFLICT (run_id, character_id)
                DO UPDATE SET
                    corporation_id = EXCLUDED.corporation_id,
                    alliance_id = EXCLUDED.alliance_id
                """,
                values,
                template="(%s::uuid, %s, %s, %s)",
                page_size=1000,
            )
            inserted = len(values)

        if status == "304":
            cur.execute(sql.SQL("""
                UPDATE {}.character_current_affiliation
                SET affiliation_checked_at=NOW(),
                    updated_at=NOW()
                WHERE character_id = ANY(%s)
            """).format(sql.Identifier(ENTITIES_SCHEMA)), (ids,))
            work_status = "unchanged_304"
        elif status == "200":
            work_status = "loaded"
        else:
            work_status = "failed"

        cur.execute(sql.SQL("""
            UPDATE {}.esi_affiliation_character_work
            SET affiliation_batch_no=%s,
                affiliation_status=%s,
                affiliation_http_status=%s,
                affiliation_error=%s,
                affiliation_processed_at=NOW(),
                updated_at=NOW()
            WHERE run_id=%s
              AND character_id = ANY(%s)
        """).format(sql.Identifier(ENTITIES_SCHEMA)), (
            batch_no,
            work_status,
            http_status,
            error,
            run_id,
            ids,
        ))

    conn.commit()
    return inserted


def run_affiliation_bulk_optimized(conn, run_id: str, args, stats: Stats) -> None:
    """Stream and parallelize STEP 4 while keeping the global 280/min limiter."""
    with conn.cursor() as cur:
        cur.execute(sql.SQL("""
            SELECT COUNT(*)
            FROM {}.esi_affiliation_character_work
            WHERE run_id=%s
              AND affiliation_status='pending'
        """).format(sql.Identifier(ENTITIES_SCHEMA)), (run_id,))
        candidates = int(cur.fetchone()[0])

    total_batches = (candidates + args.batch_size - 1) // args.batch_size if candidates else 0
    logger.info(
        "STEP 4 OPTIMIZED affiliation bulk candidates=%s batches=%s batch_size=%s workers=%s",
        candidates,
        total_batches,
        args.batch_size,
        args.workers,
    )

    stream_conn, stream_cur = open_affiliation_candidate_cursor(run_id)
    executor = ThreadPoolExecutor(max_workers=args.workers)
    inflight: dict[Any, tuple[int, list[int]]] = {}
    next_batch_no = 1
    done = 0
    started = time.monotonic()
    last_progress = 0.0

    def submit_one() -> bool:
        nonlocal next_batch_no
        rows = stream_cur.fetchmany(args.batch_size)
        if not rows:
            return False
        ids, since = make_affiliation_batch(rows)
        future = executor.submit(post_affiliation_batch_optimized, run_id, ids, since)
        inflight[future] = (next_batch_no, ids)
        next_batch_no += 1
        return True

    try:
        for _ in range(args.workers):
            if not submit_one():
                break

        while inflight:
            completed, _ = wait(list(inflight.keys()), return_when=FIRST_COMPLETED)
            for future in completed:
                batch_no, ids = inflight.pop(future)
                done += 1
                stats.affiliation_batches += 1
                try:
                    status, rows, http_status = future.result()
                    if status == "304":
                        stats.affiliation_304 += 1
                    else:
                        stats.affiliation_200 += 1
                    inserted = apply_affiliation_batch_optimized(
                        conn,
                        run_id=run_id,
                        batch_no=batch_no,
                        ids=ids,
                        status=status,
                        rows=rows,
                        http_status=http_status,
                    )
                    stats.affiliation_rows += inserted
                except Exception as exc:
                    stats.affiliation_failed += 1
                    logger.exception("Affiliation batch failed batch=%s size=%s error=%s", batch_no, len(ids), exc)
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    try:
                        apply_affiliation_batch_optimized(
                            conn,
                            run_id=run_id,
                            batch_no=batch_no,
                            ids=ids,
                            status="failed",
                            rows=[],
                            http_status=None,
                            error=type(exc).__name__,
                        )
                    except Exception:
                        logger.exception("Failed to mark affiliation batch failed batch=%s", batch_no)

                submit_one()

                now = time.monotonic()
                if now - last_progress >= 5.0 or done == total_batches:
                    elapsed = max(0.001, now - started)
                    rate = done / elapsed * 60.0
                    logger.info(
                        "AFFILIATION BULK PROGRESS done=%s total=%s calls_per_min=%.1f rows=%s failures=%s",
                        done,
                        total_batches,
                        rate,
                        stats.affiliation_rows,
                        stats.affiliation_failed,
                    )
                    last_progress = now
    finally:
        try:
            stream_cur.close()
        except Exception:
            pass
        try:
            stream_conn.close()
        except Exception:
            pass
        executor.shutdown(wait=True, cancel_futures=True)

    elapsed = max(0.001, time.monotonic() - started)
    logger.info(
        "STEP 4 OPTIMIZED DONE batches=%s duration=%.1fs calls_per_min=%.1f http_200=%s http_304=%s failed=%s rows=%s",
        stats.affiliation_batches,
        elapsed,
        stats.affiliation_batches / elapsed * 60.0,
        stats.affiliation_200,
        stats.affiliation_304,
        stats.affiliation_failed,
        stats.affiliation_rows,
    )


def process_affiliation_results_sql(conn, run_id: str) -> tuple[int, int]:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("""
            UPDATE {}.esi_affiliation_character_work w
            SET history_refresh_status='pending',
                known_corporation_id=c.corporation_id,
                current_corporation_id=r.corporation_id,
                current_alliance_id=r.alliance_id,
                updated_at=NOW()
            FROM {}.esi_affiliation_result_work r
            LEFT JOIN {}.character_current_affiliation c
              ON c.character_id = r.character_id
            WHERE w.run_id=%s
              AND r.run_id=%s
              AND w.character_id=r.character_id
              AND c.corporation_id IS DISTINCT FROM r.corporation_id
        """).format(
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
        ), (run_id, run_id))
        char_refresh_count = int(cur.rowcount)

        cur.execute(sql.SQL("""
            UPDATE {}.esi_affiliation_character_work w
            SET history_refresh_status='not_applicable',
                known_corporation_id=c.corporation_id,
                current_corporation_id=r.corporation_id,
                current_alliance_id=r.alliance_id,
                updated_at=NOW()
            FROM {}.esi_affiliation_result_work r
            LEFT JOIN {}.character_current_affiliation c
              ON c.character_id = r.character_id
            WHERE w.run_id=%s
              AND r.run_id=%s
              AND w.character_id=r.character_id
              AND c.corporation_id IS NOT DISTINCT FROM r.corporation_id
        """).format(
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
        ), (run_id, run_id))

        cur.execute(sql.SQL("""
            INSERT INTO {}.character_history_refresh_queue (
                character_id,
                reason,
                detected_at,
                known_corporation_id,
                current_corporation_id,
                current_alliance_id,
                processed_at
            )
            SELECT
                w.character_id,
                'character_corporation_changed_from_affiliation',
                NOW(),
                w.known_corporation_id,
                w.current_corporation_id,
                w.current_alliance_id,
                NULL
            FROM {}.esi_affiliation_character_work w
            WHERE w.run_id=%s
              AND w.history_refresh_status='pending'
            ON CONFLICT (character_id)
            DO UPDATE SET
                reason = EXCLUDED.reason,
                detected_at = NOW(),
                known_corporation_id = EXCLUDED.known_corporation_id,
                current_corporation_id = EXCLUDED.current_corporation_id,
                current_alliance_id = EXCLUDED.current_alliance_id,
                processed_at = NULL
        """).format(
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
        ), (run_id,))

        cur.execute(sql.SQL("""
            UPDATE {}.character_current_affiliation c
            SET alliance_id = r.alliance_id,
                affiliation_checked_at = NOW(),
                updated_at = NOW()
            FROM {}.esi_affiliation_result_work r
            WHERE r.run_id=%s
              AND c.character_id = r.character_id
        """).format(
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
        ), (run_id,))

        # Collapse all character affiliation rows to ONE row per corporation for
        # this run.  Alliance change detection happens here, directly from the
        # bulk affiliation result.  A missing corporation_current_affiliation row
        # is treated as known alliance=NULL, so NULL -> alliance_id is a change.
        # The (run_id, corporation_id) PK guarantees that the same corporation
        # seen in many bulk batches / on many characters is queued at most once.
        cur.execute(sql.SQL("""
            INSERT INTO {}.esi_affiliation_corporation_work (
                run_id,
                corporation_id,
                alliance_id,
                public_status,
                initial_history_status,
                refresh_status,
                known_alliance_id,
                current_alliance_id
            )
            SELECT DISTINCT ON (r.corporation_id)
                %s,
                r.corporation_id,
                r.alliance_id,
                CASE WHEN c.corporation_id IS NULL THEN 'pending' ELSE 'skipped' END,
                CASE
                    -- If the bulk already proves an alliance mismatch, STEP 7
                    -- will FORCE alliancehistory, so do not also do an initial
                    -- history GET in STEP 6.
                    WHEN c.corporation_id IS NULL
                     AND ca.alliance_id IS NOT DISTINCT FROM r.alliance_id
                    THEN 'pending'
                    ELSE 'not_applicable'
                END,
                CASE
                    WHEN COALESCE(c.is_deleted, FALSE) = FALSE
                     AND ca.alliance_id IS DISTINCT FROM r.alliance_id
                    THEN 'pending'
                    ELSE 'not_applicable'
                END,
                ca.alliance_id,
                r.alliance_id
            FROM {}.esi_affiliation_result_work r
            LEFT JOIN {}.corporations c
              ON c.corporation_id = r.corporation_id
            LEFT JOIN {}.corporation_current_affiliation ca
              ON ca.corporation_id = r.corporation_id
            WHERE r.run_id=%s
            ORDER BY r.corporation_id, r.alliance_id NULLS LAST
            ON CONFLICT (run_id, corporation_id)
            DO UPDATE SET
                alliance_id = EXCLUDED.alliance_id,
                public_status = EXCLUDED.public_status,
                initial_history_status = EXCLUDED.initial_history_status,
                refresh_status = EXCLUDED.refresh_status,
                known_alliance_id = EXCLUDED.known_alliance_id,
                current_alliance_id = EXCLUDED.current_alliance_id,
                updated_at = NOW()
        """).format(
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
        ), (run_id, run_id))

        cur.execute(sql.SQL("""
            SELECT COUNT(*)
            FROM {}.esi_affiliation_corporation_work
            WHERE run_id=%s
        """).format(sql.Identifier(ENTITIES_SCHEMA)), (run_id,))
        corp_distinct_count = int(cur.fetchone()[0])

    conn.commit()
    return char_refresh_count, corp_distinct_count


def rebuild_character_current_from_work_with_history(conn, run_id: str) -> int:
    """Reconcile CURRENT from HISTORY for the whole run, but write only real diffs.

    HISTORY remains the source of truth.  Every live character in the current
    workset that has history is checked against its latest non-deleted history
    row.  Missing CURRENT rows are inserted and existing CURRENT rows are
    updated only when corporation/source metadata is actually different.

    This preserves the old full-run safety net without rewriting millions of
    already-correct CURRENT rows on every run.
    """
    with conn.cursor() as cur:
        cur.execute(sql.SQL("""
            INSERT INTO {}.character_current_affiliation AS target (
                character_id,
                corporation_id,
                alliance_id,
                source_start_date,
                source_history_fetched_at,
                affiliation_checked_at,
                updated_at
            )
            WITH candidates AS (
                SELECT w.character_id
                FROM {}.esi_affiliation_character_work w
                JOIN {}.characters ch
                  ON ch.character_id = w.character_id
                 AND ch.is_deleted = FALSE
                WHERE w.run_id = %s
                  AND w.has_history IS TRUE
            ),
            latest AS (
                SELECT
                    c.character_id,
                    h.corporation_id,
                    h.start_date,
                    h.fetched_at
                FROM candidates c
                JOIN LATERAL (
                    SELECT
                        h.corporation_id,
                        h.start_date,
                        h.fetched_at
                    FROM {}.character_corporation_history h
                    WHERE h.character_id = c.character_id
                      AND h.is_deleted = FALSE
                    ORDER BY h.start_date DESC, h.record_id DESC
                    LIMIT 1
                ) h ON TRUE
            ),
            changed AS (
                SELECT
                    l.character_id,
                    l.corporation_id,
                    old.alliance_id,
                    l.start_date,
                    l.fetched_at,
                    old.affiliation_checked_at
                FROM latest l
                LEFT JOIN {}.character_current_affiliation old
                  ON old.character_id = l.character_id
                WHERE old.character_id IS NULL
                   OR old.corporation_id IS DISTINCT FROM l.corporation_id
                   OR old.source_start_date IS DISTINCT FROM l.start_date
                   OR old.source_history_fetched_at IS DISTINCT FROM l.fetched_at
            )
            SELECT
                c.character_id,
                c.corporation_id,
                c.alliance_id,
                c.start_date,
                c.fetched_at,
                c.affiliation_checked_at,
                NOW()
            FROM changed c
            ON CONFLICT (character_id)
            DO UPDATE SET
                corporation_id = EXCLUDED.corporation_id,
                source_start_date = EXCLUDED.source_start_date,
                source_history_fetched_at = EXCLUDED.source_history_fetched_at,
                updated_at = NOW()
            WHERE target.corporation_id IS DISTINCT FROM EXCLUDED.corporation_id
               OR target.source_start_date IS DISTINCT FROM EXCLUDED.source_start_date
               OR target.source_history_fetched_at IS DISTINCT FROM EXCLUDED.source_history_fetched_at
        """).format(
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
        ), (run_id,))
        rows = int(cur.rowcount)

    conn.commit()
    return rows


def populate_corporation_refresh_work(conn, run_id: str) -> int:
    """Queue the UNIQUE corporations whose alliance mismatch was detected at STEP 5."""
    with conn.cursor() as cur:
        cur.execute(sql.SQL("""
            SELECT COUNT(*)
            FROM {}.esi_affiliation_corporation_work
            WHERE run_id=%s
              AND refresh_status='pending'
        """).format(sql.Identifier(ENTITIES_SCHEMA)), (run_id,))
        queued = int(cur.fetchone()[0])

        cur.execute(sql.SQL("""
            INSERT INTO {}.corporation_alliance_history_refresh_queue (
                corporation_id,
                reason,
                detected_at,
                known_alliance_id,
                current_alliance_id,
                processed_at
            )
            SELECT
                w.corporation_id,
                'corporation_alliance_changed_from_character_affiliation',
                NOW(),
                w.known_alliance_id,
                w.current_alliance_id,
                NULL
            FROM {}.esi_affiliation_corporation_work w
            WHERE w.run_id=%s
              AND w.refresh_status='pending'
            ON CONFLICT (corporation_id)
            DO UPDATE SET
                reason = EXCLUDED.reason,
                detected_at = NOW(),
                known_alliance_id = EXCLUDED.known_alliance_id,
                current_alliance_id = EXCLUDED.current_alliance_id,
                processed_at = NULL
        """).format(
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
        ), (run_id,))

    conn.commit()
    return queued


def populate_unknown_alliance_work(conn, run_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("""
            INSERT INTO {}.esi_affiliation_alliance_work (run_id, alliance_id, public_status)
            SELECT DISTINCT
                %s::uuid,
                x.alliance_id,
                'pending'
            FROM (
                SELECT alliance_id
                FROM {}.esi_affiliation_result_work
                WHERE run_id=%s AND alliance_id IS NOT NULL

                UNION

                SELECT c.alliance_id
                FROM {}.corporation_current_affiliation c
                JOIN {}.esi_affiliation_corporation_work w
                  ON w.corporation_id = c.corporation_id
                 AND w.run_id=%s
                WHERE c.alliance_id IS NOT NULL
            ) x
            LEFT JOIN {}.alliances al
              ON al.alliance_id = x.alliance_id
            WHERE al.alliance_id IS NULL
            ON CONFLICT (run_id, alliance_id) DO NOTHING
        """).format(
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
        ), (run_id, run_id, run_id))
        rows = int(cur.rowcount)
    conn.commit()
    return rows


def mark_character_queue_processed_from_work(conn, run_id: str) -> tuple[int, int]:
    """
    Mark a targeted refresh processed when history now matches affiliation, or
    when the character became terminally Deleted during the forced verification.
    """
    with conn.cursor() as cur:
        cur.execute(sql.SQL("""
            UPDATE {}.character_history_refresh_queue q
            SET processed_at = NOW()
            FROM {}.esi_affiliation_character_work w
            JOIN {}.esi_affiliation_result_work r
              ON r.run_id = w.run_id
             AND r.character_id = w.character_id
            JOIN {}.character_current_affiliation c
              ON c.character_id = w.character_id
            LEFT JOIN {}.characters ch
              ON ch.character_id = w.character_id
            WHERE q.character_id = w.character_id
              AND w.run_id=%s
              AND (
                    (
                        w.history_refresh_status = 'loaded'
                        AND c.corporation_id IS NOT DISTINCT FROM r.corporation_id
                    )
                    OR (
                        COALESCE(ch.is_deleted, FALSE) = TRUE
                        AND c.corporation_id = %s
                    )
              )
        """).format(
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
        ), (run_id, DELETED_CORPORATION_ID))
        matched = int(cur.rowcount)

        cur.execute(sql.SQL("""
            SELECT COUNT(*)
            FROM {}.esi_affiliation_character_work w
            JOIN {}.esi_affiliation_result_work r
              ON r.run_id = w.run_id
             AND r.character_id = w.character_id
            LEFT JOIN {}.character_current_affiliation c
              ON c.character_id = w.character_id
            LEFT JOIN {}.characters ch
              ON ch.character_id = w.character_id
            WHERE w.run_id=%s
              AND w.history_refresh_status NOT IN ('not_applicable', 'deleted', 'deleted_skip')
              AND COALESCE(ch.is_deleted, FALSE) = FALSE
              AND c.corporation_id IS DISTINCT FROM r.corporation_id
        """).format(
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
        ), (run_id,))
        unresolved = int(cur.fetchone()[0])

    conn.commit()
    return matched, unresolved


def mark_corporation_queue_processed_from_work(conn, run_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("""
            UPDATE {}.corporation_alliance_history_refresh_queue q
            SET processed_at = NOW()
            FROM {}.esi_affiliation_corporation_work w
            WHERE q.corporation_id = w.corporation_id
              AND w.run_id=%s
              AND w.refresh_status IN ('loaded', 'unchanged_304', 'skipped_cache', 'deleted_skip', 'not_found')
        """).format(
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
        ), (run_id,))
    conn.commit()


def add_status_counts(summary: dict[str, int], stats: Stats, *, kind: str) -> None:
    if kind == "public_character":
        stats.public_character_loaded += summary.get("loaded", 0)
        stats.public_character_deleted += summary.get("deleted", 0)
        stats.public_character_skipped += summary.get("skipped", 0)
        stats.public_character_failed += summary.get("failed", 0)
    elif kind == "initial_character_history":
        ok = sum(summary.get(s, 0) for s in ("loaded", "unchanged_304", "skipped_cache", "deleted", "deleted_skip"))
        stats.initial_character_history_loaded += ok
        stats.initial_character_history_failed += summary.get("failed", 0) + summary.get("not_found", 0)
    elif kind == "targeted_character_history":
        ok = sum(summary.get(s, 0) for s in ("loaded", "unchanged_304", "skipped_cache", "deleted", "deleted_skip"))
        stats.refreshed_character_history += ok
        stats.refreshed_character_history_failed += summary.get("failed", 0) + summary.get("not_found", 0)
    elif kind == "public_corporation":
        stats.public_corporations_loaded += summary.get("loaded", 0)
        stats.public_corporations_deleted += summary.get("deleted", 0)
    elif kind == "corporation_history":
        ok = sum(summary.get(s, 0) for s in ("loaded", "unchanged_304", "skipped_cache", "deleted_skip"))
        stats.corporation_history_loaded += ok
        stats.corporation_history_failed += summary.get("failed", 0) + summary.get("not_found", 0)
    elif kind == "targeted_corporation_history":
        ok = sum(summary.get(s, 0) for s in ("loaded", "unchanged_304", "skipped_cache", "deleted_skip"))
        stats.refreshed_corporation_history += ok
        stats.refreshed_corporation_history_failed += summary.get("failed", 0) + summary.get("not_found", 0)
    elif kind == "public_alliance":
        stats.public_alliances_loaded += summary.get("loaded", 0)
        stats.public_alliances_deleted += summary.get("deleted", 0)
        stats.public_alliances_failed += summary.get("failed", 0)


# ============================================================
# MAIN BUILD
# ============================================================


def build(args) -> None:
    run_id = str(uuid.uuid4())
    stats = Stats()

    logger.info("Starting esi_affiliation_refresh run_id=%s", run_id)
    logger.info("Log file: %s", LOG_FILE)
    logger.info("Source: %s.%s", args.source_table, args.source_column)
    logger.info("Headers: User-Agent=%s | X-Compatibility-Date=%s", ESI_USER_AGENT, ESI_COMPATIBILITY_DATE)
    logger.info("TABLE-FIRST OPTIMIZED MODE: one-pass work materialization + streamed parallel affiliation bulk")

    conn = db_connect()
    try:
        ensure_tables(conn)
        purge_work_tables(conn)
        create_run_row(conn, run_id, args.source_table, args.source_column)

        stats.source_ids = populate_character_work(conn, run_id, args.source_table, args.source_column, args.limit)
        logger.info("Loaded source character IDs into persistent work table=%s", stats.source_ids)
        if stats.source_ids <= 0:
            logger.info("No source IDs")
            finish_run_row(conn, run_id, "done")
            return

        # STEP 1: public character missing only.
        missing_public = prepare_public_character_work(conn, run_id)
        stats.public_character_skipped += stats.source_ids - missing_public
        logger.info("STEP 1 public characters missing=%s", missing_public)

        summary = run_character_stage_from_table(
            conn,
            run_id=run_id,
            status_column="public_status",
            process_batch_size=args.process_batch_size,
            workers=args.workers,
            fn=lambda cid: fetch_public_character(run_id, cid),
            label="public_character",
            http_status_column="public_http_status",
            error_column="public_error",
            processed_column="public_processed_at",
            progress_total=missing_public,
        )
        add_status_counts(summary, stats, kind="public_character")

        # STEP 2: split with/without history.
        stats.with_history, stats.without_history = refresh_character_history_flags(conn, run_id)
        logger.info("STEP 2 split with_history=%s without_history=%s", stats.with_history, stats.without_history)

        # STEP 3: initial history for without_history only.
        logger.info("STEP 3 initial character histories=%s", stats.without_history)
        summary = run_character_stage_from_table(
            conn,
            run_id=run_id,
            status_column="initial_history_status",
            process_batch_size=args.process_batch_size,
            workers=args.workers,
            fn=lambda cid: fetch_character_history(run_id, cid),
            label="initial_character_history",
            http_status_column="initial_history_http_status",
            error_column="initial_history_error",
            processed_column="initial_history_processed_at",
            progress_total=stats.without_history,
        )
        add_status_counts(summary, stats, kind="initial_character_history")

        # Re-split after initial history, then rebuild current from history in SQL.
        stats.with_history, stats.without_history = refresh_character_history_flags(conn, run_id)
        rebuilt_current = rebuild_character_current_from_work_with_history(conn, run_id)
        logger.info(
            "After initial load with_history=%s still_without_history=%s rebuilt_character_current=%s",
            stats.with_history,
            stats.without_history,
            rebuilt_current,
        )

        # STEP 4: optimized streamed / parallel affiliation bulk.
        run_affiliation_bulk_optimized(conn, run_id, args, stats)

        # STEP 5: SQL comparisons after affiliation.
        stats.queued_character_history, corp_distinct_count = process_affiliation_results_sql(conn, run_id)
        logger.info(
            "STEP 5 affiliation SQL compare queued_character_history=%s distinct_corps=%s aff_rows=%s",
            stats.queued_character_history,
            corp_distinct_count,
            stats.affiliation_rows,
        )

        summary = run_character_stage_from_table(
            conn,
            run_id=run_id,
            status_column="history_refresh_status",
            process_batch_size=args.process_batch_size,
            workers=args.workers,
            fn=lambda cid: fetch_character_history(run_id, cid, force=True),
            label="targeted_character_history",
            http_status_column="history_refresh_http_status",
            error_column="history_refresh_error",
            processed_column="history_refresh_processed_at",
            progress_total=stats.queued_character_history,
        )
        add_status_counts(summary, stats, kind="targeted_character_history")
        verified_character_history, unresolved_character_history = mark_character_queue_processed_from_work(conn, run_id)
        logger.info(
            "STEP 5 targeted character history verification matched=%s unresolved=%s",
            verified_character_history,
            unresolved_character_history,
        )

        # STEP 6: unknown corporations from DISTINCT affiliation corps.
        stats.unknown_corporations = count_status(conn, "esi_affiliation_corporation_work", run_id, "public_status", "pending")
        logger.info("STEP 6 unknown corporations=%s", stats.unknown_corporations)

        summary = run_corporation_stage_from_table(
            conn,
            run_id=run_id,
            status_column="public_status",
            process_batch_size=args.process_batch_size,
            workers=args.workers,
            fn=lambda cid: fetch_public_corporation(run_id, cid),
            label="public_corporation",
            http_status_column="public_http_status",
            error_column="public_error",
            processed_column="public_processed_at",
        )
        add_status_counts(summary, stats, kind="public_corporation")

        summary = run_corporation_stage_from_table(
            conn,
            run_id=run_id,
            status_column="initial_history_status",
            process_batch_size=args.process_batch_size,
            workers=args.workers,
            fn=lambda cid: fetch_corporation_history(run_id, cid, force=False),
            label="initial_corporation_history",
            http_status_column="initial_history_http_status",
            error_column="initial_history_error",
            processed_column="initial_history_processed_at",
        )
        add_status_counts(summary, stats, kind="corporation_history")

        # STEP 7: process UNIQUE corporation alliance mismatches detected from the bulk at STEP 5.
        stats.queued_corporation_history = populate_corporation_refresh_work(conn, run_id)
        logger.info("STEP 7 corporation alliance history refresh queued=%s", stats.queued_corporation_history)

        summary = run_corporation_stage_from_table(
            conn,
            run_id=run_id,
            status_column="refresh_status",
            process_batch_size=args.process_batch_size,
            workers=args.workers,
            fn=lambda cid: fetch_corporation_history(run_id, cid, force=True),
            label="targeted_corporation_history",
            http_status_column="refresh_http_status",
            error_column="refresh_error",
            processed_column="refresh_processed_at",
        )
        add_status_counts(summary, stats, kind="targeted_corporation_history")
        mark_corporation_queue_processed_from_work(conn, run_id)

        # STEP 8: unknown alliances from affiliation + corporation current.
        stats.unknown_alliances = populate_unknown_alliance_work(conn, run_id)
        logger.info("STEP 8 unknown alliances=%s", stats.unknown_alliances)

        summary = run_alliance_stage_from_table(
            conn,
            run_id=run_id,
            process_batch_size=args.process_batch_size,
            workers=args.workers,
            fn=lambda aid: fetch_public_alliance(run_id, aid),
            label="public_alliance",
        )
        add_status_counts(summary, stats, kind="public_alliance")

        finish_run_row(conn, run_id, "done")

        logger.info(
            "DONE run_id=%s source_ids=%s public_char_loaded=%s public_char_deleted=%s public_char_failed=%s "
            "with_history=%s without_history=%s initial_hist_ok=%s initial_hist_failed=%s "
            "aff_batches=%s aff_304=%s aff_200=%s aff_failed=%s aff_rows=%s "
            "queued_char_hist=%s refreshed_char_hist=%s refreshed_char_hist_failed=%s "
            "unknown_corps=%s corp_public_loaded=%s corp_public_deleted=%s corp_hist_loaded=%s corp_hist_failed=%s "
            "queued_corp_hist=%s refreshed_corp_hist=%s refreshed_corp_hist_failed=%s "
            "unknown_alliances=%s alliance_loaded=%s alliance_deleted=%s alliance_failed=%s",
            run_id,
            stats.source_ids,
            stats.public_character_loaded,
            stats.public_character_deleted,
            stats.public_character_failed,
            stats.with_history,
            stats.without_history,
            stats.initial_character_history_loaded,
            stats.initial_character_history_failed,
            stats.affiliation_batches,
            stats.affiliation_304,
            stats.affiliation_200,
            stats.affiliation_failed,
            stats.affiliation_rows,
            stats.queued_character_history,
            stats.refreshed_character_history,
            stats.refreshed_character_history_failed,
            stats.unknown_corporations,
            stats.public_corporations_loaded,
            stats.public_corporations_deleted,
            stats.corporation_history_loaded,
            stats.corporation_history_failed,
            stats.queued_corporation_history,
            stats.refreshed_corporation_history,
            stats.refreshed_corporation_history_failed,
            stats.unknown_alliances,
            stats.public_alliances_loaded,
            stats.public_alliances_deleted,
            stats.public_alliances_failed,
        )

    except Exception as exc:
        try:
            finish_run_row(conn, run_id, "failed", error=f"{type(exc).__name__}: {exc}")
        except Exception:
            pass
        raise
    finally:
        conn.close()


# ============================================================
# CLI
# ============================================================


def main() -> None:
    configure_logging()

    parser = argparse.ArgumentParser(description="Optimized ESI character affiliation refresh pipeline.")
    parser.add_argument(
        "--source-table",
        default=DEFAULT_SOURCE_TABLE,
        help=f"Source table containing character IDs, format schema.table. Default: {DEFAULT_SOURCE_TABLE}",
    )
    parser.add_argument(
        "--source-column",
        default=DEFAULT_SOURCE_COLUMN,
        help=f"Source column containing character IDs. Default: {DEFAULT_SOURCE_COLUMN}",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Optional limit for testing.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Parallel workers. Default: {DEFAULT_WORKERS}",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Affiliation batch size. Default: {DEFAULT_BATCH_SIZE}. Must be <= 999.",
    )
    parser.add_argument(
        "--process-batch-size",
        type=int,
        default=DEFAULT_PROCESS_BATCH_SIZE,
        help=f"Chunk size for DB-driven worker claims. Default: {DEFAULT_PROCESS_BATCH_SIZE}.",
    )
    parser.add_argument(
        "--esi-max-calls-per-minute",
        type=int,
        default=DEFAULT_ESI_MAX_CALLS_PER_MINUTE,
        help=(
            "Global ESI HTTP call limit shared by all workers. "
            f"Default: {DEFAULT_ESI_MAX_CALLS_PER_MINUTE} to stay below 300/min."
        ),
    )

    args = parser.parse_args()

    if args.limit is not None and args.limit <= 0:
        raise RuntimeError("--limit must be greater than 0")
    if args.workers <= 0:
        raise RuntimeError("--workers must be greater than 0")
    if args.batch_size <= 0 or args.batch_size > 999:
        raise RuntimeError("--batch-size must be between 1 and 999")
    if args.process_batch_size <= 0:
        raise RuntimeError("--process-batch-size must be greater than 0")
    if args.esi_max_calls_per_minute <= 0:
        raise RuntimeError("--esi-max-calls-per-minute must be greater than 0")
    if args.esi_max_calls_per_minute > 300:
        raise RuntimeError("--esi-max-calls-per-minute must be <= 300")

    configure_esi_rate_limiter(args.esi_max_calls_per_minute)
    build(args)


if __name__ == "__main__":
    main()
