#!/usr/bin/env python3
"""Refresh the complete current sovereignty map from CCP ESI.

One cached public endpoint, one atomic current-state replacement. The
reconciled sovereignty history stores only GAIN/LOST changes detected between
two complete ESI responses.
"""
import argparse
import json
import logging
import random
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values
import requests

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "db.json"
ESI_URL = "https://esi.evetech.net/sovereignty/systems"
USER_AGENT = "EVEOSINT-Sovereignty/1.0 (+https://github.com/DictateurImperator/EVEOSINT)"
COMPATIBILITY_DATE = "2026-09-28"
TIMEOUT = (5, 60)
ATTEMPTS = 4
MAP_SCOPE = "global_systems_2026"
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
        dbname=config["db_name"],
        user=config["db_user"],
        password=config["db_password"],
        host=config["db_host"],
        port=config["db_port"],
    )


def ensure_tables(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS sovereignty")
        cur.execute("""
            SELECT to_regclass('sovereignty.reconciled_map')
        """)
        reconciled_table = cur.fetchone()[0]
        if reconciled_table is not None:
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'sovereignty'
                      AND table_name = 'reconciled_map'
                      AND column_name = 'action'
                )
            """)
            if not cur.fetchone()[0]:
                cur.execute("DROP TABLE sovereignty.reconciled_map")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS sovereignty.reconciled_map (
                event_id BIGSERIAL PRIMARY KEY,
                system_id BIGINT NOT NULL,
                event_at TIMESTAMPTZ NOT NULL,
                action TEXT NOT NULL CHECK (action IN ('GAIN', 'LOST')),
                alliance_id BIGINT,
                corporation_id BIGINT,
                faction_id BIGINT,
                source TEXT NOT NULL CHECK (source IN ('dotlan', 'esi')),
                ownership_model TEXT,
                observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (system_id, event_at, action, source)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS sov_reconciled_map_system_event_idx
            ON sovereignty.reconciled_map (system_id, event_at DESC, event_id DESC)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS sov_reconciled_map_event_idx
            ON sovereignty.reconciled_map (event_at DESC)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sovereignty.current_map (
                system_id BIGINT PRIMARY KEY,
                alliance_id BIGINT,
                corporation_id BIGINT,
                faction_id BIGINT,
                unclaimed BOOLEAN NOT NULL DEFAULT FALSE,
                observed_at TIMESTAMPTZ NOT NULL,
                source TEXT NOT NULL DEFAULT 'esi'
            )
        """)
        cur.execute("""
            ALTER TABLE sovereignty.current_map
            ADD COLUMN IF NOT EXISTS unclaimed BOOLEAN NOT NULL DEFAULT FALSE
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS sov_current_alliance_idx
            ON sovereignty.current_map (alliance_id)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sovereignty.map_changes (
                change_id BIGSERIAL PRIMARY KEY,
                system_id BIGINT NOT NULL,
                change_type TEXT NOT NULL CHECK (change_type IN ('GAIN', 'LOST')),
                old_alliance_id BIGINT,
                old_corporation_id BIGINT,
                old_faction_id BIGINT,
                new_alliance_id BIGINT,
                new_corporation_id BIGINT,
                new_faction_id BIGINT,
                source_observed_at TIMESTAMPTZ NOT NULL,
                detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                source TEXT NOT NULL DEFAULT 'esi'
            )
        """)
        cur.execute("""
            SELECT pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conrelid = 'sovereignty.map_changes'::regclass
              AND conname = 'map_changes_change_type_check'
        """)
        constraint_row = cur.fetchone()
        constraint_def = constraint_row[0] if constraint_row else None
        if constraint_def is None or "TRANSFER" in constraint_def:
            cur.execute("""
                INSERT INTO sovereignty.map_changes (
                    system_id, change_type,
                    old_alliance_id, old_corporation_id, old_faction_id,
                    new_alliance_id, new_corporation_id, new_faction_id,
                    source_observed_at, detected_at, source
                )
                SELECT
                    system_id, 'LOST',
                    old_alliance_id, old_corporation_id, old_faction_id,
                    NULL, NULL, NULL,
                    source_observed_at, detected_at, source
                FROM sovereignty.map_changes
                WHERE change_type = 'TRANSFER'
            """)
            cur.execute("""
                INSERT INTO sovereignty.map_changes (
                    system_id, change_type,
                    old_alliance_id, old_corporation_id, old_faction_id,
                    new_alliance_id, new_corporation_id, new_faction_id,
                    source_observed_at, detected_at, source
                )
                SELECT
                    system_id, 'GAIN',
                    NULL, NULL, NULL,
                    new_alliance_id, new_corporation_id, new_faction_id,
                    source_observed_at, detected_at, source
                FROM sovereignty.map_changes
                WHERE change_type = 'TRANSFER'
            """)
            cur.execute("""
                DELETE FROM sovereignty.map_changes
                WHERE change_type = 'TRANSFER'
            """)
            cur.execute("""
                ALTER TABLE sovereignty.map_changes
                DROP CONSTRAINT IF EXISTS map_changes_change_type_check
            """)
            cur.execute("""
                ALTER TABLE sovereignty.map_changes
                ADD CONSTRAINT map_changes_change_type_check
                CHECK (change_type IN ('GAIN', 'LOST'))
            """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS sov_map_changes_system_date_idx
            ON sovereignty.map_changes (system_id, source_observed_at DESC)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS sov_map_changes_date_idx
            ON sovereignty.map_changes (source_observed_at DESC)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sovereignty.esi_map_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                etag TEXT,
                expires_at TIMESTAMPTZ,
                last_modified TIMESTAMPTZ,
                fetched_at TIMESTAMPTZ,
                row_count INTEGER NOT NULL DEFAULT 0 CHECK (row_count >= 0),
                last_status INTEGER,
                map_scope TEXT
            )
        """)
        cur.execute("""
            ALTER TABLE sovereignty.esi_map_state
            ADD COLUMN IF NOT EXISTS map_scope TEXT
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sovereignty.influence_color_assignments (
                assignment_id BIGSERIAL PRIMARY KEY,
                entity_type TEXT NOT NULL CHECK (entity_type IN ('alliance', 'coalition')),
                entity_id BIGINT NOT NULL,
                color TEXT NOT NULL,
                color_hue DOUBLE PRECISION NOT NULL,
                color_saturation DOUBLE PRECISION NOT NULL,
                color_lightness DOUBLE PRECISION NOT NULL,
                valid_from DATE NOT NULL,
                valid_to DATE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CHECK (valid_to IS NULL OR valid_to >= valid_from)
            )
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS sov_influence_color_active_entity_idx
            ON sovereignty.influence_color_assignments (entity_type, entity_id)
            WHERE valid_to IS NULL
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS sov_influence_color_history_idx
            ON sovereignty.influence_color_assignments (
                entity_type, entity_id, valid_from, valid_to
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sovereignty.influence_color_state (
                entity_type TEXT PRIMARY KEY
                    CHECK (entity_type IN ('alliance', 'coalition')),
                initialized_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
    conn.commit()


def _positive_id(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError("Invalid %s in sovereignty response; current state retained" % label)
    return value


def normalize(payload):
    if not isinstance(payload, dict):
        raise ValueError("ESI sovereignty systems response is not an object; current state retained")
    solar_systems = payload.get("solar_systems")
    if not isinstance(solar_systems, list) or not solar_systems:
        raise ValueError("ESI sovereignty systems list is empty or invalid; current state retained")

    result = {}
    for row in solar_systems:
        if not isinstance(row, dict):
            raise ValueError("Invalid sovereignty system row; current state retained")

        system_id = _positive_id(row.get("solar_system_id"), "solar_system_id")
        if system_id in result:
            raise ValueError("Duplicate solar_system_id in ESI response; current state retained")

        claim = row.get("claim")
        if not isinstance(claim, dict):
            raise ValueError("Missing sovereignty claim; current state retained")

        alliance = claim.get("alliance")
        faction = claim.get("faction")
        unclaimed = claim.get("unclaimed") is True
        variants = int(isinstance(alliance, dict)) + int(isinstance(faction, dict)) + int(unclaimed)
        if variants != 1:
            raise ValueError("Invalid sovereignty claim variant; current state retained")

        if isinstance(alliance, dict):
            alliance_id = _positive_id(alliance.get("alliance_id"), "alliance_id")
            corporation_id = _positive_id(alliance.get("corporation_id"), "corporation_id")
            owner = (alliance_id, corporation_id, None)
        elif isinstance(faction, dict):
            faction_id = _positive_id(faction.get("faction_id"), "faction_id")
            owner = (None, None, faction_id)
        else:
            owner = (None, None, None)

        result[system_id] = owner
    return result


def load_current_map(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT system_id, alliance_id, corporation_id, faction_id
            FROM sovereignty.current_map
        """)
        return {
            int(system_id): (alliance_id, corporation_id, faction_id)
            for system_id, alliance_id, corporation_id, faction_id in cur.fetchall()
        }


def get_state(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT etag, expires_at, row_count, map_scope
            FROM sovereignty.esi_map_state
            WHERE id = 1
        """)
        row = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM sovereignty.current_map")
        stored_count = int(cur.fetchone()[0])
    if not row:
        return None, None, stored_count, None
    return row[0], row[1], stored_count, row[3]


def _has_owner(owner):
    return owner is not None and any(value is not None for value in owner)


def build_changes(previous, current, source_observed_at):
    changes = []
    for system_id in sorted(set(previous) | set(current)):
        old_owner = previous.get(system_id)
        new_owner = current.get(system_id)

        if old_owner == new_owner:
            continue

        if _has_owner(old_owner):
            changes.append((
                system_id,
                "LOST",
                old_owner[0], old_owner[1], old_owner[2],
                None, None, None,
                source_observed_at,
            ))

        if _has_owner(new_owner):
            changes.append((
                system_id,
                "GAIN",
                None, None, None,
                new_owner[0], new_owner[1], new_owner[2],
                source_observed_at,
            ))

    return changes


def update_not_modified(conn, old_count, etag, response):
    expires = http_date(response.headers.get("Expires"))
    modified = http_date(response.headers.get("Last-Modified"))
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sovereignty.esi_map_state (
                id, etag, expires_at, last_modified, fetched_at,
                row_count, last_status, map_scope
            )
            VALUES (1, %s, %s, %s, NOW(), %s, 304, %s)
            ON CONFLICT (id) DO UPDATE SET
                etag = EXCLUDED.etag,
                expires_at = EXCLUDED.expires_at,
                last_modified = COALESCE(
                    EXCLUDED.last_modified,
                    sovereignty.esi_map_state.last_modified
                ),
                fetched_at = EXCLUDED.fetched_at,
                row_count = EXCLUDED.row_count,
                last_status = 304,
                map_scope = EXCLUDED.map_scope
        """, (etag, expires, modified, old_count, MAP_SCOPE))
    conn.commit()


def replace_map(conn, rows, response, previous, record_history):
    observed = http_date(response.headers.get("Last-Modified")) or utcnow()
    values = [
        (
            system_id,
            owner[0],
            owner[1],
            owner[2],
            not _has_owner(owner),
            observed,
        )
        for system_id, owner in rows.items()
    ]
    changes = build_changes(previous, rows, observed) if record_history else []

    # Changes, current state and cache metadata are one transaction. A failed
    # write cannot leave history ahead of current_map or vice versa.
    with conn.cursor() as cur:
        if changes:
            execute_values(cur, """
                INSERT INTO sovereignty.map_changes (
                    system_id,
                    change_type,
                    old_alliance_id,
                    old_corporation_id,
                    old_faction_id,
                    new_alliance_id,
                    new_corporation_id,
                    new_faction_id,
                    source_observed_at
                )
                VALUES %s
            """, changes, page_size=1000)

        cur.execute("DELETE FROM sovereignty.current_map")
        execute_values(cur, """
            INSERT INTO sovereignty.current_map (
                system_id, alliance_id, corporation_id, faction_id, unclaimed, observed_at
            )
            VALUES %s
        """, values, page_size=1000)

        if changes:
            reconciled_values = []
            for (
                system_id,
                action,
                old_alliance_id,
                old_corporation_id,
                old_faction_id,
                new_alliance_id,
                new_corporation_id,
                new_faction_id,
                source_observed_at,
            ) in changes:
                if action == "GAIN":
                    alliance_id = new_alliance_id
                    corporation_id = new_corporation_id
                    faction_id = new_faction_id
                else:
                    alliance_id = old_alliance_id
                    corporation_id = old_corporation_id
                    faction_id = old_faction_id

                reconciled_values.append((
                    system_id,
                    source_observed_at,
                    action,
                    alliance_id,
                    corporation_id,
                    faction_id,
                    "esi",
                    "sovhub",
                    utcnow(),
                ))

            execute_values(cur, """
                INSERT INTO sovereignty.reconciled_map (
                    system_id,
                    event_at,
                    action,
                    alliance_id,
                    corporation_id,
                    faction_id,
                    source,
                    ownership_model,
                    observed_at
                )
                VALUES %s
                ON CONFLICT (system_id, event_at, action, source) DO UPDATE SET
                    alliance_id = EXCLUDED.alliance_id,
                    corporation_id = EXCLUDED.corporation_id,
                    faction_id = EXCLUDED.faction_id,
                    ownership_model = EXCLUDED.ownership_model,
                    observed_at = EXCLUDED.observed_at
            """, reconciled_values, page_size=1000)

        cur.execute("""
            INSERT INTO sovereignty.esi_map_state (
                id, etag, expires_at, last_modified, fetched_at,
                row_count, last_status, map_scope
            )
            VALUES (1, %s, %s, %s, NOW(), %s, 200, %s)
            ON CONFLICT (id) DO UPDATE SET
                etag = EXCLUDED.etag,
                expires_at = EXCLUDED.expires_at,
                last_modified = EXCLUDED.last_modified,
                fetched_at = EXCLUDED.fetched_at,
                row_count = EXCLUDED.row_count,
                last_status = 200,
                map_scope = EXCLUDED.map_scope
        """, (
            response.headers.get("ETag"),
            http_date(response.headers.get("Expires")),
            http_date(response.headers.get("Last-Modified")),
            len(values),
            MAP_SCOPE,
        ))
    conn.commit()
    return len(values), len(changes)


def esi_request(session, etag):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "X-Compatibility-Date": COMPATIBILITY_DATE,
        "X-Tenant": "tranquility",
    }
    if etag:
        headers["If-None-Match"] = etag

    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = session.get(
                ESI_URL,
                headers=headers,
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            LOG.warning(
                "ESI network failure attempt=%d type=%s",
                attempt,
                type(exc).__name__,
            )
            if attempt == ATTEMPTS:
                raise
            time.sleep(
                min(60.0, 3.0 * 2 ** (attempt - 1))
                + random.uniform(0, 1)
            )
            continue

        remain = seconds(response.headers.get("X-ESI-Error-Limit-Remain"))
        reset = seconds(response.headers.get("X-ESI-Error-Limit-Reset"))
        bucket = seconds(response.headers.get("X-Ratelimit-Remaining"))
        status = response.status_code
        LOG.info(
            "ESI status=%s error_remain=%s bucket_remain=%s attempt=%s",
            status,
            remain,
            bucket,
            attempt,
        )

        if status in (200, 304):
            return response

        if remain is not None and remain <= 5:
            raise RuntimeError(
                "ESI error budget low; stopping, wait %.0fs"
                % (reset or 60)
            )

        if status in (420, 429, 500, 502, 503, 504):
            retry_after = seconds(response.headers.get("Retry-After"))
            if attempt == ATTEMPTS:
                break
            if status in (420, 429):
                delay = (
                    retry_after
                    if retry_after is not None
                    else (reset if status == 420 and reset is not None else 900.0)
                )
                delay += 1
            else:
                delay = (
                    min(90.0, 4.0 * 2 ** (attempt - 1))
                    + random.uniform(0, 2)
                )
            LOG.warning(
                "ESI status=%s; retrying in %.1fs",
                status,
                delay,
            )
            time.sleep(delay)
            continue
        break

    raise RuntimeError(
        "ESI sovereignty request failed with HTTP %s; previous map retained"
        % status
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(184624, 1)")
            if not cur.fetchone()[0]:
                LOG.info("Another sovereignty ESI job is already active")
                return 0

        try:
            ensure_tables(conn)
            etag, expires_at, existing, map_scope = get_state(conn)

            # Never bypass CCP's cache window. If this database still contains
            # the old filtered EVEOSINT map, wait for expiry, then request one
            # full 200 response without If-None-Match and adopt it as baseline.
            if (
                map_scope == MAP_SCOPE
                and existing > 0
                and expires_at
                and utcnow() < expires_at
            ):
                LOG.info(
                    "SOV_ESI status=cached systems=%d scope=%s next_fetch=%s",
                    existing,
                    map_scope or "legacy",
                    expires_at.isoformat(),
                )
                return 0

            previous = load_current_map(conn)
            history_ready = bool(previous) and map_scope == MAP_SCOPE

            with requests.Session() as session:
                response = esi_request(
                    session,
                    etag if history_ready else None,
                )

                if response.status_code == 304:
                    if not history_ready:
                        raise RuntimeError(
                            "Unexpected 304 without a complete sovereignty/systems baseline"
                        )
                    update_not_modified(
                        conn,
                        existing,
                        response.headers.get("ETag") or etag,
                        response,
                    )
                    LOG.info(
                        "SOV_ESI status=not_modified systems=%d scope=global_systems_2026",
                        existing,
                    )
                    return 0

                result = normalize(response.json())
                if len(result) < 1000:
                    raise ValueError(
                        "Suspiciously incomplete global ESI map (%d systems); "
                        "current state retained" % len(result)
                    )
                if history_ready and existing >= 1000 and len(result) < existing * 0.70:
                    raise ValueError(
                        "Global ESI map shrank unexpectedly (%d -> %d); "
                        "current state retained" % (existing, len(result))
                    )

                count, change_count = replace_map(
                    conn,
                    result,
                    response,
                    previous,
                    record_history=history_ready,
                )
                if history_ready:
                    LOG.info(
                        "SOV_ESI status=updated systems=%d changes=%d scope=global_systems_2026",
                        count,
                        change_count,
                    )
                else:
                    LOG.info(
                        "SOV_ESI status=baseline systems=%d changes=0 scope=global_systems_2026",
                        count,
                    )
                return 0

        except Exception:
            conn.rollback()
            LOG.exception(
                "Sovereignty ESI refresh failed; existing map and history preserved"
            )
            return 1
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(184624, 1)")
            conn.commit()


if __name__ == "__main__":
    sys.exit(main())
