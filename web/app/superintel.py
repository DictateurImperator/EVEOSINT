import calendar
import io
import json
import os
import re
import shlex
import subprocess
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .db import db


REPORT_SCHEMA = "report_super"
RAWKM_SCHEMA = "rawkm"
RAWKM_IMPORT_DAYS_TABLE = "killmail_import_days"
RAWKM_KILLMAILS_TABLE = "killmails"
RAWKM_ATTACKERS_TABLE = "killmail_attackers"
SUPER_SCHEMA = "superintel"
SUPER_BUILD_STATE_TABLE = "build_state"
SUPER_PIPELINE_NAME = "build_superintel_events"
DAILY_TABLE_TEMPLATES = {
    "alliance": "report_daily_alliance_{date_key}",
    "corporation": "report_daily_corporation_{date_key}",
    "player": "report_daily_character_{date_key}",
}
MONTHLY_CHARACTER_TABLE_RE = re.compile(r"^report_mensuel_character_([0-9]{4}_[0-9]{2}_[0-9]{2})$")
MONTHLY_TABLE_TEMPLATES = {
    "pilot": "report_mensuel_character_{date_key}",
    "corp": "report_mensuel_corporation_{date_key}",
    "alliance": "report_mensuel_alliance_{date_key}",
}
MONTHLY_ANALYSIS_VIEWS = {
    "pilot": "Pilot",
    "corp": "Corporation",
    "alliance": "Alliance",
    "explain": "Explain",
}
MONTHLY_ANALYSIS_REASONS = {
    "all": "All reasons",
    "organic_change": "Organic change",
    "alliance_leave": "Alliance leave",
    "alliance_entrance": "Alliance entrance",
    "new_super_pilot": "New super pilot",
}
MONTHLY_SHIP_COLUMNS = [
    "erebus",
    "revenant",
    "leviathan",
    "avatar",
    "hel",
    "ragnarok",
    "nyx",
    "wyvern",
    "aeon",
    "vendetta",
    "vanquisher",
    "molok",
    "komodo",
    "azariel",
]

DETAILED_SHIP_COLUMNS = [
    {"key": "erebus", "label": "Erebus", "type_id": 671, "group": "Titan"},
    {"key": "revenant", "label": "Revenant", "type_id": 3514, "group": "Supercarrier"},
    {"key": "leviathan", "label": "Leviathan", "type_id": 3764, "group": "Titan"},
    {"key": "avatar", "label": "Avatar", "type_id": 11567, "group": "Titan"},
    {"key": "hel", "label": "Hel", "type_id": 22852, "group": "Supercarrier"},
    {"key": "ragnarok", "label": "Ragnarok", "type_id": 23773, "group": "Titan"},
    {"key": "nyx", "label": "Nyx", "type_id": 23913, "group": "Supercarrier"},
    {"key": "wyvern", "label": "Wyvern", "type_id": 23917, "group": "Supercarrier"},
    {"key": "aeon", "label": "Aeon", "type_id": 23919, "group": "Supercarrier"},
    {"key": "vendetta", "label": "Vendetta", "type_id": 42125, "group": "Supercarrier"},
    {"key": "vanquisher", "label": "Vanquisher", "type_id": 42126, "group": "Titan"},
    {"key": "molok", "label": "Molok", "type_id": 42241, "group": "Titan"},
    {"key": "komodo", "label": "Komodo", "type_id": 45649, "group": "Titan"},
    {"key": "azariel", "label": "Azariel", "type_id": 78576, "group": "Titan"},
]
DETAILED_SHIP_KEYS = {item["key"] for item in DETAILED_SHIP_COLUMNS}
DETAILED_SHIP_BY_TYPE_ID = {item["type_id"]: item for item in DETAILED_SHIP_COLUMNS}
DETAILED_RANKING_SORT_VALUES = {"score", "titans", "supers"} | DETAILED_SHIP_KEYS
DETAILED_MOVEMENT_PERIODS = {"7d", "1m"}
DETAILED_ANALYSIS_VIEWS = {
    "player": "Pilot",
    "corporation": "Corporation",
    "alliance": "Alliance",
    "explain": "Explain",
}
DETAILED_MOVEMENT_STATUSES = {
    "all": "All movements",
    "no_change": "NO CHANGE",
    "alliance_leave": "ALLIANCE LEAVE",
    "alliance_entrance": "ALLIANCE ENTRANCE",
    "new_super_pilot": "NEW SUPER PILOT",
}
DETAILED_ASSET_STATUSES = {
    "all": "All assets",
    "created": "CREATED",
    "killed": "KILLED",
    "unknown": "UNKNOWN",
    "deleted": "DELETED",
}
ENTITY_CONFIG = {
    "alliance": {
        "label": "Alliance",
        "label_plural": "Alliances",
        "id_column": "alliance_id",
        "name_column": "alliance_name",
    },
    "corporation": {
        "label": "Corporation",
        "label_plural": "Corporations",
        "id_column": "corporation_id",
        "name_column": "corporation_name",
    },
    "player": {
        "label": "Player",
        "label_plural": "Players",
        "id_column": "character_id",
        "name_column": "character_name",
    },
}
TITAN_DB_COLUMN = "total_titan"
SUPER_DB_COLUMN = "total_super"
SCORE_TITAN_POINTS = 3
SCORE_SUPER_POINTS = 1
RANKING_SORT_VALUES = {"score", "titans", "supers"}

SCRIPT_DIR = Path.home() / "eveosint" / "scripts"
SYNC_KILLMAILS_SCRIPT = SCRIPT_DIR / "sync_killmails.py"
BUILD_SUPERINTEL_EVENTS_SCRIPT = SCRIPT_DIR / "build_superintel_events.py"
ESI_AFFILIATION_REFRESH_SCRIPT = SCRIPT_DIR / "esi_affiliation_refresh.py"
BUILD_SUPER_REPORTS_SCRIPT = SCRIPT_DIR / "build_monthly_super_reports.py"
KILLMAIL_WORKERS = 4
REPORT_AVAILABLE_HOUR_LOCAL = 15
REPORT_AVAILABLE_MINUTE_LOCAL = 30
REPORT_TIMEZONE = ZoneInfo("Europe/Paris")

RUN_DIR = Path.home() / "eveosint" / "data" / "run"
STATUS_PATH = RUN_DIR / "superintel_pipeline.json"
PID_PATH = RUN_DIR / "superintel_pipeline.pid"
RUN_SCRIPT_PATH = RUN_DIR / "superintel_pipeline.sh"
LOG_PATH = Path.home() / "eveosint" / "data" / "logs" / "superintel_pipeline.log"

EXCLUDED_ALLIANCE_NAMES = {
    "",
    "NO_ALLIANCE",
    "NONE",
    "UNKNOWN",
}


class SuperIntelError(RuntimeError):
    pass


def _is_displayable_alliance(alliance_id, alliance_name):
    normalized_name = (alliance_name or "").strip().upper()

    if alliance_id is None:
        return False

    try:
        if int(alliance_id) == 0:
            return False
    except (TypeError, ValueError):
        pass

    if normalized_name in EXCLUDED_ALLIANCE_NAMES:
        return False

    return True


def _safe_identifier(value):
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", value):
        raise SuperIntelError(f"unsafe_identifier:{value}")
    return value


def _qualified_table_name(table_name):
    return f"{_safe_identifier(REPORT_SCHEMA)}.{_safe_identifier(table_name)}"


def _date_key(day):
    return day.strftime("%Y_%m_%d")


def normalize_entity_type(entity_type):
    if entity_type not in ENTITY_CONFIG:
        return "alliance"
    return entity_type


def normalize_ranking_sort(ranking_sort):
    if ranking_sort not in RANKING_SORT_VALUES:
        return "score"
    return ranking_sort


def normalize_detailed_ranking_sort(ranking_sort):
    if ranking_sort not in DETAILED_RANKING_SORT_VALUES:
        return "score"
    return ranking_sort


def normalize_detailed_movement_period(period):
    if period not in DETAILED_MOVEMENT_PERIODS:
        return "7d"
    return period


def normalize_detailed_analysis_view(view, fallback="alliance"):
    if view in DETAILED_ANALYSIS_VIEWS:
        return view
    return fallback if fallback in ENTITY_CONFIG else "alliance"


def normalize_detailed_movement_status(status):
    if status not in DETAILED_MOVEMENT_STATUSES:
        return "all"
    return status


def normalize_detailed_asset_status(status):
    if status not in DETAILED_ASSET_STATUSES:
        return "all"
    return status


def daily_table_name(day, entity_type="alliance"):
    entity_type = normalize_entity_type(entity_type)
    return DAILY_TABLE_TEMPLATES[entity_type].format(date_key=_date_key(day))


def same_day_previous_month(day):
    year = day.year
    month = day.month - 1

    if month == 0:
        year -= 1
        month = 12

    last_day = calendar.monthrange(year, month)[1]
    target_day = min(day.day, last_day)

    return date(year, month, target_day)


def current_report_day(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local_now = now.astimezone(REPORT_TIMEZONE)
    if (local_now.hour, local_now.minute) < (REPORT_AVAILABLE_HOUR_LOCAL, REPORT_AVAILABLE_MINUTE_LOCAL):
        return local_now.date() - timedelta(days=2)
    return local_now.date() - timedelta(days=1)


def required_days(today=None):
    today = today or current_report_day()
    start = same_day_previous_month(today)
    days = []
    current = start

    while current <= today:
        days.append(current)
        current += timedelta(days=1)

    return days


def _table_exists(conn, schema_name, table_name):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = %s
                  AND table_name = %s
            )
            """,
            (schema_name, table_name),
        )
        return bool(cur.fetchone()[0])


def _report_table_exists(conn, table_name):
    return _table_exists(conn, REPORT_SCHEMA, table_name)


def list_daily_status(today=None, entity_type="alliance"):
    today = today or current_report_day()
    entity_type = normalize_entity_type(entity_type)
    days = required_days(today=today)

    with db() as conn:
        existing = []
        missing = []

        for day in days:
            table_name = daily_table_name(day, entity_type)
            if _report_table_exists(conn, table_name):
                existing.append(day)
            else:
                missing.append(day)

        rawkm_latest_day = _latest_rawkm_import_day(conn)
        events_latest_day = _latest_events_day(conn)

    total = len(days)
    done = len(existing)
    percent = int(round((done / total) * 100)) if total else 100

    reports_ready = done == total
    rawkm_ready = rawkm_latest_day is not None and rawkm_latest_day >= today
    events_ready = events_latest_day is not None and events_latest_day >= today
    ready = reports_ready and rawkm_ready and events_ready

    reasons = []
    if not reports_ready:
        reasons.append("reports_missing")
    if not rawkm_ready:
        reasons.append("rawkm_not_current")
    if not events_ready:
        reasons.append("events_not_current")

    return {
        "days": days,
        "existing": existing,
        "missing": missing,
        "total": total,
        "done": done,
        "percent": percent,
        "reports_ready": reports_ready,
        "rawkm_ready": rawkm_ready,
        "events_ready": events_ready,
        "rawkm_latest_day": rawkm_latest_day,
        "events_latest_day": events_latest_day,
        "reasons": reasons,
        "ready": ready,
        "entity_type": entity_type,
    }


def latest_complete_daily_day(entity_type="alliance", not_after=None):
    """Return the newest daily snapshot usable by the dashboards.

    SuperINTEL pages are read-only views now: they must keep showing the last
    complete snapshot while the scheduled pipeline rebuilds newer data.
    """
    entity_type = normalize_entity_type(entity_type)
    not_after = not_after or current_report_day()
    template = DAILY_TABLE_TEMPLATES[entity_type]
    prefix = template.split("{date_key}", 1)[0]
    pattern = re.compile(rf"^{re.escape(prefix)}(\d{{4}}_\d{{2}}_\d{{2}})$")

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = %s
                  AND table_name LIKE %s
                """,
                (REPORT_SCHEMA, prefix + "%"),
            )
            table_names = [row[0] for row in cur.fetchall()]

    available = set()
    for table_name in table_names:
        match = pattern.fullmatch(table_name)
        if not match:
            continue
        try:
            day = datetime.strptime(match.group(1), "%Y_%m_%d").date()
        except ValueError:
            continue
        if day <= not_after:
            available.add(day)

    for candidate in sorted(available, reverse=True):
        if candidate - timedelta(days=7) not in available:
            continue
        if same_day_previous_month(candidate) not in available:
            continue
        return candidate

    return None

def _is_pid_running(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False

    return True


def _read_pid():
    if not PID_PATH.exists():
        return None

    raw_value = PID_PATH.read_text(encoding="utf-8").strip()
    if not raw_value:
        PID_PATH.unlink(missing_ok=True)
        return None

    try:
        return int(raw_value)
    except ValueError as exc:
        raise SuperIntelError("superintel_pipeline_pid_invalid") from exc


def cleanup_stale_runtime():
    pid = _read_pid()
    if pid is None:
        return False, None

    if _is_pid_running(pid):
        return True, pid

    PID_PATH.unlink(missing_ok=True)
    return False, None


def read_runtime_status():
    running, pid = cleanup_stale_runtime()
    status = {
        "running": running,
        "pid": pid,
        "started_at": None,
        "sync_from_date": None,
        "events_from_date": None,
        "to_date": None,
        "current_step": None,
        "pipeline_status": None,
        "finished_steps": [],
        "failed_step": None,
        "last_error": None,
        "pipeline": [],
    }

    if not STATUS_PATH.exists():
        return status

    with STATUS_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    if not isinstance(data, dict):
        raise SuperIntelError("superintel_pipeline_status_invalid")

    status.update({
        "started_at": data.get("started_at"),
        "sync_from_date": data.get("sync_from_date"),
        "events_from_date": data.get("events_from_date"),
        "to_date": data.get("to_date"),
        "current_step": data.get("current_step"),
        "pipeline_status": data.get("pipeline_status"),
        "finished_steps": data.get("finished_steps", []),
        "failed_step": data.get("failed_step"),
        "last_error": data.get("last_error"),
        "pipeline": data.get("pipeline", []),
    })
    return status


def _latest_rawkm_import_day(conn):
    if _table_exists(conn, RAWKM_SCHEMA, RAWKM_IMPORT_DAYS_TABLE):
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT MAX(day)
                FROM {RAWKM_SCHEMA}.{RAWKM_IMPORT_DAYS_TABLE}
                WHERE status = 'success'
                """
            )
            value = cur.fetchone()[0]
            if value is not None:
                return value

    if _table_exists(conn, RAWKM_SCHEMA, RAWKM_KILLMAILS_TABLE):
        with conn.cursor() as cur:
            cur.execute(f"SELECT MAX(killmail_time)::date FROM {RAWKM_SCHEMA}.{RAWKM_KILLMAILS_TABLE}")
            value = cur.fetchone()[0]
            if value is not None:
                return value

    return None


def _latest_events_day(conn):
    if not _table_exists(conn, SUPER_SCHEMA, SUPER_BUILD_STATE_TABLE):
        return None

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT MAX(last_killmail_time)::date
            FROM {SUPER_SCHEMA}.{SUPER_BUILD_STATE_TABLE}
            WHERE pipeline_name = %s
            """,
            (SUPER_PIPELINE_NAME,),
        )
        value = cur.fetchone()[0]
        if value is not None:
            return value

    return None


def _earliest_required_day(status):
    if status["missing"]:
        return min(status["missing"])
    return min(status["days"])


def _pipeline_dates(status, today):
    earliest_required = _earliest_required_day(status)
    rawkm_day = status.get("rawkm_latest_day")
    events_day = status.get("events_latest_day")

    candidates = [earliest_required]
    if rawkm_day is not None:
        candidates.append(rawkm_day)
    if events_day is not None:
        candidates.append(events_day)

    start_day = min(candidates)

    return {
        "sync_from": start_day,
        "events_from": start_day,
        "reports_to": today,
        "earliest_required": earliest_required,
        "rawkm_latest_day": rawkm_day,
        "events_latest_day": events_day,
    }

def _validate_scripts_exist():
    scripts = [
        SYNC_KILLMAILS_SCRIPT,
        BUILD_SUPERINTEL_EVENTS_SCRIPT,
        ESI_AFFILIATION_REFRESH_SCRIPT,
        BUILD_SUPER_REPORTS_SCRIPT,
    ]

    for script in scripts:
        if not script.is_file():
            raise SuperIntelError(f"superintel_script_missing:{script}")


def _command_to_shell(command):
    return " ".join(shlex.quote(str(part)) for part in command)


def _build_pipeline_commands(sync_from, events_from, to_date):
    python_bin = sys.executable

    return [
        [
            python_bin,
            str(SYNC_KILLMAILS_SCRIPT),
            "--from",
            sync_from.isoformat(),
            "--to",
            to_date.isoformat(),
            "--workers",
            str(KILLMAIL_WORKERS),
        ],
        [
            python_bin,
            str(BUILD_SUPERINTEL_EVENTS_SCRIPT),
            "--rebuild",
        ],
        [
            python_bin,
            str(ESI_AFFILIATION_REFRESH_SCRIPT),
            "--source-table",
            "superintel.super_pilots",
            "--source-column",
            "character_id",
            "--workers",
            "4",
        ],
        [
            python_bin,
            str(BUILD_SUPER_REPORTS_SCRIPT),
            "--schema",
            REPORT_SCHEMA,
            "--mode",
            "daily",
            "--date",
            to_date.isoformat(),
            "--replace",
        ],
    ]


def _status_update_command(updates):
    payload = json.dumps({"status_path": str(STATUS_PATH), "updates": updates}, ensure_ascii=False)
    code = (
        "import json;"
        "from pathlib import Path;"
        f"payload=json.loads({payload!r});"
        "p=Path(payload['status_path']);"
        "data={};"
        "\ntry:\n    data=json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}\nexcept Exception:\n    data={}"
        "\ndata.update(payload['updates']);"
        "p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')"
    )
    return [sys.executable, "-c", code]


def _append_shell_status(lines, updates):
    lines.append(_command_to_shell(_status_update_command(updates)))


def _write_pipeline_script(commands, metadata):
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f"mkdir -p {shlex.quote(str(LOG_PATH.parent))}",
        f"exec >> {shlex.quote(str(LOG_PATH))} 2>&1",
        "echo '[SuperINTEL] wrapper started at '$(date -Is)",
    ]

    _append_shell_status(lines, {
        **metadata,
        "pipeline_status": "running",
        "current_step": None,
        "finished_steps": [],
        "failed_step": None,
        "last_error": None,
    })

    total_steps = len(commands)
    finished_steps = []
    for index, step in enumerate(commands, start=1):
        label = step["label"]
        command = [str(part) for part in step["command"]]
        command_shell = _command_to_shell(command)

        lines.append(f"echo '[SuperINTEL] STEP {index}/{total_steps} START {label}'")
        lines.append(f"echo '[SuperINTEL] CMD {command_shell}'")
        _append_shell_status(lines, {
            "pipeline_status": "running",
            "current_step": label,
            "finished_steps": finished_steps[:],
            "failed_step": None,
            "last_error": None,
        })
        lines.append("set +e")
        lines.append(command_shell)
        lines.append("rc=$?")
        lines.append("set -e")
        lines.append("if [ \"$rc\" -ne 0 ]; then")
        lines.append(f"  echo '[SuperINTEL] STEP FAILED {label} rc='$rc")
        _append_shell_status(lines, {
            "pipeline_status": "failed",
            "current_step": None,
            "finished_steps": finished_steps[:],
            "failed_step": label,
            "last_error": f"command failed step={label}",
        })
        lines.append(f"  rm -f {shlex.quote(str(PID_PATH))}")
        lines.append("  exit $rc")
        lines.append("fi")
        finished_steps.append(label)
        lines.append(f"echo '[SuperINTEL] STEP DONE {label}'")
        _append_shell_status(lines, {
            "pipeline_status": "running",
            "current_step": None,
            "finished_steps": finished_steps[:],
            "failed_step": None,
            "last_error": None,
        })

    _append_shell_status(lines, {
        "pipeline_status": "done",
        "current_step": None,
        "finished_steps": finished_steps,
        "failed_step": None,
        "last_error": None,
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    lines.append(f"rm -f {shlex.quote(str(PID_PATH))}")
    lines.append("echo '[SuperINTEL] wrapper finished at '$(date -Is)")
    lines.append("")

    RUN_SCRIPT_PATH.write_text("\n".join(lines), encoding="utf-8")
    RUN_SCRIPT_PATH.chmod(0o750)


def start_daily_generation_if_needed(status, today=None):
    """Deprecated compatibility shim.

    SuperINTEL pages are read-only. Updates are owned exclusively by the
    independent update pipeline/systemd runner, never by a page request.
    """
    return False


def _list_columns(conn, table_name):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = %s
            ORDER BY ordinal_position
            """,
            (REPORT_SCHEMA, table_name),
        )
        return [row[0] for row in cur.fetchall()]


def _require_column(columns, expected_column):
    available = {column.lower(): column for column in columns}
    key = expected_column.lower()

    if key not in available:
        raise SuperIntelError(
            f"superintel_column_missing:{expected_column}; available=" + ",".join(columns)
        )

    return available[key]


def _score(total_titan, total_super):
    return (int(total_titan or 0) * SCORE_TITAN_POINTS) + (int(total_super or 0) * SCORE_SUPER_POINTS)


def _optional_column_sql(columns, expected_column, alias, default_sql="NULL"):
    available = {column.lower(): column for column in columns}
    real_column = available.get(expected_column.lower())

    if real_column:
        return f'"{real_column}" AS {alias}'

    return f"{default_sql} AS {alias}"


def _fetch_assets(conn, day, entity_type="alliance"):
    entity_type = normalize_entity_type(entity_type)
    config = ENTITY_CONFIG[entity_type]
    table_name = daily_table_name(day, entity_type)
    if not _report_table_exists(conn, table_name):
        raise SuperIntelError(f"superintel_table_missing:{REPORT_SCHEMA}.{table_name}")

    columns = _list_columns(conn, table_name)
    id_col = _require_column(columns, config["id_column"])
    name_col = _require_column(columns, config["name_column"])
    total_titan_col = _require_column(columns, TITAN_DB_COLUMN)
    total_super_col = _require_column(columns, SUPER_DB_COLUMN)

    optional_selects = [
        _optional_column_sql(columns, "corporation_id", "corporation_id"),
        _optional_column_sql(columns, "corporation_name", "corporation_name"),
        _optional_column_sql(columns, "corporation_ticker", "corporation_ticker"),
        _optional_column_sql(columns, "alliance_id", "alliance_id"),
        _optional_column_sql(columns, "alliance_name", "alliance_name"),
        _optional_column_sql(columns, "alliance_ticker", "alliance_ticker"),
    ]

    query = f"""
        SELECT
            "{id_col}" AS entity_id,
            "{name_col}" AS entity_name,
            COALESCE("{total_titan_col}", 0)::BIGINT AS total_titan,
            COALESCE("{total_super_col}", 0)::BIGINT AS total_super,
            {", ".join(optional_selects)}
        FROM {_qualified_table_name(table_name)}
        WHERE COALESCE("{total_titan_col}", 0) > 0
           OR COALESCE("{total_super_col}", 0) > 0
    """

    with conn.cursor() as cur:
        cur.execute(query)
        column_names = [desc[0] for desc in cur.description]
        rows = [dict(zip(column_names, row)) for row in cur.fetchall()]

    result = {}
    for row in rows:
        entity_id = row["entity_id"]
        entity_name = row["entity_name"] or "Unknown"

        if entity_type == "alliance" and not _is_displayable_alliance(entity_id, entity_name):
            continue

        if entity_id is None:
            continue

        try:
            if int(entity_id) == 0:
                continue
        except (TypeError, ValueError):
            pass

        total_titan = int(row["total_titan"] or 0)
        total_super = int(row["total_super"] or 0)
        score = _score(total_titan, total_super)
        key = str(entity_id)

        corporation_id = row.get("corporation_id")
        corporation_name = row.get("corporation_name")
        corporation_ticker = row.get("corporation_ticker")
        alliance_id = row.get("alliance_id")
        alliance_name = row.get("alliance_name")
        alliance_ticker = row.get("alliance_ticker")

        if entity_type == "alliance":
            alliance_id = entity_id
            alliance_name = entity_name
        elif entity_type == "corporation":
            corporation_id = entity_id
            corporation_name = entity_name
        elif entity_type == "player":
            pass

        result[key] = {
            "entity_id": entity_id,
            "entity_name": entity_name,
            "corporation_id": corporation_id,
            "corporation_name": corporation_name,
            "corporation_ticker": corporation_ticker,
            "alliance_id": alliance_id,
            "alliance_name": alliance_name,
            "alliance_ticker": alliance_ticker,
            "total_titan": total_titan,
            "total_super": total_super,
            "score": score,
        }

    return result

def _top_current(rows, limit=5):
    return sorted(
        rows.values(),
        key=lambda item: (-item["score"], -item["total_titan"], item["entity_name"]),
    )[:limit]


def _top_delta(current_rows, previous_rows, positive=True, limit=5):
    keys = set(current_rows) | set(previous_rows)
    deltas = []

    for key in keys:
        current = current_rows.get(key, {})
        previous = previous_rows.get(key, {})
        current_score = int(current.get("score", 0) or 0)
        previous_score = int(previous.get("score", 0) or 0)
        delta = current_score - previous_score

        if positive and delta <= 0:
            continue
        if not positive and delta >= 0:
            continue

        source = current if current else previous
        current_titan = int(current.get("total_titan", 0) or 0)
        previous_titan = int(previous.get("total_titan", 0) or 0)
        current_super = int(current.get("total_super", 0) or 0)
        previous_super = int(previous.get("total_super", 0) or 0)

        deltas.append({
            "entity_id": source.get("entity_id"),
            "entity_name": source.get("entity_name", "Unknown"),
            "corporation_id": source.get("corporation_id"),
            "corporation_name": source.get("corporation_name"),
            "corporation_ticker": source.get("corporation_ticker"),
            "alliance_id": source.get("alliance_id"),
            "alliance_name": source.get("alliance_name"),
            "alliance_ticker": source.get("alliance_ticker"),
            "current_score": current_score,
            "previous_score": previous_score,
            "score_delta": delta,
            "current_titan": current_titan,
            "previous_titan": previous_titan,
            "titan_delta": current_titan - previous_titan,
            "current_super": current_super,
            "previous_super": previous_super,
            "super_delta": current_super - previous_super,
            "delta": delta,
        })

    if positive:
        return sorted(
            deltas,
            key=lambda item: (-item["delta"], item["entity_name"]),
        )[:limit]

    return sorted(
        deltas,
        key=lambda item: (item["delta"], item["entity_name"]),
    )[:limit]


def _monthly_score_ranking(current_rows, previous_rows, limit=10, ranking_sort="score"):
    rows = []

    for key, current in current_rows.items():
        previous = previous_rows.get(key, {})
        score = int(current.get("score", 0) or 0)
        previous_score = int(previous.get("score", 0) or 0)
        total_titan = int(current.get("total_titan", 0) or 0)
        previous_titan = int(previous.get("total_titan", 0) or 0)
        total_super = int(current.get("total_super", 0) or 0)
        previous_super = int(previous.get("total_super", 0) or 0)
        rows.append({
            "entity_id": current.get("entity_id"),
            "entity_name": current.get("entity_name", "Unknown"),
            "corporation_id": current.get("corporation_id"),
            "corporation_name": current.get("corporation_name"),
            "corporation_ticker": current.get("corporation_ticker"),
            "alliance_id": current.get("alliance_id"),
            "alliance_name": current.get("alliance_name"),
            "alliance_ticker": current.get("alliance_ticker"),
            "score": score,
            "score_delta": score - previous_score,
            "total_titan": total_titan,
            "titan_delta": total_titan - previous_titan,
            "total_super": total_super,
            "super_delta": total_super - previous_super,
        })

    ranking_sort = normalize_ranking_sort(ranking_sort)

    if ranking_sort == "titans":
        sort_key = lambda item: (-item["total_titan"], -item["score"], item["entity_name"])
    elif ranking_sort == "supers":
        sort_key = lambda item: (-item["total_super"], -item["score"], item["entity_name"])
    else:
        sort_key = lambda item: (-item["score"], -item["total_titan"], item["entity_name"])

    sorted_rows = sorted(rows, key=sort_key)
    return sorted_rows if limit is None else sorted_rows[:limit]


def get_dashboard(today=None, entity_type="alliance", full_ranking=False, ranking_sort="score"):
    today = today or current_report_day()
    entity_type = normalize_entity_type(entity_type)
    ranking_sort = normalize_ranking_sort(ranking_sort)
    day_7 = today - timedelta(days=7)
    day_month = same_day_previous_month(today)

    with db() as conn:
        current_rows = _fetch_assets(conn, today, entity_type)
        rows_7 = _fetch_assets(conn, day_7, entity_type)
        rows_month = _fetch_assets(conn, day_month, entity_type)

    return {
        "today": today,
        "day_7": day_7,
        "day_month": day_month,
        "top_today": _top_current(current_rows, limit=5),
        "top_gain_7": _top_delta(current_rows, rows_7, positive=True, limit=5),
        "top_loss_7": _top_delta(current_rows, rows_7, positive=False, limit=5),
        "top_gain_month": _top_delta(current_rows, rows_month, positive=True, limit=5),
        "top_loss_month": _top_delta(current_rows, rows_month, positive=False, limit=5),
        "score_ranking_month": _monthly_score_ranking(current_rows, rows_month, limit=None if full_ranking else 10, ranking_sort=ranking_sort),
        "score_ranking_month_all": _monthly_score_ranking(current_rows, rows_month, limit=None, ranking_sort=ranking_sort),
        "score_titan_points": SCORE_TITAN_POINTS,
        "score_super_points": SCORE_SUPER_POINTS,
        "entity_type": entity_type,
        "entity_label": ENTITY_CONFIG[entity_type]["label"],
        "entity_label_plural": ENTITY_CONFIG[entity_type]["label_plural"],
        "full_ranking": full_ranking,
        "ranking_sort": ranking_sort,
    }




def _fetch_detailed_assets(conn, day, entity_type="alliance"):
    """Read one daily snapshot with the full hull breakdown.

    Kept separate from _fetch_assets so the existing dashboard query and output
    stay untouched.
    """
    entity_type = normalize_entity_type(entity_type)
    config = ENTITY_CONFIG[entity_type]
    table_name = daily_table_name(day, entity_type)
    if not _report_table_exists(conn, table_name):
        raise SuperIntelError(f"superintel_table_missing:{REPORT_SCHEMA}.{table_name}")

    columns = _list_columns(conn, table_name)
    id_col = _require_column(columns, config["id_column"])
    name_col = _require_column(columns, config["name_column"])
    total_titan_col = _require_column(columns, TITAN_DB_COLUMN)
    total_super_col = _require_column(columns, SUPER_DB_COLUMN)

    optional_selects = [
        _optional_column_sql(columns, "corporation_id", "corporation_id"),
        _optional_column_sql(columns, "corporation_name", "corporation_name"),
        _optional_column_sql(columns, "corporation_ticker", "corporation_ticker"),
        _optional_column_sql(columns, "alliance_id", "alliance_id"),
        _optional_column_sql(columns, "alliance_name", "alliance_name"),
        _optional_column_sql(columns, "alliance_ticker", "alliance_ticker"),
        _optional_column_sql(columns, "nb_characters", "nb_characters", "0"),
        _optional_column_sql(columns, "nb_corporations", "nb_corporations", "0"),
    ]
    ship_selects = [
        _optional_column_sql(columns, ship["key"], ship["key"], "0")
        for ship in DETAILED_SHIP_COLUMNS
    ]

    query = f"""
        SELECT
            \"{id_col}\" AS entity_id,
            \"{name_col}\" AS entity_name,
            COALESCE(\"{total_titan_col}\", 0)::BIGINT AS total_titan,
            COALESCE(\"{total_super_col}\", 0)::BIGINT AS total_super,
            {", ".join(optional_selects + ship_selects)}
        FROM {_qualified_table_name(table_name)}
        WHERE COALESCE(\"{total_titan_col}\", 0) > 0
           OR COALESCE(\"{total_super_col}\", 0) > 0
    """

    with conn.cursor() as cur:
        cur.execute(query)
        column_names = [desc[0] for desc in cur.description]
        rows = [dict(zip(column_names, row)) for row in cur.fetchall()]

    result = {}
    for row in rows:
        entity_id = row.get("entity_id")
        entity_name = row.get("entity_name") or "Unknown"

        if entity_type == "alliance" and not _is_displayable_alliance(entity_id, entity_name):
            continue
        if entity_id is None:
            continue
        try:
            if int(entity_id) == 0:
                continue
        except (TypeError, ValueError):
            pass

        corporation_id = row.get("corporation_id")
        corporation_name = row.get("corporation_name")
        corporation_ticker = row.get("corporation_ticker")
        alliance_id = row.get("alliance_id")
        alliance_name = row.get("alliance_name")
        alliance_ticker = row.get("alliance_ticker")

        if entity_type == "alliance":
            alliance_id = entity_id
            alliance_name = entity_name
        elif entity_type == "corporation":
            corporation_id = entity_id
            corporation_name = entity_name

        ship_counts = {
            ship["key"]: int(row.get(ship["key"], 0) or 0)
            for ship in DETAILED_SHIP_COLUMNS
        }
        total_titan = int(row.get("total_titan", 0) or 0)
        total_super = int(row.get("total_super", 0) or 0)

        result[str(entity_id)] = {
            "entity_id": entity_id,
            "entity_name": entity_name,
            "corporation_id": corporation_id,
            "corporation_name": corporation_name,
            "corporation_ticker": corporation_ticker,
            "alliance_id": alliance_id,
            "alliance_name": alliance_name,
            "alliance_ticker": alliance_ticker,
            "nb_characters": int(row.get("nb_characters", 0) or 0),
            "nb_corporations": int(row.get("nb_corporations", 0) or 0),
            "ship_counts": ship_counts,
            "total_titan": total_titan,
            "total_super": total_super,
            "score": _score(total_titan, total_super),
        }

    return result


def _detailed_source_fields(source):
    return {
        "entity_id": source.get("entity_id"),
        "entity_name": source.get("entity_name", "Unknown"),
        "corporation_id": source.get("corporation_id"),
        "corporation_name": source.get("corporation_name"),
        "corporation_ticker": source.get("corporation_ticker"),
        "alliance_id": source.get("alliance_id"),
        "alliance_name": source.get("alliance_name"),
        "alliance_ticker": source.get("alliance_ticker"),
        "nb_characters": int(source.get("nb_characters", 0) or 0),
        "nb_corporations": int(source.get("nb_corporations", 0) or 0),
    }


def _detailed_delta_row(current, previous):
    current = current or {}
    previous = previous or {}
    source = current if current else previous
    row = _detailed_source_fields(source)

    current_ship_counts = {
        ship["key"]: int((current.get("ship_counts") or {}).get(ship["key"], 0) or 0)
        for ship in DETAILED_SHIP_COLUMNS
    }
    previous_ship_counts = {
        ship["key"]: int((previous.get("ship_counts") or {}).get(ship["key"], 0) or 0)
        for ship in DETAILED_SHIP_COLUMNS
    }
    ship_deltas = {
        key: current_ship_counts[key] - previous_ship_counts[key]
        for key in current_ship_counts
    }

    total_titan = int(current.get("total_titan", 0) or 0)
    previous_titan = int(previous.get("total_titan", 0) or 0)
    total_super = int(current.get("total_super", 0) or 0)
    previous_super = int(previous.get("total_super", 0) or 0)
    score = _score(total_titan, total_super)
    previous_score = _score(previous_titan, previous_super)

    row.update({
        "ship_counts": current_ship_counts,
        "previous_ship_counts": previous_ship_counts,
        "ship_deltas": ship_deltas,
        "total_titan": total_titan,
        "previous_titan": previous_titan,
        "titan_delta": total_titan - previous_titan,
        "total_super": total_super,
        "previous_super": previous_super,
        "super_delta": total_super - previous_super,
        "score": score,
        "previous_score": previous_score,
        "score_delta": score - previous_score,
    })
    return row


def _top_current_detailed(rows, limit=5):
    return sorted(
        rows.values(),
        key=lambda item: (-item["score"], -item["total_titan"], item["entity_name"]),
    )[:limit]


def _ship_top_flop(current_rows, previous_rows, limit=5):
    keys = set(current_rows) | set(previous_rows)
    result = []

    for ship in DETAILED_SHIP_COLUMNS:
        ship_key = ship["key"]
        movements = []
        for key in keys:
            current = current_rows.get(key, {})
            previous = previous_rows.get(key, {})
            source = current if current else previous
            current_value = int((current.get("ship_counts") or {}).get(ship_key, 0) or 0)
            previous_value = int((previous.get("ship_counts") or {}).get(ship_key, 0) or 0)
            delta = current_value - previous_value
            if delta == 0:
                continue

            movement = _detailed_source_fields(source)
            movement.update({
                "current": current_value,
                "previous": previous_value,
                "delta": delta,
            })
            movements.append(movement)

        gains = sorted(
            (item for item in movements if item["delta"] > 0),
            key=lambda item: (-item["delta"], -item["current"], item["entity_name"]),
        )[:limit]
        losses = sorted(
            (item for item in movements if item["delta"] < 0),
            key=lambda item: (item["delta"], item["current"], item["entity_name"]),
        )[:limit]

        result.append({
            **ship,
            "gains": gains,
            "losses": losses,
        })

    return result


def _detailed_row_matches_query(row, query):
    needle = (query or "").strip().lower()
    if not needle:
        return True
    values = [
        row.get("entity_id"),
        row.get("entity_name"),
        row.get("corporation_id"),
        row.get("corporation_name"),
        row.get("corporation_ticker"),
        row.get("alliance_id"),
        row.get("alliance_name"),
        row.get("alliance_ticker"),
    ]
    return any(needle in str(value or "").lower() for value in values)


def _detailed_global_ranking(current_rows, previous_rows, ranking_sort="score", q="", page=1, page_size=50):
    ranking_sort = normalize_detailed_ranking_sort(ranking_sort)
    try:
        page = max(1, int(page or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = max(10, min(200, int(page_size or 50)))
    except (TypeError, ValueError):
        page_size = 50

    rows = [
        _detailed_delta_row(current, previous_rows.get(key, {}))
        for key, current in current_rows.items()
    ]
    rows = [row for row in rows if _detailed_row_matches_query(row, q)]

    if ranking_sort in DETAILED_SHIP_KEYS:
        sort_key = lambda item: (
            -int(item["ship_counts"].get(ranking_sort, 0) or 0),
            -item["score"],
            item["entity_name"],
        )
    elif ranking_sort == "titans":
        sort_key = lambda item: (-item["total_titan"], -item["score"], item["entity_name"])
    elif ranking_sort == "supers":
        sort_key = lambda item: (-item["total_super"], -item["score"], item["entity_name"])
    else:
        sort_key = lambda item: (-item["score"], -item["total_titan"], item["entity_name"])

    rows = sorted(rows, key=sort_key)
    total_rows = len(rows)
    total_pages = max(1, (total_rows + page_size - 1) // page_size)
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * page_size

    return {
        "items": rows[offset:offset + page_size],
        "total_rows": total_rows,
        "total_pages": total_pages,
        "page": page,
        "page_size": page_size,
        "has_previous": page > 1,
        "has_next": page < total_pages,
        "ranking_sort": ranking_sort,
        "query": (q or "").strip(),
    }


def _detailed_paginate(rows, page=1, page_size=50):
    try:
        page = max(1, int(page or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = max(10, min(200, int(page_size or 50)))
    except (TypeError, ValueError):
        page_size = 50

    total_rows = len(rows)
    total_pages = max(1, (total_rows + page_size - 1) // page_size)
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * page_size

    return {
        "items": rows[offset:offset + page_size],
        "total_rows": total_rows,
        "total_pages": total_pages,
        "page": page,
        "page_size": page_size,
        "has_previous": page > 1,
        "has_next": page < total_pages,
    }


def _detailed_movement_score(ship_deltas):
    score = 0
    for ship in DETAILED_SHIP_COLUMNS:
        points = SCORE_TITAN_POINTS if ship["group"] == "Titan" else SCORE_SUPER_POINTS
        score += abs(int(ship_deltas.get(ship["key"], 0) or 0)) * points
    return score


def _build_detailed_analysis_rows(current_rows, previous_rows, q=""):
    rows = []
    for key in set(current_rows) | set(previous_rows):
        row = _detailed_delta_row(current_rows.get(key, {}), previous_rows.get(key, {}))
        row["movement_score"] = _detailed_movement_score(row["ship_deltas"])
        if _detailed_row_matches_query(row, q):
            rows.append(row)

    return sorted(
        rows,
        key=lambda item: (
            -item["movement_score"],
            -item["score"],
            item["entity_name"],
        ),
    )


def _detailed_ship_diffs(current, previous):
    current_counts = current.get("ship_counts") or {}
    previous_counts = previous.get("ship_counts") or {}
    return {
        ship["key"]: int(current_counts.get(ship["key"], 0) or 0)
        - int(previous_counts.get(ship["key"], 0) or 0)
        for ship in DETAILED_SHIP_COLUMNS
    }


def _detailed_ship_stock(row, sign=1):
    counts = row.get("ship_counts") or {}
    return {
        ship["key"]: sign * int(counts.get(ship["key"], 0) or 0)
        for ship in DETAILED_SHIP_COLUMNS
    }


def _detailed_any_nonzero(values):
    return any(int(value or 0) != 0 for value in values.values())


def _detailed_explain_row(
    current,
    previous,
    movement_status,
    asset_status,
    analysed_alliance_id,
    analysed_alliance_name,
    analysed_alliance_ticker,
    ship_deltas,
):
    source = current or previous or {}
    total_titan_delta = sum(
        int(ship_deltas.get(ship["key"], 0) or 0)
        for ship in DETAILED_SHIP_COLUMNS
        if ship["group"] == "Titan"
    )
    total_super_delta = sum(
        int(ship_deltas.get(ship["key"], 0) or 0)
        for ship in DETAILED_SHIP_COLUMNS
        if ship["group"] != "Titan"
    )

    return {
        "entity_id": source.get("entity_id"),
        "entity_name": source.get("entity_name", "Unknown"),
        "corporation_id": (current or {}).get("corporation_id") or (previous or {}).get("corporation_id"),
        "corporation_name": (current or {}).get("corporation_name") or (previous or {}).get("corporation_name"),
        "corporation_ticker": (current or {}).get("corporation_ticker") or (previous or {}).get("corporation_ticker"),
        "previous_alliance_id": (previous or {}).get("alliance_id"),
        "previous_alliance_name": (previous or {}).get("alliance_name"),
        "previous_alliance_ticker": (previous or {}).get("alliance_ticker"),
        "current_alliance_id": (current or {}).get("alliance_id"),
        "current_alliance_name": (current or {}).get("alliance_name"),
        "current_alliance_ticker": (current or {}).get("alliance_ticker"),
        "analysed_alliance_id": analysed_alliance_id,
        "analysed_alliance_name": analysed_alliance_name,
        "analysed_alliance_ticker": analysed_alliance_ticker,
        "movement_status": movement_status,
        "asset_status": asset_status,
        "ship_deltas": ship_deltas,
        "total_titan_delta": total_titan_delta,
        "total_super_delta": total_super_delta,
        "score_delta": total_titan_delta * SCORE_TITAN_POINTS + total_super_delta * SCORE_SUPER_POINTS,
        "movement_score": _detailed_movement_score(ship_deltas),
    }


def _fetch_deleted_character_ids(conn, character_ids):
    ids = [int(character_id) for character_id in character_ids if character_id is not None]
    if not ids:
        return set()

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT character_id
            FROM entities.characters
            WHERE character_id = ANY(%s)
              AND COALESCE(is_deleted, FALSE) = TRUE
            """,
            (ids,),
        )
        return {int(row[0]) for row in cur.fetchall()}


def _fetch_detailed_character_super_kills(conn, character_ids, from_day, to_day):
    ids = [int(character_id) for character_id in character_ids if character_id is not None]
    if not ids:
        return {}

    ship_type_ids = [ship["type_id"] for ship in DETAILED_SHIP_COLUMNS]
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                killmail_id,
                victim_character_id,
                victim_ship_type_id,
                killmail_time
            FROM {RAWKM_SCHEMA}.{RAWKM_KILLMAILS_TABLE}
            WHERE victim_character_id = ANY(%s)
              AND victim_ship_type_id = ANY(%s)
              AND killmail_time >= %s::date
              AND killmail_time < (%s::date + INTERVAL '1 day')
            ORDER BY victim_character_id, killmail_time, killmail_id
            """,
            (ids, ship_type_ids, from_day, to_day),
        )
        rows = cur.fetchall()

    result = {}
    for killmail_id, character_id, ship_type_id, killmail_time in rows:
        result.setdefault(int(character_id), []).append({
            "killmail_id": int(killmail_id),
            "ship_type_id": int(ship_type_id),
            "killmail_time": killmail_time,
        })
    return result


def _fetch_detailed_character_super_attack_kills(conn, character_ids, from_day, to_day):
    ids = [int(character_id) for character_id in character_ids if character_id is not None]
    if not ids:
        return {}

    ship_type_ids = [ship["type_id"] for ship in DETAILED_SHIP_COLUMNS]
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                killmail_id,
                character_id,
                ship_type_id,
                killmail_time
            FROM {RAWKM_SCHEMA}.{RAWKM_ATTACKERS_TABLE}
            WHERE character_id = ANY(%s)
              AND ship_type_id = ANY(%s)
              AND killmail_time >= %s::date
              AND killmail_time < (%s::date + INTERVAL '1 day')
            ORDER BY character_id, killmail_time, killmail_id
            """,
            (ids, ship_type_ids, from_day, to_day),
        )
        rows = cur.fetchall()

    result = {}
    for killmail_id, character_id, ship_type_id, killmail_time in rows:
        result.setdefault(int(character_id), []).append({
            "killmail_id": int(killmail_id),
            "ship_type_id": int(ship_type_id),
            "killmail_time": killmail_time,
        })
    return result


def _detailed_explain_matches_query(row, query):
    """Explain search is a strict filter on Analysed alliance only."""
    needle = (query or "").strip().lower()
    if not needle:
        return True

    values = [
        row.get("analysed_alliance_id"),
        row.get("analysed_alliance_name"),
        row.get("analysed_alliance_ticker"),
    ]
    return any(needle in str(value or "").lower() for value in values)


def _detailed_snapshot_alliance_matches_query(row, query):
    needle = (query or "").strip().lower()
    if not needle:
        return True
    row = row or {}
    values = [
        row.get("alliance_id"),
        row.get("alliance_name"),
        row.get("alliance_ticker"),
    ]
    return any(needle in str(value or "").lower() for value in values)


def _detailed_match_asset_events(ship_deltas, events, sign):
    """Match observed killmails to requested hull deltas.

    sign=+1: attacker evidence -> CREATED
    sign=-1: victim evidence   -> KILLED
    """
    requested = {}
    for ship in DETAILED_SHIP_COLUMNS:
        key = ship["key"]
        delta = int(ship_deltas.get(key, 0) or 0)
        requested[key] = max(sign * delta, 0)

    matched_counts = {ship["key"]: 0 for ship in DETAILED_SHIP_COLUMNS}
    remaining_counts = dict(requested)

    for event in events or []:
        ship = DETAILED_SHIP_BY_TYPE_ID.get(event.get("ship_type_id"))
        if ship is None:
            continue
        key = ship["key"]
        if remaining_counts.get(key, 0) <= 0:
            continue
        matched_counts[key] += 1
        remaining_counts[key] -= 1

    matched = {key: sign * count for key, count in matched_counts.items()}
    remaining = {key: sign * count for key, count in remaining_counts.items()}
    return matched, remaining


def _detailed_add_explain_row(
    rows,
    current,
    previous,
    movement_status,
    asset_status,
    analysed_source,
    ship_deltas,
):
    if not _detailed_any_nonzero(ship_deltas):
        return

    analysed_source = analysed_source or {}
    rows.append(_detailed_explain_row(
        current,
        previous,
        movement_status,
        asset_status,
        analysed_source.get("alliance_id"),
        analysed_source.get("alliance_name"),
        analysed_source.get("alliance_ticker"),
        ship_deltas,
    ))


def _build_detailed_explain_rows(
    conn,
    current_rows,
    previous_rows,
    from_day,
    to_day,
    analysed_alliance_query="",
):
    rows = []
    keys = set(current_rows) | set(previous_rows)

    # Le filtre "Analysed alliance" est appliqué avant les recherches rawkm :
    # une ligne Explain ne peut analyser que l'alliance précédente ou courante.
    if (analysed_alliance_query or "").strip():
        keys = {
            key
            for key in keys
            if _detailed_snapshot_alliance_matches_query(
                current_rows.get(key),
                analysed_alliance_query,
            )
            or _detailed_snapshot_alliance_matches_query(
                previous_rows.get(key),
                analysed_alliance_query,
            )
        }

    # IMPORTANT PERF:
    # on ne cherche des killmails que pour les pilotes dont le nombre de hulls
    # a réellement augmenté ou diminué. Les simples transferts d'alliance
    # n'ont pas besoin d'interroger rawkm.
    deleted_candidate_ids = set()
    victim_candidate_ids = set()
    attacker_candidate_ids = set()

    for key in keys:
        current = current_rows.get(key)
        previous = previous_rows.get(key)
        source = current or previous or {}
        character_id = source.get("entity_id")
        if character_id is None:
            continue
        character_id = int(character_id)

        if previous is None and current is not None:
            if _detailed_any_nonzero(_detailed_ship_stock(current, sign=1)):
                attacker_candidate_ids.add(character_id)
            continue

        if current is None and previous is not None:
            if _detailed_any_nonzero(_detailed_ship_stock(previous, sign=-1)):
                deleted_candidate_ids.add(character_id)
                victim_candidate_ids.add(character_id)
            continue

        if current is None or previous is None:
            continue

        diffs = _detailed_ship_diffs(current, previous)
        if any(int(value or 0) > 0 for value in diffs.values()):
            attacker_candidate_ids.add(character_id)
        if any(int(value or 0) < 0 for value in diffs.values()):
            victim_candidate_ids.add(character_id)

    deleted_ids = _fetch_deleted_character_ids(
        conn,
        deleted_candidate_ids,
    )
    kills_by_character = _fetch_detailed_character_super_kills(
        conn,
        victim_candidate_ids,
        from_day,
        to_day,
    )
    attack_kills_by_character = _fetch_detailed_character_super_attack_kills(
        conn,
        attacker_candidate_ids,
        from_day,
        to_day,
    )

    zero = {ship["key"]: 0 for ship in DETAILED_SHIP_COLUMNS}

    for key in keys:
        current = current_rows.get(key)
        previous = previous_rows.get(key)
        source = current or previous or {}
        character_id = source.get("entity_id")
        victim_events = kills_by_character.get(int(character_id), []) if character_id is not None else []
        attacker_events = attack_kills_by_character.get(int(character_id), []) if character_id is not None else []

        if previous is None and current is not None:
            stock = _detailed_ship_stock(current, sign=1)
            if not _detailed_any_nonzero(stock):
                continue

            created, unknown = _detailed_match_asset_events(stock, attacker_events, +1)

            _detailed_add_explain_row(
                rows, current, None,
                "new_super_pilot", "created",
                current, created,
            )
            _detailed_add_explain_row(
                rows, current, None,
                "new_super_pilot", "unknown",
                current, unknown,
            )
            continue

        if current is None and previous is not None:
            previous_stock = _detailed_ship_stock(previous, sign=-1)
            if not _detailed_any_nonzero(previous_stock):
                continue

            if character_id is not None and int(character_id) in deleted_ids:
                _detailed_add_explain_row(
                    rows, None, previous,
                    "no_change", "deleted",
                    previous, previous_stock,
                )
                continue

            killed, unknown = _detailed_match_asset_events(previous_stock, victim_events, -1)

            _detailed_add_explain_row(
                rows, None, previous,
                "no_change", "killed",
                previous, killed,
            )
            _detailed_add_explain_row(
                rows, None, previous,
                "no_change", "unknown",
                previous, unknown,
            )
            continue

        if current is None or previous is None:
            continue

        diffs = _detailed_ship_diffs(current, previous)
        same_alliance = previous.get("alliance_id") == current.get("alliance_id")

        if same_alliance:
            if not _detailed_any_nonzero(diffs):
                continue

            created, unknown_positive = _detailed_match_asset_events(diffs, attacker_events, +1)
            killed, unknown_negative = _detailed_match_asset_events(diffs, victim_events, -1)
            unknown = {
                ship["key"]: int(unknown_positive.get(ship["key"], 0) or 0)
                + int(unknown_negative.get(ship["key"], 0) or 0)
                for ship in DETAILED_SHIP_COLUMNS
            }

            _detailed_add_explain_row(
                rows, current, previous,
                "no_change", "created",
                current, created,
            )
            _detailed_add_explain_row(
                rows, current, previous,
                "no_change", "killed",
                current, killed,
            )
            _detailed_add_explain_row(
                rows, current, previous,
                "no_change", "unknown",
                current, unknown,
            )
            continue

        previous_counts = previous.get("ship_counts") or {}
        current_counts = current.get("ship_counts") or {}

        leave_unknown = dict(zero)
        entrance_unknown = dict(zero)
        net_diffs = {}

        for ship in DETAILED_SHIP_COLUMNS:
            ship_key = ship["key"]
            before = int(previous_counts.get(ship_key, 0) or 0)
            now = int(current_counts.get(ship_key, 0) or 0)
            shared = min(before, now)
            leave_unknown[ship_key] = -shared
            entrance_unknown[ship_key] = shared
            net_diffs[ship_key] = now - before

        created, unknown_positive = _detailed_match_asset_events(net_diffs, attacker_events, +1)
        killed, unknown_negative = _detailed_match_asset_events(net_diffs, victim_events, -1)

        for ship in DETAILED_SHIP_COLUMNS:
            ship_key = ship["key"]
            entrance_unknown[ship_key] += int(unknown_positive.get(ship_key, 0) or 0)
            leave_unknown[ship_key] += int(unknown_negative.get(ship_key, 0) or 0)

        _detailed_add_explain_row(
            rows, current, previous,
            "alliance_leave", "unknown",
            previous, leave_unknown,
        )
        _detailed_add_explain_row(
            rows, current, previous,
            "alliance_leave", "killed",
            previous, killed,
        )
        _detailed_add_explain_row(
            rows, current, previous,
            "alliance_entrance", "unknown",
            current, entrance_unknown,
        )
        _detailed_add_explain_row(
            rows, current, previous,
            "alliance_entrance", "created",
            current, created,
        )

    return sorted(
        rows,
        key=lambda item: (
            -item["movement_score"],
            item["entity_name"],
            item["movement_status"],
            item["asset_status"],
        ),
    )



def get_detailed_dashboard(
    today=None,
    entity_type="alliance",
    movement_period="7d",
    q="",
    ranking_sort="score",
    page=1,
    page_size=50,
    analysis_view=None,
    analysis_movement="all",
    analysis_asset="all",
    analysis_q="",
    analysis_page=1,
    analysis_page_size=50,
):
    """Detailed companion to the existing SuperINTEL dashboard.

    The existing dashboard is intentionally not modified. This page reads the
    same daily snapshots but exposes every Titan/Supercarrier hull.
    """
    today = today or current_report_day()
    entity_type = normalize_entity_type(entity_type)
    movement_period = normalize_detailed_movement_period(movement_period)
    ranking_sort = normalize_detailed_ranking_sort(ranking_sort)
    analysis_view = normalize_detailed_analysis_view(analysis_view, fallback=entity_type)
    analysis_movement = normalize_detailed_movement_status(analysis_movement)
    analysis_asset = normalize_detailed_asset_status(analysis_asset)
    analysis_q = (analysis_q or "").strip()
    day_7 = today - timedelta(days=7)
    day_month = same_day_previous_month(today)
    movement_from = day_7 if movement_period == "7d" else day_month

    with db() as conn:
        snapshot_cache = {}

        def snapshot(snapshot_entity_type, snapshot_day):
            cache_key = (snapshot_entity_type, snapshot_day)
            if cache_key not in snapshot_cache:
                snapshot_cache[cache_key] = _fetch_detailed_assets(
                    conn,
                    snapshot_day,
                    snapshot_entity_type,
                )
            return snapshot_cache[cache_key]

        current_rows = snapshot(entity_type, today)
        rows_7 = snapshot(entity_type, day_7)
        rows_month = snapshot(entity_type, day_month)

        analysis_entity_type = "player" if analysis_view == "explain" else analysis_view
        analysis_current_rows = snapshot(analysis_entity_type, today)
        analysis_previous_rows = snapshot(analysis_entity_type, movement_from)

        if analysis_view == "explain":
            analysis_rows = _build_detailed_explain_rows(
                conn,
                analysis_current_rows,
                analysis_previous_rows,
                movement_from,
                today,
                analysed_alliance_query=analysis_q,
            )
            if analysis_movement != "all":
                analysis_rows = [
                    row for row in analysis_rows
                    if row["movement_status"] == analysis_movement
                ]
            if analysis_asset != "all":
                analysis_rows = [
                    row for row in analysis_rows
                    if row["asset_status"] == analysis_asset
                ]
            analysis_rows = [
                row for row in analysis_rows
                if _detailed_explain_matches_query(row, analysis_q)
            ]
        else:
            analysis_rows = _build_detailed_analysis_rows(
                analysis_current_rows,
                analysis_previous_rows,
                q=analysis_q,
            )

        analysis_pagination = _detailed_paginate(
            analysis_rows,
            page=analysis_page,
            page_size=analysis_page_size,
        )

    movement_previous = rows_7 if movement_period == "7d" else rows_month

    return {
        "today": today,
        "day_7": day_7,
        "day_month": day_month,
        "movement_period": movement_period,
        "movement_from": movement_from,
        "movement_label": "7 days" if movement_period == "7d" else "1 month",
        "top_today": _top_current_detailed(current_rows, limit=5),
        "ship_top_flop": _ship_top_flop(current_rows, movement_previous, limit=5),
        "global_ranking": _detailed_global_ranking(
            current_rows,
            rows_month,
            ranking_sort=ranking_sort,
            q=q,
            page=page,
            page_size=page_size,
        ),
        "ship_columns": DETAILED_SHIP_COLUMNS,
        "score_titan_points": SCORE_TITAN_POINTS,
        "score_super_points": SCORE_SUPER_POINTS,
        "entity_type": entity_type,
        "entity_label": ENTITY_CONFIG[entity_type]["label"],
        "entity_label_plural": ENTITY_CONFIG[entity_type]["label_plural"],
        "analysis": {
            "view": analysis_view,
            "view_labels": DETAILED_ANALYSIS_VIEWS,
            "entity_type": analysis_entity_type,
            "entity_label": ENTITY_CONFIG[analysis_entity_type]["label"],
            "entity_label_plural": ENTITY_CONFIG[analysis_entity_type]["label_plural"],
            "movement_status": analysis_movement,
            "movement_status_labels": DETAILED_MOVEMENT_STATUSES,
            "asset_status": analysis_asset,
            "asset_status_labels": DETAILED_ASSET_STATUSES,
            "query": analysis_q,
            "from_day": movement_from,
            "to_day": today,
            "period": movement_period,
            "period_label": "7 days" if movement_period == "7d" else "1 month",
            "rows": analysis_pagination["items"],
            "pagination": analysis_pagination,
        },
    }


def normalize_profile_entity_type(entity_type):
    if entity_type in ("character", "player"):
        return "character"
    if entity_type == "corporation":
        return "corporation"
    if entity_type == "alliance":
        return "alliance"
    raise SuperIntelError("entity_profile_type_invalid")


def profile_url(entity_type, entity_id):
    entity_type = normalize_profile_entity_type(entity_type)
    return f"/{entity_type}/{int(entity_id)}"


def search_entities(query, limit=8):
    term = (query or "").strip()
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 8
    limit = max(1, min(limit, 12))

    if len(term) < 2:
        return []

    term_lower = term.lower()
    prefix_lower = f"{term_lower}%"
    numeric_id = int(term) if term.isdigit() else None
    rows = []

    with db() as conn:
        if numeric_id is not None:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 'character' AS entity_type, character_id AS entity_id,
                           COALESCE(NULLIF(name, ''), 'Unknown') AS name,
                           NULL::text AS ticker,
                           0 AS rank_score, 0 AS type_rank
                    FROM entities.characters
                    WHERE character_id = %s AND COALESCE(is_deleted, FALSE) = FALSE
                    UNION ALL
                    SELECT 'corporation' AS entity_type, corporation_id AS entity_id,
                           COALESCE(NULLIF(name, ''), 'Unknown') AS name,
                           ticker,
                           0 AS rank_score, 1 AS type_rank
                    FROM entities.corporations
                    WHERE corporation_id = %s AND COALESCE(is_deleted, FALSE) = FALSE
                    UNION ALL
                    SELECT 'alliance' AS entity_type, alliance_id AS entity_id,
                           COALESCE(NULLIF(name, ''), 'Unknown') AS name,
                           ticker,
                           0 AS rank_score, 2 AS type_rank
                    FROM entities.alliances
                    WHERE alliance_id = %s AND COALESCE(is_deleted, FALSE) = FALSE
                    LIMIT %s
                    """,
                    (numeric_id, numeric_id, numeric_id, limit),
                )
                rows.extend(cur.fetchall())

        with conn.cursor() as cur:
            cur.execute(
                """
                WITH candidates AS (
                    (
                        SELECT 'character' AS entity_type, character_id AS entity_id,
                               COALESCE(NULLIF(name, ''), 'Unknown') AS name,
                               NULL::text AS ticker,
                               CASE WHEN lower(name) = %s THEN 0 ELSE 2 END AS rank_score,
                               0 AS type_rank
                        FROM entities.characters
                        WHERE COALESCE(is_deleted, FALSE) = FALSE
                          AND name IS NOT NULL
                          AND lower(name) LIKE %s
                        ORDER BY rank_score ASC, lower(name) ASC
                        LIMIT %s
                    )
                    UNION ALL
                    (
                        SELECT 'corporation' AS entity_type, corporation_id AS entity_id,
                               COALESCE(NULLIF(name, ''), 'Unknown') AS name,
                               ticker,
                               CASE
                                   WHEN lower(name) = %s THEN 0
                                   WHEN lower(ticker) = %s THEN 0
                                   WHEN ticker IS NOT NULL AND lower(ticker) LIKE %s THEN 1
                                   ELSE 2
                               END AS rank_score,
                               1 AS type_rank
                        FROM entities.corporations
                        WHERE COALESCE(is_deleted, FALSE) = FALSE
                          AND (
                              (name IS NOT NULL AND lower(name) LIKE %s)
                              OR (ticker IS NOT NULL AND lower(ticker) LIKE %s)
                          )
                        ORDER BY rank_score ASC, lower(name) ASC
                        LIMIT %s
                    )
                    UNION ALL
                    (
                        SELECT 'alliance' AS entity_type, alliance_id AS entity_id,
                               COALESCE(NULLIF(name, ''), 'Unknown') AS name,
                               ticker,
                               CASE
                                   WHEN lower(name) = %s THEN 0
                                   WHEN lower(ticker) = %s THEN 0
                                   WHEN ticker IS NOT NULL AND lower(ticker) LIKE %s THEN 1
                                   ELSE 2
                               END AS rank_score,
                               2 AS type_rank
                        FROM entities.alliances
                        WHERE COALESCE(is_deleted, FALSE) = FALSE
                          AND (
                              (name IS NOT NULL AND lower(name) LIKE %s)
                              OR (ticker IS NOT NULL AND lower(ticker) LIKE %s)
                          )
                        ORDER BY rank_score ASC, lower(name) ASC
                        LIMIT %s
                    )
                )
                SELECT entity_type, entity_id, name, ticker, rank_score, type_rank
                FROM candidates
                ORDER BY rank_score ASC, type_rank ASC, lower(name) ASC
                LIMIT %s
                """,
                (
                    term_lower, prefix_lower, limit,
                    term_lower, term_lower, prefix_lower, prefix_lower, prefix_lower, limit,
                    term_lower, term_lower, prefix_lower, prefix_lower, prefix_lower, limit,
                    limit,
                ),
            )
            rows.extend(cur.fetchall())

    deduped = {}
    for entity_type, entity_id, name, ticker, rank_score, type_rank in rows:
        entity_id = int(entity_id)
        key = (entity_type, entity_id)
        old = deduped.get(key)
        item = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "name": name or "Unknown",
            "ticker": ticker or None,
            "rank_score": int(rank_score),
            "type_rank": int(type_rank),
        }
        if old is None or (item["rank_score"], item["type_rank"], item["name"].lower()) < (old["rank_score"], old["type_rank"], old["name"].lower()):
            deduped[key] = item

    results = list(deduped.values())
    results.sort(key=lambda item: (item["rank_score"], item["type_rank"], item["name"].lower()))

    output = []
    for item in results[:limit]:
        entity_type = item["entity_type"]
        entity_id = item["entity_id"]
        ticker = item["ticker"]
        name = item["name"]

        if entity_type == "character":
            url = profile_url("character", entity_id)
            image_url = f"https://images.evetech.net/characters/{entity_id}/portrait?size=32"
            subtitle = "Character"
            label = name
        elif entity_type == "corporation":
            url = profile_url("corporation", entity_id)
            image_url = f"https://images.evetech.net/corporations/{entity_id}/logo?size=32"
            subtitle = "Corporation"
            label = f"{name} [{ticker}]" if ticker else name
        else:
            url = profile_url("alliance", entity_id)
            image_url = f"https://images.evetech.net/alliances/{entity_id}/logo?size=32"
            subtitle = "Alliance"
            label = f"{name} [{ticker}]" if ticker else name

        output.append({
            "entity_type": entity_type,
            "entity_id": entity_id,
            "name": name,
            "ticker": ticker,
            "label": label,
            "subtitle": subtitle,
            "url": url,
            "image_url": image_url,
        })

    return output

def _entity_table_lookup(conn, entity_type, entity_id):
    if entity_type == "alliance":
        schema = "entities"
        table = "alliances"
        id_column = "alliance_id"
        name_column = "name"
        subtitle = "Alliance profile"
    elif entity_type == "corporation":
        schema = "entities"
        table = "corporations"
        id_column = "corporation_id"
        name_column = "name"
        subtitle = "Corporation profile"
    else:
        schema = "entities"
        table = "characters"
        id_column = "character_id"
        name_column = "name"
        subtitle = "Character profile"

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = %s
                  AND table_name = %s
            )
            """,
            (schema, table),
        )
        if not bool(cur.fetchone()[0]):
            return None, subtitle

        cur.execute(
            f'SELECT "{name_column}" FROM "{schema}"."{table}" WHERE "{id_column}" = %s LIMIT 1',
            (entity_id,),
        )
        row = cur.fetchone()

    if not row:
        return None, subtitle

    return row[0], subtitle


def _entity_report_lookup(conn, entity_type, entity_id):
    if entity_type == "character":
        report_type = "player"
    else:
        report_type = entity_type

    report_day = current_report_day()
    table_name = daily_table_name(report_day, report_type)
    if not _report_table_exists(conn, table_name):
        return None

    config = ENTITY_CONFIG[report_type]
    columns = _list_columns(conn, table_name)
    id_col = _require_column(columns, config["id_column"])
    name_col = _require_column(columns, config["name_column"])

    with conn.cursor() as cur:
        cur.execute(
            f'SELECT "{name_col}" FROM {_qualified_table_name(table_name)} WHERE "{id_col}" = %s LIMIT 1',
            (entity_id,),
        )
        row = cur.fetchone()

    return row[0] if row else None


def _table_exists_quiet(conn, schema, table):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema=%s AND table_name=%s
            )
            """,
            (schema, table),
        )
        return bool(cur.fetchone()[0])


def _current_corporation_profile(conn, corporation_id):
    if not _table_exists_quiet(conn, "entities", "corporation_current_affiliation"):
        return None

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                cca.alliance_id,
                a.name AS alliance_name,
                a.ticker AS alliance_ticker
            FROM entities.corporation_current_affiliation cca
            LEFT JOIN entities.alliances a
              ON a.alliance_id = cca.alliance_id
            WHERE cca.corporation_id = %s
            LIMIT 1
            """,
            (corporation_id,),
        )
        row = cur.fetchone()

    if not row or row[0] is None:
        return None

    return {
        "entity_type": "alliance",
        "entity_id": row[0],
        "name": row[1] or "Unknown",
        "ticker": row[2],
        "url": profile_url("alliance", row[0]),
    }


def _current_character_profile(conn, character_id):
    if not _table_exists_quiet(conn, "entities", "character_current_affiliation"):
        return None, None

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                cca.corporation_id,
                c.name AS corporation_name,
                c.ticker AS corporation_ticker,
                COALESCE(cca.alliance_id, corp_aff.alliance_id) AS alliance_id,
                a.name AS alliance_name,
                a.ticker AS alliance_ticker
            FROM entities.character_current_affiliation cca
            LEFT JOIN entities.corporations c
              ON c.corporation_id = cca.corporation_id
            LEFT JOIN entities.corporation_current_affiliation corp_aff
              ON corp_aff.corporation_id = cca.corporation_id
            LEFT JOIN entities.alliances a
              ON a.alliance_id = COALESCE(cca.alliance_id, corp_aff.alliance_id)
            WHERE cca.character_id = %s
            LIMIT 1
            """,
            (character_id,),
        )
        row = cur.fetchone()

    if not row:
        return None, None

    corporation = None
    if row[0] is not None:
        corporation = {
            "entity_type": "corporation",
            "entity_id": row[0],
            "name": row[1] or "Unknown",
            "ticker": row[2],
            "url": profile_url("corporation", row[0]),
        }

    alliance = None
    if row[3] is not None:
        alliance = {
            "entity_type": "alliance",
            "entity_id": row[3],
            "name": row[4] or "Unknown",
            "ticker": row[5],
            "url": profile_url("alliance", row[3]),
        }

    return corporation, alliance


def get_entity_profile(entity_type, entity_id):
    entity_type = normalize_profile_entity_type(entity_type)

    try:
        normalized_id = int(entity_id)
    except (TypeError, ValueError) as exc:
        raise SuperIntelError("entity_profile_id_invalid") from exc

    current_corporation = None
    current_alliance = None

    with db() as conn:
        name = _entity_report_lookup(conn, entity_type, normalized_id)
        fallback_name, subtitle = _entity_table_lookup(conn, entity_type, normalized_id)

        if entity_type == "character":
            current_corporation, current_alliance = _current_character_profile(conn, normalized_id)
        elif entity_type == "corporation":
            current_alliance = _current_corporation_profile(conn, normalized_id)

    if not name:
        name = fallback_name
    if not name:
        name = f"Unknown {entity_type.title()}"

    return {
        "entity_type": entity_type,
        "entity_id": normalized_id,
        "name": name,
        "subtitle": subtitle,
        "current_corporation": current_corporation,
        "current_alliance": current_alliance,
    }


def monthly_table_name(day, view="pilot"):
    view = normalize_monthly_view(view)
    if view == "explain":
        view = "pilot"
    return MONTHLY_TABLE_TEMPLATES[view].format(date_key=_date_key(day))


def monthly_character_table_name(day):
    return monthly_table_name(day, "pilot")


def _parse_monthly_suffix(value):
    return datetime.strptime(value, "%Y_%m_%d").date()


def normalize_monthly_reason(reason):
    if reason not in MONTHLY_ANALYSIS_REASONS:
        return "all"
    return reason


def normalize_monthly_view(view):
    if view not in MONTHLY_ANALYSIS_VIEWS:
        return "pilot"
    return view


def list_monthly_analysis_periods():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = %s
                  AND table_name LIKE 'report_mensuel_character_%%'
                ORDER BY table_name
                """,
                (REPORT_SCHEMA,),
            )
            table_names = [row[0] for row in cur.fetchall()]

    available = set()
    for table_name in table_names:
        match = MONTHLY_CHARACTER_TABLE_RE.match(table_name)
        if match:
            available.add(_parse_monthly_suffix(match.group(1)))

    periods = []
    for day in sorted(available):
        previous_day = same_day_previous_month(day)
        if previous_day in available:
            periods.append(day)

    return periods


def _monthly_selectable_columns(conn, table_name, wanted_columns):
    columns = _list_columns(conn, table_name)
    available = {column.lower(): column for column in columns}
    result = []

    for output_name, expected_name, default_value in wanted_columns:
        real_column = available.get(expected_name.lower())
        if real_column:
            result.append((output_name, f'"{real_column}"', default_value))
        else:
            result.append((output_name, None, default_value))

    return result, columns


def _monthly_query_column_sql(output_name, real_column_sql, default_value):
    if real_column_sql:
        return f'{real_column_sql} AS "{output_name}"'
    if default_value is None:
        return f'NULL AS "{output_name}"'
    if isinstance(default_value, int):
        return f'{default_value} AS "{output_name}"'
    escaped = str(default_value).replace("'", "''")
    return f"'{escaped}' AS \"{output_name}\""


def _fetch_monthly_table_rows(conn, day, view):
    view = normalize_monthly_view(view)
    if view == "explain":
        view = "pilot"

    table_name = monthly_table_name(day, view)
    if not _report_table_exists(conn, table_name):
        raise SuperIntelError(f"superintel_table_missing:{REPORT_SCHEMA}.{table_name}")

    if view == "pilot":
        key_column = "pilot_id"
        wanted_columns = [
            ("jour", "jour", None),
            ("pilot_name", "character_name", "Unknown"),
            ("corp_name", "corporation_name", "UNKNOWN_CORP"),
            ("corp_ticker", "corporation_ticker", ""),
            ("alliance_name", "alliance_name", "NO_ALLIANCE"),
            ("alliance_ticker", "alliance_ticker", ""),
            ("pilot_id", "character_id", None),
            ("corp_id", "corporation_id", None),
            ("alliance_id", "alliance_id", None),
        ]
    elif view == "corp":
        key_column = "corp_id"
        wanted_columns = [
            ("jour", "jour", None),
            ("corp_name", "corporation_name", "UNKNOWN_CORP"),
            ("corp_ticker", "corporation_ticker", ""),
            ("corp_id", "corporation_id", None),
            ("nb_pilots", "nb_characters", 0),
        ]
    else:
        key_column = "alliance_id"
        wanted_columns = [
            ("jour", "jour", None),
            ("alliance_name", "alliance_name", "NO_ALLIANCE"),
            ("alliance_ticker", "alliance_ticker", ""),
            ("alliance_id", "alliance_id", None),
            ("nb_pilots", "nb_characters", 0),
            ("nb_corps", "nb_corporations", 0),
        ]

    for column in MONTHLY_SHIP_COLUMNS:
        wanted_columns.append((column, column, 0))
    wanted_columns.extend([
        ("total_super", "total_super", 0),
        ("total_titan", "total_titan", 0),
    ])

    selectable_columns, _ = _monthly_selectable_columns(conn, table_name, wanted_columns)
    select_sql = ",\n            ".join(
        _monthly_query_column_sql(output_name, real_column_sql, default_value)
        for output_name, real_column_sql, default_value in selectable_columns
    )

    query = f"""
        SELECT
            {select_sql}
        FROM {_qualified_table_name(table_name)}
    """

    with conn.cursor() as cur:
        cur.execute(query)
        column_names = [desc[0] for desc in cur.description]
        rows = [dict(zip(column_names, row)) for row in cur.fetchall()]

    result = {}
    for row in rows:
        key = row.get(key_column)
        if key is None:
            continue
        result[str(key)] = row

    return result


def _monthly_number(value):
    return int(value or 0)


def _monthly_text(value, default=""):
    return default if value is None else str(value)


def _monthly_metric_cell(current, previous):
    current_value = _monthly_number(current)
    previous_value = _monthly_number(previous)
    return {
        "value": current_value,
        "delta": current_value - previous_value,
    }


def _monthly_display_columns(view):
    view = normalize_monthly_view(view)

    if view == "pilot":
        base_columns = [
            {"key": "jour", "label": "jour", "type": "text"},
            {"key": "pilot_name", "label": "pilot_name", "type": "text"},
            {"key": "corp_name", "label": "corp_name", "type": "text"},
            {"key": "corp_ticker", "label": "corp_ticker", "type": "text"},
            {"key": "alliance_name", "label": "alliance_name", "type": "text"},
            {"key": "alliance_ticker", "label": "alliance_ticker", "type": "text"},
            {"key": "pilot_id", "label": "pilot_id", "type": "text"},
            {"key": "corp_id", "label": "corp_id", "type": "text"},
            {"key": "alliance_id", "label": "alliance_id", "type": "text"},
        ]
    elif view == "corp":
        base_columns = [
            {"key": "jour", "label": "jour", "type": "text"},
            {"key": "corp_name", "label": "corp_name", "type": "text"},
            {"key": "corp_ticker", "label": "corp_ticker", "type": "text"},
            {"key": "corp_id", "label": "corp_id", "type": "text"},
            {"key": "nb_pilots", "label": "nb_pilots", "type": "metric"},
        ]
    elif view == "alliance":
        base_columns = [
            {"key": "jour", "label": "jour", "type": "text"},
            {"key": "alliance_name", "label": "alliance_name", "type": "text"},
            {"key": "alliance_ticker", "label": "alliance_ticker", "type": "text"},
            {"key": "alliance_id", "label": "alliance_id", "type": "text"},
            {"key": "nb_pilots", "label": "nb_pilots", "type": "metric"},
            {"key": "nb_corps", "label": "nb_corps", "type": "metric"},
        ]
    else:
        return [
            {"key": "jour", "label": "jour", "type": "text"},
            {"key": "pilot_id", "label": "pilot_id", "type": "text"},
            {"key": "pilot_name", "label": "pilot_name", "type": "text"},
            {"key": "alliance_prev_id", "label": "alliance_prev_id", "type": "text"},
            {"key": "alliance_prev", "label": "alliance_prev", "type": "text"},
            {"key": "alliance_prev_ticker", "label": "alliance_prev_ticker", "type": "text"},
            {"key": "alliance_curr_id", "label": "alliance_curr_id", "type": "text"},
            {"key": "alliance_curr", "label": "alliance_curr", "type": "text"},
            {"key": "alliance_curr_ticker", "label": "alliance_curr_ticker", "type": "text"},
            {"key": "alliance_analysee_id", "label": "alliance_analysee_id", "type": "text"},
            {"key": "alliance_analysee", "label": "alliance_analysee", "type": "text"},
            {"key": "alliance_analysee_ticker", "label": "alliance_analysee_ticker", "type": "text"},
            {"key": "analyse_result", "label": "analyse_result", "type": "text"},
        ] + [
            {"key": f"{column}_diff", "label": f"{column}_diff", "type": "diff"}
            for column in MONTHLY_SHIP_COLUMNS + ["total_super", "total_titan"]
        ]

    return base_columns + [
        {"key": column, "label": column, "type": "metric"}
        for column in MONTHLY_SHIP_COLUMNS + ["total_super", "total_titan"]
    ]


def _build_monthly_diff_rows(current_rows, previous_rows, view, selected_day):
    view = normalize_monthly_view(view)
    columns = _monthly_display_columns(view)
    keys = set(current_rows) | set(previous_rows)
    rows = []

    for key in keys:
        current = current_rows.get(key, {})
        previous = previous_rows.get(key, {})
        source = current if current else previous
        values = {}
        sort_delta = 0
        sort_score = 0

        for column in columns:
            column_key = column["key"]
            if column["type"] == "metric":
                cell = _monthly_metric_cell(current.get(column_key, 0), previous.get(column_key, 0))
                values[column_key] = cell
                if column_key == "total_titan":
                    sort_delta += abs(cell["delta"]) * SCORE_TITAN_POINTS
                    sort_score += cell["value"] * SCORE_TITAN_POINTS
                elif column_key == "total_super":
                    sort_delta += abs(cell["delta"]) * SCORE_SUPER_POINTS
                    sort_score += cell["value"] * SCORE_SUPER_POINTS
            else:
                if column_key == "jour":
                    value = selected_day.isoformat()
                else:
                    value = source.get(column_key)
                    if hasattr(value, "isoformat"):
                        value = value.isoformat()
                values[column_key] = _monthly_text(value)

        rows.append({
            "values": values,
            "sort_delta": sort_delta,
            "sort_score": sort_score,
            "sort_name": _monthly_text(
                values.get("pilot_name")
                or values.get("corp_name")
                or values.get("alliance_name")
            ),
        })

    rows = [row for row in rows if row["sort_score"] != 0 or row["sort_delta"] != 0]
    return sorted(rows, key=lambda item: (-item["sort_delta"], -item["sort_score"], item["sort_name"]))


def _fetch_monthly_players(conn, day):
    return _fetch_monthly_table_rows(conn, day, "pilot")


def _monthly_analysis_row(source, reason, analysed_alliance_id, analysed_alliance_name, analysed_alliance_ticker, diffs):
    values = {
        "jour": source.get("jour"),
        "pilot_id": source.get("pilot_id"),
        "pilot_name": source.get("pilot_name", "Unknown"),
        "alliance_prev_id": source.get("previous_alliance_id"),
        "alliance_prev": source.get("previous_alliance_name", "NO_ALLIANCE"),
        "alliance_prev_ticker": source.get("previous_alliance_ticker", ""),
        "alliance_curr_id": source.get("current_alliance_id"),
        "alliance_curr": source.get("current_alliance_name", "NO_ALLIANCE"),
        "alliance_curr_ticker": source.get("current_alliance_ticker", ""),
        "alliance_analysee_id": analysed_alliance_id,
        "alliance_analysee": analysed_alliance_name or "NO_ALLIANCE",
        "alliance_analysee_ticker": analysed_alliance_ticker or "",
        "analyse_result": reason,
    }

    sort_delta = 0
    for column in MONTHLY_SHIP_COLUMNS + ["total_super", "total_titan"]:
        value = _monthly_number(diffs.get(column, 0))
        values[f"{column}_diff"] = value
        if column == "total_titan":
            sort_delta += abs(value) * SCORE_TITAN_POINTS
        elif column == "total_super":
            sort_delta += abs(value) * SCORE_SUPER_POINTS

    return {
        "values": values,
        "reason": reason,
        "sort_delta": sort_delta,
        "sort_score": sort_delta,
        "sort_name": _monthly_text(values.get("pilot_name")),
    }


def _monthly_ship_diffs(current, previous):
    return {
        column: _monthly_number(current.get(column, 0)) - _monthly_number(previous.get(column, 0))
        for column in MONTHLY_SHIP_COLUMNS + ["total_super", "total_titan"]
    }


def _monthly_ship_stock(row, sign=1):
    return {
        column: sign * _monthly_number(row.get(column, 0))
        for column in MONTHLY_SHIP_COLUMNS + ["total_super", "total_titan"]
    }


def _monthly_any_nonzero(values):
    return any(_monthly_number(value) != 0 for value in values.values())


def _build_monthly_explain_rows(current_rows, previous_rows):
    rows = []

    for key, current in current_rows.items():
        previous = previous_rows.get(key)
        if previous is None:
            source = {
                **current,
                "previous_alliance_id": None,
                "previous_alliance_name": "NEWSUPERPILOT",
                "previous_alliance_ticker": "NEWSUPERPILOT",
                "current_alliance_id": current.get("alliance_id"),
                "current_alliance_name": current.get("alliance_name"),
                "current_alliance_ticker": current.get("alliance_ticker"),
            }
            stock = _monthly_ship_stock(current, sign=1)
            if _monthly_any_nonzero(stock):
                rows.append(_monthly_analysis_row(
                    source,
                    "new_super_pilot",
                    current.get("alliance_id"),
                    current.get("alliance_name"),
                    current.get("alliance_ticker"),
                    stock,
                ))
            continue

        source = {
            **current,
            "previous_alliance_id": previous.get("alliance_id"),
            "previous_alliance_name": previous.get("alliance_name"),
            "previous_alliance_ticker": previous.get("alliance_ticker"),
            "current_alliance_id": current.get("alliance_id"),
            "current_alliance_name": current.get("alliance_name"),
            "current_alliance_ticker": current.get("alliance_ticker"),
        }

        same_alliance = previous.get("alliance_id") == current.get("alliance_id")
        diffs = _monthly_ship_diffs(current, previous)

        if same_alliance:
            if _monthly_any_nonzero(diffs):
                rows.append(_monthly_analysis_row(
                    source,
                    "organic_change",
                    current.get("alliance_id"),
                    current.get("alliance_name"),
                    current.get("alliance_ticker"),
                    diffs,
                ))
            continue

        current_stock = _monthly_ship_stock(current, sign=1)
        if _monthly_any_nonzero(current_stock):
            rows.append(_monthly_analysis_row(
                source,
                "alliance_leave",
                previous.get("alliance_id"),
                previous.get("alliance_name"),
                previous.get("alliance_ticker"),
                _monthly_ship_stock(current, sign=-1),
            ))
            rows.append(_monthly_analysis_row(
                source,
                "alliance_entrance",
                current.get("alliance_id"),
                current.get("alliance_name"),
                current.get("alliance_ticker"),
                current_stock,
            ))

    return rows



def _monthly_visible_columns(view):
    return [column for column in _monthly_display_columns(view) if not column["key"].endswith("_id")]


def _monthly_normalize_sort_key(sort_key, view):
    visible_keys = {column["key"] for column in _monthly_visible_columns(view)}
    if sort_key in visible_keys:
        return sort_key
    return "sort_delta"


def _monthly_normalize_sort_dir(sort_dir):
    return "asc" if sort_dir == "asc" else "desc"


def _monthly_row_sort_value(row, sort_key, columns_by_key):
    if sort_key == "sort_delta":
        return int(row.get("sort_delta") or 0)
    if sort_key == "sort_score":
        return int(row.get("sort_score") or 0)

    column = columns_by_key.get(sort_key, {"type": "text"})
    value = row["values"].get(sort_key)

    if column["type"] == "metric":
        if isinstance(value, dict):
            return int(value.get("value") or 0)
        return int(value or 0)
    if column["type"] == "diff":
        return int(value or 0)

    return str(value or "").lower()


def _monthly_normalize_filters(filters, view):
    visible_text_keys = {
        column["key"]
        for column in _monthly_visible_columns(view)
        if column["type"] == "text"
    }
    normalized = {}
    for key, value in (filters or {}).items():
        if key in visible_text_keys:
            text = str(value or "").strip()
            if text:
                normalized[key] = text
    return normalized


def _monthly_cell_text(value):
    if isinstance(value, dict):
        return f"{value.get('value', '')} {value.get('delta', '')}".lower()
    return str(value or "").lower()


def _monthly_row_matches(row, query, filters=None):
    if query:
        needle = query.lower()
        found = False
        for value in row["values"].values():
            if needle in _monthly_cell_text(value):
                found = True
                break
        if not found:
            return False

    for key, value in (filters or {}).items():
        if value.lower() not in _monthly_cell_text(row["values"].get(key)):
            return False

    return True


def _monthly_paginate(rows, page, page_size):
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(page_size)
    except (TypeError, ValueError):
        page_size = 50

    if page_size <= 0:
        page_size = 50
    if page_size > 200:
        page_size = 200

    total_rows = len(rows)
    total_pages = max(1, (total_rows + page_size - 1) // page_size)
    if page < 1:
        page = 1
    if page > total_pages:
        page = total_pages

    offset = (page - 1) * page_size
    return rows[offset:offset + page_size], {
        "page": page,
        "page_size": page_size,
        "total_rows": total_rows,
        "total_pages": total_pages,
        "has_previous": page > 1,
        "has_next": page < total_pages,
    }



def _monthly_backend_url(*, period, view, reason, query, filters, sort, direction, page, page_size):
    params = []
    if period:
        params.append(("period", period.isoformat() if hasattr(period, "isoformat") else period))
    params.append(("view", view))
    if view == "explain":
        params.append(("reason", reason))
    if query:
        params.append(("q", query))
    for key, value in sorted((filters or {}).items()):
        if value:
            params.append((f"f_{key}", value))
    if sort:
        params.append(("sort", sort))
        params.append(("dir", direction))
    params.append(("page", page))
    params.append(("page_size", page_size))
    return "/superintel/monthly-analysis?" + urlencode(params)


def _monthly_urls(*, selected_day, selected_view, selected_reason, query, filters, sort_key, sort_dir, pagination):
    page_size = pagination["page_size"]
    page = pagination["page"]
    total_pages = pagination["total_pages"]
    return {
        "previous_url": _monthly_backend_url(
            period=selected_day,
            view=selected_view,
            reason=selected_reason,
            query=query,
            filters=filters,
            sort=sort_key,
            direction=sort_dir,
            page=page - 1,
            page_size=page_size,
        ) if page > 1 else None,
        "next_url": _monthly_backend_url(
            period=selected_day,
            view=selected_view,
            reason=selected_reason,
            query=query,
            filters=filters,
            sort=sort_key,
            direction=sort_dir,
            page=page + 1,
            page_size=page_size,
        ) if page < total_pages else None,
        "view_urls": {
            key: _monthly_backend_url(
                period=selected_day,
                view=key,
                reason=selected_reason,
                query=query,
                filters=filters,
                sort=sort_key,
                direction=sort_dir,
                page=1,
                page_size=page_size,
            )
            for key in MONTHLY_ANALYSIS_VIEWS
        },
        "reason_urls": {
            key: _monthly_backend_url(
                period=selected_day,
                view=selected_view,
                reason=key,
                query=query,
                filters=filters,
                sort=sort_key,
                direction=sort_dir,
                page=1,
                page_size=page_size,
            )
            for key in MONTHLY_ANALYSIS_REASONS
        },
    }


def _monthly_attach_sort_urls(columns, *, selected_day, selected_view, selected_reason, query, filters, sort_key, sort_dir, page_size):
    for column in columns:
        next_dir = "asc" if sort_key == column["key"] and sort_dir == "desc" else "desc"
        column["sort_url"] = _monthly_backend_url(
            period=selected_day,
            view=selected_view,
            reason=selected_reason,
            query=query,
            filters=filters,
            sort=column["key"],
            direction=next_dir,
            page=1,
            page_size=page_size,
        )
    return columns



def _xlsx_col_name(index):
    name = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _xlsx_escape(value):
    text = "" if value is None else str(value)
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _xlsx_cell_xml(row_index, col_index, value):
    ref = f"{_xlsx_col_name(col_index)}{row_index}"
    if value is None:
        return f'<c r="{ref}"/>'
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, int) or isinstance(value, float):
        return f'<c r="{ref}"><v>{value}</v></c>'
    return f'<c r="{ref}" t="inlineStr"><is><t>{_xlsx_escape(value)}</t></is></c>'


def _xlsx_sheet_xml(headers, rows):
    sheet_data = []
    header_cells = ''.join(_xlsx_cell_xml(1, col_index, header) for col_index, header in enumerate(headers))
    sheet_data.append(f'<row r="1">{header_cells}</row>')
    for row_offset, row in enumerate(rows, start=2):
        cells = ''.join(_xlsx_cell_xml(row_offset, col_index, value) for col_index, value in enumerate(row))
        sheet_data.append(f'<row r="{row_offset}">{cells}</row>')
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
  <sheetData>{''.join(sheet_data)}</sheetData>
</worksheet>'''


def _xlsx_workbook_xml(sheet_names):
    sheets = ''.join(
        f'<sheet name="{_xlsx_escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, name in enumerate(sheet_names, start=1)
    )
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>{sheets}</sheets></workbook>'''


def _xlsx_workbook_rels_xml(sheet_names):
    rels = ''.join(
        f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
        for index, _ in enumerate(sheet_names, start=1)
    )
    rels += f'<Relationship Id="rId{len(sheet_names) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>'''


def _xlsx_content_types_xml(sheet_count):
    sheets = ''.join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>{sheets}</Types>'''


def _xlsx_root_rels_xml():
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'''


def _xlsx_styles_xml():
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts><fills count="1"><fill><patternFill patternType="none"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs></styleSheet>'''


def _monthly_export_cell_value(column, value):
    if column["type"] == "metric":
        if isinstance(value, dict):
            return _monthly_number(value.get("value"))
        return _monthly_number(value)
    if column["type"] == "diff":
        return _monthly_number(value)
    return _monthly_text(value, "Unknown") or "Unknown"


def _monthly_export_headers(columns):
    headers = []
    for column in columns:
        if column["key"].endswith("_id"):
            continue
        headers.append(column["label"])
        if column["type"] == "metric":
            headers.append(f'{column["label"]}_diff')
    return headers


def _monthly_export_row(columns, row):
    values = []
    for column in columns:
        if column["key"].endswith("_id"):
            continue
        value = row["values"].get(column["key"])
        if column["type"] == "metric":
            values.append(_monthly_export_cell_value(column, value))
            values.append(_monthly_number(value.get("delta") if isinstance(value, dict) else 0))
        else:
            values.append(_monthly_export_cell_value(column, value))
    return values


def _monthly_export_rows_for_view(conn, selected_day, view):
    selected_view = normalize_monthly_view(view)
    previous_day = same_day_previous_month(selected_day)
    columns = _monthly_visible_columns(selected_view)

    if selected_view == "explain":
        current_rows = _fetch_monthly_players(conn, selected_day)
        previous_rows = _fetch_monthly_players(conn, previous_day)
        rows = _build_monthly_explain_rows(current_rows, previous_rows)
    else:
        current_rows = _fetch_monthly_table_rows(conn, selected_day, selected_view)
        previous_rows = _fetch_monthly_table_rows(conn, previous_day, selected_view)
        rows = _build_monthly_diff_rows(current_rows, previous_rows, selected_view, selected_day)

    rows = sorted(
        rows,
        key=lambda item: (
            int(item.get("sort_delta") or 0),
            item.get("sort_name") or "",
        ),
        reverse=True,
    )

    headers = _monthly_export_headers(columns)
    data_rows = [_monthly_export_row(columns, row) for row in rows]
    return headers, data_rows


def get_monthly_analysis_excel(period=None):
    periods = list_monthly_analysis_periods()
    if not periods:
        raise SuperIntelError("superintel_monthly_period_missing")

    if period:
        try:
            selected_day = datetime.strptime(period, "%Y-%m-%d").date()
        except ValueError as exc:
            raise SuperIntelError("superintel_monthly_period_invalid") from exc
        if selected_day not in periods:
            raise SuperIntelError("superintel_monthly_period_unknown")
    else:
        selected_day = periods[-1]

    sheet_order = [
        ("Pilot", "pilot"),
        ("Corporation", "corp"),
        ("Alliance", "alliance"),
        ("Explain", "explain"),
    ]

    workbook_sheets = []
    with db() as conn:
        for sheet_name, view in sheet_order:
            headers, rows = _monthly_export_rows_for_view(conn, selected_day, view)
            workbook_sheets.append((sheet_name, headers, rows))

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        sheet_names = [sheet_name for sheet_name, _, _ in workbook_sheets]
        zf.writestr("[Content_Types].xml", _xlsx_content_types_xml(len(workbook_sheets)))
        zf.writestr("_rels/.rels", _xlsx_root_rels_xml())
        zf.writestr("xl/workbook.xml", _xlsx_workbook_xml(sheet_names))
        zf.writestr("xl/_rels/workbook.xml.rels", _xlsx_workbook_rels_xml(sheet_names))
        zf.writestr("xl/styles.xml", _xlsx_styles_xml())
        for index, (_, headers, rows) in enumerate(workbook_sheets, start=1):
            zf.writestr(f"xl/worksheets/sheet{index}.xml", _xlsx_sheet_xml(headers, rows))

    filename = f"superintel_monthly_analysis_{selected_day.isoformat()}.xlsx"
    return filename, buffer.getvalue()


def get_monthly_analysis(period=None, reason="all", view="pilot", q="", filters=None, sort="sort_delta", direction="desc", page=1, page_size=50):
    periods = list_monthly_analysis_periods()
    selected_view = normalize_monthly_view(view)
    selected_reason = normalize_monthly_reason(reason)
    query = (q or "").strip()
    column_filters = _monthly_normalize_filters(filters, selected_view)
    sort_key = _monthly_normalize_sort_key(sort, selected_view)
    sort_dir = _monthly_normalize_sort_dir(direction)
    columns = _monthly_display_columns(selected_view)
    columns_by_key = {column["key"]: column for column in columns}

    if not periods:
        empty_rows, pagination = _monthly_paginate([], page, page_size)
        columns = _monthly_attach_sort_urls(
            columns,
            selected_day=None,
            selected_view=selected_view,
            selected_reason=selected_reason,
            query=query,
            filters=column_filters,
            sort_key=sort_key,
            sort_dir=sort_dir,
            page_size=pagination["page_size"],
        )
        urls = _monthly_urls(
            selected_day=None,
            selected_view=selected_view,
            selected_reason=selected_reason,
            query=query,
            filters=column_filters,
            sort_key=sort_key,
            sort_dir=sort_dir,
            pagination=pagination,
        )
        return {
            "periods": [],
            "selected_day": None,
            "previous_day": None,
            "view": selected_view,
            "view_labels": MONTHLY_ANALYSIS_VIEWS,
            "view_urls": urls["view_urls"],
            "reason": selected_reason,
            "reason_labels": MONTHLY_ANALYSIS_REASONS,
            "reason_urls": urls["reason_urls"],
            "columns": columns,
            "rows": empty_rows,
            "query": query,
            "q": query,
            "filters": column_filters,
            "sort": sort_key,
            "direction": sort_dir,
            "dir": sort_dir,
            "pagination": pagination,
            "page": pagination["page"],
            "page_size": pagination["page_size"],
            "total_rows": pagination["total_rows"],
            "total_pages": pagination["total_pages"],
            "previous_url": urls["previous_url"],
            "next_url": urls["next_url"],
            "score_titan_points": SCORE_TITAN_POINTS,
            "score_super_points": SCORE_SUPER_POINTS,
        }

    if period:
        try:
            selected_day = datetime.strptime(period, "%Y-%m-%d").date()
        except ValueError as exc:
            raise SuperIntelError("superintel_monthly_period_invalid") from exc
        if selected_day not in periods:
            selected_day = periods[-1]
    else:
        selected_day = periods[-1]

    previous_day = same_day_previous_month(selected_day)

    with db() as conn:
        if selected_view == "explain":
            current_rows = _fetch_monthly_players(conn, selected_day)
            previous_rows = _fetch_monthly_players(conn, previous_day)
            rows = _build_monthly_explain_rows(current_rows, previous_rows)
        else:
            current_rows = _fetch_monthly_table_rows(conn, selected_day, selected_view)
            previous_rows = _fetch_monthly_table_rows(conn, previous_day, selected_view)
            rows = _build_monthly_diff_rows(current_rows, previous_rows, selected_view, selected_day)

    if selected_view == "explain" and selected_reason != "all":
        rows = [row for row in rows if row["reason"] == selected_reason]

    rows = [row for row in rows if _monthly_row_matches(row, query, column_filters)]

    reverse = sort_dir == "desc"
    rows = sorted(
        rows,
        key=lambda item: (
            _monthly_row_sort_value(item, sort_key, columns_by_key),
            item.get("sort_name") or "",
        ),
        reverse=reverse,
    )

    paged_rows, pagination = _monthly_paginate(rows, page, page_size)
    columns = _monthly_attach_sort_urls(
        columns,
        selected_day=selected_day,
        selected_view=selected_view,
        selected_reason=selected_reason,
        query=query,
        filters=column_filters,
        sort_key=sort_key,
        sort_dir=sort_dir,
        page_size=pagination["page_size"],
    )
    urls = _monthly_urls(
        selected_day=selected_day,
        selected_view=selected_view,
        selected_reason=selected_reason,
        query=query,
        filters=column_filters,
        sort_key=sort_key,
        sort_dir=sort_dir,
        pagination=pagination,
    )

    return {
        "periods": periods,
        "selected_day": selected_day,
        "previous_day": previous_day,
        "view": selected_view,
        "view_labels": MONTHLY_ANALYSIS_VIEWS,
        "view_urls": urls["view_urls"],
        "reason": selected_reason,
        "reason_labels": MONTHLY_ANALYSIS_REASONS,
        "reason_urls": urls["reason_urls"],
        "columns": columns,
        "rows": paged_rows,
        "query": query,
        "q": query,
        "filters": column_filters,
        "sort": sort_key,
        "direction": sort_dir,
        "dir": sort_dir,
        "pagination": pagination,
        "page": pagination["page"],
        "page_size": pagination["page_size"],
        "total_rows": pagination["total_rows"],
        "total_pages": pagination["total_pages"],
        "previous_url": urls["previous_url"],
        "next_url": urls["next_url"],
        "score_titan_points": SCORE_TITAN_POINTS,
        "score_super_points": SCORE_SUPER_POINTS,
    }
