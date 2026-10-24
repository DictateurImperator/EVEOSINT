"""One rolling ESI request budget shared by processes and threads on this host."""
import fcntl
import json
import time
from pathlib import Path

ROOT = Path.home() / 'eveosint/data/run/esi_quota'
DEFAULT_LIMIT = 280


def wait_for_slot(limit=DEFAULT_LIMIT, *, root=None, clock=time.time, sleep=time.sleep):
    limit = min(DEFAULT_LIMIT, max(1, int(limit)))
    directory = Path(root) if root is not None else ROOT
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    while True:
        with (directory / 'quota.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            now = clock()
            path = directory / 'quota.json'
            try:
                data = json.loads(path.read_text())
            except FileNotFoundError:
                data = []
            # Limits accompany grants: a lower budget used by an active job
            # applies to the other consumers for the same rolling minute.
            grants = [(float(at), int(cap)) for at, cap in data if now-60 < float(at) <= now]
            cap = min([limit] + [cap for _at, cap in grants])
            delay = max(0, grants[0][0]+60.001-now) if len(grants) >= cap else 0
            if not delay:
                grants.append((now, limit))
                temporary = path.with_suffix('.tmp')
                temporary.write_text(json.dumps(grants))
                temporary.replace(path)
                return
        sleep(delay)
