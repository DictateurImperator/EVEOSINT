#!/usr/bin/env python3
"""One-shot reconciliation of existing DOTLAN history with existing ESI SOV data.

Run once after DOTLAN history and the first ESI sovereignty snapshot already
exist. The reconciled table contains only ownership-change events:
GAIN at date X, LOST at date Y.

DOTLAN is imported once. Existing ESI GAIN/LOST changes are appended after the
DOTLAN hand-off point. The current ESI map is used only to correct the final
state when needed; it is never copied as daily snapshots.

After this one-shot, sync_sovereignty_esi.py appends future ESI GAIN/LOST events.
"""
import argparse
import json
import logging
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "db.json"
LOG = logging.getLogger("sov_reconcile")


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


def _has_owner(owner):
    return owner is not None and any(value is not None for value in owner)


def _same_sov_owner(left, right):
    left = left or (None, None, None)
    right = right or (None, None, None)

    left_alliance = left[0]
    right_alliance = right[0]
    if left_alliance is not None or right_alliance is not None:
        return left_alliance == right_alliance

    # Corporation changes are irrelevant for sovereignty ownership.
    return left[2] == right[2]


def ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS sovereignty")

        cur.execute("SELECT to_regclass('sovereignty.reconciled_map')")
        table_exists = cur.fetchone()[0] is not None
        if table_exists:
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
                # _018 briefly used a snapshot-shaped reconciled table.
                # Its content is fully reproducible from DOTLAN + ESI sources.
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
            ON sovereignty.reconciled_map (
                system_id,
                event_at DESC,
                event_id DESC
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS sov_reconciled_map_event_idx
            ON sovereignty.reconciled_map (event_at DESC)
        """)
    conn.commit()


def _load_latest_reconciled_state(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (system_id)
                system_id,
                action,
                alliance_id,
                corporation_id,
                faction_id
            FROM sovereignty.reconciled_map
            ORDER BY system_id, event_at DESC, event_id DESC
        """)
        rows = cur.fetchall()

    state = {}
    for system_id, action, alliance_id, corporation_id, faction_id in rows:
        if action == "GAIN":
            state[int(system_id)] = (
                alliance_id,
                corporation_id,
                faction_id,
            )
        else:
            state[int(system_id)] = (None, None, None)
    return state


def reconcile(conn, force=False):
    ensure_table(conn)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*)
            FROM sovereignty.reconciled_map
            WHERE source = 'dotlan'
        """)
        existing_dotlan = int(cur.fetchone()[0])
        if existing_dotlan and not force:
            raise RuntimeError(
                "DOTLAN has already been reconciled into reconciled_map; "
                "this job is one-shot (use --force only to rebuild intentionally)"
            )

        cur.execute("SELECT to_regclass('sovereignty.dotlan_events')")
        if cur.fetchone()[0] is None:
            raise RuntimeError("sovereignty.dotlan_events is missing")

        cur.execute("SELECT to_regclass('sovereignty.current_map')")
        if cur.fetchone()[0] is None:
            raise RuntimeError("sovereignty.current_map is missing")

        cur.execute("SELECT COUNT(*) FROM sovereignty.current_map")
        current_count = int(cur.fetchone()[0])
        if current_count == 0:
            raise RuntimeError("sovereignty.current_map is empty")

        if force:
            cur.execute("TRUNCATE sovereignty.reconciled_map RESTART IDENTITY")

        # ONE SHOT DOTLAN import. Keep only actual ownership changes.
        cur.execute("""
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
            SELECT
                system_id,
                event_at AT TIME ZONE 'UTC',
                action,
                alliance_id,
                NULL,
                NULL,
                'dotlan',
                ownership_model,
                fetched_at
            FROM sovereignty.dotlan_events
            WHERE action IN ('GAIN', 'LOST')
              AND (action = 'LOST' OR alliance_id IS NOT NULL)
            ORDER BY
                system_id,
                event_at,
                CASE action WHEN 'LOST' THEN 0 ELSE 1 END,
                row_position DESC
            ON CONFLICT (system_id, event_at, action, source) DO UPDATE SET
                alliance_id = EXCLUDED.alliance_id,
                ownership_model = EXCLUDED.ownership_model,
                observed_at = EXCLUDED.observed_at
        """)
        dotlan_rows = cur.rowcount

        # Existing ESI changes collected after each system's DOTLAN crawl are
        # the hand-off from the historical one-shot to the live ESI stream.
        cur.execute("SELECT to_regclass('sovereignty.map_changes')")
        changes_exists = cur.fetchone()[0] is not None
        esi_change_rows = 0

        if changes_exists:
            cur.execute("""
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
                SELECT
                    mc.system_id,
                    mc.source_observed_at,
                    mc.change_type,
                    CASE
                        WHEN mc.change_type = 'GAIN' THEN mc.new_alliance_id
                        ELSE mc.old_alliance_id
                    END,
                    CASE
                        WHEN mc.change_type = 'GAIN' THEN mc.new_corporation_id
                        ELSE mc.old_corporation_id
                    END,
                    CASE
                        WHEN mc.change_type = 'GAIN' THEN mc.new_faction_id
                        ELSE mc.old_faction_id
                    END,
                    'esi',
                    'sovhub',
                    mc.detected_at
                FROM sovereignty.map_changes mc
                LEFT JOIN sovereignty.dotlan_system_sync ds
                  ON ds.system_id = mc.system_id
                WHERE mc.change_type IN ('GAIN', 'LOST')
                  AND (
                      ds.fetched_at IS NULL
                      OR mc.source_observed_at > ds.fetched_at
                  )
                ORDER BY mc.source_observed_at, mc.change_id
                ON CONFLICT (system_id, event_at, action, source) DO UPDATE SET
                    alliance_id = EXCLUDED.alliance_id,
                    corporation_id = EXCLUDED.corporation_id,
                    faction_id = EXCLUDED.faction_id,
                    ownership_model = EXCLUDED.ownership_model,
                    observed_at = EXCLUDED.observed_at
            """)
            esi_change_rows = cur.rowcount

    conn.commit()

    # Use current_map only as a final reconciliation check. We emit corrections
    # only when the event replay does not match the first/current ESI snapshot.
    reconciled_state = _load_latest_reconciled_state(conn)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                system_id,
                alliance_id,
                corporation_id,
                faction_id,
                observed_at
            FROM sovereignty.current_map
            ORDER BY system_id
        """)
        current_rows = cur.fetchall()

    corrections = []
    for (
        system_id,
        alliance_id,
        corporation_id,
        faction_id,
        observed_at,
    ) in current_rows:
        system_id = int(system_id)
        old_owner = reconciled_state.get(system_id, (None, None, None))
        new_owner = (alliance_id, corporation_id, faction_id)

        if _same_sov_owner(old_owner, new_owner):
            continue

        if _has_owner(old_owner):
            corrections.append((
                system_id,
                observed_at,
                "LOST",
                old_owner[0],
                old_owner[1],
                old_owner[2],
                "esi",
                "sovhub",
                observed_at,
            ))

        if _has_owner(new_owner):
            corrections.append((
                system_id,
                observed_at,
                "GAIN",
                new_owner[0],
                new_owner[1],
                new_owner[2],
                "esi",
                "sovhub",
                observed_at,
            ))

    if corrections:
        with conn.cursor() as cur:
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
            """, corrections, page_size=1000)
        conn.commit()

    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                COUNT(*),
                MIN(event_at),
                MAX(event_at),
                COUNT(*) FILTER (WHERE source = 'dotlan'),
                COUNT(*) FILTER (WHERE source = 'esi'),
                COUNT(*) FILTER (WHERE action = 'GAIN'),
                COUNT(*) FILTER (WHERE action = 'LOST')
            FROM sovereignty.reconciled_map
        """)
        (
            total,
            min_event,
            max_event,
            dotlan_total,
            esi_total,
            gain_total,
            lost_total,
        ) = cur.fetchone()

    LOG.info(
        "SOV_RECONCILE done rows=%d range=%s..%s dotlan=%d esi=%d "
        "gain=%d lost=%d imported_dotlan=%d imported_esi=%d corrections=%d",
        total,
        min_event,
        max_event,
        dotlan_total,
        esi_total,
        gain_total,
        lost_total,
        dotlan_rows,
        esi_change_rows,
        len(corrections),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild reconciled_map even if DOTLAN was already imported.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(184624, 3)")
            if not cur.fetchone()[0]:
                LOG.error("Another sovereignty reconciliation job is already active")
                return 1

        try:
            reconcile(conn, force=args.force)
            return 0
        except Exception:
            conn.rollback()
            LOG.exception("Sovereignty reconciliation failed")
            return 1
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(184624, 3)")
            conn.commit()


if __name__ == "__main__":
    sys.exit(main())
