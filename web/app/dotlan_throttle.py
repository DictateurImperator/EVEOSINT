import fcntl
import os
import tempfile
import threading
import time
from pathlib import Path


REQUEST_INTERVAL_SECONDS = 1.0 / 3.0
_LOCK_FILE = Path(tempfile.gettempdir()) / "eveosint_dotlan_global.lock"
_PROCESS_LOCK = threading.Lock()


def wait_for_dotlan_slot():
    """Serialize DOTLAN request starts across all EVEOSINT processes on this host."""
    with _PROCESS_LOCK:
        with _LOCK_FILE.open("a+", encoding="ascii") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                lock_file.seek(0)
                raw_value = lock_file.read().strip()
                try:
                    last_request_started = float(raw_value)
                except (TypeError, ValueError):
                    last_request_started = 0.0

                remaining = REQUEST_INTERVAL_SECONDS - (time.time() - last_request_started)
                if remaining > 0:
                    time.sleep(remaining)

                request_started = time.time()
                lock_file.seek(0)
                lock_file.truncate()
                lock_file.write(f"{request_started:.6f}\n")
                lock_file.flush()
                os.fsync(lock_file.fileno())
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
