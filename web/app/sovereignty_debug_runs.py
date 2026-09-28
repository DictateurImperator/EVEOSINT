"""Admin Debug launch/status/logs for the two fixed sovereignty collectors.

Separate from the affiliation benchmark runner. Only explicitly whitelisted
commands can be started; HTTP clients cannot supply executable paths or
arbitrary command-line switches. The detached worker owns a file lock, watches
the child exit code and preserves per-run logs.
"""
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path.home() / "eveosint"
PYTHON_BIN = BASE_DIR / "venv" / "bin" / "python"
RUN_DIR = BASE_DIR / "data" / "run" / "admin_debug" / "sovereignty"
LOG_DIR = BASE_DIR / "data" / "logs" / "admin_debug" / "sovereignty"
HISTORY_DIR = RUN_DIR / "history"
JOBS = {
    "esi": {
        "label": "Current SOV · CCP ESI",
        "script": BASE_DIR / "scripts" / "sync_sovereignty_esi.py",
    },
    "dotlan": {
        "label": "SOV History · DOTLAN",
        "script": BASE_DIR / "scripts" / "import_sovereignty_dotlan.py",
    },
}
RUN_ID_RE = re.compile(r"^\d{8}_\d{6}_\d{6}_(?:esi|dotlan)$")
FINISHED = {"done", "failed", "stopped"}


class SovereigntyDebugError(RuntimeError):
    pass


def _job(key):
    if key not in JOBS:
        raise SovereigntyDebugError("sovereignty_job_unknown")
    return JOBS[key]


def _lock_path(key):
    return RUN_DIR / (key + ".lock")


def _status_path(key):
    return RUN_DIR / (key + ".json")


def _read(path):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError) as exc:
        raise SovereigntyDebugError("sovereignty_status_invalid") from exc
    return payload if isinstance(payload, dict) else None


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _locked(key):
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    with _lock_path(key).open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


def _options(key, *, scope="current", limit=25, system="", refresh=False):
    _job(key)
    if key == "esi":
        return []
    if scope not in ("current", "all-nullsec"):
        raise SovereigntyDebugError("sovereignty_scope_invalid")
    if type(limit) is not int or limit < 0 or limit > 500:
        raise SovereigntyDebugError("sovereignty_limit_invalid")
    system = str(system or "").strip()
    if len(system) > 100 or any(ord(char) < 32 for char in system):
        raise SovereigntyDebugError("sovereignty_system_invalid")
    command = ["--scope", scope, "--limit", str(limit)]
    if system:
        command.extend(["--system", system])
    if refresh:
        command.append("--refresh")
    return command


def get_sovereignty_debug_status():
    output = {}
    for key, metadata in JOBS.items():
        state = _read(_status_path(key)) or {
            "run_id": None,
            "job": key,
            "label": metadata["label"],
            "status": "idle",
            "started_at": None,
            "finished_at": None,
            "pid": None,
            "return_code": None,
            "error": None,
        }
        is_running = _locked(key)
        if not is_running and state.get("status") in {"starting", "running"}:
            state["status"] = "failed"
            state["error"] = "Worker exited before recording its result; inspect log."
        if is_running and state.get("status") in {"starting", "running"}:
            state["status"] = "running"
        state["running"] = is_running
        state["label"] = metadata["label"]
        output[key] = state
    return output


def start_sovereignty_debug(key, *, scope="current", limit=25, system="", refresh=False):
    metadata = _job(key)
    options = _options(key, scope=scope, limit=limit, system=system, refresh=refresh)
    if not PYTHON_BIN.is_file():
        raise SovereigntyDebugError("sovereignty_python_missing")
    if not metadata["script"].is_file():
        raise SovereigntyDebugError("sovereignty_script_missing")

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    lock_handle = _lock_path(key).open("a+")
    try:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SovereigntyDebugError("sovereignty_already_running") from exc

        run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f") + "_" + key
        state = {
            "run_id": run_id,
            "job": key,
            "label": metadata["label"],
            "status": "starting",
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "finished_at": None,
            "pid": None,
            "return_code": None,
            "error": None,
            "options": options,
        }
        _write(_status_path(key), state)

        command = [
            str(PYTHON_BIN), str(Path(__file__).resolve()), "worker",
            "--job", key,
            "--run-id", run_id,
            "--lock-fd", str(lock_handle.fileno()),
            *options,
        ]
        try:
            process = subprocess.Popen(
                command,
                cwd=str(BASE_DIR),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
                pass_fds=(lock_handle.fileno(),),
            )
        except OSError as exc:
            state.update(
                status="failed",
                finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                error="Worker launch failed: " + type(exc).__name__,
            )
            _write(_status_path(key), state)
            raise SovereigntyDebugError("sovereignty_start_failed") from exc
        return process.pid
    finally:
        # Don't LOCK_UN: the inherited descriptor must continue holding this
        # shared flock until the worker (and its child) have finished.
        lock_handle.close()


def stop_sovereignty_debug(key):
    _job(key)
    state = get_sovereignty_debug_status()[key]
    if not state["running"]:
        raise SovereigntyDebugError("sovereignty_not_running")
    try:
        pid = int(state["pid"])
    except (TypeError, ValueError) as exc:
        raise SovereigntyDebugError("sovereignty_pid_invalid") from exc

    try:
        parts = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\x00")
    except (FileNotFoundError, PermissionError) as exc:
        raise SovereigntyDebugError("sovereignty_process_missing") from exc
    args = [part.decode("utf-8", errors="replace") for part in parts if part]
    if not (
        str(Path(__file__).resolve()) in args
        and "worker" in args
        and "--job" in args
        and key in args
        and state.get("run_id") in args
    ):
        raise SovereigntyDebugError("sovereignty_process_not_ours")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError as exc:
        raise SovereigntyDebugError("sovereignty_process_missing") from exc
    return pid


def read_sovereignty_debug_log(key, *, lines=500):
    _job(key)
    state = _read(_status_path(key)) or {}
    run_id = state.get("run_id") or ""
    if not RUN_ID_RE.fullmatch(run_id) or not run_id.endswith("_" + key):
        return ""
    try:
        lines = min(5000, max(20, int(lines)))
    except (TypeError, ValueError) as exc:
        raise SovereigntyDebugError("sovereignty_lines_invalid") from exc
    path = LOG_DIR / (run_id + ".log")
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except FileNotFoundError:
        return ""


def _worker(argv):
    import argparse

    parser = argparse.ArgumentParser(description="Internal detached Debug sovereignty worker")
    parser.add_argument("--job", choices=tuple(JOBS), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--lock-fd", type=int, required=True)
    parser.add_argument("--scope", choices=("current", "all-nullsec"), default="current")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--system", default="")
    parser.add_argument("--refresh", action="store_true")
    opts = parser.parse_args(argv)

    key, run_id = opts.job, opts.run_id
    if not RUN_ID_RE.fullmatch(run_id) or not run_id.endswith("_" + key):
        raise SovereigntyDebugError("sovereignty_run_id_invalid")
    if opts.lock_fd < 3:
        raise SovereigntyDebugError("sovereignty_lock_fd_invalid")

    command = [
        str(PYTHON_BIN),
        str(_job(key)["script"]),
        *_options(key, scope=opts.scope, limit=opts.limit, system=opts.system, refresh=opts.refresh),
    ]
    state = _read(_status_path(key))
    if not state or state.get("run_id") != run_id:
        raise SovereigntyDebugError("sovereignty_run_state_mismatch")

    state.update(status="running", pid=os.getpid())
    _write(_status_path(key), state)
    stop_requested = False
    child = None

    def on_stop(_signal, _frame):
        nonlocal stop_requested
        stop_requested = True
        if child is not None and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, on_stop)
    log_path = LOG_DIR / (run_id + ".log")
    return_code = 1
    error = None
    try:
        with log_path.open("a", encoding="utf-8", buffering=1) as logfile:
            logfile.write(f"RUN {run_id} · {_job(key)['label']}\n")
            logfile.write("COMMAND " + " ".join(command) + "\n\n")
            logfile.flush()
            if not stop_requested:
                child = subprocess.Popen(
                    command,
                    cwd=str(BASE_DIR),
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                    stdin=subprocess.DEVNULL,
                    stdout=logfile,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                )
                if stop_requested and child.poll() is None:
                    child.terminate()
                return_code = child.wait()
            else:
                return_code = -signal.SIGTERM
            logfile.write(f"\nFINISHED return_code={return_code} stopped={stop_requested}\n")
    except Exception as exc:
        error = "Worker execution failed: " + type(exc).__name__
        try:
            with log_path.open("a", encoding="utf-8") as logfile:
                logfile.write(error + "\n")
        except OSError:
            pass
    finally:
        state.update(
            status="stopped" if stop_requested else ("done" if return_code == 0 and error is None else "failed"),
            finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            return_code=return_code,
            error=error,
        )
        _write(_status_path(key), state)
        _write(HISTORY_DIR / (run_id + ".json"), state)
        os.close(opts.lock_fd)
    return 0 if state["status"] == "done" else 1


if __name__ == "__main__":
    if sys.argv[1:2] != ["worker"]:
        raise SystemExit("Internal sovereignty Debug worker only")
    raise SystemExit(_worker(sys.argv[2:]))
