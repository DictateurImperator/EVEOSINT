import json
import os
import re
import signal
import subprocess
import sys
from collections import deque
from datetime import date, datetime
from pathlib import Path

from .config import JOBS_CONFIG_PATH


class JobError(Exception):
    pass


RUN_DIR = Path.home() / "eveosint" / "data" / "run"


def _read_config_file():
    if not JOBS_CONFIG_PATH.exists():
        raise JobError("jobs_config_missing")

    with JOBS_CONFIG_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    if not isinstance(data, dict):
        raise JobError("jobs_config_invalid")

    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        raise JobError("jobs_list_missing")

    return data


def _pid_path(job_key):
    return RUN_DIR / f"{job_key}.pid"


def _status_path(job_key):
    return RUN_DIR / f"{job_key}.json"


def _is_pid_running(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False

    # A finished child can remain briefly as a zombie until it is reaped.
    # os.kill(pid, 0) still succeeds for zombies, so do not report them as
    # running in the Admin jobs page.
    try:
        stat_content = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        close_paren = stat_content.rfind(")")
        if close_paren != -1:
            fields_after_comm = stat_content[close_paren + 2 :].split()
            if fields_after_comm and fields_after_comm[0] == "Z":
                return False
    except FileNotFoundError:
        return False
    except (OSError, UnicodeError):
        # If /proc cannot be inspected, keep the existing conservative
        # behaviour based on os.kill(pid, 0).
        pass

    return True


def _read_pid(job_key):
    path = _pid_path(job_key)
    if not path.exists():
        return None

    content = path.read_text(encoding="utf-8").strip()
    if not content:
        raise JobError(f"job_pid_file_empty:{job_key}")

    try:
        return int(content)
    except ValueError as exc:
        raise JobError(f"job_pid_file_invalid:{job_key}") from exc


def _cleanup_stale_pid(job_key):
    pid = _read_pid(job_key)
    if pid is None:
        return False, None

    if _is_pid_running(pid):
        return True, pid

    _pid_path(job_key).unlink(missing_ok=True)
    return False, None


def _write_runtime_status(job_key, pid, started_at):
    RUN_DIR.mkdir(parents=True, exist_ok=True)

    _pid_path(job_key).write_text(str(pid), encoding="utf-8")
    _status_path(job_key).write_text(
        json.dumps(
            {
                "pid": pid,
                "started_at": started_at,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _read_runtime_status(job_key):
    running, pid = _cleanup_stale_pid(job_key)
    status = {
        "running": running,
        "pid": pid,
        "started_at": None,
    }

    path = _status_path(job_key)
    if not path.exists():
        return status

    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    if not isinstance(data, dict):
        raise JobError(f"job_status_invalid:{job_key}")

    status["started_at"] = data.get("started_at")
    return status


def _normalize_job(raw_job):
    if not isinstance(raw_job, dict):
        raise JobError("job_invalid")

    key = str(raw_job.get("key", "")).strip()
    if not key:
        raise JobError("job_key_missing")

    label = str(raw_job.get("label", key)).strip() or key
    job_type = str(raw_job.get("type", "")).strip()
    command = raw_job.get("command")
    log_path = raw_job.get("log_path")
    enabled = bool(raw_job.get("enabled", True))

    if job_type not in {"sde", "killmails", "recent_kill_pilots_affiliation", "character_skill_inference", "population_alliances_init", "population_alliances_daily", "sovereignty_esi", "killmail_forensics_setup", "killmail_forensics_analysis", "killmail_archive_refresh", "mer_economy"}:
        raise JobError(f"job_type_invalid:{key}")

    if not isinstance(command, list) or not command:
        raise JobError(f"job_command_missing:{key}")

    command = [str(part) for part in command]

    if not log_path:
        raise JobError(f"job_log_path_missing:{key}")

    log_path = str(log_path)
    path = Path(log_path).expanduser()
    runtime = _read_runtime_status(key)

    return {
        "key": key,
        "label": label,
        "type": job_type,
        "command": command,
        "log_path": log_path,
        "log_exists": path.is_file(),
        "enabled": enabled,
        "running": runtime["running"],
        "started_at": runtime["started_at"],
    }


def load_jobs_config():
    data = _read_config_file()
    jobs = []

    for raw_job in data["jobs"]:
        job = _normalize_job(raw_job)
        jobs.append(job)

    if not any(job["key"] == "population_alliances_init" for job in jobs):
        jobs.append(
            _normalize_job(
                {
                    "key": "population_alliances_init",
                    "label": "Population · Initialize alliances (DOTLAN)",
                    "type": "population_alliances_init",
                    "command": [
                        sys.executable,
                        str(Path(__file__).resolve().parent / "population_alliances_init.py"),
                    ],
                    "log_path": str(Path.home() / "eveosint" / "data" / "logs" / "population_alliances_init.log"),
                    "enabled": True,
                }
            )
        )

    if not any(job["key"] == "population_alliances_daily" for job in jobs):
        jobs.append(
            _normalize_job(
                {
                    "key": "population_alliances_daily",
                    "label": "Population · Update live alliances (DOTLAN)",
                    "type": "population_alliances_daily",
                    "command": [
                        sys.executable,
                        str(Path(__file__).resolve().parent / "population_alliances_daily.py"),
                    ],
                    "log_path": str(Path.home() / "eveosint" / "data" / "logs" / "population_alliances_daily.log"),
                    "enabled": True,
                }
            )
        )

    if not any(job["key"] == "sync_sovereignty_esi" for job in jobs):
        jobs.append(
            _normalize_job(
                {
                    "key": "sync_sovereignty_esi",
                    "label": "Sovereignty · Refresh ESI",
                    "type": "sovereignty_esi",
                    "command": [
                        sys.executable,
                        str(Path(__file__).resolve().parents[2] / "scripts" / "sync_sovereignty_esi.py"),
                    ],
                    "log_path": str(Path.home() / "eveosint" / "data" / "logs" / "sovereignty_esi.log"),
                    "enabled": True,
                }
            )
        )

    if not any(job["key"] == "setup_killmail_forensics" for job in jobs):
        jobs.append(
            _normalize_job({
                "key": "setup_killmail_forensics",
                "label": "Forensics · Create investigation tables (run once)",
                "type": "killmail_forensics_setup",
                "command": [
                    sys.executable,
                    str(Path(__file__).resolve().parents[2] / "scripts/setup_killmail_forensics.py"),
                ],
                "log_path": str(Path.home() / "eveosint/data/logs/killmail_forensics_setup.log"),
                "enabled": True,
            })
        )

    if not any(job["key"] == "analyze_hidden_killmails" for job in jobs):
        jobs.append(_normalize_job({
            "key": "analyze_hidden_killmails",
            "label": "Forensics · Analyze all hidden killmails",
            "type": "killmail_forensics_analysis",
            "command": [sys.executable, str(Path(__file__).resolve().parents[2] / "scripts/analyze_hidden_killmails.py")],
            "log_path": str(Path.home() / "eveosint/data/logs/killmail_forensics_analysis.log"),
            "enabled": True,
        }))

    if not any(job["key"] == "refresh_killmail_archives" for job in jobs):
        jobs.append(_normalize_job({
            "key": "refresh_killmail_archives",
            "label": "Killmails · Refresh updated EVE Ref archives",
            "type": "killmail_archive_refresh",
            "command": [sys.executable, str(Path(__file__).resolve().parents[2] / "scripts/refresh_killmail_archives.py")],
            "log_path": str(Path.home() / "eveosint/data/logs/killmail_archive_refresh.log"),
            "enabled": True,
        }))

    if not any(job["key"] == "import_mer_economy" for job in jobs):
        jobs.append(_normalize_job({
            "key": "import_mer_economy",
            "label": "MER · Import economic data",
            "type": "mer_economy",
            "command": [sys.executable, str(Path(__file__).resolve().parents[2] / "scripts/import_mer_economy.py")],
            "log_path": str(Path.home() / "eveosint/data/logs/mer_economy_import.log"),
            "enabled": True,
        }))

    allowed_keys = {
        "import_mer_economy",
        "sync_sde",
        "sync_killmails",
        "refresh_killmail_archives",
        "sync_recent_kill_pilots_affiliation",
        "sync_character_skill_inference",
        "population_alliances_init",
        "population_alliances_daily",
        "sync_sovereignty_esi",
        "setup_killmail_forensics",
        "analyze_hidden_killmails",
    }
    unexpected = {job["key"] for job in jobs} - allowed_keys
    if unexpected:
        raise JobError("job_not_whitelisted:" + ",".join(sorted(unexpected)))

    return jobs


def get_job(job_key):
    for job in load_jobs_config():
        if job["key"] == job_key:
            return job

    raise JobError(f"job_unknown:{job_key}")


def _tail_file(path, lines=80):
    log_path = Path(path).expanduser()

    if not log_path.is_file():
        raise JobError("job_log_missing")

    content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


def list_jobs_with_logs():
    jobs = load_jobs_config()

    today = date.today()
    yesterday = date.fromordinal(today.toordinal() - 1)

    for job in jobs:
        job["last_lines"] = _tail_file(job["log_path"]) if job["log_exists"] else ""

        if job["type"] == "killmails":
            job["default_from_date"] = yesterday.isoformat()
            job["default_to_date"] = today.isoformat()
            job["default_workers"] = 4

        if job["type"] == "recent_kill_pilots_affiliation":
            job["default_days"] = 7
            job["default_workers"] = 4

        if job["type"] == "character_skill_inference":
            job["default_progress_batch_size"] = 1000
            job["default_limit"] = ""

    return jobs


def read_job_log(job_key, lines=200):
    job = get_job(job_key)
    return _tail_file(job["log_path"], lines=lines)


def _open_log_for_append(log_path):
    path = Path(log_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("ab")


def _start_process(job, command):
    if not job["enabled"]:
        raise JobError(f"job_disabled:{job['key']}")

    running, _pid = _cleanup_stale_pid(job["key"])
    if running:
        raise JobError(f"job_already_running:{job['key']}")

    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with _open_log_for_append(job["log_path"]) as log_handle:
        process = subprocess.Popen(
            command,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )

    _write_runtime_status(job["key"], process.pid, started_at)
    return True, "started"


def run_sde_job():
    job = get_job("sync_sde")
    if job["type"] != "sde":
        raise JobError("job_type_mismatch:sync_sde")

    return _start_process(job, job["command"])


def _parse_date(value, field_name):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception as exc:
        raise JobError(f"invalid_date:{field_name}") from exc


def run_killmail_job(from_date, to_date, workers):
    if _cleanup_stale_pid("refresh_killmail_archives")[0]:
        raise JobError("job_already_running:refresh_killmail_archives")
    job = get_job("sync_killmails")
    if job["type"] != "killmails":
        raise JobError("job_type_mismatch:sync_killmails")

    start = _parse_date(from_date, "from")
    end = _parse_date(to_date, "to")

    if start > end:
        raise JobError("date_range_invalid")

    try:
        workers_value = int(workers)
    except Exception as exc:
        raise JobError("workers_invalid") from exc

    if workers_value < 1 or workers_value > 16:
        raise JobError("workers_invalid")

    command = list(job["command"]) + [
        "--from",
        start.isoformat(),
        "--to",
        end.isoformat(),
        "--workers",
        str(workers_value),
    ]

    return _start_process(job, command)


def run_recent_kill_pilots_affiliation_job(days, workers):
    job = get_job("sync_recent_kill_pilots_affiliation")
    if job["type"] != "recent_kill_pilots_affiliation":
        raise JobError("job_type_mismatch:sync_recent_kill_pilots_affiliation")

    try:
        days_value = int(days)
    except Exception as exc:
        raise JobError("days_invalid") from exc

    if days_value < 1:
        raise JobError("days_invalid")

    try:
        workers_value = int(workers)
    except Exception as exc:
        raise JobError("workers_invalid") from exc

    if workers_value < 1 or workers_value > 16:
        raise JobError("workers_invalid")

    command = list(job["command"]) + [
        "--days",
        str(days_value),
        "--workers",
        str(workers_value),
    ]

    return _start_process(job, command)


def run_character_skill_inference_job(progress_batch_size, limit):
    job = get_job("sync_character_skill_inference")
    if job["type"] != "character_skill_inference":
        raise JobError("job_type_mismatch:sync_character_skill_inference")

    try:
        progress_batch_size_value = int(progress_batch_size)
    except Exception as exc:
        raise JobError("progress_batch_size_invalid") from exc

    if progress_batch_size_value < 1 or progress_batch_size_value > 100000:
        raise JobError("progress_batch_size_invalid")

    command = list(job["command"]) + [
        "--progress-batch-size",
        str(progress_batch_size_value),
    ]

    limit_value = None
    if limit is not None and str(limit).strip():
        try:
            limit_value = int(limit)
        except Exception as exc:
            raise JobError("limit_invalid") from exc

        if limit_value < 1:
            raise JobError("limit_invalid")

        command.extend(["--limit", str(limit_value)])

    return _start_process(job, command)


def run_population_alliances_init_job():
    job = get_job("population_alliances_init")
    if job["type"] != "population_alliances_init":
        raise JobError("job_type_mismatch:population_alliances_init")

    return _start_process(job, job["command"])


def run_population_alliances_daily_job():
    job = get_job("population_alliances_daily")
    if job["type"] != "population_alliances_daily":
        raise JobError("job_type_mismatch:population_alliances_daily")

    return _start_process(job, job["command"])


def run_sovereignty_esi_job():
    job = get_job("sync_sovereignty_esi")
    if job["type"] != "sovereignty_esi":
        raise JobError("job_type_mismatch:sync_sovereignty_esi")

    return _start_process(job, job["command"])


def run_killmail_forensics_setup_job():
    job = get_job("setup_killmail_forensics")
    if job["type"] != "killmail_forensics_setup":
        raise JobError("job_type_mismatch:setup_killmail_forensics")
    return _start_process(job, job["command"])


def run_forensics_analysis_job(user_id, date_from=None, date_to=None, refresh_only=False):
    from .forensics_batch import date_scope
    try:
        bounds = date_scope(date_from, date_to)
        if int(user_id) <= 0:
            raise ValueError("A user ID is required.")
    except (ValueError, TypeError) as exc:
        raise JobError(str(exc)) from exc
    job = get_job("analyze_hidden_killmails")
    if job["type"] != "killmail_forensics_analysis":
        raise JobError("job_type_mismatch:analyze_hidden_killmails")
    command = list(job["command"]) + ["--user-id", str(int(user_id))]
    if refresh_only:
        command.append("--refresh-only")
    for option, value in zip(("--date-from", "--date-to"), bounds):
        if value:
            command.extend([option, value.isoformat()])
    return _start_process(job, command)


def run_killmail_archive_refresh_job(date_from=None, date_to=None):
    start = _parse_date(date_from, "from") if date_from else None
    end = _parse_date(date_to, "to") if date_to else None
    if start and end and start > end:
        raise JobError("date_range_invalid")
    if _cleanup_stale_pid("sync_killmails")[0]:
        raise JobError("job_already_running:sync_killmails")
    job = get_job("refresh_killmail_archives")
    if job["type"] != "killmail_archive_refresh":
        raise JobError("job_type_mismatch:refresh_killmail_archives")
    command = list(job["command"])
    for option, value in (("--from", start), ("--to", end)):
        if value:
            command.extend([option, value.isoformat()])
    return _start_process(job, command)


def stop_killmail_archive_refresh_job():
    job = get_job("refresh_killmail_archives")
    running, pid = _cleanup_stale_pid(job["key"])
    if not running:
        return True, "already_stopped"
    try:
        arguments = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        if os.fsencode(job["command"][1]) not in arguments:
            raise JobError("job_process_mismatch:refresh_killmail_archives")
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True, "already_stopped"
    except OSError as exc:
        raise JobError("job_stop_failed:refresh_killmail_archives") from exc
    return True, "stop_requested"


def read_killmail_archive_refresh_progress():
    runtime = _read_runtime_status("refresh_killmail_archives")
    path = Path.home() / "eveosint/data/killmails/refresh_progress.json"
    try:
        progress = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(progress, dict):
            progress = None
    except (OSError, ValueError):
        progress = None
    if runtime["running"] and (not progress or progress.get("pid") != runtime["pid"]):
        progress = {"phase": "starting"}
    elif progress and not runtime["running"] and progress.get("phase") not in {"completed", "stopped", "failed"}:
        progress = dict(progress, phase="interrupted")
    return {"running": runtime["running"], "progress": progress}


def read_killmail_archive_errors(limit=20):
    """Read bounded error blocks from the full append-only log, including tracebacks."""
    job = get_job("refresh_killmail_archives")
    path = Path(job["log_path"]).expanduser()
    header = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[,.]\d+)?\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b")
    blocks = deque(maxlen=limit)
    current = None
    truncated = False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = header.match(line)
                if match:
                    if current:
                        blocks.append("".join(current))
                    current = [line] if match.group(1) in {"ERROR", "CRITICAL"} else None
                    truncated = False
                elif line.startswith("Traceback (most recent call last):") and current is None:
                    current = [line]
                elif current is not None:
                    if len(current) < 100:
                        current.append(line)
                    elif not truncated:
                        current.append("[Traceback truncated after 100 lines]\n")
                        truncated = True
    except OSError as exc:
        raise JobError("job_log_missing") from exc
    if current:
        blocks.append("".join(current))
    return "\n".join(blocks) or "No errors recorded in the archive refresh log."


def run_mer_economy_import_job():
    job = get_job("import_mer_economy")
    if job["type"] != "mer_economy":
        raise JobError("job_type_mismatch:import_mer_economy")
    return _start_process(job, job["command"])


def read_mer_economy_progress():
    runtime = _read_runtime_status("import_mer_economy")
    path = Path.home() / "eveosint/data/mer/economy_progress.json"
    try:
        progress = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(progress, dict):
            progress = None
    except (OSError, ValueError):
        progress = None
    if runtime["running"] and (not progress or progress.get("pid") != runtime["pid"]):
        progress = {"phase": "starting"}
    elif progress and not runtime["running"] and progress.get("phase") not in {"completed", "failed"}:
        progress = dict(progress, phase="interrupted")
    return {"running": runtime["running"], "progress": progress}
