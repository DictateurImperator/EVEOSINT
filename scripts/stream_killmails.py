#!/usr/bin/env python3
"""Sequential R2Z2 consumer. Exactly one pending JSON, removed after DB commit."""
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import threading
import time
from datetime import datetime, timezone, timedelta

import requests

if __package__:
    from .live_killmail_coordination import ROOT, atomic_json, live_boundary, paused, legacy_daily_running
else:
    from live_killmail_coordination import ROOT, atomic_json, live_boundary, paused, legacy_daily_running

BASE_URL = 'https://r2z2.zkillboard.com/ephemeral'
USER_AGENT = 'EVEOSINT-LiveFeed/1.0 (+https://eveosint.com)'
LOG = logging.getLogger('live_killmails')
MAX_JSON_BYTES = 32 * 1024 * 1024


def get_json(session, path):
    with session.get(f'{BASE_URL}/{path}',timeout=(5,30),stream=True) as response:
        if response.status_code==404:return None
        response.raise_for_status()
        body=bytearray()
        for chunk in response.iter_content(65536):
            body.extend(chunk)
            if len(body)>MAX_JSON_BYTES:raise ValueError('Feed payload exceeds size limit')
        return json.loads(body)


def idle_status(phase, reason=None):
    try:value=json.loads((ROOT/'status.json').read_text())
    except (FileNotFoundError,ValueError):value={}
    value.update(phase=phase,reason=reason,pid=os.getpid(),updated_at=datetime.now(timezone.utc).isoformat())
    atomic_json(ROOT/'status.json',value)


def enabled(root=ROOT):
    try:return json.loads((root/'control.json').read_text()).get('enabled') is not False
    except FileNotFoundError:return True


class Consumer:
    def __init__(self, backend, session, root=ROOT):
        self.backend,self.session,self.root=backend,session,Path(root)
        self.state={}
        self.pending=self.root/'pending.json'
        self.backend.on_progress=self.report

    def report(self, phase, **fields):
        self.state.update(phase=phase,updated_at=datetime.now(timezone.utc).isoformat(),pid=os.getpid(),**fields)
        atomic_json(self.root/'status.json',self.state)

    def tick(self):
        cursor=self.backend.cursor()
        if cursor is None:
            pointer=get_json(self.session,'sequence.json')
            self.backend.initialize(int(pointer['sequence']))
            cursor=self.backend.cursor()
        sequence,updated=cursor
        if self.pending.exists():
            raw=json.loads(self.pending.read_text())
            if int(raw.get('sequence_id',-1))<sequence:
                # Commit succeeded, but the process died before unlinking.
                self.pending.unlink()
                return .1
            if raw.get('sequence_id')!=sequence:raise ValueError('Pending KM does not match the saved cursor')
        else:
            self.report('receiving',next_sequence=sequence,error=None)
            raw=get_json(self.session,f'{sequence}.json')
            if raw is None:
                if datetime.now(timezone.utc)-updated>timedelta(hours=24):
                    pointer=get_json(self.session,'sequence.json')
                    if pointer and int(pointer['sequence'])>sequence:
                        available=int(pointer['sequence'])
                        self.backend.skip_expired(sequence,available)
                        self.report('archive_recovery_needed',gap_from=sequence,gap_to=available-1,
                                    message='Expired feed interval; daily archives reconcile missing kills.')
                        return 6
                self.report('waiting_for_kill',next_sequence=sequence)
                return 6
            if not isinstance(raw,dict) or raw.get('sequence_id')!=sequence:raise ValueError('Invalid feed sequence')
            atomic_json(self.pending,raw)
        self.report('processing',next_sequence=sequence,current_killmail=raw.get('killmail_id'),error=None,
                    feed_lag_seconds=max(0,int(time.time()-raw['uploaded_at'])) if isinstance(raw.get('uploaded_at'),(int,float)) else None)
        result=self.backend.process(raw,sequence)
        self.pending.unlink(missing_ok=True)
        self.report('completed_kill',next_sequence=sequence+1,**result)
        LOG.info('KM_DONE sequence=%s killmail=%s inserted=%s valued=%s',sequence,result['killmail_id'],result['inserted'],result['valued'])
        return .1


def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    ROOT.mkdir(parents=True,exist_ok=True,mode=0o700)
    stopped=threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stopped.set())
    backend=consumer=None
    with (ROOT/'consumer.lock').open('a') as singleton,requests.Session() as session:
        fcntl.flock(singleton,fcntl.LOCK_EX|fcntl.LOCK_NB)
        session.headers.update({'User-Agent':USER_AGENT,'Accept-Encoding':'gzip'})
        try:
            while not stopped.is_set():
                if not Path(__file__).exists():break  # Rollback to a version without live support.
                reason='deployment' if Path('/run/eveosint-maintenance/active').exists() else paused()
                if not reason and legacy_daily_running(ROOT.parents[2]):reason='waiting for the pre-upgrade daily job'
                if reason or not enabled():
                    idle_status('paused',reason or 'administrator')
                    stopped.wait(1)
                    continue
                with live_boundary() as acquired:
                    if not acquired:
                        stopped.wait(.2)
                        continue
                    if backend is None:
                        # Start only after the first deployment has finished.
                        if __package__:
                            from .live_killmail_backend import Backend
                        else:
                            from live_killmail_backend import Backend
                        backend=Backend();consumer=Consumer(backend,session)
                    try:delay=consumer.tick()
                    except Exception as error:
                        backend.conn.rollback()
                        consumer.report('retrying_current_kill',error=type(error).__name__,http_status=getattr(getattr(error,'response',None),'status_code',None))
                        LOG.exception('Current KM retained; cursor was not advanced')
                        delay=30
                stopped.wait(delay)
        finally:
            if backend:backend.close()
            idle_status('stopped')
    return 0


if __name__=='__main__':raise SystemExit(main())
