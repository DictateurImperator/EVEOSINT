"""Read-only monitoring plus a cooperative pause flag; no background work in web."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path.home()/'eveosint/data/run/live_killmails'


def status():
    try:state=json.loads((ROOT/'status.json').read_text())
    except (FileNotFoundError,ValueError):state=dict(phase='not_started')
    try:control=json.loads((ROOT/'control.json').read_text())
    except (FileNotFoundError,ValueError):control={}
    alive=False
    try:
        pid=int(state['pid'])
        os.kill(pid,0)
        alive=b'stream_killmails.py' in Path(f'/proc/{pid}/cmdline').read_bytes()
    except (KeyError,ValueError,OSError):pass
    return dict(state,enabled=control.get('enabled') is not False,running=alive)


def set_enabled(enabled):
    ROOT.mkdir(parents=True,exist_ok=True,mode=0o700)
    value=dict(enabled=enabled,updated_at=datetime.now(timezone.utc).isoformat())
    temporary=ROOT/'control.tmp'
    temporary.write_text(json.dumps(value));temporary.replace(ROOT/'control.json')
