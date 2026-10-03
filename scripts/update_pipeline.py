#!/usr/bin/env python3
import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


BASE_DIR = Path.home() / "eveosint"
WEB_DIR = BASE_DIR / "web"
SCRIPT_DIR = BASE_DIR / "scripts"
DATA_DIR = BASE_DIR / "data"
RUN_DIR = DATA_DIR / "run" / "update_pipeline"
HISTORY_DIR = RUN_DIR / "history"
LOG_DIR = DATA_DIR / "logs" / "update_pipeline"
STATUS_PATH = RUN_DIR / "status.json"
LOCK_PATH = RUN_DIR / "update_pipeline.lock"
TIMEZONE = ZoneInfo("Europe/Paris")
PYTHON_BIN = BASE_DIR / "venv" / "bin" / "python"
WEEKLY_DAYS = 10950


class PipelineError(RuntimeError):
    pass


def now_local():
    return datetime.now(TIMEZONE)


def iso_now():
    return now_local().isoformat(timespec="seconds")


def atomic_write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def duration_seconds(started_at, finished_at=None):
    if not started_at:
        return None
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(finished_at) if finished_at else now_local()
        return round((end - start).total_seconds(), 1)
    except Exception:
        return None




KV_NUMBER_RE = re.compile(r"([A-Za-z][A-Za-z0-9_]*)=(-?\d+(?:\.\d+)?)")


def _number(value):
    try:
        if "." in str(value):
            return float(value)
        return int(value)
    except (TypeError, ValueError):
        return value


def _kv_numbers(line):
    return {key: _number(value) for key, value in KV_NUMBER_RE.findall(line or "")}


def _set_metric(item, key, value):
    if value is None:
        return False
    metrics = item.setdefault("metrics", {})
    normalized = _number(value)
    if metrics.get(key) == normalized:
        return False
    metrics[key] = normalized
    return True


def _set_text_metric(item, key, value):
    if value is None:
        return False
    metrics = item.setdefault("metrics", {})
    normalized = str(value)
    if metrics.get(key) == normalized:
        return False
    metrics[key] = normalized
    return True


def _increment_metric(item, key, amount=1):
    metrics = item.setdefault("metrics", {})
    metrics[key] = int(metrics.get(key, 0) or 0) + int(amount)
    return True


def _sum_values(values):
    total = 0
    found = False
    for value in values:
        if isinstance(value, (int, float)):
            total += value
            found = True
    return total if found else None


def refresh_run_summary(state):
    summary_keys = {
        "killmails_scanned",
        "events_inserted",
        "new_pilots",
        "pilot_affiliation_changes",
        "new_character_histories",
        "character_histories_refreshed",
        "skill_killmails_processed",
        "new_corporations",
        "new_alliances",
        "pilotable_processed",
        "api_failures",
    }
    summary = {}
    for key in summary_keys:
        values = [item.get("metrics", {}).get(key) for item in state.get("steps", [])]
        total = _sum_values(values)
        if total is not None:
            summary[key] = total
    state["summary"] = summary


def parse_step_metrics(item, line):
    step_id = item.get("id") or ""
    changed = False
    kv = _kv_numbers(line)

    if step_id == "sync_sde":
        match = re.search(r"\bbuild:\s*(\d+)", line)
        if match:
            changed |= _set_metric(item, "sde_build", match.group(1))
        if "déjà à jour" in line or "deja a jour" in line.lower():
            changed |= _set_metric(item, "sde_already_current", 1)
        if "SDE TABLES total=" in line:
            changed |= _set_metric(item, "sde_tables_total", kv.get("total"))
        if "SDE PROGRESS done=" in line:
            changed |= _set_metric(item, "sde_tables_done", kv.get("done"))
            changed |= _set_metric(item, "sde_tables_total", kv.get("total"))

    if "sync_killmails" in step_id:
        if re.search(r"\bSUCCESS\s+\d{4}-\d{2}-\d{2}\b", line):
            changed |= _increment_metric(item, "killmail_days_loaded")
        elif re.search(r"\bSKIP\s+\d{4}-\d{2}-\d{2}\s+already success", line):
            changed |= _increment_metric(item, "killmail_days_skipped")
        elif re.search(r"\b\d{4}-\d{2}-\d{2}\s+missing\b", line):
            changed |= _increment_metric(item, "killmail_days_missing")

    if step_id == "superintel_build_events":
        if "Todo killmails=" in line:
            changed |= _set_metric(item, "killmails_scanned", kv.get("killmails"))
        if "super_pilots upsert affected=" in line:
            changed |= _set_metric(item, "super_pilots_upserted", kv.get("affected"))
        if "pilot_last_activity upsert affected=" in line:
            changed |= _set_metric(item, "last_activity_upserted", kv.get("affected"))
        if "ship_sessions upsert affected=" in line:
            changed |= _set_metric(item, "ship_sessions_upserted", kv.get("affected"))
        if "Victim events inserted=" in line and "inserted" in kv:
            changed |= _set_metric(item, "victim_events_inserted", kv.get("inserted"))
        if "Attacker events inserted=" in line and "inserted" in kv:
            changed |= _set_metric(item, "attacker_events_inserted", kv.get("inserted"))
        if "SUCCESS run_id=" in line and "scanned=" in line:
            changed |= _set_metric(item, "killmails_scanned", kv.get("scanned"))
            changed |= _set_metric(item, "events_inserted", kv.get("inserted_events"))
            changed |= _set_metric(item, "super_killmails", kv.get("super_killmails"))
            changed |= _set_metric(item, "super_pilots_upserted", kv.get("super_pilots_affected"))
            changed |= _set_metric(item, "last_activity_upserted", kv.get("last_activity_affected"))
            changed |= _set_metric(item, "ship_sessions_upserted", kv.get("ship_sessions_affected"))

    affiliation_steps = {
        "superintel_refresh_affiliations",
        "recent_pilot_affiliation_3d",
        "weekly_pilot_affiliation_30y",
    }
    if step_id in affiliation_steps:
        if "Loaded source character IDs into persistent work table=" in line:
            changed |= _set_metric(item, "source_pilots", kv.get("table"))
        if "STEP 1 public characters missing=" in line:
            changed |= _set_text_metric(item, "current_phase", "public_characters")
            changed |= _set_metric(item, "new_pilots_pending", kv.get("missing"))
        if "STEP 3 initial character histories=" in line:
            changed |= _set_text_metric(item, "current_phase", "initial_histories")
            changed |= _set_metric(item, "initial_histories_total", kv.get("histories"))
        if "STEP 4 affiliation bulk candidates=" in line:
            changed |= _set_text_metric(item, "current_phase", "affiliation_bulk")
            candidates = kv.get("candidates")
            batch_size = kv.get("batch_size")
            changed |= _set_metric(item, "affiliation_candidates", candidates)
            changed |= _set_metric(item, "affiliation_batch_size", batch_size)
            if isinstance(candidates, int) and isinstance(batch_size, int) and batch_size > 0:
                changed |= _set_metric(item, "affiliation_batches_total", (candidates + batch_size - 1) // batch_size)
        if "AFFILIATION BATCH size=" in line:
            changed |= _increment_metric(item, "affiliation_batches_processed")
        if "PROGRESS stage=" in line:
            stage_match = re.search(r"PROGRESS stage=([A-Za-z0-9_]+)", line)
            stage_name = stage_match.group(1) if stage_match else None
            processed = kv.get("processed")
            total = kv.get("total")
            if stage_name == "public_character":
                changed |= _set_text_metric(item, "current_phase", "public_characters")
                changed |= _set_metric(item, "public_characters_processed", processed)
                changed |= _set_metric(item, "public_characters_total", total)
            elif stage_name == "initial_character_history":
                changed |= _set_text_metric(item, "current_phase", "initial_histories")
                changed |= _set_metric(item, "initial_histories_processed", processed)
                changed |= _set_metric(item, "initial_histories_total", total)
            elif stage_name == "targeted_character_history":
                changed |= _set_text_metric(item, "current_phase", "targeted_histories")
                changed |= _set_metric(item, "character_histories_processed", processed)
                changed |= _set_metric(item, "character_histories_total", total)
        if "STEP 5 affiliation SQL compare" in line:
            changed |= _set_text_metric(item, "current_phase", "targeted_histories")
            changed |= _set_metric(item, "pilot_affiliation_changes", kv.get("queued_character_history"))
        if "STEP 6 unknown corporations=" in line:
            changed |= _set_text_metric(item, "current_phase", "corporations")
            changed |= _set_metric(item, "unknown_corporations", kv.get("corporations"))
        if "STEP 7 corporation alliance history refresh queued=" in line:
            changed |= _set_text_metric(item, "current_phase", "corporation_histories")
            changed |= _set_metric(item, "corporation_histories_queued", kv.get("queued"))
        if "STEP 8 unknown alliances=" in line:
            changed |= _set_text_metric(item, "current_phase", "alliances")
            changed |= _set_metric(item, "unknown_alliances", kv.get("alliances"))
        if "DONE rebuild recent killmail pilots" in line:
            changed |= _set_metric(item, "recent_pilots", kv.get("rows"))
        if "DONE run_id=" in line and "source_ids=" in line:
            changed |= _set_metric(item, "source_pilots", kv.get("source_ids"))
            changed |= _set_metric(item, "new_pilots", kv.get("public_char_loaded"))
            changed |= _set_metric(item, "deleted_pilots", kv.get("public_char_deleted"))
            changed |= _set_metric(item, "new_character_histories", kv.get("initial_hist_ok"))
            changed |= _set_metric(item, "pilot_affiliation_changes", kv.get("queued_char_hist"))
            changed |= _set_metric(item, "character_histories_refreshed", kv.get("refreshed_char_hist"))
            changed |= _set_metric(item, "new_corporations", kv.get("corp_public_loaded"))
            changed |= _set_metric(item, "deleted_corporations", kv.get("corp_public_deleted"))
            corp_hist = _sum_values([kv.get("corp_hist_loaded"), kv.get("refreshed_corp_hist")])
            changed |= _set_metric(item, "corporation_histories_refreshed", corp_hist)
            changed |= _set_metric(item, "new_alliances", kv.get("alliance_loaded"))
            changed |= _set_metric(item, "deleted_alliances", kv.get("alliance_deleted"))
            failures = _sum_values([
                kv.get("public_char_failed"), kv.get("initial_hist_failed"), kv.get("aff_failed"),
                kv.get("refreshed_char_hist_failed"), kv.get("corp_hist_failed"),
                kv.get("refreshed_corp_hist_failed"), kv.get("alliance_failed"),
            ])
            changed |= _set_metric(item, "api_failures", failures)

    if step_id == "character_skill_inference":
        month_match = re.search(r"\bMONTH\s+(\d{4}_\d{2})\s+start\b", line)
        if month_match:
            changed |= _set_text_metric(item, "current_period", month_match.group(1).replace("_", "-"))
        if "PROGRESS month=" in line:
            changed |= _set_metric(item, "skill_killmails_processed", kv.get("total_processed"))
        if "DONE character skill inference" in line:
            changed |= _set_metric(item, "skill_killmails_processed", kv.get("total_processed"))

    if step_id in {"pilotable_ships_recent", "weekly_pilotable_ships"}:
        if "progress processed=" in line:
            match = re.search(r"processed=(\d+)/(\d+)", line)
            if match:
                changed |= _set_metric(item, "pilotable_processed", match.group(1))
                changed |= _set_metric(item, "pilotable_total", match.group(2))
            changed |= _set_metric(item, "pilotable_upserted", kv.get("upserted"))
        if "DONE mode=" in line:
            changed |= _set_metric(item, "pilotable_processed", kv.get("processed"))
            changed |= _set_metric(item, "pilotable_upserted", kv.get("upserted"))

    if step_id == "superintel_build_report" and "DONE character=" in line:
        changed |= _set_metric(item, "reports_character", kv.get("character"))
        changed |= _set_metric(item, "reports_corporation", kv.get("corporation"))
        changed |= _set_metric(item, "reports_alliance", kv.get("alliance"))

    if step_id == "weekly_entities_backfill_audit":
        if "STEP 1 history corporations=" in line:
            changed |= _set_text_metric(item, "current_phase", "corporations_from_history")
            changed |= _set_metric(item, "history_corporations", kv.get("corporations"))
        if "STEP 2 unknown corporations=" in line:
            changed |= _set_text_metric(item, "current_phase", "unknown_corporations")
            changed |= _set_metric(item, "unknown_corporations", kv.get("corporations"))
        if "STEP 3 history alliances=" in line:
            changed |= _set_text_metric(item, "current_phase", "alliances_from_history")
            changed |= _set_metric(item, "history_alliances", kv.get("alliances"))
        if "STEP 4 unknown alliances=" in line:
            changed |= _set_text_metric(item, "current_phase", "unknown_alliances")
            changed |= _set_metric(item, "unknown_alliances", kv.get("alliances"))
        if "PROGRESS stage=" in line:
            stage_match = re.search(r"PROGRESS stage=([A-Za-z0-9_]+)", line)
            stage_name = stage_match.group(1) if stage_match else None
            processed = kv.get("processed")
            total = kv.get("total")
            if stage_name == "public_corporation_unknown":
                changed |= _set_text_metric(item, "current_phase", "unknown_corporation_profiles")
                changed |= _set_metric(item, "corporation_profiles_processed", processed)
                changed |= _set_metric(item, "corporation_profiles_total", total)
            elif stage_name == "corporation_history_unknown":
                changed |= _set_text_metric(item, "current_phase", "unknown_corporation_histories")
                changed |= _set_metric(item, "corporation_histories_processed", processed)
                changed |= _set_metric(item, "corporation_histories_total", total)
            elif stage_name == "public_alliance_unknown":
                changed |= _set_text_metric(item, "current_phase", "unknown_alliance_profiles")
                changed |= _set_metric(item, "alliance_profiles_processed", processed)
                changed |= _set_metric(item, "alliance_profiles_total", total)
        if "DONE run_id=" in line and "history_corps=" in line:
            changed |= _set_metric(item, "history_corporations", kv.get("history_corps"))
            changed |= _set_metric(item, "unknown_corporations", kv.get("unknown_corps"))
            changed |= _set_metric(item, "new_corporations", kv.get("corp_public_loaded"))
            changed |= _set_metric(item, "deleted_corporations", kv.get("corp_public_deleted"))
            changed |= _set_metric(item, "corporation_histories_refreshed", kv.get("corp_history_loaded"))
            changed |= _set_metric(item, "history_alliances", kv.get("history_alliances"))
            changed |= _set_metric(item, "unknown_alliances", kv.get("unknown_alliances"))
            changed |= _set_metric(item, "new_alliances", kv.get("alliance_loaded"))
            changed |= _set_metric(item, "deleted_alliances", kv.get("alliance_deleted"))
            failures = _sum_values([
                kv.get("corp_public_failed"),
                kv.get("corp_history_failed"),
                kv.get("alliance_failed"),
            ])
            changed |= _set_metric(item, "api_failures", failures)

    return changed


def step(step_id, label, group, command=None):
    return {
        "id": step_id,
        "label": label,
        "group": group,
        "status": "waiting",
        "started_at": None,
        "finished_at": None,
        "duration_seconds": None,
        "return_code": None,
        "command": [str(part) for part in command] if command else [],
        "error": None,
        "metrics": {},
    }


def write_state(state):
    state["updated_at"] = iso_now()
    state["running"] = state.get("pipeline_status") == "running"
    atomic_write_json(STATUS_PATH, state)


def archive_state(state):
    run_id = state.get("run_id")
    if not run_id:
        return
    atomic_write_json(HISTORY_DIR / f"{run_id}.json", state)


def log_line(handle, step_id, message):
    line = f"{iso_now()} [{step_id}] {message.rstrip()}"
    handle.write(line + "\n")
    handle.flush()
    print(line, flush=True)


def mark_blocked(state, from_index):
    for item in state["steps"][from_index:]:
        if item["status"] == "waiting":
            item["status"] = "blocked"
            item["error"] = "previous_step_failed"


def run_step(state, index, log_handle):
    item = state["steps"][index]
    command = item.get("command") or []
    if not command:
        raise PipelineError(f"step_command_missing:{item['id']}")

    item["status"] = "running"
    item["started_at"] = iso_now()
    state["current_step"] = item["id"]
    write_state(state)

    log_line(log_handle, item["id"], f"START {item['label']}")
    log_line(log_handle, item["id"], "CMD " + " ".join(command))

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    process = subprocess.Popen(
        command,
        cwd=str(BASE_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=env,
    )

    last_metric_write = 0.0
    if process.stdout is not None:
        for raw_line in process.stdout:
            child_line = raw_line.rstrip("\n")
            log_line(log_handle, item["id"], child_line)
            if parse_step_metrics(item, child_line):
                refresh_run_summary(state)
                monotonic_now = time.monotonic()
                if monotonic_now - last_metric_write >= 1.0:
                    write_state(state)
                    last_metric_write = monotonic_now

    refresh_run_summary(state)
    write_state(state)
    rc = process.wait()
    item["return_code"] = int(rc)
    item["finished_at"] = iso_now()
    item["duration_seconds"] = duration_seconds(item["started_at"], item["finished_at"])
    state["current_step"] = None

    if rc == 0:
        item["status"] = "done"
        log_line(log_handle, item["id"], f"DONE rc=0 duration={item['duration_seconds']}s")
        write_state(state)
        return True

    item["status"] = "failed"
    item["error"] = f"command_failed_rc_{rc}"
    state["pipeline_status"] = "failed"
    state["failed_step"] = item["id"]
    state["last_error"] = item["error"]
    mark_blocked(state, index + 1)
    log_line(log_handle, item["id"], f"FAILED rc={rc} duration={item['duration_seconds']}s")
    write_state(state)
    return False


def superintel_commands(target_day):
    if str(WEB_DIR) not in sys.path:
        sys.path.insert(0, str(WEB_DIR))

    from app.superintel import (  # noqa: PLC0415
        _build_pipeline_commands,
        _pipeline_dates,
        list_daily_status,
    )

    status = list_daily_status(today=target_day, entity_type="alliance")
    if status.get("ready"):
        return None, {
            "ready": True,
            "target_day": target_day.isoformat(),
            "rawkm_latest_day": status.get("rawkm_latest_day").isoformat() if status.get("rawkm_latest_day") else None,
            "events_latest_day": status.get("events_latest_day").isoformat() if status.get("events_latest_day") else None,
        }

    dates = _pipeline_dates(status, target_day)
    commands = _build_pipeline_commands(dates["sync_from"], dates["events_from"], dates["reports_to"])
    meta = {
        "ready": False,
        "target_day": target_day.isoformat(),
        "sync_from_date": dates["sync_from"].isoformat(),
        "events_from_date": dates["events_from"].isoformat(),
        "to_date": dates["reports_to"].isoformat(),
        "earliest_required_date": dates["earliest_required"].isoformat(),
        "rawkm_latest_day": dates["rawkm_latest_day"].isoformat() if dates["rawkm_latest_day"] else None,
        "events_latest_day": dates["events_latest_day"].isoformat() if dates["events_latest_day"] else None,
    }
    return commands, meta


def weekly_steps():
    py = str(PYTHON_BIN)
    return [
        step("sync_sde", "SDE sync", "sde", [py, str(SCRIPT_DIR / "sync_sde.py")]),
        step("superintel_sync_killmails", "SuperINTEL · sync killmails", "superintel"),
        step("superintel_build_events", "SuperINTEL · build events", "superintel"),
        step("superintel_refresh_affiliations", "SuperINTEL · refresh affiliations", "superintel"),
        step("superintel_build_report", "SuperINTEL · build daily report", "superintel"),
        step(
            "character_skill_inference",
            "Character skill inference",
            "skills",
            [py, str(SCRIPT_DIR / "infer_character_skills_from_killmails.py")],
        ),
        step(
            "weekly_pilot_affiliation_30y",
            "Affiliations · 30 years",
            "weekly",
            [
                py,
                str(SCRIPT_DIR / "run_recent_kill_pilots_affiliation_refresh.py"),
                "--days",
                str(WEEKLY_DAYS),
                "--workers",
                "4",
            ],
        ),
        step(
            "weekly_entities_backfill_audit",
            "Entities backfill",
            "weekly",
            [py, str(SCRIPT_DIR / "esi_entities_backfill_audit.py")],
        ),
        step(
            "weekly_pilotable_ships",
            "Pilotable ships refresh",
            "weekly",
            [
                py,
                str(SCRIPT_DIR / "build_pilotable_ships.py"),
                "--mode",
                "update",
                "--source-table",
                "entities.recent_killmail_pilots",
                "--source-column",
                "character_id",
            ],
        ),
        step(
            "population_alliances_daily",
            "Population · DOTLAN alliances daily",
            "population",
            [py, str(WEB_DIR / "app" / "population_alliances_daily.py")],
        ),
        step(
            "sovereignty_esi",
            "Sovereignty · ESI refresh",
            "sovereignty",
            [py, str(SCRIPT_DIR / "sync_sovereignty_esi.py")],
        ),
    ]


def validate_scripts(state):
    missing = []
    for item in state["steps"]:
        command = item.get("command") or []
        if len(command) >= 2 and command[1].endswith(".py") and not Path(command[1]).is_file():
            missing.append(command[1])
    if missing:
        raise PipelineError("missing_scripts:" + ",".join(sorted(set(missing))))


def run_pipeline(mode, trigger):
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    lock_handle = LOCK_PATH.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise PipelineError("pipeline_already_running") from exc

    lock_handle.seek(0)
    lock_handle.truncate()
    lock_handle.write(str(os.getpid()))
    lock_handle.flush()

    local_now = now_local()
    if mode not in {"scheduled", "weekly"}:
        raise PipelineError(f"invalid_mode:{mode}")

    target_day = local_now.date() - timedelta(days=1)

    run_id = f"{local_now.strftime('%Y%m%d_%H%M%S')}_{mode}"
    log_path = LOG_DIR / f"{run_id}.log"
    steps = weekly_steps()

    state = {
        "run_id": run_id,
        "mode": mode,
        "trigger": trigger,
        "pid": os.getpid(),
        "timezone": "Europe/Paris",
        "scheduled_time": "15:30",
        "weekly_days": WEEKLY_DAYS,
        "target_day": target_day.isoformat(),
        "pipeline_status": "running",
        "running": True,
        "started_at": iso_now(),
        "finished_at": None,
        "duration_seconds": None,
        "current_step": None,
        "failed_step": None,
        "last_error": None,
        "superintel": {},
        "summary": {},
        "log_path": str(log_path),
        "steps": steps,
    }
    validate_scripts(state)
    write_state(state)

    with log_path.open("a", encoding="utf-8", buffering=1) as log_handle:
        log_line(log_handle, "pipeline", f"START run_id={run_id} mode={mode} trigger={trigger} pid={os.getpid()}")
        log_line(log_handle, "pipeline", f"weekly=True target_day={state['target_day']}")

        try:
            # Step 1: SDE first. Only calculate SuperINTEL's dynamic date range afterwards.
            if not run_step(state, 0, log_handle):
                raise PipelineError("step_failed:sync_sde")

            commands, super_meta = superintel_commands(target_day)
            state["superintel"] = super_meta

            super_indexes = [1, 2, 3, 4]
            if commands is None:
                for idx in super_indexes:
                    state["steps"][idx]["status"] = "skipped"
                    state["steps"][idx]["error"] = "already_ready"
                log_line(log_handle, "superintel", "SKIP SuperINTEL pipeline: target already ready")
                write_state(state)
            else:
                for idx, command in zip(super_indexes, commands):
                    state["steps"][idx]["command"] = [str(part) for part in command]
                write_state(state)
                for idx in super_indexes:
                    if not run_step(state, idx, log_handle):
                        raise PipelineError(f"step_failed:{state['steps'][idx]['id']}")

            # Remaining unified weekly pipeline steps.
            for idx in range(5, len(state["steps"])):
                if not run_step(state, idx, log_handle):
                    raise PipelineError(f"step_failed:{state['steps'][idx]['id']}")

            state["pipeline_status"] = "done"
            state["finished_at"] = iso_now()
            state["duration_seconds"] = duration_seconds(state["started_at"], state["finished_at"])
            state["current_step"] = None
            state["running"] = False
            write_state(state)
            archive_state(state)
            log_line(log_handle, "pipeline", f"DONE duration={state['duration_seconds']}s")
            return 0

        except Exception as exc:
            if state.get("pipeline_status") != "failed":
                state["pipeline_status"] = "failed"
                state["last_error"] = str(exc)
                current = state.get("current_step")
                if current:
                    state["failed_step"] = current
                blocked_from = 0
                for idx, item in enumerate(state["steps"]):
                    if item["status"] == "running":
                        item["status"] = "failed"
                        item["finished_at"] = iso_now()
                        item["duration_seconds"] = duration_seconds(item["started_at"], item["finished_at"])
                        item["error"] = str(exc)
                        blocked_from = idx + 1
                        break
                    if item["status"] in {"done", "skipped"}:
                        blocked_from = idx + 1
                mark_blocked(state, blocked_from)
            state["finished_at"] = iso_now()
            state["duration_seconds"] = duration_seconds(state["started_at"], state["finished_at"])
            state["current_step"] = None
            state["running"] = False
            write_state(state)
            archive_state(state)
            log_line(log_handle, "pipeline", f"FAILED error={exc}")
            return 1
        finally:
            lock_handle.seek(0)
            lock_handle.truncate()
            lock_handle.flush()
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()


def main():
    parser = argparse.ArgumentParser(description="EVEOSINT scheduled update orchestrator.")
    parser.add_argument("--mode", choices=["scheduled", "weekly"], default="scheduled")
    parser.add_argument("--trigger", default="manual")
    parser.add_argument("--print-plan-json", action="store_true")
    args = parser.parse_args()

    if args.print_plan_json:
        print(json.dumps({"steps": weekly_steps()}, ensure_ascii=False))
        return

    if not PYTHON_BIN.is_file():
        raise SystemExit(f"missing_python:{PYTHON_BIN}")

    try:
        rc = run_pipeline(args.mode, args.trigger)
    except PipelineError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        raise SystemExit(75 if str(exc) == "pipeline_already_running" else 1) from exc

    raise SystemExit(rc)


if __name__ == "__main__":
    main()
