import fcntl
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


BASE_DIR = Path.home() / "eveosint"
RUNNER_PATH = BASE_DIR / "scripts" / "update_pipeline.py"
PYTHON_BIN = BASE_DIR / "venv" / "bin" / "python"
RUN_DIR = BASE_DIR / "data" / "run" / "update_pipeline"
HISTORY_DIR = RUN_DIR / "history"
STATUS_PATH = RUN_DIR / "status.json"
LOCK_PATH = RUN_DIR / "update_pipeline.lock"
LOG_DIR = BASE_DIR / "data" / "logs" / "update_pipeline"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
TIMEZONE = ZoneInfo("Europe/Paris")


class UpdatePipelineError(RuntimeError):
    pass


def _load_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:
        raise UpdatePipelineError(f"pipeline_status_invalid:{path.name}") from exc
    return data if isinstance(data, dict) else None


def _lock_is_held():
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def _safe_duration(value):
    if value is None:
        return None
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def list_pipeline_history(limit=20):
    limit = max(1, min(100, int(limit or 20)))
    if not HISTORY_DIR.is_dir():
        return []

    result = []
    for path in sorted(HISTORY_DIR.glob("*.json"), key=lambda item: item.name, reverse=True):
        data = _load_json(path)
        if not data:
            continue
        result.append({
            "run_id": data.get("run_id") or path.stem,
            "mode": data.get("mode"),
            "trigger": data.get("trigger"),
            "pipeline_status": data.get("pipeline_status"),
            "started_at": data.get("started_at"),
            "finished_at": data.get("finished_at"),
            "duration_seconds": _safe_duration(data.get("duration_seconds")),
            "failed_step": data.get("failed_step"),
            "last_error": data.get("last_error"),
            "log_path": data.get("log_path"),
            "summary": data.get("summary") or {},
        })
        if len(result) >= limit:
            break
    return result


def _systemctl_show(unit_name, properties):
    command = ["systemctl", "show", unit_name]
    for prop in properties:
        command.extend(["-p", prop])
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return {}
    if completed.returncode != 0:
        return {}
    values = {}
    for line in completed.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def get_timer_status():
    timer = _systemctl_show(
        "eveosint-update.timer",
        ["LoadState", "ActiveState", "SubState", "UnitFileState", "NextElapseUSecRealtime", "LastTriggerUSec"],
    )
    service = _systemctl_show(
        "eveosint-update.service",
        ["LoadState", "ActiveState", "SubState", "Result"],
    )
    return {
        "installed": timer.get("LoadState") == "loaded",
        "active": timer.get("ActiveState") == "active",
        "timer_state": timer.get("ActiveState") or "unknown",
        "timer_substate": timer.get("SubState") or "unknown",
        "enabled": timer.get("UnitFileState") == "enabled",
        "next_run": timer.get("NextElapseUSecRealtime") or None,
        "last_trigger": timer.get("LastTriggerUSec") or None,
        "service_state": service.get("ActiveState") or "unknown",
        "service_substate": service.get("SubState") or "unknown",
        "service_result": service.get("Result") or None,
        "schedule": "Every day 15:30 Europe/Paris",
        "weekly_schedule": "Every day: unified weekly pipeline",
    }


def get_next_update_plan():
    if not PYTHON_BIN.is_file():
        return {"steps": [], "error": "pipeline_python_missing"}
    if not RUNNER_PATH.is_file():
        return {"steps": [], "error": "pipeline_runner_missing"}

    try:
        completed = subprocess.run(
            [str(PYTHON_BIN), str(RUNNER_PATH), "--print-plan-json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        return {"steps": [], "error": f"pipeline_plan_failed:{exc}"}

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        return {"steps": [], "error": detail or f"pipeline_plan_rc_{completed.returncode}"}

    try:
        payload = json.loads(completed.stdout)
    except Exception:
        return {"steps": [], "error": "pipeline_plan_invalid_json"}

    steps = payload.get("steps") if isinstance(payload, dict) else None
    if not isinstance(steps, list):
        return {"steps": [], "error": "pipeline_plan_invalid"}

    return {"steps": steps, "error": None}


def get_pipeline_status(include_history=True, include_next_plan=True):
    state = _load_json(STATUS_PATH) or {
        "run_id": None,
        "mode": None,
        "trigger": None,
        "pid": None,
        "pipeline_status": "idle",
        "running": False,
        "started_at": None,
        "finished_at": None,
        "duration_seconds": None,
        "current_step": None,
        "failed_step": None,
        "last_error": None,
        "target_day": None,
        "superintel": {},
        "summary": {},
        "log_path": None,
        "steps": [],
    }
    state["running"] = _lock_is_held()
    if state["running"] and state.get("pipeline_status") not in {"failed", "done"}:
        state["pipeline_status"] = "running"
    state["timer"] = get_timer_status()
    state["next_plan"] = get_next_update_plan() if include_next_plan else {"steps": [], "error": None}
    if include_history:
        state["history"] = list_pipeline_history(limit=20)
    return state


def _history_run(run_id):
    if not run_id or not RUN_ID_RE.fullmatch(run_id):
        raise UpdatePipelineError("pipeline_run_id_invalid")
    data = _load_json(HISTORY_DIR / f"{run_id}.json")
    if not data:
        raise UpdatePipelineError("pipeline_run_not_found")
    return data


def read_pipeline_log(run_id=None, lines=600):
    try:
        lines = int(lines)
    except (TypeError, ValueError) as exc:
        raise UpdatePipelineError("pipeline_log_lines_invalid") from exc
    lines = max(20, min(5000, lines))

    if run_id:
        state = _history_run(run_id)
    else:
        state = _load_json(STATUS_PATH) or {}

    raw_path = state.get("log_path")
    if not raw_path:
        return ""
    path = Path(raw_path).expanduser()
    try:
        path.relative_to(LOG_DIR)
    except ValueError as exc:
        raise UpdatePipelineError("pipeline_log_path_invalid") from exc
    if not path.is_file():
        return ""

    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


def start_update_pipeline(mode="weekly", trigger="admin"):
    if mode != "weekly":
        raise UpdatePipelineError("pipeline_mode_invalid")
    if _lock_is_held():
        raise UpdatePipelineError("pipeline_already_running")
    if not PYTHON_BIN.is_file():
        raise UpdatePipelineError("pipeline_python_missing")
    if not RUNNER_PATH.is_file():
        raise UpdatePipelineError("pipeline_runner_missing")

    process = subprocess.Popen(
        [str(PYTHON_BIN), str(RUNNER_PATH), "--mode", mode, "--trigger", trigger],
        cwd=str(BASE_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    return process.pid


def get_superintel_runtime_status():
    state = get_pipeline_status(include_history=False, include_next_plan=False)
    super_steps = [item for item in state.get("steps", []) if item.get("group") == "superintel"]
    current = next((item for item in super_steps if item.get("status") == "running"), None)
    failed = next((item for item in super_steps if item.get("status") == "failed"), None)
    finished = [item.get("label") for item in super_steps if item.get("status") in {"done", "skipped"}]
    super_meta = state.get("superintel") or {}

    if failed:
        pipeline_status = "failed"
    elif current:
        pipeline_status = "running"
    elif super_steps and all(item.get("status") in {"done", "skipped"} for item in super_steps):
        pipeline_status = "done"
    elif state.get("running") and super_steps:
        pipeline_status = "waiting"
    else:
        pipeline_status = None

    return {
        "running": bool(current),
        "pid": state.get("pid"),
        "started_at": state.get("started_at"),
        "sync_from_date": super_meta.get("sync_from_date"),
        "events_from_date": super_meta.get("events_from_date"),
        "to_date": super_meta.get("to_date") or state.get("target_day"),
        "current_step": current.get("label") if current else None,
        "pipeline_status": pipeline_status,
        "finished_steps": finished,
        "failed_step": failed.get("label") if failed else None,
        "last_error": failed.get("error") if failed else None,
        "pipeline": [
            {"label": item.get("label"), "command": item.get("command") or []}
            for item in super_steps
        ],
    }


def _fmt_count(value):
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return None


def _ratio_detail(prefix, processed, total):
    try:
        processed_i = int(processed)
        total_i = int(total)
    except (TypeError, ValueError):
        return None, None
    if total_i <= 0:
        return None, None
    percent = round((processed_i / total_i) * 100.0, 1)
    return f"{prefix} {_fmt_count(processed_i)} / {_fmt_count(total_i)}", percent


def _affiliation_public_detail(metrics):
    phase = metrics.get("current_phase")

    if phase == "public_characters":
        detail, percent = _ratio_detail(
            "new characters",
            metrics.get("public_characters_processed", 0),
            metrics.get("public_characters_total") or metrics.get("new_pilots_pending"),
        )
        if detail:
            return detail, percent

    if phase == "initial_histories":
        detail, percent = _ratio_detail(
            "initial histories",
            metrics.get("initial_histories_processed", 0),
            metrics.get("initial_histories_total"),
        )
        if detail:
            return detail, percent

    if phase == "affiliation_bulk":
        detail, percent = _ratio_detail(
            "affiliation batches",
            metrics.get("affiliation_batches_processed", 0),
            metrics.get("affiliation_batches_total"),
        )
        if detail:
            return detail, percent

    if phase == "targeted_histories":
        detail, percent = _ratio_detail(
            "histories",
            metrics.get("character_histories_processed", 0),
            metrics.get("character_histories_total") or metrics.get("pilot_affiliation_changes"),
        )
        if detail:
            return detail, percent

    if phase == "corporations":
        value = _fmt_count(metrics.get("unknown_corporations"))
        return (f"{value} corporations to complete" if value else "corporation data", None)

    if phase == "corporation_histories":
        value = _fmt_count(metrics.get("corporation_histories_queued"))
        return (f"{value} corporation histories" if value else "corporation histories", None)

    if phase == "alliances":
        value = _fmt_count(metrics.get("unknown_alliances"))
        return (f"{value} alliances to complete" if value else "alliance data", None)

    return None, None


def _pipeline_pid_is_running(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False

    cmdline_path = Path(f"/proc/{pid}/cmdline")
    try:
        cmdline = cmdline_path.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace")
    except (FileNotFoundError, PermissionError, OSError):
        return False

    return RUNNER_PATH.name in cmdline


def get_public_update_status():
    """Return a deliberately filtered status payload for the global UI banner.

    This does not call systemctl and never exposes child commands, internal
    SuperINTEL step names, log paths or implementation details.
    """
    state = _load_json(STATUS_PATH) or {}

    # The lock remains the primary source of truth.  The live runner PID is a
    # safe fallback for launches where the status is already running but the
    # web process cannot observe the flock (for example a service/daemon launch).
    running = _lock_is_held() or (
        state.get("pipeline_status") == "running"
        and _pipeline_pid_is_running(state.get("pid"))
    )
    if not running:
        return {"running": False}

    steps = state.get("steps") or []
    current_step_id = state.get("current_step")
    current = next((item for item in steps if item.get("id") == current_step_id), None)
    if current is None:
        current = next((item for item in steps if item.get("status") == "running"), None)

    if current is None:
        return {
            "running": True,
            "kind": "generic",
            "label": "EVEOSINT update running",
            "detail": "Starting pipeline",
            "percent": None,
        }

    try:
        step_index = steps.index(current) + 1
    except ValueError:
        step_index = None
    step_total = len(steps)
    stage_prefix = (
        f"Step {step_index}/{step_total}"
        if step_index and step_total
        else None
    )

    def stage_detail(value=None):
        parts = [part for part in (stage_prefix, value) if part]
        return " · ".join(parts) if parts else None

    step_id = current.get("id") or ""
    group = current.get("group") or ""
    metrics = current.get("metrics") or {}
    detail = None
    percent = None

    if step_id == "sync_sde":
        detail, percent = _ratio_detail(
            "tables",
            metrics.get("sde_tables_done", 0),
            metrics.get("sde_tables_total"),
        )
        return {
            "running": True,
            "kind": "sde",
            "label": "SDE synchronization",
            "detail": stage_detail(detail),
            "percent": percent,
        }

    if group == "superintel" or step_id.startswith("superintel_"):
        return {
            "running": True,
            "kind": "superintel",
            "label": "SuperINTEL update running",
            "detail": stage_detail(),
            "percent": None,
        }

    if step_id == "character_skill_inference":
        period = metrics.get("current_period")
        return {
            "running": True,
            "kind": "character_intelligence",
            "label": "Character intelligence update",
            "detail": stage_detail(f"processing {period}" if period else None),
            "percent": None,
        }

    if step_id == "recent_pilot_affiliation_3d":
        detail, percent = _affiliation_public_detail(metrics)
        prefix_parts = []
        recent = _fmt_count(metrics.get("recent_pilots"))
        if recent:
            prefix_parts.append(f"{recent} pilots")
        pending = _fmt_count(metrics.get("new_pilots_pending"))
        if pending and metrics.get("current_phase") == "public_characters":
            prefix_parts.append(f"{pending} new")
        if detail:
            prefix_parts.append(detail)
        return {
            "running": True,
            "kind": "recent_pilots",
            "label": "Recent pilots update",
            "detail": stage_detail(" · ".join(prefix_parts) if prefix_parts else None),
            "percent": percent,
        }

    if step_id == "pilotable_ships_recent":
        return {
            "running": True,
            "kind": "pilotable",
            "label": "Pilotable ships calculation running",
            "detail": stage_detail(),
            "percent": None,
        }

    if step_id == "weekly_pilot_affiliation_30y":
        detail, percent = _affiliation_public_detail(metrics)
        return {
            "running": True,
            "kind": "historical_affiliations",
            "label": "Historical affiliations update",
            "detail": stage_detail(detail),
            "percent": percent,
        }

    if step_id == "weekly_entities_backfill_audit":
        phase = metrics.get("current_phase")
        if phase == "unknown_corporation_profiles":
            detail, percent = _ratio_detail(
                "corporation profiles",
                metrics.get("corporation_profiles_processed", 0),
                metrics.get("corporation_profiles_total"),
            )
        elif phase == "unknown_corporation_histories":
            detail, percent = _ratio_detail(
                "corporation histories",
                metrics.get("corporation_histories_processed", 0),
                metrics.get("corporation_histories_total"),
            )
        elif phase == "unknown_alliance_profiles":
            detail, percent = _ratio_detail(
                "alliance profiles",
                metrics.get("alliance_profiles_processed", 0),
                metrics.get("alliance_profiles_total"),
            )
        elif phase == "corporations_from_history":
            value = _fmt_count(metrics.get("history_corporations"))
            detail, percent = (f"{value} historical corporations" if value else "historical corporations", None)
        elif phase == "unknown_corporations":
            value = _fmt_count(metrics.get("unknown_corporations"))
            detail, percent = (f"{value} missing corporations" if value else "missing corporations", None)
        elif phase == "alliances_from_history":
            value = _fmt_count(metrics.get("history_alliances"))
            detail, percent = (f"{value} historical alliances" if value else "historical alliances", None)
        elif phase == "unknown_alliances":
            value = _fmt_count(metrics.get("unknown_alliances"))
            detail, percent = (f"{value} missing alliances" if value else "missing alliances", None)
        else:
            detail, percent = None, None
        return {
            "running": True,
            "kind": "entities",
            "label": "Entities maintenance",
            "detail": stage_detail(detail),
            "percent": percent,
        }

    if step_id == "weekly_pilotable_ships":
        return {
            "running": True,
            "kind": "pilotable_full",
            "label": "Full pilotable ships calculation running",
            "detail": stage_detail(),
            "percent": None,
        }

    return {
        "running": True,
        "kind": "generic",
        "label": current.get("label") or "EVEOSINT update running",
        "detail": stage_detail(),
        "percent": None,
    }
