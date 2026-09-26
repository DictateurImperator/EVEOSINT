from datetime import date, datetime, timezone

from psycopg2.extras import execute_values

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.db import db
    from app.population_alliances_init import (
        DotlanClient,
        REQUEST_INTERVAL_SECONDS,
        discover_alliances,
        ensure_tables,
        initialize_alliance,
        parse_stats_page,
        upsert_daily_rows,
        upsert_sync_success,
    )
else:
    from .db import db
    from .population_alliances_init import (
        DotlanClient,
        REQUEST_INTERVAL_SECONDS,
        discover_alliances,
        ensure_tables,
        initialize_alliance,
        parse_stats_page,
        upsert_daily_rows,
        upsert_sync_success,
    )


def load_sync_index(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT alliance_id,
                   alliance_name,
                   dotlan_slug,
                   first_available_date,
                   oldest_synced_date,
                   last_synced_date,
                   initialization_done
            FROM population.alliance_sync
            """
        )
        rows = cur.fetchall()

    by_slug = {}
    by_id = {}
    for row in rows:
        item = {
            "alliance_id": row[0],
            "alliance_name": row[1],
            "dotlan_slug": row[2],
            "first_available_date": row[3],
            "oldest_synced_date": row[4],
            "last_synced_date": row[5],
            "initialization_done": bool(row[6]),
        }
        if item["dotlan_slug"]:
            by_slug[item["dotlan_slug"]] = item
        by_id[item["alliance_id"]] = item

    return by_slug, by_id


def save_known_live_snapshots(conn, records, snapshot_date):
    if not records:
        return

    daily_values = [
        (
            item["alliance_id"],
            snapshot_date,
            item["members"],
            item["corporations"],
            item["systems"],
        )
        for item in records
    ]

    sync_values = [
        (
            item["alliance_id"],
            item["name"],
            item["slug"],
            snapshot_date,
        )
        for item in records
    ]

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO population.alliance_daily (
                alliance_id,
                snapshot_date,
                member_count,
                corporation_count,
                sovereignty_count
            ) VALUES %s
            ON CONFLICT (alliance_id, snapshot_date) DO UPDATE SET
                member_count = EXCLUDED.member_count,
                corporation_count = EXCLUDED.corporation_count,
                sovereignty_count = EXCLUDED.sovereignty_count,
                source = 'dotlan',
                updated_at = now()
            """,
            daily_values,
            page_size=1000,
        )

        execute_values(
            cur,
            """
            UPDATE population.alliance_sync AS s
            SET alliance_name = v.alliance_name,
                dotlan_slug = v.dotlan_slug,
                last_synced_date = CASE
                    WHEN s.last_synced_date IS NULL THEN v.snapshot_date
                    ELSE GREATEST(s.last_synced_date, v.snapshot_date)
                END,
                last_attempt_at = now(),
                last_success_at = now(),
                last_error = NULL,
                updated_at = now()
            FROM (VALUES %s) AS v(alliance_id, alliance_name, dotlan_slug, snapshot_date)
            WHERE s.alliance_id = v.alliance_id
            """,
            sync_values,
            page_size=1000,
        )

    conn.commit()


def resolve_unknown_live_alliance(conn, client, alliance, snapshot_date):
    """Resolve a live DOTLAN slug to its EVE alliance ID with one small stats call.

    This also handles alliance renames: if the resolved alliance_id is already known,
    the existing sync row is reused and only its current DOTLAN name/slug is updated.
    """
    # Use the normal stats page only to resolve the stable EVE alliance_id.
    # The actual current snapshot always comes from the global ranking page.
    html = client.get(f"{alliance['href']}/stats")
    parsed = parse_stats_page(html)
    alliance_id = parsed["alliance_id"]

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT initialization_done
            FROM population.alliance_sync
            WHERE alliance_id = %s
            """,
            (alliance_id,),
        )
        existing = cur.fetchone()

    current_row = {
        "snapshot_date": snapshot_date,
        "member_count": alliance["members"],
        "corporation_count": alliance["corporations"],
        "sovereignty_count": alliance["systems"],
    }

    upsert_daily_rows(conn, alliance_id, [current_row])
    upsert_sync_success(
        conn,
        alliance_id,
        alliance["name"],
        alliance["slug"],
        parsed["first_available_date"],
        [current_row],
        bool(existing and existing[0]),
    )

    return alliance_id, bool(existing and existing[0])


def main():
    started = datetime.now(timezone.utc)
    snapshot_date = date.today()

    print(
        f"=== Population live alliance update started {started.isoformat()} ===",
        flush=True,
    )
    print(
        f"Snapshot date: {snapshot_date.isoformat()} | "
        f"DOTLAN throttle: 1 request / {REQUEST_INTERVAL_SECONDS:.0f}s",
        flush=True,
    )

    client = DotlanClient()
    conn = db()

    stats = {
        "snapshot_saved": 0,
        "new_or_renamed": 0,
        "resolved": 0,
        "resolve_errors": 0,
        "backfill_completed": 0,
        "backfill_skipped": 0,
        "backfill_errors": 0,
    }

    try:
        ensure_tables(conn)
        alliances, page_count, advertised_total = discover_alliances(client)
        print(
            f"[DISCOVERY] complete pages={page_count} unique_alliances={len(alliances)} "
            f"advertised_total={advertised_total or '?'}",
            flush=True,
        )

        sync_by_slug, _sync_by_id = load_sync_index(conn)

        known = []
        unresolved = []
        backfill_queue = []

        for alliance in alliances:
            sync = sync_by_slug.get(alliance["slug"])
            if sync is None:
                unresolved.append(alliance)
                continue

            item = dict(alliance)
            item["alliance_id"] = sync["alliance_id"]
            known.append(item)

            if not sync["initialization_done"]:
                backfill_queue.append(alliance)

        # Save the whole known live snapshot first. Historical backfills are done only
        # after the current-day snapshot is safely committed for every known alliance.
        save_known_live_snapshots(conn, known, snapshot_date)
        stats["snapshot_saved"] += len(known)
        print(
            f"[SNAPSHOT] saved={len(known)} unresolved={len(unresolved)} "
            f"backfill_pending={len(backfill_queue)}",
            flush=True,
        )

        # Unknown slug can mean a genuinely new alliance or an existing alliance that
        # was renamed on DOTLAN. Resolve the EVE alliance_id before deciding whether a
        # historical initialization is actually required.
        for index, alliance in enumerate(unresolved, start=1):
            print(
                f"[RESOLVE {index}/{len(unresolved)}] {alliance['name']} "
                f"slug={alliance['slug']}",
                flush=True,
            )
            try:
                alliance_id, already_initialized = resolve_unknown_live_alliance(
                    conn,
                    client,
                    alliance,
                    snapshot_date,
                )
            except Exception as exc:
                stats["resolve_errors"] += 1
                print(
                    f"[RESOLVE {index}/{len(unresolved)}] ERROR "
                    f"{alliance['name']}: {exc}",
                    flush=True,
                )
                continue

            stats["new_or_renamed"] += 1
            stats["resolved"] += 1
            stats["snapshot_saved"] += 1

            print(
                f"[RESOLVE {index}/{len(unresolved)}] OK {alliance['name']} "
                f"alliance_id={alliance_id} "
                f"initialized={'yes' if already_initialized else 'no'}",
                flush=True,
            )

            if not already_initialized:
                backfill_queue.append(alliance)

        print(
            f"[SNAPSHOT] complete saved={stats['snapshot_saved']}/{len(alliances)} "
            f"resolve_errors={stats['resolve_errors']} http_calls={client.request_count}",
            flush=True,
        )

        # Only now do potentially long historical work. If this part is interrupted,
        # initialize_alliance() resumes from alliance_sync.oldest_synced_date next run.
        total_backfill = len(backfill_queue)
        print(
            f"[BACKFILL] queued={total_backfill}",
            flush=True,
        )

        for index, alliance in enumerate(backfill_queue, start=1):
            result = initialize_alliance(
                conn,
                client,
                alliance,
                index,
                total_backfill,
            )
            if result == "completed":
                stats["backfill_completed"] += 1
            elif result == "skipped":
                stats["backfill_skipped"] += 1
            else:
                stats["backfill_errors"] += 1

            print(
                f"[BACKFILL PROGRESS] {index}/{total_backfill} "
                f"completed={stats['backfill_completed']} "
                f"skipped={stats['backfill_skipped']} "
                f"errors={stats['backfill_errors']} "
                f"http_calls={client.request_count}",
                flush=True,
            )

    finally:
        conn.close()

    finished = datetime.now(timezone.utc)
    print(
        f"=== Population live alliance update finished {finished.isoformat()} "
        f"duration={finished - started} snapshot_saved={stats['snapshot_saved']} "
        f"new_or_renamed={stats['new_or_renamed']} "
        f"resolve_errors={stats['resolve_errors']} "
        f"backfill_completed={stats['backfill_completed']} "
        f"backfill_skipped={stats['backfill_skipped']} "
        f"backfill_errors={stats['backfill_errors']} "
        f"http_calls={client.request_count} ===",
        flush=True,
    )


if __name__ == "__main__":
    main()
