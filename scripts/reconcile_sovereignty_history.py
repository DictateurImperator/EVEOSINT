#!/usr/bin/env python3
"""One-shot reconciliation of existing DOTLAN history with existing ESI SOV data.

Run once after DOTLAN history and the first ESI sovereignty snapshot already exist.
DOTLAN contributes only GAIN / LOST ownership checkpoints. Existing ESI history
then overrides DOTLAN, and the current ESI map is written as the latest snapshot.
After this one-shot, sync_sovereignty_esi.py keeps the table updated daily.
"""
import argparse
import json
import logging
import sys
from pathlib import Path

import psycopg2

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


def ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS sovereignty")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sovereignty.reconciled_map (
                day DATE NOT NULL,
                system_id BIGINT NOT NULL,
                alliance_id BIGINT,
                corporation_id BIGINT,
                faction_id BIGINT,
                source TEXT NOT NULL CHECK (source IN ('dotlan', 'esi')),
                observed_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (day, system_id)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS sov_reconciled_map_system_day_idx
            ON sovereignty.reconciled_map (system_id, day DESC)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS sov_reconciled_map_day_idx
            ON sovereignty.reconciled_map (day)
        """)
    conn.commit()


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
            cur.execute("TRUNCATE sovereignty.reconciled_map")

        # DOTLAN is imported ONCE. Only ownership changes matter:
        # GAIN => owner becomes alliance, LOST => system becomes unclaimed.
        # If multiple ownership events happen on the same day, keep the last one.
        cur.execute("""
            INSERT INTO sovereignty.reconciled_map (
                day,
                system_id,
                alliance_id,
                corporation_id,
                faction_id,
                source,
                observed_at
            )
            SELECT
                event_at::date,
                system_id,
                CASE WHEN action = 'GAIN' THEN alliance_id ELSE NULL END,
                NULL,
                NULL,
                'dotlan',
                event_at AT TIME ZONE 'UTC'
            FROM (
                SELECT DISTINCT ON (system_id, event_at::date)
                    system_id,
                    event_at,
                    action,
                    alliance_id,
                    row_position
                FROM sovereignty.dotlan_events
                WHERE action IN ('GAIN', 'LOST')
                  AND (action = 'LOST' OR alliance_id IS NOT NULL)
                ORDER BY
                    system_id,
                    event_at::date,
                    event_at DESC,
                    CASE WHEN action = 'GAIN' THEN 1 ELSE 0 END DESC,
                    row_position DESC
            ) final_event
            ORDER BY system_id, event_at
            ON CONFLICT (day, system_id) DO NOTHING
        """)
        dotlan_rows = cur.rowcount

        # Preserve any ESI change history already collected before this one-shot.
        cur.execute("SELECT to_regclass('sovereignty.map_changes')")
        changes_exists = cur.fetchone()[0] is not None
        esi_change_rows = 0
        if changes_exists:
            cur.execute("""
                INSERT INTO sovereignty.reconciled_map (
                    day,
                    system_id,
                    alliance_id,
                    corporation_id,
                    faction_id,
                    source,
                    observed_at
                )
                SELECT
                    (source_observed_at AT TIME ZONE 'UTC')::date,
                    system_id,
                    CASE WHEN change_type = 'GAIN' THEN new_alliance_id ELSE NULL END,
                    CASE WHEN change_type = 'GAIN' THEN new_corporation_id ELSE NULL END,
                    CASE WHEN change_type = 'GAIN' THEN new_faction_id ELSE NULL END,
                    'esi',
                    source_observed_at
                FROM (
                    SELECT DISTINCT ON (
                        system_id,
                        (source_observed_at AT TIME ZONE 'UTC')::date
                    )
                        change_id,
                        system_id,
                        change_type,
                        new_alliance_id,
                        new_corporation_id,
                        new_faction_id,
                        source_observed_at
                    FROM sovereignty.map_changes
                    WHERE change_type IN ('GAIN', 'LOST')
                    ORDER BY
                        system_id,
                        (source_observed_at AT TIME ZONE 'UTC')::date,
                        source_observed_at DESC,
                        change_id DESC
                ) final_change
                ON CONFLICT (day, system_id) DO UPDATE SET
                    alliance_id = EXCLUDED.alliance_id,
                    corporation_id = EXCLUDED.corporation_id,
                    faction_id = EXCLUDED.faction_id,
                    source = 'esi',
                    observed_at = EXCLUDED.observed_at
            """)
            esi_change_rows = cur.rowcount

        # The already-existing ESI current map is the hand-off point.
        # ESI wins on any same-day conflict with DOTLAN.
        cur.execute("""
            INSERT INTO sovereignty.reconciled_map (
                day,
                system_id,
                alliance_id,
                corporation_id,
                faction_id,
                source,
                observed_at
            )
            SELECT
                (observed_at AT TIME ZONE 'UTC')::date,
                system_id,
                alliance_id,
                corporation_id,
                faction_id,
                'esi',
                observed_at
            FROM sovereignty.current_map
            ON CONFLICT (day, system_id) DO UPDATE SET
                alliance_id = EXCLUDED.alliance_id,
                corporation_id = EXCLUDED.corporation_id,
                faction_id = EXCLUDED.faction_id,
                source = 'esi',
                observed_at = EXCLUDED.observed_at
        """)
        esi_snapshot_rows = cur.rowcount

        cur.execute("""
            SELECT
                COUNT(*),
                MIN(day),
                MAX(day),
                COUNT(*) FILTER (WHERE source = 'dotlan'),
                COUNT(*) FILTER (WHERE source = 'esi')
            FROM sovereignty.reconciled_map
        """)
        total, min_day, max_day, dotlan_total, esi_total = cur.fetchone()

    conn.commit()

    LOG.info(
        "SOV_RECONCILE done rows=%d range=%s..%s dotlan=%d esi=%d "
        "inserted_dotlan=%d merged_esi_changes=%d current_esi=%d",
        total,
        min_day,
        max_day,
        dotlan_total,
        esi_total,
        dotlan_rows,
        esi_change_rows,
        esi_snapshot_rows,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild reconciled_map even if it already contains rows.",
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
