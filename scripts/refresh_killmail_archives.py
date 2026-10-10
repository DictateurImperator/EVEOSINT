#!/usr/bin/env python3
"""Manually refresh changed, previously imported EVE Ref daily archives.

No scheduler, CCP requests or zKillboard submissions. The first run uses totals
as a baseline so unchanged historical archives need not be downloaded again.
"""

import argparse
import fcntl
import json
import logging
import os
import sys
import tarfile
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path

import requests

BASE_URL = "https://data.everef.net/killmails"
USER_AGENT = "EVEOSINT-ArchiveRefresh/1.0 (github.com/DictateurImperator/EVEOSINT)"
LOG = logging.getLogger(__name__)
BATCH_SIZE = 500


def load_state(path):
    if not path.exists():
        return {"archives": {}, "pending_months": []}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state.get("archives"), dict) or not isinstance(state.get("pending_months"), list):
        raise TypeError("Invalid archive refresh checkpoint")
    return state


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def get_json(session, url):
    with session.get(url, timeout=(15, 120)) as response:
        response.raise_for_status()
        return response.json()


def year_index(session, year):
    result = {}
    for entry in get_json(session, f"{BASE_URL}/{year}/index.json")["files"]:
        name = entry.get("name", "")
        if not name.startswith("killmails-") or not name.endswith(".tar.bz2"):
            continue
        try:
            day = date.fromisoformat(name[len("killmails-"):-len(".tar.bz2")])
        except ValueError:
            continue
        if day.year == year:
            result[day.isoformat()] = entry
    return result


def signature(entry):
    return {key: entry.get(key) for key in ("etag", "last_modified", "size")}


def needs_refresh(local_count, remote_count, previous, entry):
    # Counts also detect changes on the first run and interrupted earlier runs.
    if remote_count is not None and local_count is not None:
        if remote_count < local_count:
            raise ValueError("EVE Ref reports fewer killmails than the successful local import")
        if remote_count != local_count:
            return True
        if previous is None:
            return False
    if previous is None:
        return True  # No reliable baseline: verify the archive itself.
    return signature(previous) != signature(entry)


def ensure_recovery_table(conn):
    # Created only by this explicitly launched job; records new imports atomically.
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rawkm.killmail_archive_recoveries (
                killmail_id BIGINT NOT NULL,
                killmail_time TIMESTAMPTZ NOT NULL,
                archive_day DATE NOT NULL,
                recovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (killmail_id, killmail_time)
            )
        """)
    conn.commit()


def import_batch(conn, importer, payloads, archive_day, months, state, state_path, ensured):
    stamps = [datetime.fromisoformat(p["killmail_time"]) for p in payloads]
    if any(stamp.tzinfo is None for stamp in stamps):
        raise ValueError("Killmail timestamp must include its timezone")
    for stamp in stamps:
        month = stamp.astimezone(UTC).date().replace(day=1)
        if month not in ensured:
            importer.ensure_month_partitions(conn, month, month)
            ensured.add(month)
        months.add(month.isoformat()[:7])
    state["pending_months"] = sorted(set(state["pending_months"]) | months)
    save_state(state_path, state)  # Persist reconciliation work before committing imports.
    with conn.cursor() as cur:
        cur.execute("""
            SELECT killmail_id, killmail_time FROM rawkm.killmails
            WHERE killmail_time >= %s AND killmail_time <= %s
              AND killmail_id = ANY(%s)
        """, (min(stamps), max(stamps), [int(p["killmail_id"]) for p in payloads]))
        existing = set(cur.fetchall())
    added = 0
    for payload, stamp in zip(payloads, stamps):
        key = (int(payload["killmail_id"]), stamp)
        if key in existing:
            continue
        importer.insert_killmail(conn, payload)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO rawkm.killmail_archive_recoveries
                    (killmail_id, killmail_time, archive_day) VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (*key, archive_day))
        existing.add(key)
        added += 1
    conn.commit()
    return added


def refresh_archive(conn, importer, session, day, entry, state, state_path):
    destination = importer.archive_tar_path(day)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix=".refresh-", suffix=".tar.bz2", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(filename)
    count = added = 0
    months, ensured, batch = set(), set(), []
    try:
        # Always fetch anew: the original import cache may be the outdated file.
        with session.get(importer.day_url(day), stream=True, timeout=(15, 300)) as response:
            response.raise_for_status()
            with temporary.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    handle.write(chunk)
        if entry.get("size") is not None and temporary.stat().st_size != int(entry["size"]):
            raise ValueError("Archive size changed since the index was read; retry the job")
        with tarfile.open(temporary, "r:bz2") as archive:
            for member in archive:
                if not member.isfile() or not member.name.endswith(".json"):
                    continue
                # Read in memory, never extract paths supplied by the archive.
                with archive.extractfile(member) as handle:
                    batch.append(json.load(handle))
                count += 1
                if len(batch) >= BATCH_SIZE:
                    added += import_batch(conn, importer, batch, day, months, state, state_path, ensured)
                    LOG.info("ARCHIVE_PROGRESS day=%s read=%s added=%s", day, count, added)
                    batch.clear()
            if batch:
                added += import_batch(conn, importer, batch, day, months, state, state_path, ensured)
        temporary.replace(destination)
        importer.mark_day(conn, day, "success", files_count=count, archive_count=1)
        state["archives"][day.isoformat()] = signature(entry)
        save_state(state_path, state)
        LOG.info("ARCHIVE_DONE day=%s files=%s added=%s modified=%s", day, count, added, entry.get("last_modified"))
        return added
    finally:
        temporary.unlink(missing_ok=True)


def run_refresh(conn, importer, session, state_path, match, start=None, end=None):
    state = load_state(state_path)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT day, files_count FROM rawkm.killmail_import_days
            WHERE status='success' AND (%s::date IS NULL OR day >= %s)
              AND (%s::date IS NULL OR day <= %s) ORDER BY day
        """, (start, start, end, end))
        days = cur.fetchall()
    conn.commit()
    LOG.info("START imported_days=%s from=%s through=%s", len(days), start or "all", end or "all")
    totals = get_json(session, f"{BASE_URL}/totals.json") if days else {}
    indexes = {}
    for year in sorted({day.year for day, _count in days}):
        LOG.info("INDEX_START year=%s", year)
        try:
            indexes[year] = year_index(session, year)
        except Exception:
            LOG.exception("INDEX_FAILED year=%s (will retry next launch)", year)
            indexes[year] = None
    checked = changed = added = failed = 0
    for day, local_count in days:
        if indexes[day.year] is None:
            failed += 1
            continue
        try:
            entry = indexes[day.year].get(day.isoformat())
            if entry is None:
                raise ValueError("Previously imported archive is missing from the EVE Ref index")
            raw_count = totals.get(day.isoformat())
            remote_count = int(raw_count) if raw_count is not None else None
            checked += 1
            if needs_refresh(local_count, remote_count, state["archives"].get(day.isoformat()), entry):
                changed += 1
                LOG.info("ARCHIVE_CHANGED day=%s local=%s remote=%s modified=%s", day, local_count, remote_count, entry.get("last_modified"))
                # Even a failed, partially committed import must be reconciled.
                state["pending_months"] = sorted(set(state["pending_months"]) | {day.isoformat()[:7]})
                save_state(state_path, state)
                added += refresh_archive(conn, importer, session, day, entry, state, state_path)
            else:
                state["archives"][day.isoformat()] = signature(entry)
            if checked % 100 == 0:
                save_state(state_path, state)
                LOG.info("CHECK_PROGRESS checked=%s/%s changed=%s added=%s failed=%s", checked, len(days), changed, added, failed)
        except Exception:
            conn.rollback()
            failed += 1
            LOG.exception("ARCHIVE_FAILED day=%s (will retry next launch)", day)
    save_state(state_path, state)
    if state["pending_months"]:
        LOG.info("MER_MATCH_START months=%s", ",".join(state["pending_months"]))
        result = match(months=state["pending_months"])
        if result.get("busy"):
            raise RuntimeError("MER matching is busy; restart this job to finish pending matches")
        LOG.info("MER_MATCH_DONE %s", json.dumps(result, sort_keys=True))
        state["pending_months"] = []
        save_state(state_path, state)
    LOG.info("DONE checked=%s changed=%s added=%s failed=%s", checked, changed, added, failed)
    if failed:
        raise RuntimeError(f"{failed} archive(s) failed; see ARCHIVE_FAILED entries and restart to retry")
    return {"checked": checked, "changed": changed, "added": added, "failed": failed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="start", type=date.fromisoformat)
    parser.add_argument("--to", dest="end", type=date.fromisoformat)
    args = parser.parse_args()
    if args.start and args.end and args.start > args.end:
        parser.error("From must not be after Through")
    # Import the regular importer only when actually launched, never in Admin pages.
    import sync_killmails as importer
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))
    from app.mer import match_mer_killmails

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", force=True)
    state_path = importer.BASE_DIR / "data/killmails/refresh_index.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("An archive refresh is already running") from None
        conn = importer.db()
        try:
            ensure_recovery_table(conn)
            with requests.Session() as session:
                session.headers["User-Agent"] = USER_AGENT
                run_refresh(conn, importer, session, state_path, match_mer_killmails, args.start, args.end)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
