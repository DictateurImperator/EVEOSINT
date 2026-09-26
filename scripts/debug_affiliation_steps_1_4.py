#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import signal
import statistics
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values
import requests

BASE_DIR = Path.home() / "eveosint"
CONFIG_PATH = BASE_DIR / "config" / "db.json"
RUN_DIR = BASE_DIR / "data" / "run" / "admin_debug"
HISTORY_DIR = RUN_DIR / "history"
STATUS_PATH = RUN_DIR / "status.json"
LOCK_PATH = RUN_DIR / "admin_debug.lock"
LOG_DIR = BASE_DIR / "data" / "logs" / "admin_debug"

ESI_BASE = "https://esi.evetech.net/latest"
DATASOURCE = "tranquility"
ESI_COMPATIBILITY_DATE = "2026-05-07"
ESI_USER_AGENT = (
    "EveOsint/1.0.0 "
    "(source: https://github.com/DictateurImperator/EVEOSINT; "
    "discord: 206798315935760386 / old name DictateurImperator#0447; "
    "eve-character: Dictateur Imperator)"
)
REQUEST_TIMEOUT = (3.05, 30)
MAX_ATTEMPTS = 6
DEBUG_WORK_TABLE = "esi_affiliation_debug_character_work"
DEBUG_RESULT_TABLE = "esi_affiliation_debug_result_work"
DEBUG_CORP_WORK_TABLE = "esi_affiliation_debug_corporation_work"
DEBUG_CHAR_QUEUE_TABLE = "esi_affiliation_debug_character_queue"
DEBUG_CORP_QUEUE_TABLE = "esi_affiliation_debug_corporation_queue"
ENTITIES_SCHEMA = "entities"

_STOP = threading.Event()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def load_db_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def db_connect():
    cfg = load_db_config()
    return psycopg2.connect(
        dbname=cfg["db_name"],
        user=cfg["db_user"],
        password=cfg["db_password"],
        host=cfg["db_host"],
        port=cfg["db_port"],
    )


def validate_identifier(value: str, label: str) -> str:
    if not value or not value.replace("_", "a").isalnum() or value[0].isdigit():
        raise RuntimeError(f"Invalid {label}: {value}")
    return value


def parse_source(value: str, column: str) -> tuple[str, str, str]:
    parts = value.split(".")
    if len(parts) != 2:
        raise RuntimeError("Source table must be schema.table")
    schema = validate_identifier(parts[0], "source schema")
    table = validate_identifier(parts[1], "source table")
    column = validate_identifier(column, "source column")
    return schema, table, column


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class DebugState:
    def __init__(self, run_id: str, log_path: Path, args: argparse.Namespace):
        self._lock = threading.Lock()
        self.data: dict[str, Any] = {
            "run_id": run_id,
            "module": "affiliation_steps_1_4",
            "variant": args.variant,
            "status": "running",
            "running": True,
            "pid": os.getpid(),
            "started_at": iso_now(),
            "finished_at": None,
            "duration_seconds": None,
            "current_phase": "startup",
            "source_table": args.source_table,
            "source_column": args.source_column,
            "limit": args.limit,
            "workers": args.workers,
            "batch_size": args.batch_size,
            "rate_limit": args.esi_max_calls_per_minute,
            "store_results": args.store_results,
            "use_etag_cache": args.use_etag_cache,
            "log_path": str(log_path),
            "error": None,
            "metrics": {},
            "phases": [],
            "bulk": {
                "batches_total": 0,
                "batches_done": 0,
                "http_200": 0,
                "http_304": 0,
                "http_errors": 0,
                "rows_received": 0,
                "rows_stored": 0,
                "rows_status_updated": 0,
                "calls_per_minute": 0.0,
                "avg_total_ms": 0.0,
                "avg_rate_wait_ms": 0.0,
                "avg_http_ms": 0.0,
                "avg_store_ms": 0.0,
                "avg_db_apply_ms": 0.0,
                "p50_total_ms": 0.0,
                "p95_total_ms": 0.0,
            },
        }
        self.started_monotonic = time.monotonic()
        self.flush()

    def flush(self) -> None:
        with self._lock:
            payload = json.loads(json.dumps(self.data))
        atomic_write_json(STATUS_PATH, payload)

    def set(self, **kwargs: Any) -> None:
        with self._lock:
            self.data.update(kwargs)
        self.flush()

    def metrics(self, **kwargs: Any) -> None:
        with self._lock:
            self.data["metrics"].update(kwargs)
        self.flush()

    def bulk(self, **kwargs: Any) -> None:
        with self._lock:
            self.data["bulk"].update(kwargs)
        self.flush()

    def phase_start(self, key: str, label: str, group: str) -> dict[str, Any]:
        phase = {
            "key": key,
            "label": label,
            "group": group,
            "status": "running",
            "started_at": iso_now(),
            "finished_at": None,
            "duration_seconds": None,
            "metrics": {},
            "error": None,
            "_mono": time.monotonic(),
        }
        with self._lock:
            self.data["current_phase"] = key
            self.data["phases"].append(phase)
        self.flush()
        return phase

    def phase_done(self, phase: dict[str, Any], **metrics: Any) -> None:
        elapsed = time.monotonic() - phase["_mono"]
        with self._lock:
            phase["status"] = "done"
            phase["finished_at"] = iso_now()
            phase["duration_seconds"] = round(elapsed, 3)
            phase["metrics"].update(metrics)
            phase.pop("_mono", None)
        self.flush()

    def phase_fail(self, phase: dict[str, Any], exc: Exception) -> None:
        elapsed = time.monotonic() - phase.get("_mono", time.monotonic())
        with self._lock:
            phase["status"] = "failed"
            phase["finished_at"] = iso_now()
            phase["duration_seconds"] = round(elapsed, 3)
            phase["error"] = f"{type(exc).__name__}: {exc}"
            phase.pop("_mono", None)
        self.flush()

    def finish(self, status: str, error: str | None = None) -> None:
        elapsed = time.monotonic() - self.started_monotonic
        with self._lock:
            self.data["status"] = status
            self.data["running"] = False
            self.data["current_phase"] = None
            self.data["finished_at"] = iso_now()
            self.data["duration_seconds"] = round(elapsed, 3)
            self.data["error"] = error
        self.flush()
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_json(HISTORY_DIR / f"{self.data['run_id']}.json", self.data)


class Logger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()

    def log(self, message: str) -> None:
        line = f"{datetime.now().astimezone().isoformat(timespec='seconds')} [debug] {message}"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        print(line, flush=True)


class GlobalRateLimiter:
    def __init__(self, calls_per_minute: int):
        self.interval = 60.0 / float(calls_per_minute)
        self._lock = threading.Lock()
        self._next = time.monotonic()

    def wait(self) -> float:
        with self._lock:
            now = time.monotonic()
            target = max(now, self._next)
            self._next = target + self.interval
        delay = target - now
        if delay > 0:
            time.sleep(delay)
        return max(0.0, delay)


_thread_local = threading.local()


def worker_session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({
            "User-Agent": ESI_USER_AGENT,
            "X-Compatibility-Date": ESI_COMPATIBILITY_DATE,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        _thread_local.session = session
    return session


def worker_db():
    conn = getattr(_thread_local, "db", None)
    if conn is None or conn.closed:
        conn = db_connect()
        _thread_local.db = conn
    return conn


def batch_hash(ids: list[int]) -> str:
    normalized = ",".join(str(i) for i in sorted(ids))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def cache_etag_for_batch(ids: list[int]) -> str | None:
    endpoint = f"/characters/affiliation/?batch={batch_hash(ids)}"
    conn = worker_db()
    with conn.cursor() as cur:
        cur.execute("SELECT etag FROM esi.endpoint_cache WHERE endpoint=%s", (endpoint,))
        row = cur.fetchone()
    return row[0] if row else None


def http_date(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return format_datetime(value.astimezone(timezone.utc), usegmt=True)


def call_affiliation_batch(
    *,
    ids: list[int],
    since: datetime | None,
    limiter: GlobalRateLimiter,
    use_etag_cache: bool,
) -> dict[str, Any]:
    started = time.perf_counter()
    rate_wait_ms = 0.0
    session = worker_session()
    url = f"{ESI_BASE}/characters/affiliation/"
    params = {"datasource": DATASOURCE}
    headers: dict[str, str] = {}
    if since is not None:
        headers["If-Modified-Since"] = http_date(since) or ""
    if use_etag_cache:
        etag = cache_etag_for_batch(ids)
        if etag:
            headers["If-None-Match"] = etag

    last_exc: Exception | None = None
    response = None
    http_elapsed_ms = 0.0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if _STOP.is_set():
            raise RuntimeError("debug_run_stopped")
        try:
            wait_started = time.perf_counter()
            limiter.wait()
            rate_wait_ms += (time.perf_counter() - wait_started) * 1000.0
            http_started = time.perf_counter()
            response = session.post(
                url,
                params=params,
                headers=headers,
                json=ids,
                timeout=REQUEST_TIMEOUT,
            )
            http_elapsed_ms += (time.perf_counter() - http_started) * 1000.0
        except requests.RequestException as exc:
            last_exc = exc
            if attempt >= MAX_ATTEMPTS:
                raise
            time.sleep(min(30.0, 2.0 ** attempt))
            continue

        if response.status_code in (420, 429) or 500 <= response.status_code <= 599:
            if attempt >= MAX_ATTEMPTS:
                break
            retry_after = response.headers.get("Retry-After")
            try:
                sleep_for = float(retry_after) if retry_after else min(60.0, 2.0 ** attempt)
            except ValueError:
                sleep_for = min(60.0, 2.0 ** attempt)
            time.sleep(sleep_for)
            continue
        break

    if response is None:
        raise RuntimeError(f"No ESI response: {last_exc}")

    status = int(response.status_code)
    payload: list[dict[str, Any]] = []
    if status == 200:
        raw = response.json()
        if not isinstance(raw, list):
            raise RuntimeError("Invalid affiliation payload")
        payload = raw
    elif status != 304:
        snippet = response.text[:300]
        raise RuntimeError(f"Unexpected HTTP {status}: {snippet}")

    return {
        "status": status,
        "payload": payload,
        "rate_wait_ms": rate_wait_ms,
        "http_ms": http_elapsed_ms,
        "total_ms": (time.perf_counter() - started) * 1000.0,
    }


def ensure_debug_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("""
            CREATE TABLE IF NOT EXISTS {}.{} (
                run_id UUID NOT NULL,
                character_id BIGINT NOT NULL,
                public_exists BOOLEAN NOT NULL,
                is_deleted BOOLEAN NOT NULL,
                has_history BOOLEAN NOT NULL,
                affiliation_checked_at TIMESTAMPTZ,
                source_history_fetched_at TIMESTAMPTZ,
                eligible_bulk BOOLEAN NOT NULL,

                affiliation_batch_no INTEGER,
                affiliation_status TEXT NOT NULL DEFAULT 'not_applicable',
                affiliation_http_status INTEGER,
                affiliation_error TEXT,
                affiliation_processed_at TIMESTAMPTZ,

                history_refresh_status TEXT NOT NULL DEFAULT 'not_applicable',
                known_corporation_id BIGINT,
                current_corporation_id BIGINT,
                current_alliance_id BIGINT,

                sim_current_alliance_id BIGINT,
                sim_affiliation_checked_at TIMESTAMPTZ,

                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (run_id, character_id)
            )
        """).format(sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_WORK_TABLE)))

        # Upgrade an already-deployed v1 debug table in place.
        for ddl in (
            "ADD COLUMN IF NOT EXISTS affiliation_batch_no INTEGER",
            "ADD COLUMN IF NOT EXISTS affiliation_status TEXT NOT NULL DEFAULT 'not_applicable'",
            "ADD COLUMN IF NOT EXISTS affiliation_http_status INTEGER",
            "ADD COLUMN IF NOT EXISTS affiliation_error TEXT",
            "ADD COLUMN IF NOT EXISTS affiliation_processed_at TIMESTAMPTZ",
            "ADD COLUMN IF NOT EXISTS history_refresh_status TEXT NOT NULL DEFAULT 'not_applicable'",
            "ADD COLUMN IF NOT EXISTS known_corporation_id BIGINT",
            "ADD COLUMN IF NOT EXISTS current_corporation_id BIGINT",
            "ADD COLUMN IF NOT EXISTS current_alliance_id BIGINT",
            "ADD COLUMN IF NOT EXISTS sim_current_alliance_id BIGINT",
            "ADD COLUMN IF NOT EXISTS sim_affiliation_checked_at TIMESTAMPTZ",
            "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
        ):
            cur.execute(sql.SQL("ALTER TABLE {}.{} " + ddl).format(
                sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_WORK_TABLE)
            ))

        cur.execute(sql.SQL("""
            CREATE INDEX IF NOT EXISTS esi_aff_debug_candidate_idx
            ON {}.{} (
                run_id,
                eligible_bulk,
                affiliation_checked_at,
                source_history_fetched_at,
                character_id
            )
        """).format(sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_WORK_TABLE)))
        cur.execute(sql.SQL("""
            CREATE INDEX IF NOT EXISTS esi_aff_debug_status_idx
            ON {}.{} (run_id, affiliation_status, character_id)
        """).format(sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_WORK_TABLE)))
        cur.execute(sql.SQL("""
            CREATE INDEX IF NOT EXISTS esi_aff_debug_history_refresh_idx
            ON {}.{} (run_id, history_refresh_status, character_id)
        """).format(sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_WORK_TABLE)))

        cur.execute(sql.SQL("""
            CREATE TABLE IF NOT EXISTS {}.{} (
                run_id UUID NOT NULL,
                character_id BIGINT NOT NULL,
                corporation_id BIGINT NOT NULL,
                alliance_id BIGINT,
                received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (run_id, character_id)
            )
        """).format(sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_RESULT_TABLE)))
        cur.execute(sql.SQL("""
            CREATE INDEX IF NOT EXISTS esi_aff_debug_result_corp_idx
            ON {}.{} (run_id, corporation_id, alliance_id)
        """).format(sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_RESULT_TABLE)))

        cur.execute(sql.SQL("""
            CREATE TABLE IF NOT EXISTS {}.{} (
                run_id UUID NOT NULL,
                corporation_id BIGINT NOT NULL,
                alliance_id BIGINT,
                public_status TEXT NOT NULL DEFAULT 'skipped',
                initial_history_status TEXT NOT NULL DEFAULT 'not_applicable',
                refresh_status TEXT NOT NULL DEFAULT 'not_applicable',
                known_alliance_id BIGINT,
                current_alliance_id BIGINT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (run_id, corporation_id)
            )
        """).format(sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_CORP_WORK_TABLE)))
        cur.execute(sql.SQL("""
            CREATE INDEX IF NOT EXISTS esi_aff_debug_corp_refresh_idx
            ON {}.{} (run_id, refresh_status, corporation_id)
        """).format(sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_CORP_WORK_TABLE)))

        cur.execute(sql.SQL("""
            CREATE TABLE IF NOT EXISTS {}.{} (
                run_id UUID NOT NULL,
                character_id BIGINT NOT NULL,
                reason TEXT NOT NULL,
                known_corporation_id BIGINT,
                current_corporation_id BIGINT,
                current_alliance_id BIGINT,
                detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (run_id, character_id)
            )
        """).format(sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_CHAR_QUEUE_TABLE)))

        cur.execute(sql.SQL("""
            CREATE TABLE IF NOT EXISTS {}.{} (
                run_id UUID NOT NULL,
                corporation_id BIGINT NOT NULL,
                reason TEXT NOT NULL,
                known_alliance_id BIGINT,
                current_alliance_id BIGINT,
                detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (run_id, corporation_id)
            )
        """).format(sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_CORP_QUEUE_TABLE)))
    conn.commit()

def cleanup_debug_rows(conn, keep_run_id: str | None = None) -> tuple[int, int]:
    tables = [
        DEBUG_CORP_QUEUE_TABLE,
        DEBUG_CHAR_QUEUE_TABLE,
        DEBUG_CORP_WORK_TABLE,
        DEBUG_RESULT_TABLE,
        DEBUG_WORK_TABLE,
    ]
    with conn.cursor() as cur:
        if keep_run_id:
            counts = {}
            for table in tables:
                cur.execute(sql.SQL("DELETE FROM {}.{} WHERE run_id<>%s").format(
                    sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(table)
                ), (keep_run_id,))
                counts[table] = cur.rowcount
            work = counts.get(DEBUG_WORK_TABLE, 0)
            results = counts.get(DEBUG_RESULT_TABLE, 0)
        else:
            for table in tables:
                cur.execute(sql.SQL("TRUNCATE TABLE {}.{}").format(
                    sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(table)
                ))
            work = -1
            results = -1
    conn.commit()
    return work, results

def source_exists(conn, schema: str, table: str, column: str) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema=%s AND table_name=%s AND column_name=%s
            )
        """, (schema, table, column))
        if not cur.fetchone()[0]:
            raise RuntimeError(f"Missing source {schema}.{table}.{column}")


def wal_lsn(conn) -> str | None:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_current_wal_lsn()::text")
            return cur.fetchone()[0]
    except Exception:
        conn.rollback()
        return None


def wal_diff(conn, start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_wal_lsn_diff(%s::pg_lsn,%s::pg_lsn)::bigint", (end, start))
            return int(cur.fetchone()[0])
    except Exception:
        conn.rollback()
        return None


def rel_stats(conn, table_name: str) -> dict[str, Any]:
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    pg_total_relation_size(%s::regclass),
                    COALESCE(s.n_live_tup,0)::bigint,
                    COALESCE(s.n_dead_tup,0)::bigint
                FROM pg_stat_user_tables s
                WHERE s.relid=%s::regclass
            """, (f"{ENTITIES_SCHEMA}.{table_name}", f"{ENTITIES_SCHEMA}.{table_name}"))
            row = cur.fetchone()
            if not row:
                return {}
            return {"relation_bytes": int(row[0]), "live_tuples": int(row[1]), "dead_tuples": int(row[2])}
    except Exception:
        conn.rollback()
        return {}


def materialize_optimized(
    conn,
    *,
    run_id: str,
    source_schema: str,
    source_table: str,
    source_column: str,
    limit: int | None,
) -> int:
    limit_sql = sql.SQL("")
    params: list[Any] = []
    if limit is not None:
        limit_sql = sql.SQL(" LIMIT %s")
        params.append(limit)
    params.append(run_id)

    query = sql.SQL("""
        WITH src AS (
            SELECT DISTINCT {source_column}::BIGINT AS character_id
            FROM {source_schema}.{source_table}
            WHERE {source_column} IS NOT NULL
            ORDER BY {source_column}::BIGINT
            {limit_sql}
        )
        INSERT INTO {entities}.{debug_work} (
            run_id,
            character_id,
            public_exists,
            is_deleted,
            has_history,
            affiliation_checked_at,
            source_history_fetched_at,
            eligible_bulk,
            affiliation_status,
            history_refresh_status,
            sim_current_alliance_id,
            sim_affiliation_checked_at
        )
        SELECT
            %s::uuid,
            src.character_id,
            (c.character_id IS NOT NULL) AS public_exists,
            COALESCE(c.is_deleted, FALSE) AS is_deleted,
            (h.character_id IS NOT NULL) AS has_history,
            cca.affiliation_checked_at,
            cca.source_history_fetched_at,
            (
                c.character_id IS NOT NULL
                AND COALESCE(c.is_deleted, FALSE) = FALSE
                AND h.character_id IS NOT NULL
            ) AS eligible_bulk,
            CASE
                WHEN c.character_id IS NOT NULL
                 AND COALESCE(c.is_deleted, FALSE) = FALSE
                 AND h.character_id IS NOT NULL
                THEN 'pending'
                ELSE 'not_applicable'
            END AS affiliation_status,
            'not_applicable' AS history_refresh_status,
            cca.alliance_id AS sim_current_alliance_id,
            cca.affiliation_checked_at AS sim_affiliation_checked_at
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
        source_column=sql.Identifier(source_column),
        source_schema=sql.Identifier(source_schema),
        source_table=sql.Identifier(source_table),
        limit_sql=limit_sql,
        entities=sql.Identifier(ENTITIES_SCHEMA),
        debug_work=sql.Identifier(DEBUG_WORK_TABLE),
    )
    with conn.cursor() as cur:
        cur.execute(query, tuple(params))
        rows = int(cur.rowcount)
    conn.commit()
    return rows


def classify_counts(conn, run_id: str) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("""
            SELECT
                COUNT(*)::bigint AS source_ids,
                COUNT(*) FILTER (WHERE public_exists)::bigint AS public_known,
                COUNT(*) FILTER (WHERE NOT public_exists)::bigint AS public_missing,
                COUNT(*) FILTER (WHERE is_deleted)::bigint AS deleted,
                COUNT(*) FILTER (WHERE public_exists AND NOT is_deleted AND has_history)::bigint AS with_history,
                COUNT(*) FILTER (WHERE public_exists AND NOT is_deleted AND NOT has_history)::bigint AS without_history,
                COUNT(*) FILTER (WHERE eligible_bulk)::bigint AS affiliation_candidates
            FROM {}.{}
            WHERE run_id=%s
        """).format(sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_WORK_TABLE)), (run_id,))
        row = cur.fetchone()
    keys = ["source_ids", "public_known", "public_missing", "deleted", "with_history", "without_history", "affiliation_candidates"]
    return {key: int(value or 0) for key, value in zip(keys, row)}


def analyze_debug_work(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("ANALYZE {}.{}").format(sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_WORK_TABLE)))
    conn.commit()


def open_candidate_cursor(conn, run_id: str):
    cursor_name = f"debug_aff_{uuid.uuid4().hex[:12]}"
    cur = conn.cursor(name=cursor_name)
    cur.itersize = 5000
    cur.execute(sql.SQL("""
        SELECT character_id, affiliation_checked_at, source_history_fetched_at
        FROM {}.{}
        WHERE run_id=%s
          AND eligible_bulk=TRUE
        ORDER BY affiliation_checked_at ASC NULLS FIRST,
                 source_history_fetched_at ASC NULLS FIRST,
                 character_id ASC
    """).format(sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_WORK_TABLE)), (run_id,))
    return cur


def make_batch(rows: list[tuple[Any, Any, Any]]) -> tuple[list[int], datetime | None]:
    ids = [int(row[0]) for row in rows]
    checks = [row[1] for row in rows]
    if any(value is None for value in checks):
        since = None
    else:
        since = min(checks) if checks else None
    return ids, since


def apply_batch_result(
    *,
    run_id: str,
    batch_no: int,
    ids: list[int],
    status: int,
    payload: list[dict[str, Any]],
    store_results: bool,
    error: str | None = None,
) -> tuple[int, int, float]:
    """Apply the DB work normally associated with one affiliation bulk batch.

    Everything is written to DEBUG tables only.  One transaction per batch is
    intentional: it benchmarks an optimized version of the production plumbing
    without touching business current/history tables.
    """
    started = time.perf_counter()
    conn = worker_db()
    stored = 0
    with conn.cursor() as cur:
        if status == 200 and store_results and payload:
            values = []
            for item in payload:
                alliance = item.get("alliance_id")
                values.append((
                    run_id,
                    int(item["character_id"]),
                    int(item["corporation_id"]),
                    int(alliance) if alliance is not None else None,
                ))
            execute_values(
                cur,
                f"""
                INSERT INTO {ENTITIES_SCHEMA}.{DEBUG_RESULT_TABLE}
                    (run_id, character_id, corporation_id, alliance_id)
                VALUES %s
                ON CONFLICT (run_id, character_id)
                DO UPDATE SET
                    corporation_id=EXCLUDED.corporation_id,
                    alliance_id=EXCLUDED.alliance_id,
                    received_at=NOW()
                """,
                values,
                template="(%s::uuid, %s, %s, %s)",
                page_size=1000,
            )
            stored = len(values)

        if status == 200:
            work_status = "loaded"
        elif status == 304:
            work_status = "unchanged_304"
        else:
            work_status = "failed"

        cur.execute(
            sql.SQL("""
                UPDATE {}.{}
                SET affiliation_batch_no=%s,
                    affiliation_status=%s,
                    affiliation_http_status=%s,
                    affiliation_error=%s,
                    affiliation_processed_at=NOW(),
                    sim_affiliation_checked_at = CASE
                        WHEN %s = 304 THEN NOW()
                        ELSE sim_affiliation_checked_at
                    END,
                    updated_at=NOW()
                WHERE run_id=%s
                  AND character_id = ANY(%s)
            """).format(sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_WORK_TABLE)),
            (batch_no, work_status, status, error, status, run_id, ids),
        )
        status_rows = int(cur.rowcount)
    conn.commit()
    return stored, status_rows, (time.perf_counter() - started) * 1000.0


def apply_batch_failure(*, run_id: str, batch_no: int, ids: list[int], error: str) -> tuple[int, float]:
    started = time.perf_counter()
    conn = worker_db()
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
                UPDATE {}.{}
                SET affiliation_batch_no=%s,
                    affiliation_status='failed',
                    affiliation_http_status=NULL,
                    affiliation_error=%s,
                    affiliation_processed_at=NOW(),
                    updated_at=NOW()
                WHERE run_id=%s
                  AND character_id = ANY(%s)
            """).format(sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_WORK_TABLE)),
            (batch_no, error, run_id, ids),
        )
        rows = int(cur.rowcount)
    conn.commit()
    return rows, (time.perf_counter() - started) * 1000.0


def process_debug_affiliation_results_sql(conn, run_id: str) -> dict[str, int]:
    """Mirror production post-bulk SQL in isolated DEBUG tables.

    No character/corporation history GET is made and no production queue/current
    table is modified.  Production current/history tables are READ ONLY inputs.
    """
    with conn.cursor() as cur:
        # Character corporation mismatches: same comparison as production.
        cur.execute(sql.SQL("""
            UPDATE {}.{} w
            SET history_refresh_status='pending',
                known_corporation_id=c.corporation_id,
                current_corporation_id=r.corporation_id,
                current_alliance_id=r.alliance_id,
                updated_at=NOW()
            FROM {}.{} r
            LEFT JOIN {}.character_current_affiliation c
              ON c.character_id = r.character_id
            WHERE w.run_id=%s
              AND r.run_id=%s
              AND w.character_id=r.character_id
              AND c.corporation_id IS DISTINCT FROM r.corporation_id
        """).format(
            sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_WORK_TABLE),
            sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_RESULT_TABLE),
            sql.Identifier(ENTITIES_SCHEMA),
        ), (run_id, run_id))
        char_mismatches = int(cur.rowcount)

        cur.execute(sql.SQL("""
            UPDATE {}.{} w
            SET history_refresh_status='not_applicable',
                known_corporation_id=c.corporation_id,
                current_corporation_id=r.corporation_id,
                current_alliance_id=r.alliance_id,
                updated_at=NOW()
            FROM {}.{} r
            LEFT JOIN {}.character_current_affiliation c
              ON c.character_id = r.character_id
            WHERE w.run_id=%s
              AND r.run_id=%s
              AND w.character_id=r.character_id
              AND c.corporation_id IS NOT DISTINCT FROM r.corporation_id
        """).format(
            sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_WORK_TABLE),
            sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_RESULT_TABLE),
            sql.Identifier(ENTITIES_SCHEMA),
        ), (run_id, run_id))
        char_matches = int(cur.rowcount)

        cur.execute(sql.SQL("""
            INSERT INTO {}.{} (
                run_id, character_id, reason,
                known_corporation_id, current_corporation_id, current_alliance_id
            )
            SELECT
                w.run_id,
                w.character_id,
                'character_corporation_changed_from_affiliation',
                w.known_corporation_id,
                w.current_corporation_id,
                w.current_alliance_id
            FROM {}.{} w
            WHERE w.run_id=%s
              AND w.history_refresh_status='pending'
            ON CONFLICT (run_id, character_id)
            DO UPDATE SET
                reason=EXCLUDED.reason,
                known_corporation_id=EXCLUDED.known_corporation_id,
                current_corporation_id=EXCLUDED.current_corporation_id,
                current_alliance_id=EXCLUDED.current_alliance_id,
                detected_at=NOW()
        """).format(
            sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_CHAR_QUEUE_TABLE),
            sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_WORK_TABLE),
        ), (run_id,))
        char_queue_rows = int(cur.rowcount)

        # Simulate production character_current_affiliation alliance/check update
        # inside the debug work row instead of touching the business table.
        cur.execute(sql.SQL("""
            UPDATE {}.{} w
            SET sim_current_alliance_id=r.alliance_id,
                sim_affiliation_checked_at=NOW(),
                updated_at=NOW()
            FROM {}.{} r
            WHERE w.run_id=%s
              AND r.run_id=%s
              AND w.character_id=r.character_id
        """).format(
            sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_WORK_TABLE),
            sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_RESULT_TABLE),
        ), (run_id, run_id))
        simulated_current_updates = int(cur.rowcount)

        # ONE row per corporation for the whole run.  This is the current fixed
        # production logic: missing current affiliation means known alliance NULL.
        cur.execute(sql.SQL("""
            INSERT INTO {}.{} (
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
                %s::uuid,
                r.corporation_id,
                r.alliance_id,
                CASE WHEN c.corporation_id IS NULL THEN 'pending' ELSE 'skipped' END,
                CASE
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
            FROM {}.{} r
            LEFT JOIN {}.corporations c
              ON c.corporation_id = r.corporation_id
            LEFT JOIN {}.corporation_current_affiliation ca
              ON ca.corporation_id = r.corporation_id
            WHERE r.run_id=%s
            ORDER BY r.corporation_id, r.alliance_id NULLS LAST
            ON CONFLICT (run_id, corporation_id)
            DO UPDATE SET
                alliance_id=EXCLUDED.alliance_id,
                public_status=EXCLUDED.public_status,
                initial_history_status=EXCLUDED.initial_history_status,
                refresh_status=EXCLUDED.refresh_status,
                known_alliance_id=EXCLUDED.known_alliance_id,
                current_alliance_id=EXCLUDED.current_alliance_id,
                updated_at=NOW()
        """).format(
            sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_CORP_WORK_TABLE),
            sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_RESULT_TABLE),
            sql.Identifier(ENTITIES_SCHEMA),
            sql.Identifier(ENTITIES_SCHEMA),
        ), (run_id, run_id))
        # rowcount may be insert/update count; exact distinct count read below.

        cur.execute(sql.SQL("""
            SELECT
                COUNT(*)::bigint,
                COUNT(*) FILTER (WHERE public_status='pending')::bigint,
                COUNT(*) FILTER (WHERE initial_history_status='pending')::bigint,
                COUNT(*) FILTER (WHERE refresh_status='pending')::bigint
            FROM {}.{}
            WHERE run_id=%s
        """).format(sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_CORP_WORK_TABLE)), (run_id,))
        distinct_corps, unknown_corps, initial_corp_histories, corp_history_queued = [int(x or 0) for x in cur.fetchone()]

        cur.execute(sql.SQL("""
            INSERT INTO {}.{} (
                run_id, corporation_id, reason, known_alliance_id, current_alliance_id
            )
            SELECT
                w.run_id,
                w.corporation_id,
                'corporation_alliance_changed_from_character_affiliation',
                w.known_alliance_id,
                w.current_alliance_id
            FROM {}.{} w
            WHERE w.run_id=%s
              AND w.refresh_status='pending'
            ON CONFLICT (run_id, corporation_id)
            DO UPDATE SET
                reason=EXCLUDED.reason,
                known_alliance_id=EXCLUDED.known_alliance_id,
                current_alliance_id=EXCLUDED.current_alliance_id,
                detected_at=NOW()
        """).format(
            sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_CORP_QUEUE_TABLE),
            sql.Identifier(ENTITIES_SCHEMA), sql.Identifier(DEBUG_CORP_WORK_TABLE),
        ), (run_id,))
        corp_queue_rows = int(cur.rowcount)

    conn.commit()
    return {
        "character_mismatches": char_mismatches,
        "character_matches": char_matches,
        "character_queue_rows": char_queue_rows,
        "simulated_current_updates": simulated_current_updates,
        "distinct_corporations": distinct_corps,
        "unknown_corporations": unknown_corps,
        "initial_corporation_histories": initial_corp_histories,
        "corporation_history_queued": corp_history_queued,
        "corporation_queue_rows": corp_queue_rows,
    }

def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil((pct / 100.0) * len(ordered)) - 1))
    return ordered[index]


def run_bulk_phase(
    conn,
    state: DebugState,
    logger: Logger,
    *,
    run_id: str,
    candidates: int,
    workers: int,
    batch_size: int,
    calls_per_minute: int,
    store_results: bool,
    use_etag_cache: bool,
) -> dict[str, Any]:
    total_batches = math.ceil(candidates / batch_size) if candidates else 0
    state.bulk(batches_total=total_batches)
    logger.log(
        f"STEP 4 optimized bulk candidates={candidates} batches={total_batches} workers={workers} "
        f"rate={calls_per_minute}/min store_results={store_results} production_like_batch_db_apply=yes"
    )

    limiter = GlobalRateLimiter(calls_per_minute)
    cur = open_candidate_cursor(conn, run_id)
    executor = ThreadPoolExecutor(max_workers=workers)
    inflight: dict[Any, tuple[int, list[int]]] = {}
    next_batch_no = 1
    done = 0
    http_200 = 0
    http_304 = 0
    errors = 0
    rows_received = 0
    rows_stored = 0
    rows_status_updated = 0
    totals_ms: list[float] = []
    http_ms: list[float] = []
    wait_ms: list[float] = []
    db_apply_ms: list[float] = []
    bulk_started = time.monotonic()
    last_status_flush = 0.0

    def submit_one() -> bool:
        nonlocal next_batch_no
        if _STOP.is_set():
            return False
        rows = cur.fetchmany(batch_size)
        if not rows:
            return False
        ids, since = make_batch(rows)
        future = executor.submit(
            call_affiliation_batch,
            ids=ids,
            since=since,
            limiter=limiter,
            use_etag_cache=use_etag_cache,
        )
        inflight[future] = (next_batch_no, ids)
        next_batch_no += 1
        return True

    try:
        for _ in range(workers):
            if not submit_one():
                break

        while inflight:
            completed, _ = wait(list(inflight.keys()), return_when=FIRST_COMPLETED)
            for future in completed:
                batch_no, ids = inflight.pop(future)
                done += 1
                try:
                    result = future.result()
                    status = int(result["status"])
                    payload = result["payload"]
                    if status == 200:
                        http_200 += 1
                        rows_received += len(payload)
                    elif status == 304:
                        http_304 += 1

                    stored, status_rows, this_db_ms = apply_batch_result(
                        run_id=run_id,
                        batch_no=batch_no,
                        ids=ids,
                        status=status,
                        payload=payload,
                        store_results=store_results,
                    )
                    rows_stored += stored
                    rows_status_updated += status_rows
                    db_apply_ms.append(this_db_ms)

                    totals_ms.append(float(result["total_ms"]) + this_db_ms)
                    http_ms.append(float(result["http_ms"]))
                    wait_ms.append(float(result["rate_wait_ms"]))
                except Exception as exc:
                    errors += 1
                    logger.log(f"BULK ERROR batch={batch_no} size={len(ids)} {type(exc).__name__}: {exc}")
                    try:
                        status_rows, this_db_ms = apply_batch_failure(
                            run_id=run_id,
                            batch_no=batch_no,
                            ids=ids,
                            error=type(exc).__name__,
                        )
                        rows_status_updated += status_rows
                        db_apply_ms.append(this_db_ms)
                    except Exception as mark_exc:
                        logger.log(
                            f"BULK STATUS ERROR batch={batch_no} {type(mark_exc).__name__}: {mark_exc}"
                        )

                if not _STOP.is_set():
                    submit_one()

                now = time.monotonic()
                if now - last_status_flush >= 1.0 or done == total_batches:
                    elapsed = max(0.001, now - bulk_started)
                    calls_per_min = done / elapsed * 60.0
                    avg_db = statistics.fmean(db_apply_ms) if db_apply_ms else 0.0
                    state.bulk(
                        batches_done=done,
                        http_200=http_200,
                        http_304=http_304,
                        http_errors=errors,
                        rows_received=rows_received,
                        rows_stored=rows_stored,
                        rows_status_updated=rows_status_updated,
                        calls_per_minute=round(calls_per_min, 2),
                        avg_total_ms=round(statistics.fmean(totals_ms), 2) if totals_ms else 0.0,
                        avg_rate_wait_ms=round(statistics.fmean(wait_ms), 2) if wait_ms else 0.0,
                        avg_http_ms=round(statistics.fmean(http_ms), 2) if http_ms else 0.0,
                        avg_store_ms=round(avg_db, 2),
                        avg_db_apply_ms=round(avg_db, 2),
                        p50_total_ms=round(percentile(totals_ms, 50), 2),
                        p95_total_ms=round(percentile(totals_ms, 95), 2),
                    )
                    last_status_flush = now

            if _STOP.is_set():
                raise RuntimeError("debug_run_stopped")
    finally:
        cur.close()
        executor.shutdown(wait=True, cancel_futures=True)

    elapsed = time.monotonic() - bulk_started
    avg_db = statistics.fmean(db_apply_ms) if db_apply_ms else 0.0
    return {
        "batches": done,
        "http_200": http_200,
        "http_304": http_304,
        "errors": errors,
        "rows_received": rows_received,
        "rows_stored": rows_stored,
        "rows_status_updated": rows_status_updated,
        "calls_per_minute": round(done / max(elapsed, 0.001) * 60.0, 2),
        "avg_total_ms": round(statistics.fmean(totals_ms), 2) if totals_ms else 0.0,
        "avg_http_ms": round(statistics.fmean(http_ms), 2) if http_ms else 0.0,
        "avg_rate_wait_ms": round(statistics.fmean(wait_ms), 2) if wait_ms else 0.0,
        "avg_store_ms": round(avg_db, 2),
        "avg_db_apply_ms": round(avg_db, 2),
        "p50_total_ms": round(percentile(totals_ms, 50), 2),
        "p95_total_ms": round(percentile(totals_ms, 95), 2),
    }

def acquire_lock():
    import fcntl
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError("debug_already_running") from exc
    return handle


def install_signal_handlers() -> None:
    def handler(signum, frame):
        _STOP.set()
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def run(args: argparse.Namespace) -> int:
    install_signal_handlers()
    lock_handle = acquire_lock()
    run_id = str(uuid.uuid4())
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{run_id}.log"
    logger = Logger(log_path)
    state = DebugState(run_id, log_path, args)
    overall_error: str | None = None
    conn = None
    try:
        logger.log(
            f"START run_id={run_id} variant={args.variant} source={args.source_table}.{args.source_column} "
            f"workers={args.workers} batch_size={args.batch_size} rate={args.esi_max_calls_per_minute}/min "
            f"store_results={args.store_results}"
        )
        source_schema, source_table, source_column = parse_source(args.source_table, args.source_column)
        conn = db_connect()
        source_exists(conn, source_schema, source_table, source_column)

        phase = state.phase_start("ensure_debug_tables", "Create / verify isolated debug tables", "setup")
        try:
            ensure_debug_tables(conn)
            cleanup_debug_rows(conn)
            state.phase_done(phase)
        except Exception as exc:
            state.phase_fail(phase, exc)
            raise

        phase = state.phase_start("source_count", "STEP 1 · Read source pilot set", "step1")
        try:
            query = sql.SQL("SELECT COUNT(DISTINCT {column}::BIGINT) FROM {schema}.{table} WHERE {column} IS NOT NULL").format(
                column=sql.Identifier(source_column), schema=sql.Identifier(source_schema), table=sql.Identifier(source_table)
            )
            if args.limit is not None:
                query = sql.SQL("SELECT COUNT(*) FROM (SELECT DISTINCT {column}::BIGINT FROM {schema}.{table} WHERE {column} IS NOT NULL ORDER BY {column}::BIGINT LIMIT %s) s").format(
                    column=sql.Identifier(source_column), schema=sql.Identifier(source_schema), table=sql.Identifier(source_table)
                )
                params = (args.limit,)
            else:
                params = ()
            with conn.cursor() as cur:
                cur.execute(query, params)
                source_count = int(cur.fetchone()[0])
            state.metrics(source_ids=source_count)
            state.phase_done(phase, source_ids=source_count)
        except Exception as exc:
            state.phase_fail(phase, exc)
            raise

        wal_start = wal_lsn(conn)
        before_stats = rel_stats(conn, DEBUG_WORK_TABLE)
        phase = state.phase_start(
            "optimized_materialize",
            "STEP 1–2 · One-pass source + public + deleted + history/current JOIN",
            "step2",
        )
        try:
            inserted = materialize_optimized(
                conn,
                run_id=run_id,
                source_schema=source_schema,
                source_table=source_table,
                source_column=source_column,
                limit=args.limit,
            )
            wal_end = wal_lsn(conn)
            wal_bytes = wal_diff(conn, wal_start, wal_end)
            state.phase_done(phase, rows_inserted=inserted, wal_bytes=wal_bytes)
        except Exception as exc:
            state.phase_fail(phase, exc)
            raise

        phase = state.phase_start("analyze", "ANALYZE isolated debug work table", "step2")
        try:
            analyze_debug_work(conn)
            after_stats = rel_stats(conn, DEBUG_WORK_TABLE)
            state.phase_done(
                phase,
                relation_bytes=after_stats.get("relation_bytes"),
                live_tuples=after_stats.get("live_tuples"),
                dead_tuples=after_stats.get("dead_tuples"),
                dead_tuples_delta=(after_stats.get("dead_tuples", 0) - before_stats.get("dead_tuples", 0)),
            )
        except Exception as exc:
            state.phase_fail(phase, exc)
            raise

        phase = state.phase_start("classify", "STEP 2–3 · Classify history debt and bulk candidates", "step3")
        try:
            counts = classify_counts(conn, run_id)
            state.metrics(**counts)
            state.phase_done(phase, **counts)
            logger.log(
                "CLASSIFY " + " ".join(f"{key}={value}" for key, value in counts.items())
            )
        except Exception as exc:
            state.phase_fail(phase, exc)
            raise

        if args.skip_bulk:
            logger.log("STEP 4 skipped by request")
        else:
            phase = state.phase_start("bulk", "STEP 4 · Optimized streamed / parallel affiliation bulk", "step4")
            try:
                bulk_metrics = run_bulk_phase(
                    conn,
                    state,
                    logger,
                    run_id=run_id,
                    candidates=counts["affiliation_candidates"],
                    workers=args.workers,
                    batch_size=args.batch_size,
                    calls_per_minute=args.esi_max_calls_per_minute,
                    store_results=args.store_results,
                    use_etag_cache=args.use_etag_cache,
                )
                state.phase_done(phase, **bulk_metrics)
            except Exception as exc:
                state.phase_fail(phase, exc)
                raise

            if args.store_results:
                wal_post_start = wal_lsn(conn)
                post_before = rel_stats(conn, DEBUG_WORK_TABLE)
                phase = state.phase_start(
                    "post_bulk_sql",
                    "STEP 4B · Production-like result SQL / mismatch + corporation queues (DEBUG only)",
                    "step4",
                )
                try:
                    post_metrics = process_debug_affiliation_results_sql(conn, run_id)
                    wal_post_end = wal_lsn(conn)
                    post_metrics["wal_bytes"] = wal_diff(conn, wal_post_start, wal_post_end)
                    state.metrics(**post_metrics)
                    state.phase_done(phase, **post_metrics)
                    logger.log(
                        "POST BULK SQL " + " ".join(f"{key}={value}" for key, value in post_metrics.items())
                    )
                except Exception as exc:
                    state.phase_fail(phase, exc)
                    raise

                phase = state.phase_start(
                    "post_bulk_analyze",
                    "ANALYZE debug work after full bulk treatment",
                    "step4",
                )
                try:
                    analyze_debug_work(conn)
                    post_after = rel_stats(conn, DEBUG_WORK_TABLE)
                    state.phase_done(
                        phase,
                        relation_bytes=post_after.get("relation_bytes"),
                        live_tuples=post_after.get("live_tuples"),
                        dead_tuples=post_after.get("dead_tuples"),
                        dead_tuples_delta=(post_after.get("dead_tuples", 0) - post_before.get("dead_tuples", 0)),
                    )
                except Exception as exc:
                    state.phase_fail(phase, exc)
                    raise
            else:
                logger.log("POST BULK SQL skipped because Store ESI payload is disabled")

        logger.log("DONE — no business current/history/queue tables were modified")
        state.finish("done")
        return 0
    except KeyboardInterrupt:
        overall_error = "debug_run_stopped"
        logger.log("STOPPED by request")
        state.finish("stopped", error=overall_error)
        return 130
    except Exception as exc:
        overall_error = f"{type(exc).__name__}: {exc}"
        logger.log(f"FAILED {overall_error}")
        state.finish("stopped" if _STOP.is_set() else "failed", error=overall_error)
        return 2
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        try:
            lock_handle.close()
        except Exception:
            pass


def cleanup_only() -> int:
    lock_handle = acquire_lock()
    try:
        conn = db_connect()
        try:
            ensure_debug_tables(conn)
            cleanup_debug_rows(conn)
        finally:
            conn.close()
        return 0
    finally:
        lock_handle.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="EVEOSINT isolated affiliation Steps 1-4 benchmark")
    parser.add_argument("--source-table", default="entities.recent_killmail_pilots")
    parser.add_argument("--source-column", default="character_id")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=999)
    parser.add_argument("--esi-max-calls-per-minute", type=int, default=280)
    parser.add_argument("--variant", choices=["optimized"], default="optimized")
    parser.add_argument("--store-results", action="store_true")
    parser.add_argument("--use-etag-cache", action="store_true")
    parser.add_argument("--skip-bulk", action="store_true")
    parser.add_argument("--cleanup-only", action="store_true")
    args = parser.parse_args()

    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be > 0")
    if args.workers < 1 or args.workers > 16:
        parser.error("--workers must be between 1 and 16")
    if args.batch_size < 1 or args.batch_size > 999:
        parser.error("--batch-size must be between 1 and 999")
    if args.esi_max_calls_per_minute < 1 or args.esi_max_calls_per_minute > 300:
        parser.error("--esi-max-calls-per-minute must be between 1 and 300")

    if args.cleanup_only:
        raise SystemExit(cleanup_only())
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
