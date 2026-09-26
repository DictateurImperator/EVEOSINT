#!/usr/bin/env python3
"""
esi_entities_backfill_audit.py

EVEOSINT — backfill des corporations et alliances depuis les histories.

But :
  - Compléter entities.corporations depuis entities.character_corporation_history.
  - Compléter entities.corporation_alliance_history pour les corporations réellement manquantes.
  - Compléter entities.alliances depuis entities.corporation_alliance_history.

Important :
  - Aucun audit massif des corporations sans membre courant.
  - Aucun audit massif des alliances sans présence courante.
  - Le script ne traite que les entités/historiques réellement manquants découverts par le référentiel historique.

Règles :
  - Ce script est séparé de esi_affiliation_refresh.py.
  - Les gros ensembles sont en TEMP TABLE SQL.
  - Les tables temporaires sont explicitement drop au début et à la fin.
  - Pas de DELETE/TRUNCATE/DROP métier.
  - entities.* = données métier/référentiel.
  - esi.* = cache et logs techniques.
  - Les tables current sont des caches dérivés et sont mises à jour APRES les histories.
  - Les corporations/alliances is_deleted=true ne sont plus vérifiées.

Endpoints utilisés :
  GET /corporations/{corporation_id}/
  GET /corporations/{corporation_id}/alliancehistory/
  GET /alliances/{alliance_id}/
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

import psycopg2
from psycopg2 import sql
import requests


# ============================================================
# CONFIG
# ============================================================

CONFIG_PATH = Path.home() / "eveosint" / "config" / "db.json"
BASE_DIR = Path.home() / "eveosint"
LOG_DIR = BASE_DIR / "data" / "logs"
LOG_FILE = LOG_DIR / "esi_entities_backfill_audit.log"

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

DEFAULT_WORKERS = 2
DEFAULT_CHUNK_SIZE = 1000

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
    history_corporations: int = 0
    unknown_corporations: int = 0
    new_corporations_excluded: int = 0

    public_corp_loaded: int = 0
    public_corp_deleted: int = 0
    public_corp_skipped: int = 0
    public_corp_failed: int = 0

    corp_history_loaded: int = 0
    corp_history_skipped: int = 0
    corp_history_deleted_skip: int = 0
    corp_history_failed: int = 0


    history_alliances: int = 0
    unknown_alliances: int = 0
    new_alliances_excluded: int = 0
    public_alliance_loaded: int = 0
    public_alliance_deleted: int = 0
    public_alliance_skipped: int = 0
    public_alliance_failed: int = 0



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


def ensure_required_tables(conn) -> None:
    required = {
        "character_corporation_history": ["character_id", "corporation_id", "start_date", "record_id", "is_deleted", "fetched_at"],
        "character_current_affiliation": ["character_id", "corporation_id"],
        "corporations": ["corporation_id", "is_deleted"],
        "corporation_alliance_history": ["corporation_id", "alliance_id", "start_date", "record_id", "is_deleted", "fetched_at"],
        "corporation_current_affiliation": ["corporation_id", "alliance_id"],
        "alliances": ["alliance_id", "is_deleted"],
    }

    for table, columns in required.items():
        if not table_exists(conn, ENTITIES_SCHEMA, table):
            raise RuntimeError(f"Missing table {ENTITIES_SCHEMA}.{table}")
        missing = [c for c in columns if not column_exists(conn, ENTITIES_SCHEMA, table, c)]
        if missing:
            raise RuntimeError(f"Missing columns in {ENTITIES_SCHEMA}.{table}: {', '.join(missing)}")


def ensure_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(ENTITIES_SCHEMA)))
        cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(ESI_SCHEMA)))

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
            method TEXT NOT NULL DEFAULT 'GET',
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

        # Migrations non destructives pour tables techniques créées par anciens scripts.
        cur.execute(sql.SQL("ALTER TABLE {}.call_log ADD COLUMN IF NOT EXISTS method TEXT NOT NULL DEFAULT 'GET';").format(sql.Identifier(ESI_SCHEMA)))
        cur.execute(sql.SQL("ALTER TABLE {}.call_log ALTER COLUMN entity_id DROP NOT NULL;").format(sql.Identifier(ESI_SCHEMA)))
        cur.execute(sql.SQL("ALTER TABLE {}.endpoint_cache ALTER COLUMN entity_id DROP NOT NULL;").format(sql.Identifier(ESI_SCHEMA)))

        cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS corp_deleted_idx ON {}.corporations (is_deleted);").format(sql.Identifier(ENTITIES_SCHEMA)))
        cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS corp_hist_corp_start_idx ON {}.corporation_alliance_history (corporation_id, start_date);").format(sql.Identifier(ENTITIES_SCHEMA)))
        cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS corp_current_alliance_idx ON {}.corporation_current_affiliation (alliance_id);").format(sql.Identifier(ENTITIES_SCHEMA)))
        cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS alliance_deleted_idx ON {}.alliances (is_deleted);").format(sql.Identifier(ENTITIES_SCHEMA)))
        cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS esi_cache_entity_idx ON {}.endpoint_cache (entity_type, entity_id);").format(sql.Identifier(ESI_SCHEMA)))

    conn.commit()


# ============================================================
# TEMP TABLES
# ============================================================


TEMP_TABLES = [
    "tmp_history_corporations",
    "tmp_unknown_corporations",
    "tmp_new_corporations",
    "tmp_history_alliances",
    "tmp_unknown_alliances",
    "tmp_new_alliances",
]


def drop_temp_tables(conn) -> None:
    with conn.cursor() as cur:
        for table in TEMP_TABLES:
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(table)))
    conn.commit()


def create_temp_tables(conn) -> None:
    drop_temp_tables(conn)

    with conn.cursor() as cur:
        cur.execute("""
        CREATE TEMP TABLE tmp_history_corporations (
            corporation_id BIGINT PRIMARY KEY
        ) ON COMMIT PRESERVE ROWS;
        """)

        cur.execute("""
        CREATE TEMP TABLE tmp_unknown_corporations (
            corporation_id BIGINT PRIMARY KEY
        ) ON COMMIT PRESERVE ROWS;
        """)

        cur.execute("""
        CREATE TEMP TABLE tmp_new_corporations (
            corporation_id BIGINT PRIMARY KEY
        ) ON COMMIT PRESERVE ROWS;
        """)


        cur.execute("""
        CREATE TEMP TABLE tmp_history_alliances (
            alliance_id BIGINT PRIMARY KEY
        ) ON COMMIT PRESERVE ROWS;
        """)

        cur.execute("""
        CREATE TEMP TABLE tmp_unknown_alliances (
            alliance_id BIGINT PRIMARY KEY
        ) ON COMMIT PRESERVE ROWS;
        """)

        cur.execute("""
        CREATE TEMP TABLE tmp_new_alliances (
            alliance_id BIGINT PRIMARY KEY
        ) ON COMMIT PRESERVE ROWS;
        """)


    conn.commit()


def count_temp(conn, table: str) -> int:
    if not IDENT_RE.match(table):
        raise RuntimeError(f"Invalid temp table name: {table}")

    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table)))
        return int(cur.fetchone()[0])


def fill_history_corporations(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            INSERT INTO tmp_history_corporations (corporation_id)
            SELECT DISTINCT corporation_id
            FROM {}.character_corporation_history
            WHERE corporation_id IS NOT NULL
            ON CONFLICT DO NOTHING
            """).format(sql.Identifier(ENTITIES_SCHEMA))
        )
    conn.commit()
    return count_temp(conn, "tmp_history_corporations")


def fill_unknown_corporations(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            INSERT INTO tmp_unknown_corporations (corporation_id)
            SELECT hc.corporation_id
            FROM tmp_history_corporations hc
            LEFT JOIN {}.corporations c
              ON c.corporation_id = hc.corporation_id
            WHERE c.corporation_id IS NULL
            ON CONFLICT DO NOTHING
            """).format(sql.Identifier(ENTITIES_SCHEMA))
        )

        # Exclusion temporaire du même run.
        cur.execute("""
        INSERT INTO tmp_new_corporations (corporation_id)
        SELECT corporation_id
        FROM tmp_unknown_corporations
        ON CONFLICT DO NOTHING
        """)
    conn.commit()
    return count_temp(conn, "tmp_unknown_corporations")



def fill_history_alliances(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("TRUNCATE tmp_history_alliances;")
        cur.execute(
            sql.SQL("""
            INSERT INTO tmp_history_alliances (alliance_id)
            SELECT DISTINCT alliance_id
            FROM {}.corporation_alliance_history
            WHERE alliance_id IS NOT NULL
            ON CONFLICT DO NOTHING
            """).format(sql.Identifier(ENTITIES_SCHEMA))
        )
    conn.commit()
    return count_temp(conn, "tmp_history_alliances")


def fill_unknown_alliances(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("TRUNCATE tmp_unknown_alliances;")
        cur.execute(
            sql.SQL("""
            INSERT INTO tmp_unknown_alliances (alliance_id)
            SELECT ha.alliance_id
            FROM tmp_history_alliances ha
            LEFT JOIN {}.alliances a
              ON a.alliance_id = ha.alliance_id
            WHERE a.alliance_id IS NULL
            ON CONFLICT DO NOTHING
            """).format(sql.Identifier(ENTITIES_SCHEMA))
        )

        # Exclusion temporaire du même run.
        cur.execute("""
        INSERT INTO tmp_new_alliances (alliance_id)
        SELECT alliance_id
        FROM tmp_unknown_alliances
        ON CONFLICT DO NOTHING
        """)
    conn.commit()
    return count_temp(conn, "tmp_unknown_alliances")



def iter_temp_ids(conn, table: str, column: str, chunk_size: int):
    if not IDENT_RE.match(table) or not IDENT_RE.match(column):
        raise RuntimeError(f"Invalid temp iterator: {table}.{column}")

    last_id = -1

    while True:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("""
                SELECT {column}
                FROM {table}
                WHERE {column} > %s
                ORDER BY {column}
                LIMIT %s
                """).format(
                    column=sql.Identifier(column),
                    table=sql.Identifier(table),
                ),
                (last_id, chunk_size),
            )
            rows = [int(r[0]) for r in cur.fetchall()]

        if not rows:
            break

        yield rows
        last_id = rows[-1]


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


def parse_esi_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


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
                run_id,
                entity_type,
                entity_id,
                endpoint,
                url,
                method,
                called_at,
                http_status,
                etag,
                expires_at,
                error_limit_remain,
                error_limit_reset,
                ESI_COMPATIBILITY_DATE,
                ESI_USER_AGENT,
                error,
            ),
        )
    conn.commit()


# ============================================================
# HTTP ESI
# ============================================================


def build_headers(etag: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": ESI_USER_AGENT,
        "Accept": "application/json",
        "X-Compatibility-Date": ESI_COMPATIBILITY_DATE,
    }
    if etag:
        headers["If-None-Match"] = etag
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
        reason,
        attempt,
        seconds,
        reset,
        retry_after,
    )
    time.sleep(seconds)


def esi_get(
    *,
    conn,
    session: requests.Session,
    run_id: str,
    endpoint: str,
    entity_type: str,
    entity_id: int,
    etag: str | None,
) -> HttpResult:
    url = f"{ESI_BASE}{endpoint}"
    params = {"datasource": DATASOURCE}

    for attempt in range(1, MAX_ATTEMPTS + 1):
        called_at = utc_now()
        try:
            response = session.get(
                url,
                params=params,
                headers=build_headers(etag),
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            insert_call_log(
                conn,
                run_id=run_id,
                entity_type=entity_type,
                entity_id=entity_id,
                endpoint=endpoint,
                url=url,
                method="GET",
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
            method="GET",
            called_at=called_at,
            http_status=status,
            etag=new_etag,
            expires_at=expires_at,
            error_limit_remain=remain,
            error_limit_reset=reset,
            error=None,
        )

        logger.info(
            "CALL endpoint=%s entity_type=%s entity_id=%s status=%s remain=%s reset=%s",
            endpoint,
            entity_type,
            entity_id,
            status,
            remain,
            reset,
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

    raise RuntimeError(f"Max attempts reached endpoint={endpoint}")


# ============================================================
# ENTITY HELPERS
# ============================================================


def corporation_exists(conn, corporation_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT EXISTS (SELECT 1 FROM {}.corporations WHERE corporation_id=%s)").format(sql.Identifier(ENTITIES_SCHEMA)),
            (corporation_id,),
        )
        return bool(cur.fetchone()[0])


def corporation_is_deleted(conn, corporation_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT is_deleted FROM {}.corporations WHERE corporation_id=%s").format(sql.Identifier(ENTITIES_SCHEMA)),
            (corporation_id,),
        )
        row = cur.fetchone()
    return bool(row[0]) if row else False


def corporation_has_history(conn, corporation_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT EXISTS (SELECT 1 FROM {}.corporation_alliance_history WHERE corporation_id=%s)").format(sql.Identifier(ENTITIES_SCHEMA)),
            (corporation_id,),
        )
        return bool(cur.fetchone()[0])


def alliance_exists(conn, alliance_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT EXISTS (SELECT 1 FROM {}.alliances WHERE alliance_id=%s)").format(sql.Identifier(ENTITIES_SCHEMA)),
            (alliance_id,),
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


# ============================================================
# WRITE ENTITIES
# ============================================================


def upsert_corporation(conn, corporation_id: int, payload: dict[str, Any], fetched_at: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            INSERT INTO {}.corporations (
                corporation_id,
                name,
                ticker,
                date_founded,
                is_deleted,
                fetched_at,
                updated_at
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
                corporation_id,
                name,
                ticker,
                date_founded,
                is_deleted,
                fetched_at,
                updated_at
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

            # L'ancienne appli gérait record_id, mais par prudence on supporte absence.
            record_id = int(row.get("record_id", i))
            start_dt = parse_esi_datetime(row["start_date"])
            end_dt = parse_esi_datetime(rows[i + 1]["start_date"]) if i + 1 < len(rows) else None

            cur.execute(
                sql.SQL("""
                INSERT INTO {}.corporation_alliance_history (
                    corporation_id,
                    record_id,
                    alliance_id,
                    start_date,
                    end_date,
                    is_deleted,
                    fetched_at,
                    updated_at
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

    # Current APRES history.
    rebuild_corporation_current(conn, [corporation_id])
    return affected


def upsert_alliance(conn, alliance_id: int, payload: dict[str, Any], fetched_at: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            INSERT INTO {}.alliances (
                alliance_id,
                name,
                ticker,
                date_founded,
                is_deleted,
                fetched_at,
                updated_at
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
                alliance_id,
                name,
                ticker,
                date_founded,
                is_deleted,
                fetched_at,
                updated_at
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


def fetch_public_corporation(run_id: str, corporation_id: int, force_status_check: bool = False) -> str:
    conn = db_connect()
    session = requests.Session()

    try:
        if corporation_is_deleted(conn, corporation_id):
            return "deleted_skip"

        if corporation_exists(conn, corporation_id) and not force_status_check:
            return "skipped"

        endpoint = f"/corporations/{corporation_id}/"
        cache = get_cache(conn, endpoint)

        if cache and cache.last_status == 404:
            mark_corporation_deleted(conn, corporation_id, utc_now())
            return "deleted"

        if cache_is_fresh(cache):
            return "skipped_cache"

        result = esi_get(
            conn=conn,
            session=session,
            run_id=run_id,
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

        if result.status_code == 304:
            return "unchanged_304"

        if result.status_code == 404:
            mark_corporation_deleted(conn, corporation_id, now)
            return "deleted"

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
                corporation_id,
                endpoint,
            )
            # Cache technique incohérent : on le retire pour pouvoir refaire le call.
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("DELETE FROM {}.endpoint_cache WHERE endpoint=%s").format(sql.Identifier(ESI_SCHEMA)),
                    (endpoint,),
                )
            conn.commit()
            cache = None

        result = esi_get(
            conn=conn,
            session=session,
            run_id=run_id,
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

        if result.status_code == 304:
            return "unchanged_304"

        if result.status_code == 200:
            if not isinstance(result.payload, list):
                raise RuntimeError(f"Invalid corporation history payload corporation_id={corporation_id}")
            upsert_corporation_history(conn, corporation_id, result.payload, now)
            return "loaded"

        if result.status_code == 404:
            logger.warning("Corporation alliance history 404 corporation_id=%s payload=%s", corporation_id, result.payload)
            return "not_found"

        raise RuntimeError(f"Unexpected corporation history status corporation_id={corporation_id} status={result.status_code} payload={result.payload}")

    finally:
        session.close()
        conn.close()


def fetch_public_alliance(run_id: str, alliance_id: int, force_status_check: bool = False) -> str:
    conn = db_connect()
    session = requests.Session()

    try:
        if alliance_is_deleted(conn, alliance_id):
            return "deleted_skip"

        if alliance_exists(conn, alliance_id) and not force_status_check:
            return "skipped"

        endpoint = f"/alliances/{alliance_id}/"
        cache = get_cache(conn, endpoint)

        if cache and cache.last_status == 404:
            mark_alliance_deleted(conn, alliance_id, utc_now())
            return "deleted"

        if cache_is_fresh(cache):
            return "skipped_cache"

        result = esi_get(
            conn=conn,
            session=session,
            run_id=run_id,
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

        if result.status_code == 304:
            return "unchanged_304"

        if result.status_code == 404:
            mark_alliance_deleted(conn, alliance_id, now)
            return "deleted"

        raise RuntimeError(f"Unexpected alliance status alliance_id={alliance_id} status={result.status_code} payload={result.payload}")

    finally:
        session.close()
        conn.close()


# ============================================================
# PARALLEL TEMP PROCESSING
# ============================================================


def process_ids_parallel(
    *,
    conn,
    table: str,
    column: str,
    chunk_size: int,
    workers: int,
    fn: Callable[[int], str],
    label: str,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    total = count_temp(conn, table)
    processed = 0

    if total == 0:
        logger.info("PROGRESS stage=%s processed=0 total=0", label)
        return counts

    for ids in iter_temp_ids(conn, table, column, chunk_size):
        logger.info("Processing %s chunk size=%s first=%s last=%s", label, len(ids), ids[0], ids[-1])

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {executor.submit(fn, item): item for item in ids}

            try:
                for fut in as_completed(future_map):
                    item = future_map[fut]
                    try:
                        status = fut.result()
                    except Exception as exc:
                        logger.exception("FAILED %s id=%s error=%s", label, item, exc)
                        status = "failed"

                    counts[status] = counts.get(status, 0) + 1

            except KeyboardInterrupt:
                logger.warning("KeyboardInterrupt: cancelling pending %s futures", label)
                for fut in future_map:
                    fut.cancel()
                raise

        processed += len(ids)
        logger.info("PROGRESS stage=%s processed=%s total=%s", label, processed, total)

    return counts


def count_status(counts: dict[str, int], *statuses: str) -> int:
    return sum(counts.get(s, 0) for s in statuses)


# ============================================================
# BUILD
# ============================================================


def build(args) -> None:
    run_id = str(uuid.uuid4())
    stats = Stats()

    logger.info("Starting esi_entities_backfill_audit run_id=%s", run_id)
    logger.info("Log file: %s", LOG_FILE)
    logger.info("Headers: User-Agent=%s | X-Compatibility-Date=%s", ESI_USER_AGENT, ESI_COMPATIBILITY_DATE)

    conn = db_connect()

    try:
        ensure_tables(conn)
        # DDL uniquement ici, jamais dans les workers : évite les deadlocks ALTER/INSERT concurrents.
        ensure_required_tables(conn)
        create_temp_tables(conn)

        # STEP 1 — corporations depuis character_corporation_history.
        stats.history_corporations = fill_history_corporations(conn)
        logger.info("STEP 1 history corporations=%s", stats.history_corporations)

        # STEP 2 — corporations inconnues + exclusion temporaire.
        stats.unknown_corporations = fill_unknown_corporations(conn)
        stats.new_corporations_excluded = count_temp(conn, "tmp_new_corporations")
        logger.info(
            "STEP 2 unknown corporations=%s new_excluded=%s",
            stats.unknown_corporations,
            stats.new_corporations_excluded,
        )

        corp_public_counts = process_ids_parallel(
            conn=conn,
            table="tmp_unknown_corporations",
            column="corporation_id",
            chunk_size=args.chunk_size,
            workers=args.workers,
            fn=lambda cid: fetch_public_corporation(run_id, cid, force_status_check=False),
            label="public_corporation_unknown",
        )
        stats.public_corp_loaded = count_status(corp_public_counts, "loaded")
        stats.public_corp_deleted = count_status(corp_public_counts, "deleted")
        stats.public_corp_skipped = count_status(corp_public_counts, "skipped", "skipped_cache", "unchanged_304", "deleted_skip")
        stats.public_corp_failed = count_status(corp_public_counts, "failed")

        corp_history_counts = process_ids_parallel(
            conn=conn,
            table="tmp_unknown_corporations",
            column="corporation_id",
            chunk_size=args.chunk_size,
            workers=args.workers,
            fn=lambda cid: fetch_corporation_history(run_id, cid, force=False),
            label="corporation_history_unknown",
        )
        stats.corp_history_loaded = count_status(corp_history_counts, "loaded", "unchanged_304")
        stats.corp_history_skipped = count_status(corp_history_counts, "skipped_cache")
        stats.corp_history_deleted_skip = count_status(corp_history_counts, "deleted_skip")
        stats.corp_history_failed = count_status(corp_history_counts, "failed", "not_found")

        # STEP 3 — alliances réellement découvertes dans les histories de corporations.
        stats.history_alliances = fill_history_alliances(conn)
        logger.info("STEP 3 history alliances=%s", stats.history_alliances)

        # STEP 4 — alliances réellement inconnues.
        stats.unknown_alliances = fill_unknown_alliances(conn)
        stats.new_alliances_excluded = count_temp(conn, "tmp_new_alliances")
        logger.info(
            "STEP 4 unknown alliances=%s new_excluded=%s",
            stats.unknown_alliances,
            stats.new_alliances_excluded,
        )

        alliance_public_counts = process_ids_parallel(
            conn=conn,
            table="tmp_unknown_alliances",
            column="alliance_id",
            chunk_size=args.chunk_size,
            workers=args.workers,
            fn=lambda aid: fetch_public_alliance(run_id, aid, force_status_check=False),
            label="public_alliance_unknown",
        )
        stats.public_alliance_loaded = count_status(alliance_public_counts, "loaded")
        stats.public_alliance_deleted = count_status(alliance_public_counts, "deleted")
        stats.public_alliance_skipped = count_status(
            alliance_public_counts,
            "skipped",
            "skipped_cache",
            "unchanged_304",
            "deleted_skip",
        )
        stats.public_alliance_failed = count_status(alliance_public_counts, "failed")

        logger.info(
            "DONE run_id=%s "
            "history_corps=%s unknown_corps=%s new_corps_excluded=%s "
            "corp_public_loaded=%s corp_public_deleted=%s corp_public_skipped=%s corp_public_failed=%s "
            "corp_history_loaded=%s corp_history_skipped=%s corp_history_deleted_skip=%s corp_history_failed=%s "
            "history_alliances=%s unknown_alliances=%s new_alliances_excluded=%s "
            "alliance_loaded=%s alliance_deleted=%s alliance_skipped=%s alliance_failed=%s",
            run_id,
            stats.history_corporations,
            stats.unknown_corporations,
            stats.new_corporations_excluded,
            stats.public_corp_loaded,
            stats.public_corp_deleted,
            stats.public_corp_skipped,
            stats.public_corp_failed,
            stats.corp_history_loaded,
            stats.corp_history_skipped,
            stats.corp_history_deleted_skip,
            stats.corp_history_failed,
            stats.history_alliances,
            stats.unknown_alliances,
            stats.new_alliances_excluded,
            stats.public_alliance_loaded,
            stats.public_alliance_deleted,
            stats.public_alliance_skipped,
            stats.public_alliance_failed,
        )

    finally:
        try:
            drop_temp_tables(conn)
        finally:
            conn.close()


# ============================================================
# CLI
# ============================================================


def main() -> None:
    configure_logging()

    parser = argparse.ArgumentParser(description="Backfill missing corporations, corporation histories and alliances from EVEOSINT entity histories.")
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Parallel workers. Default: {DEFAULT_WORKERS}",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help=f"Number of temp-table IDs loaded per processing chunk. Default: {DEFAULT_CHUNK_SIZE}",
    )

    args = parser.parse_args()

    if args.workers <= 0:
        raise RuntimeError("--workers must be greater than 0")
    if args.chunk_size <= 0:
        raise RuntimeError("--chunk-size must be greater than 0")

    build(args)


if __name__ == "__main__":
    main()
