#!/usr/bin/env python3
"""Manually reprice abyssal killmails, then refresh initialized rankings."""
import argparse
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from datetime import UTC, datetime
import psycopg2

if __package__:
    from .calculate_killmail_values import load_config
else:
    from calculate_killmail_values import load_config


def refresh_rankings(conn, run, progress, stopped):
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('analytics.killmail_ranking_current_state')")
        if cur.fetchone()[0] is None:
            return 'awaiting_initial_build'
        cur.execute('SELECT ready FROM analytics.killmail_ranking_current_state WHERE singleton')
        if not (cur.fetchone() or (False,))[0]:
            return 'awaiting_initial_build'
    conn.commit()
    while not stopped.is_set():
        with conn.cursor() as cur:
            cur.execute('SELECT pg_try_advisory_lock(79130425)')
            free = cur.fetchone()[0]
            if free:
                cur.execute('SELECT pg_advisory_unlock(79130425)')
        conn.commit()
        if not free:
            progress(phase='waiting_rankings')
            stopped.wait(5)
            continue
        run('rankings', 'build_killmail_rankings.py', ['--incremental-only'])
        if stopped.is_set():
            return 'stopped'
        # A concurrently launched builder may have acquired the lock first.
        with conn.cursor() as cur:
            cur.execute("""SELECT count(*) FROM analytics.killmail_ranking_months m
                JOIN analytics.killmail_ranking_versions v USING(month)
                WHERE m.built_revision<>v.revision""")
            dirty = cur.fetchone()[0]
        conn.commit()
        if not dirty:
            return 'updated'
        progress(phase='waiting_rankings')
        stopped.wait(5)
    return 'stopped'


def main():
    root = Path.home()/'eveosint'
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db-config', type=Path, default=root/'config/db.json')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    directory = root/'data/market';directory.mkdir(parents=True, exist_ok=True)
    path = directory/'abyssal_repricing_progress.json'
    state = dict(pid=os.getpid(), phase='starting', started_at=datetime.now(UTC).isoformat())
    stopped = threading.Event()
    child = None
    def progress(**values):
        state.update(values, updated_at=datetime.now(UTC).isoformat())
        temporary = path.with_suffix('.tmp');temporary.write_text(json.dumps(state));temporary.replace(path)
    def stop(*_):
        stopped.set()
        if child is not None and child.poll() is None:
            child.terminate()
    signal.signal(signal.SIGTERM, stop);signal.signal(signal.SIGINT, stop)
    def run(phase, filename, flags):
        nonlocal child
        child_progress = directory / ('killmail_rankings_progress.json' if phase == 'rankings' else f'abyssal_repricing_{phase}.json')
        command = [sys.executable,str(Path(__file__).with_name(filename)), '--db-config',str(args.db_config),
                   '--progress',str(child_progress),*flags]
        progress(phase=phase)
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL)
        while child.poll() is None:
            try:
                data = json.loads(child_progress.read_text())
                if data.get('pid') == child.pid:
                    progress(phase=phase, detail={k:v for k,v in data.items() if k not in ('pid','error')})
            except (OSError,ValueError):
                pass
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        # Capture the final counters even if the child finishes between polls.
        try:
            data = json.loads(child_progress.read_text())
            if data.get('pid') == child.pid:
                progress(detail={k:v for k,v in data.items() if k not in ('pid','error')})
        except (OSError,ValueError):
            pass
        if child.returncode and not stopped.is_set():
            raise RuntimeError(f'{phase} failed; see job log')
    with (directory/'abyssal_repricing.lock').open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another abyssal repricing job is running')
        progress(phase='starting')
        try:
            run('valuing','calculate_killmail_values.py',['--recalculate-abyssal'])
            if stopped.is_set():
                progress(phase='stopped');return
            progress(valuation=state.get('detail',{}))
            if state.get('detail',{}).get('phase')!='completed':
                raise RuntimeError('Valuation did not complete; inspect the job log')
            with psycopg2.connect(**load_config(args.db_config), application_name='abyssal_rankings_refresh') as conn:
                rankings = refresh_rankings(conn,run,progress,stopped)
            progress(phase='stopped' if stopped.is_set() else 'completed',rankings=rankings)
        except Exception as exc:
            progress(phase='failed',error=str(exc))
            logging.exception('Abyssal repricing failed');raise


if __name__=='__main__':main()
