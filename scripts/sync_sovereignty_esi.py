#!/usr/bin/env python3
"""Refresh the complete current sovereignty map from CCP ESI.

One cached public endpoint, one atomic database replacement. No browser/server
requests are triggered by page views. Run manually or from update_pipeline.
"""
import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values
import requests

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "db.json"
ESI_URL = "https://esi.evetech.net/latest/sovereignty/map/"
USER_AGENT = "EVEOSINT-Sovereignty/1.0 (+https://github.com/DictateurImperator/EVEOSINT)"
COMPATIBILITY_DATE = "2026-09-28"
TIMEOUT = (5, 60)
ATTEMPTS = 4
LOG = logging.getLogger("sov_esi")


def utcnow():
    return datetime.now(timezone.utc)


def http_date(raw):
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def seconds(raw, default=None):
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        if raw:
            retry_date = http_date(raw)
            if retry_date:
                return max(0.0, (retry_date - utcnow()).total_seconds())
        return default


def connect():
    with CONFIG_PATH.open(encoding="utf-8") as fp:
        config = json.load(fp)
    return psycopg2.connect(
        dbname=config["db_name"], user=config["db_user"],
        password=config["db_password"], host=config["db_host"],
        port=config["db_port"],
    )


def ensure_tables(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS sovereignty")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sovereignty.current_map (
                system_id BIGINT PRIMARY KEY,
                alliance_id BIGINT,
                corporation_id BIGINT,
                faction_id BIGINT,
                observed_at TIMESTAMPTZ NOT NULL,
                source TEXT NOT NULL DEFAULT 'esi'
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS sov_current_alliance_idx
            ON sovereignty.current_map (alliance_id)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sovereignty.esi_map_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                etag TEXT,
                expires_at TIMESTAMPTZ,
                last_modified TIMESTAMPTZ,
                fetched_at TIMESTAMPTZ,
                row_count INTEGER NOT NULL DEFAULT 0 CHECK (row_count >= 0),
                last_status INTEGER
            )
        """)
    conn.commit()


def normalize(payload):
    if not isinstance(payload, list) or not payload:
        raise ValueError("ESI sovereignty map is empty or not an array; current state retained")
    result = {}
    for row in payload:
        if not isinstance(row, dict) or type(row.get("system_id")) is not int or row["system_id"] <= 0:
            raise ValueError("Invalid sovereignty system; current state retained")
        system_id = row["system_id"]
        if system_id in result:
            raise ValueError("Duplicate system_id in ESI response; current state retained")
        values = []
        for key in ("alliance_id", "corporation_id", "faction_id"):
            value = row.get(key)
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError("Invalid sovereignty owner; current state retained")
            values.append(value)
        if all(value is None for value in values):
            raise ValueError("Ownerless sovereignty row; current state retained")
        result[system_id] = tuple(values)
    return result


def get_state(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT etag, expires_at, row_count
            FROM sovereignty.esi_map_state WHERE id = 1
        """)
        row = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM sovereignty.current_map")
        stored_count = cur.fetchone()[0]
    if not row:
        return None, None, stored_count, stored_count
    return row[0], row[1], row[2], stored_count


def update_not_modified(conn, old_count, etag, response):
    expires = http_date(response.headers.get("Expires"))
    modified = http_date(response.headers.get("Last-Modified"))
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sovereignty.esi_map_state
                (id, etag, expires_at, last_modified, fetched_at, row_count, last_status)
            VALUES (1, %s, %s, %s, NOW(), %s, 304)
            ON CONFLICT (id) DO UPDATE SET
                etag = EXCLUDED.etag,
                expires_at = EXCLUDED.expires_at,
                last_modified = COALESCE(EXCLUDED.last_modified, sovereignty.esi_map_state.last_modified),
                fetched_at = EXCLUDED.fetched_at,
                last_status = 304
        """, (etag, expires, modified, old_count))
    conn.commit()


def replace_map(conn, rows, response):
    # Validation must finish before the first DELETE; both DELETE and INSERT
    # and cache metadata are committed as one transaction.
    observed = http_date(response.headers.get("Last-Modified")) or utcnow()
    values = [
        (system_id, owner[0], owner[1], owner[2], observed)
        for system_id, owner in rows.items()
    ]
    with conn.cursor() as cur:
        cur.execute("DELETE FROM sovereignty.current_map")
        execute_values(cur, """
            INSERT INTO sovereignty.current_map
                (system_id, alliance_id, corporation_id, faction_id, observed_at)
            VALUES %s
        """, values, page_size=1000)
        cur.execute("""
            INSERT INTO sovereignty.esi_map_state
                (id, etag, expires_at, last_modified, fetched_at, row_count, last_status)
            VALUES (1, %s, %s, %s, NOW(), %s, 200)
            ON CONFLICT (id) DO UPDATE SET
                etag = EXCLUDED.etag,
                expires_at = EXCLUDED.expires_at,
                last_modified = EXCLUDED.last_modified,
                fetched_at = EXCLUDED.fetched_at,
                row_count = EXCLUDED.row_count,
                last_status = 200
        """, (
            response.headers.get("ETag"),
            http_date(response.headers.get("Expires")),
            http_date(response.headers.get("Last-Modified")),
            len(values),
        ))
    conn.commit()
    return len(values)


def esi_request(session, etag):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "X-Compatibility-Date": COMPATIBILITY_DATE,
    }
    if etag:
        headers["If-None-Match"] = etag
    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = session.get(
                ESI_URL, params={"datasource": "tranquility"},
                headers=headers, timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            LOG.warning("ESI network failure attempt=%d type=%s", attempt, type(exc).__name__)
            if attempt == ATTEMPTS:
                raise
            time.sleep(min(60.0, 3.0 * 2 ** (attempt - 1)) + random.uniform(0, 1))
            continue

        remain = seconds(response.headers.get("X-ESI-Error-Limit-Remain"))
        reset = seconds(response.headers.get("X-ESI-Error-Limit-Reset"))
        bucket = seconds(response.headers.get("X-Ratelimit-Remaining"))
        status = response.status_code
        LOG.info("ESI status=%s error_remain=%s bucket_remain=%s attempt=%s",
                 status, remain, bucket, attempt)

        if status in (200, 304):
            return response

        # Stop on a low old-style error budget instead of generating new errors.
        if remain is not None and remain <= 5:
            raise RuntimeError("ESI error budget low; stopping, wait %.0fs" % (reset or 60))

        if status in (420, 429, 500, 502, 503, 504):
            retry_after = seconds(response.headers.get("Retry-After"))
            if attempt == ATTEMPTS:
                break
            if status in (420, 429):
                delay = retry_after if retry_after is not None else (reset if status == 420 and reset is not None else 900.0)
                delay += 1
            else:
                delay = min(90.0, 4.0 * 2 ** (attempt - 1)) + random.uniform(0, 2)
            LOG.warning("ESI status=%s; retrying in %.1fs", status, delay)
            time.sleep(delay)
            continue
        break
    raise RuntimeError("ESI sovereignty request failed with HTTP %s; previous map retained" % status)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Ignore cached Expires (manual investigation only).")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    with connect() as conn:
        # Single-job advisory lock: do not allow concurrent ESI updates.
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(184624, 1)")
            if not cur.fetchone()[0]:
                LOG.info("Another sovereignty ESI job is already active")
                return 0
        try:
            ensure_tables(conn)
            etag, expires_at, expected, existing = get_state(conn)
            if not args.force and existing > 0 and expected == existing and expires_at and utcnow() < expires_at:
                LOG.info("SOV_ESI status=cached systems=%d next_fetch=%s", existing, expires_at.isoformat())
                return 0

            with requests.Session() as session:
                response = esi_request(session, etag if existing > 0 and expected == existing else None)
                if response.status_code == 304:
                    if existing <= 0 or expected != existing:
                        raise RuntimeError("Unexpected 304 without a valid local map")
                    update_not_modified(conn, existing, response.headers.get("ETag") or etag, response)
                    LOG.info("SOV_ESI status=not_modified systems=%d", existing)
                    return 0
                result = normalize(response.json())
                count = replace_map(conn, result, response)
                LOG.info("SOV_ESI status=updated systems=%d", count)
                return 0
        except Exception:
            conn.rollback()
            LOG.exception("Sovereignty ESI refresh failed; existing map preserved")
            return 1
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(184624, 1)")
            conn.commit()


if __name__ == "__main__":
    sys.exit(main())
