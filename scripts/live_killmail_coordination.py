"""Cross-process boundaries: a live KM finishes before archive import/deployment."""
from contextlib import contextmanager
from pathlib import Path
import fcntl
import json
import os

ROOT = Path.home() / 'eveosint/data/run/live_killmails'


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)


@contextmanager
def exclusive(reason, root=None):
    directory = Path(root) if root is not None else ROOT
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Hold a second lock so that overlapping import requests cannot remove
    # another importer's pause notice.
    with (directory/'pause.lock').open('a') as pause:
        fcntl.flock(pause, fcntl.LOCK_EX)
        notice = directory/'pause.json'
        atomic_json(notice, dict(pid=os.getpid(), reason=reason))
        try:
            with (directory/'boundary.lock').open('a') as boundary:
                fcntl.flock(boundary, fcntl.LOCK_EX)
                yield
        finally:
            notice.unlink(missing_ok=True)


def paused(root=None):
    directory=Path(root) if root is not None else ROOT
    if not (directory/'pause.json').exists():return None
    try:
        with (directory/'pause.lock').open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                try:return json.loads((directory/'pause.json').read_text())['reason']
                except (FileNotFoundError,ValueError,KeyError):return 'maintenance'
            return None
    except FileNotFoundError:return None


@contextmanager
def live_boundary(root=None):
    directory = Path(root) if root is not None else ROOT
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory/'boundary.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield not bool(paused(directory))


def legacy_daily_running(base):
    directory=Path(base)/'data/run/update_pipeline'
    if not (directory/'update_pipeline.lock').exists():return False
    with (directory/'update_pipeline.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            try:return json.loads((directory/'status.json').read_text()).get('live_coordination') is not True
            except (FileNotFoundError,ValueError):return True
    return False
