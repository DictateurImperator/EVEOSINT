#!/usr/bin/env python3
"""Manual one-day recovery: prefer EVE Ref, otherwise use zKill's full daily archive.

Existing killmails are skipped before invoking the regular importer, preserving
items, attackers and calculated values. No per-kill API calls are made.
"""
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import signal
import shutil
import sys
import tarfile
import tempfile
import time
from datetime import date, datetime, UTC

import requests

if __package__:
    from . import refresh_killmail_archives as refresh
else:
    import refresh_killmail_archives as refresh

DAY = date(2026, 7, 13)
LOG = logging.getLogger('killmail_day_recovery')
USER_AGENT = 'EVEOSINT-DayRecovery/1.0 (github.com/DictateurImperator/EVEOSINT)'


def normalize(payload, day, kmid=None, hashes=None):
    if not isinstance(payload, dict):
        raise ValueError('Archive entry must be a killmail object')
    payload = dict(payload)
    ident = int(payload['killmail_id'])
    if ident <= 0 or (kmid is not None and ident != int(kmid)):
        raise ValueError('Archive key and killmail ID disagree')
    stamp = datetime.fromisoformat(payload['killmail_time'])
    if stamp.tzinfo is None or stamp.astimezone(UTC).date() != day:
        raise ValueError('Archive contains a killmail outside the requested UTC day')
    if not isinstance(payload.get('victim'), dict) or not isinstance(payload.get('attackers'), list):
        raise ValueError('Incomplete raw killmail')
    if not payload.get('solar_system_id') or not payload['victim'].get('ship_type_id'):
        raise ValueError('Missing system or victim ship')
    hash_value = payload.get('killmail_hash') or (hashes or {}).get(str(ident))
    if hash_value is not None:
        if not isinstance(hash_value, str) or not re.fullmatch('[a-fA-F0-9]{40}', hash_value):
            raise ValueError('Invalid killmail hash')
        payload['killmail_hash'] = hash_value
    return payload


def download(session, url, destination, report):
    with session.get(url, stream=True, timeout=(15, 180)) as response:
        response.raise_for_status()
        size = 0
        last_report = 0
        with destination.open('wb') as handle:
            for chunk in response.iter_content(1024 * 1024):
                if not chunk:
                    continue
                handle.write(chunk)
                size += len(chunk)
                if time.monotonic() - last_report >= 1:
                    report(bytes_downloaded=size)
                    last_report = time.monotonic()
        report(bytes_downloaded=size)


def recover_day(conn, importer, session, match, state_path, report, day=DAY, cache=None):
    state = refresh.load_state(state_path)
    directory = importer.ARCHIVE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix='.day-recovery-', dir=directory)
    os.close(descriptor)
    temporary = Path(filename)
    cached = cache is not None and (cache / 'killmails.json').is_file() and (cache / 'hashes.json').is_file()
    source = 'zKill R2Z2' if cached else 'EVE Ref'
    report(phase='checking_source', day=str(day))
    url = importer.day_url(day)
    try:
        # Check only this file. A source outage must not prevent the zKill fallback.
        try:
            if cached:
                raise requests.RequestException('Using the downloaded zKill archive')
            with session.head(url, timeout=(10, 30), allow_redirects=True) as response:
                response.raise_for_status()
        except requests.RequestException:
            source = 'zKill R2Z2'
        report(phase='downloading', source=source, bytes_downloaded=0)
        if source == 'EVE Ref':
            try:
                download(session, url, temporary, report)
            except requests.RequestException:
                source = 'zKill R2Z2'
                report(phase='downloading', source=source, bytes_downloaded=0)
        if source != 'EVE Ref' and cached:
            shutil.copyfile(cache / 'killmails.json', temporary)
            report(bytes_downloaded=temporary.stat().st_size, cached=True)
        elif source != 'EVE Ref':
            url = f'https://r2z2.zkillboard.com/history/raw/{day:%Y%m%d}.json'
            download(session, url, temporary, report)
        report(phase='validating')
        # Validate the whole file before any killmail is written.
        if source == 'EVE Ref':
            payloads = []
            with tarfile.open(temporary, 'r:bz2') as archive:
                for member in archive:
                    if member.isfile() and member.name.endswith('.json'):
                        with archive.extractfile(member) as handle:
                            payloads.append(normalize(json.load(handle), day))
        else:
            if cached:
                hashes = json.loads((cache / 'hashes.json').read_text())
            else:
                time.sleep(1)  # Sequential daily archive/index calls, never per-kill requests.
                with session.get(f'https://r2z2.zkillboard.com/history/{day:%Y%m%d}.json', timeout=(15, 60)) as response:
                    response.raise_for_status()
                    hashes = response.json()
            if not isinstance(hashes, dict):
                raise ValueError('Unexpected zKill daily hash index')
            with temporary.open() as handle:
                archive = json.load(handle)
            if not isinstance(archive, dict):
                raise ValueError('Unexpected zKill full daily archive')
            payloads = [normalize(value, day, key, hashes) for key, value in archive.items()]
        if not payloads:
            raise ValueError('Empty archive; day not marked complete')
        report(phase='importing', total=len(payloads), processed=0, added=0, skipped=0)
        importer.ensure_tables(conn)
        refresh.ensure_recovery_table(conn)
        added = processed = 0
        months, ensured = set(), set()
        for offset in range(0, len(payloads), refresh.BATCH_SIZE):
            batch = payloads[offset:offset + refresh.BATCH_SIZE]
            added += refresh.import_batch(conn, importer, batch, day, months, state, state_path, ensured)
            processed += len(batch)
            report(processed=processed, added=added, skipped=processed-added)
        if source == 'EVE Ref':
            os.replace(temporary, importer.archive_tar_path(day))
            importer.mark_day(conn, day, 'success', files_count=len(payloads), archive_count=1)
        # A zKill fallback is not an imported EVE Ref archive: preserve that source's
        # missing status. Otherwise the daily refresh would repeatedly fail on its 503.
        report(phase='matching_mer')
        result = match(months=[day.strftime('%Y-%m')])
        if result.get('busy'):
            raise RuntimeError('MER matching is busy; rerun to finish reconciliation')
        state['pending_months'] = []
        state['last_recovery'] = dict(day=str(day), source=source, total=len(payloads), added=added,
                                      completed_at=datetime.now(UTC).isoformat())
        refresh.save_state(state_path, state)
        report(phase='completed', mer_matched=result.get('matched_unique', 0))
        return dict(total=len(payloads), added=added, skipped=len(payloads)-added, source=source)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    import sync_killmails as importer
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'web'))
    from app.mer import match_mer_killmails
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    root = importer.BASE_DIR / 'data/killmails'
    root.mkdir(parents=True, exist_ok=True)
    state = dict(pid=os.getpid(), started_at=datetime.now(UTC).isoformat(), phase='starting',
                 day=str(DAY), total=0, processed=0, added=0, skipped=0)
    def report(**kw):
        state.update(kw, updated_at=datetime.now(UTC).isoformat())
        refresh.save_state(root / 'day_recovery_progress.json', state)
        LOG.info('DAY_RECOVERY %s', json.dumps(state))
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with (root / 'refresh_index.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            report(phase='failed', error='Archive refresh is running; retry after it finishes.')
            raise SystemExit(1)
        report(phase='starting')
        conn = None
        try:
            conn = importer.db()
            with requests.Session() as session:
                session.headers['User-Agent'] = USER_AGENT
                recover_day(conn, importer, session, match_mer_killmails,
                            root / 'day_recovery_checkpoint.json', report,
                            cache=Path('/tmp/eveosint-day-recovery-2026-07-13'))
        except KeyboardInterrupt:
            if conn is not None:
                conn.rollback()
            report(phase='stopped')
        except Exception:
            if conn is not None:
                conn.rollback()
            report(phase='failed', error='Recovery failed; see job log. Saved batches are preserved.')
            LOG.exception('Day recovery failed')
            raise
        finally:
            if conn is not None:
                conn.close()


if __name__ == '__main__':
    main()
