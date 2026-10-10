#!/usr/bin/env python3
"""Manually refresh changed, previously imported EVE Ref daily archives.

No scheduler, CCP requests or zKillboard submissions. Archives are downloaded only when their remote modification date is later than
our local download date, including on the first run.
"""

import argparse
import fcntl
import json
import logging
import os
import signal
import sys
import tarfile
import tempfile
import time
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
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


class Progress:
    def __init__(self, path):
        self.path = path
        self.data = {
            "pid": os.getpid(), "phase": "starting", "started_at": datetime.now(UTC).isoformat(),
            "scanned": 0, "archives_total": 0, "to_update": 0, "scan_complete": False,
            "processed": 0, "updated": 0, "failed": 0, "added": 0, "current_day": None,
            "bytes_downloaded": 0, "bytes_total": None, "files_read": 0,
        }
        self.update()

    def update(self, **values):
        self.data.update(values)
        self.data["updated_at"] = datetime.now(UTC).isoformat()
        save_state(self.path, self.data)


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


def file_metadata(entry, headers):
    """The file response is authoritative when the yearly index lags behind."""
    result = dict(entry)
    if headers.get("Last-Modified"):
        modified = parsedate_to_datetime(headers["Last-Modified"])
        if modified.tzinfo is None:
            raise ValueError("Remote modification time must include its timezone")
        result["last_modified"] = modified.astimezone(UTC).isoformat()
    if headers.get("Content-Length") and not headers.get("Content-Encoding"):
        result["size"] = int(headers["Content-Length"])
    if headers.get("ETag"):
        result["etag"] = headers["ETag"].strip('"')
    return result


def missing_index_entry(session, importer, day):
    # Only missing index entries need this extra request; never HEAD all history.
    with session.head(importer.day_url(day), timeout=(15, 120), allow_redirects=True) as response:
        if response.status_code == 404:
            return None
        response.raise_for_status()
        if not response.headers.get("Last-Modified"):
            return None
        return file_metadata({"name": f"killmails-{day}.tar.bz2"}, response.headers)


class ArchiveNotNewer(Exception):
    pass


def signature(entry):
    return {key: entry.get(key) for key in ("etag", "last_modified", "size")}


def download_time(archive_path, previous=None):
    # The regular importer writes the download then moves it without preserving
    # EVE Ref's Last-Modified, so this mtime is our local download time.
    if archive_path.is_file():
        return datetime.fromtimestamp(archive_path.stat().st_mtime, UTC)
    value = (previous or {}).get("downloaded_at")
    if value:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            raise ValueError("Download time must include its timezone")
        return stamp
    return None


def needs_refresh(downloaded_at, entry):
    if downloaded_at is None:
        return False  # An unknown date must never trigger a historical redownload.
    modified = datetime.fromisoformat(entry["last_modified"])
    if modified.tzinfo is None or downloaded_at.tzinfo is None:
        raise ValueError("Archive dates must include their timezone")
    return modified > downloaded_at


def cleanup_downloads(archive_dir):
    # Caller holds the refresh lock: these belong to interrupted workers only.
    for path in archive_dir.rglob(".refresh-*.tar.bz2"):
        if path.is_file():
            size = path.stat().st_size
            path.unlink()
            LOG.info("TEMP_REMOVED path=%s bytes=%s", path, size)


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


def refresh_archive(conn, importer, session, day, entry, state, state_path, report=lambda **_values: None):
    destination = importer.archive_tar_path(day)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix=".refresh-", suffix=".tar.bz2", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(filename)
    count = added = 0
    months, ensured, batch = set(), set(), []
    report(phase="downloading", current_day=day.isoformat(), bytes_downloaded=0,
           bytes_total=entry.get("size"), files_read=0, current_added=0)
    try:
        # Always fetch anew: the original import cache may be the outdated file.
        with session.get(importer.day_url(day), stream=True, timeout=(15, 300)) as response:
            response.raise_for_status()
            headers = getattr(response, "headers", {})
            received_entry = file_metadata(entry, headers)
            downloaded_at = download_time(destination, state["archives"].get(day.isoformat()))
            if not needs_refresh(downloaded_at, received_entry):
                raise ArchiveNotNewer("File response is not newer than our download")
            expected_size = int(headers["Content-Length"]) if headers.get("Content-Length") and not headers.get("Content-Encoding") else None
            metadata_changed = (received_entry.get("size") != entry.get("size")
                                or received_entry.get("etag") != entry.get("etag")
                                or int(datetime.fromisoformat(received_entry["last_modified"]).timestamp())
                                != int(datetime.fromisoformat(entry["last_modified"]).timestamp()))
            if metadata_changed:
                LOG.warning("ARCHIVE_INDEX_STALE day=%s index_size=%s response_size=%s", day, entry.get("size"), received_entry.get("size"))
            entry = received_entry
            with temporary.open("wb") as handle:
                downloaded, last_report = 0, 0
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if time.monotonic() - last_report >= 1:
                        report(bytes_downloaded=downloaded)
                        last_report = time.monotonic()
        report(phase="importing", bytes_downloaded=downloaded)
        if expected_size is not None and temporary.stat().st_size != expected_size:
            raise ValueError("Downloaded archive does not match response Content-Length")
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
                    report(files_read=count, current_added=added)
                    LOG.info("ARCHIVE_PROGRESS day=%s read=%s added=%s", day, count, added)
                    batch.clear()
            if batch:
                added += import_batch(conn, importer, batch, day, months, state, state_path, ensured)
        report(files_read=count, current_added=added)
        temporary.replace(destination)
        importer.mark_day(conn, day, "success", files_count=count, archive_count=1)
        state["archives"][day.isoformat()] = signature(entry) | {
            "downloaded_at": download_time(destination).isoformat(),
        }
        save_state(state_path, state)
        LOG.info("ARCHIVE_DONE day=%s files=%s added=%s modified=%s", day, count, added, entry.get("last_modified"))
        return added
    finally:
        temporary.unlink(missing_ok=True)


def run_refresh(conn, importer, session, state_path, match, start=None, end=None, report=lambda **_values: None):
    state = load_state(state_path)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT day, files_count FROM rawkm.killmail_import_days
            WHERE status='success' AND (%s::date IS NULL OR day >= %s)
              AND (%s::date IS NULL OR day <= %s) ORDER BY day
        """, (start, start, end, end))
        days = cur.fetchall()
    conn.commit()
    report(phase="checking", archives_total=len(days), date_from=start.isoformat() if start else None,
           date_to=end.isoformat() if end else None)
    LOG.info("START imported_days=%s from=%s through=%s", len(days), start or "all", end or "all")
    indexes = {}
    for year in sorted({day.year for day, _count in days}):
        report(current_year=year)
        LOG.info("INDEX_START year=%s", year)
        try:
            indexes[year] = year_index(session, year)
        except Exception:
            LOG.exception("INDEX_FAILED year=%s (will retry next launch)", year)
            indexes[year] = None
    checked = added = failed = 0
    candidates = []
    for day, _local_count in days:
        checked += 1
        try:
            if indexes[day.year] is None:
                failed += 1
                continue
            entry = indexes[day.year].get(day.isoformat())
            if entry is None:
                LOG.info("ARCHIVE_INDEX_MISSING day=%s checking file headers", day)
                entry = missing_index_entry(session, importer, day)
                if entry is None:
                    LOG.warning("ARCHIVE_SKIPPED day=%s reason=unavailable_remote_metadata", day)
                    continue
            downloaded_at = download_time(importer.archive_tar_path(day), state["archives"].get(day.isoformat()))
            if downloaded_at is None:
                LOG.warning("ARCHIVE_SKIPPED day=%s reason=unknown_download_date", day)
            if needs_refresh(downloaded_at, entry):
                candidates.append((day, entry))
                LOG.info("ARCHIVE_CHANGED day=%s downloaded=%s modified=%s", day, downloaded_at.isoformat(), entry["last_modified"])
            else:
                state["archives"][day.isoformat()] = signature(entry) | {
                    "downloaded_at": downloaded_at.isoformat() if downloaded_at else None,
                }
        except Exception:
            failed += 1
            LOG.exception("ARCHIVE_FAILED day=%s (will retry next launch)", day)
        finally:
            if checked % 100 == 0 or checked == len(days):
                save_state(state_path, state)
                report(scanned=checked, to_update=len(candidates), failed=failed)
                LOG.info("CHECK_PROGRESS checked=%s/%s changed=%s failed=%s", checked, len(days), len(candidates), failed)
    save_state(state_path, state)
    report(scanned=checked, to_update=len(candidates), scan_complete=True, current_year=None)
    updated = 0
    for processed, (day, entry) in enumerate(candidates, 1):
        archive_added = [0]
        try:
            state["pending_months"] = sorted(set(state["pending_months"]) | {day.isoformat()[:7]})
            save_state(state_path, state)

            def archive_report(_previous_added=added, _archive_added=archive_added, **values):
                current_added = values.pop("current_added", None)
                if current_added is not None:
                    _archive_added[0] = current_added
                    values["added"] = _previous_added + current_added
                report(**values)

            added += refresh_archive(conn, importer, session, day, entry, state, state_path, archive_report)
            updated += 1
        except ArchiveNotNewer:
            LOG.info("ARCHIVE_SKIPPED day=%s reason=response_not_newer_than_download", day)
        except Exception:
            conn.rollback()
            added += archive_added[0]
            failed += 1
            LOG.exception("ARCHIVE_FAILED day=%s (will retry next launch)", day)
        finally:
            report(processed=processed, updated=updated, failed=failed, added=added)
    if state["pending_months"]:
        report(phase="matching", current_day=None, months=state["pending_months"])
        LOG.info("MER_MATCH_START months=%s", ",".join(state["pending_months"]))
        result = match(months=state["pending_months"])
        if result.get("busy"):
            raise RuntimeError("MER matching is busy; restart this job to finish pending matches")
        LOG.info("MER_MATCH_DONE %s", json.dumps(result, sort_keys=True))
        state["pending_months"] = []
        save_state(state_path, state)
    LOG.info("DONE checked=%s changed=%s added=%s failed=%s", checked, len(candidates), added, failed)
    report(phase="failed" if failed else "completed", current_day=None, finished_at=datetime.now(UTC).isoformat())
    if failed:
        raise RuntimeError(f"{failed} archive(s) failed; see ARCHIVE_FAILED entries and restart to retry")
    return {"checked": checked, "changed": len(candidates), "added": added, "failed": failed}


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
        progress = Progress(state_path.parent / "refresh_progress.json")
        cleanup_downloads(importer.ARCHIVE_DIR)

        def stop(_signal, _frame):
            LOG.info("STOP_REQUESTED")
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        conn = importer.db()
        try:
            ensure_recovery_table(conn)
            with requests.Session() as session:
                session.headers["User-Agent"] = USER_AGENT
                run_refresh(conn, importer, session, state_path, match_mer_killmails, args.start, args.end, progress.update)
        except KeyboardInterrupt:
            progress.update(phase="stopped", finished_at=datetime.now(UTC).isoformat())
            LOG.info("STOPPED (completed batches retained; temporary download removed)")
        except Exception:
            progress.update(phase="failed", finished_at=datetime.now(UTC).isoformat())
            raise
        finally:
            conn.close()


if __name__ == "__main__":
    main()
