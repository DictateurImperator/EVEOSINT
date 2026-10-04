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

try:
    from sovereignty_scope import load_claimable_sov_systems
except ModuleNotFoundError:
    from scripts.sovereignty_scope import load_claimable_sov_systems

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


def _sov_owner_key(owner):
    owner = owner or (None, None, None)
    alliance_id = owner[0]
    faction_id = owner[2]

    if alliance_id is not None:
        return ("alliance", int(alliance_id))
    if faction_id is not None:
        return ("faction", int(faction_id))
    return ("unclaimed", None)


def _has_owner(owner):
    return _sov_owner_key(owner)[0] != "unclaimed"


def _same_sov_owner(left, right):
    return _sov_owner_key(left) == _sov_owner_key(right)


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
                faction_id
            FROM sovereignty.reconciled_map
            ORDER BY system_id, event_at DESC, event_id DESC
        """)
        rows = cur.fetchall()

    state = {}
    for system_id, action, alliance_id, faction_id in rows:
        if action == "GAIN":
            state[int(system_id)] = (
                alliance_id,
                None,
                faction_id,
            )
        else:
            state[int(system_id)] = (None, None, None)
    return state


def reconcile(conn, force=False):
    ensure_table(conn)

    claimable_ids = set(load_claimable_sov_systems(conn))
    if len(claimable_ids) < 1000:
        raise RuntimeError(
            "SDE conquerable-nullsec scope looks incomplete (%d systems)"
            % len(claimable_ids)
        )
    claimable_list = sorted(claimable_ids)

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
                WITH grouped AS (
                    SELECT
                        mc.system_id,
                        mc.source_observed_at,
                        MAX(mc.old_alliance_id) FILTER (
                            WHERE mc.change_type = 'LOST'
                        ) AS old_alliance_id,
                        MAX(mc.old_faction_id) FILTER (
                            WHERE mc.change_type = 'LOST'
                        ) AS old_faction_id,
                        MAX(mc.new_alliance_id) FILTER (
                            WHERE mc.change_type = 'GAIN'
                        ) AS new_alliance_id,
                        MAX(mc.new_faction_id) FILTER (
                            WHERE mc.change_type = 'GAIN'
                        ) AS new_faction_id,
                        MIN(mc.detected_at) AS detected_at
                    FROM sovereignty.map_changes mc
                    LEFT JOIN sovereignty.dotlan_system_sync ds
                      ON ds.system_id = mc.system_id
                    WHERE mc.change_type IN ('GAIN', 'LOST')
                      AND mc.system_id = ANY(%s)
                      AND (
                          ds.fetched_at IS NULL
                          OR mc.source_observed_at > ds.fetched_at
                      )
                    GROUP BY mc.system_id, mc.source_observed_at
                ),
                effective AS (
                    SELECT *
                    FROM grouped
                    WHERE
                        CASE
                            WHEN old_alliance_id IS NOT NULL
                                THEN 'alliance:' || old_alliance_id::text
                            WHEN old_faction_id IS NOT NULL
                                THEN 'faction:' || old_faction_id::text
                            ELSE 'unclaimed'
                        END
                        <>
                        CASE
                            WHEN new_alliance_id IS NOT NULL
                                THEN 'alliance:' || new_alliance_id::text
                            WHEN new_faction_id IS NOT NULL
                                THEN 'faction:' || new_faction_id::text
                            ELSE 'unclaimed'
                        END
                ),
                canonical_events AS (
                    SELECT
                        system_id,
                        source_observed_at AS event_at,
                        'LOST'::text AS action,
                        old_alliance_id AS alliance_id,
                        old_faction_id AS faction_id,
                        detected_at
                    FROM effective
                    WHERE old_alliance_id IS NOT NULL
                       OR old_faction_id IS NOT NULL

                    UNION ALL

                    SELECT
                        system_id,
                        source_observed_at AS event_at,
                        'GAIN'::text AS action,
                        new_alliance_id AS alliance_id,
                        new_faction_id AS faction_id,
                        detected_at
                    FROM effective
                    WHERE new_alliance_id IS NOT NULL
                       OR new_faction_id IS NOT NULL
                )
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
                    event_at,
                    action,
                    alliance_id,
                    NULL,
                    faction_id,
                    'esi',
                    'sovhub',
                    detected_at
                FROM canonical_events
                ORDER BY event_at, system_id, action
                ON CONFLICT (system_id, event_at, action, source) DO UPDATE SET
                    alliance_id = EXCLUDED.alliance_id,
                    corporation_id = NULL,
                    faction_id = EXCLUDED.faction_id,
                    ownership_model = EXCLUDED.ownership_model,
                    observed_at = EXCLUDED.observed_at
            """, (claimable_list,))
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
                faction_id,
                observed_at
            FROM sovereignty.current_map
            WHERE system_id = ANY(%s)
            ORDER BY system_id
        """, (claimable_list,))
        current_rows = cur.fetchall()

    corrections = []
    for (
        system_id,
        alliance_id,
        faction_id,
        observed_at,
    ) in current_rows:
        system_id = int(system_id)
        old_owner = reconciled_state.get(system_id, (None, None, None))
        new_owner = (alliance_id, None, faction_id)

        if _same_sov_owner(old_owner, new_owner):
            continue

        if _has_owner(old_owner):
            corrections.append((
                system_id,
                observed_at,
                "LOST",
                old_owner[0],
                None,
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
                None,
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
                    corporation_id = NULL,
                    faction_id = EXCLUDED.faction_id,
                    ownership_model = EXCLUDED.ownership_model,
                    observed_at = EXCLUDED.observed_at
            """, corrections, page_size=1000)
        conn.commit()

    with conn.cursor() as cur:
        cur.execute("""
            UPDATE sovereignty.reconciled_map
            SET corporation_id = NULL
            WHERE corporation_id IS NOT NULL
        """)
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
        "gain=%d lost=%d imported_dotlan=%d imported_esi=%d corrections=%d scope=%d",
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
        len(claimable_ids),
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
