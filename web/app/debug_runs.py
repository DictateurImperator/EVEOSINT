import fcntl
import json
import os
import re
import signal
import subprocess
from pathlib import Path

BASE_DIR = Path.home() / "eveosint"
PYTHON_BIN = BASE_DIR / "venv" / "bin" / "python"
SCRIPT_PATH = BASE_DIR / "scripts" / "debug_affiliation_steps_1_4.py"
RUN_DIR = BASE_DIR / "data" / "run" / "admin_debug"
HISTORY_DIR = RUN_DIR / "history"
STATUS_PATH = RUN_DIR / "status.json"
LOCK_PATH = RUN_DIR / "admin_debug.lock"
LOG_DIR = BASE_DIR / "data" / "logs" / "admin_debug"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class DebugRunError(RuntimeError):
    pass


def _load_json(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:
        raise DebugRunError(f"debug_status_invalid:{path.name}") from exc
    return data if isinstance(data, dict) else None


def _lock_is_held() -> bool:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def list_debug_history(limit=20):
    limit = max(1, min(100, int(limit or 20)))
    if not HISTORY_DIR.is_dir():
        return []
    items = []
    for path in sorted(HISTORY_DIR.glob("*.json"), key=lambda p: p.name, reverse=True):
        data = _load_json(path)
        if not data:
            continue
        items.append({
            "run_id": data.get("run_id") or path.stem,
            "module": data.get("module"),
            "variant": data.get("variant"),
            "status": data.get("status"),
            "started_at": data.get("started_at"),
            "finished_at": data.get("finished_at"),
            "duration_seconds": data.get("duration_seconds"),
            "source_table": data.get("source_table"),
            "metrics": data.get("metrics") or {},
            "bulk": data.get("bulk") or {},
            "log_path": data.get("log_path"),
        })
        if len(items) >= limit:
            break
    return items


def get_debug_status(include_history=True):
    state = _load_json(STATUS_PATH) or {
        "run_id": None,
        "module": "affiliation_steps_1_4",
        "variant": "optimized",
        "status": "idle",
        "running": False,
        "pid": None,
        "started_at": None,
        "finished_at": None,
        "duration_seconds": None,
        "current_phase": None,
        "metrics": {},
        "bulk": {},
        "phases": [],
        "log_path": None,
        "error": None,
    }
    state["running"] = _lock_is_held()
    if state["running"] and state.get("status") not in {"done", "failed", "stopped"}:
        state["status"] = "running"
    if include_history:
        state["history"] = list_debug_history(20)
    return state


def start_affiliation_debug(
    *,
    source_table="entities.recent_killmail_pilots",
    source_column="character_id",
    limit=None,
    workers=4,
    batch_size=999,
    esi_max_calls_per_minute=280,
    store_results=True,
    use_etag_cache=False,
    skip_bulk=False,
):
    if _lock_is_held():
        raise DebugRunError("debug_already_running")
    if not PYTHON_BIN.is_file():
        raise DebugRunError("debug_python_missing")
    if not SCRIPT_PATH.is_file():
        raise DebugRunError("debug_script_missing")

    command = [
        str(PYTHON_BIN),
        str(SCRIPT_PATH),
        "--source-table", str(source_table),
        "--source-column", str(source_column),
        "--workers", str(int(workers)),
        "--batch-size", str(int(batch_size)),
        "--esi-max-calls-per-minute", str(int(esi_max_calls_per_minute)),
        "--variant", "optimized",
    ]
    if limit is not None:
        command.extend(["--limit", str(int(limit))])
    if store_results:
        command.append("--store-results")
    if use_etag_cache:
        command.append("--use-etag-cache")
    if skip_bulk:
        command.append("--skip-bulk")

    process = subprocess.Popen(
        command,
        cwd=str(BASE_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    return process.pid


def stop_debug_run():
    state = get_debug_status(include_history=False)
    if not state.get("running"):
        raise DebugRunError("debug_not_running")
    pid = state.get("pid")
    try:
        pid = int(pid)
    except (TypeError, ValueError) as exc:
        raise DebugRunError("debug_pid_invalid") from exc

    cmdline_path = Path(f"/proc/{pid}/cmdline")
    try:
        cmdline = cmdline_path.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise DebugRunError("debug_process_missing") from exc
    if SCRIPT_PATH.name not in cmdline:
        raise DebugRunError("debug_pid_not_ours")
    os.kill(pid, signal.SIGTERM)
    return pid


def _resolve_state_for_log(run_id=None):
    if run_id:
        if not RUN_ID_RE.fullmatch(run_id):
            raise DebugRunError("debug_run_id_invalid")
        state = _load_json(HISTORY_DIR / f"{run_id}.json")
        if not state:
            raise DebugRunError("debug_run_not_found")
        return state
    return _load_json(STATUS_PATH) or {}


def read_debug_log(run_id=None, lines=800):
    lines = max(20, min(5000, int(lines or 800)))
    state = _resolve_state_for_log(run_id)
    raw = state.get("log_path")
    if not raw:
        return ""
    path = Path(raw).expanduser()
    try:
        path.relative_to(LOG_DIR)
    except ValueError as exc:
        raise DebugRunError("debug_log_path_invalid") from exc
    if not path.is_file():
        return ""
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


def cleanup_debug_data():
    if _lock_is_held():
        raise DebugRunError("debug_already_running")
    completed = subprocess.run(
        [str(PYTHON_BIN), str(SCRIPT_PATH), "--cleanup-only"],
        cwd=str(BASE_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise DebugRunError(f"debug_cleanup_failed:{completed.stdout[-500:]}")
    return True
