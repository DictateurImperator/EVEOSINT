import gzip
import re
import shutil
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import SYSTEM_SERVICES
from .db import db


NGINX_ACCESS_LOGS = [
    "/var/log/nginx/access.log",
    "/var/log/nginx/access.log.1",
    "/var/log/nginx/access.log.*.gz",
]

NGINX_LOG_RE = re.compile(
    r'^(?P<ip>\S+) \S+ \S+ '
    r'\[(?P<time>[^\]]+)\] '
    r'"(?P<request>[^"]*)" '
    r'(?P<status>\d{3}) '
    r'(?P<bytes>\S+)'
)

IGNORED_INTERFACES = {
    "lo",
}


class SystemInfoError(RuntimeError):
    pass


def _run(command):
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except Exception as exc:
        return 1, "", str(exc)


def format_bytes(value):
    try:
        value = float(value)
    except Exception:
        return "-"

    units = ["B", "KB", "MB", "GB", "TB"]
    unit_index = 0

    while value >= 1024 and unit_index < len(units) - 1:
        value = value / 1024
        unit_index += 1

    if unit_index == 0:
        return f"{int(value)} {units[unit_index]}"

    return f"{value:.2f} {units[unit_index]}"


def format_rate(value):
    return f"{format_bytes(value)}/s"


def get_uptime():
    code, stdout, stderr = _run(["uptime", "-p"])
    if code == 0 and stdout:
        return stdout
    return stderr or "Indisponible"


def get_disk_usage(path="/"):
    usage = shutil.disk_usage(path)
    return {
        "path": path,
        "total_gb": round(usage.total / 1024 / 1024 / 1024, 2),
        "used_gb": round(usage.used / 1024 / 1024 / 1024, 2),
        "free_gb": round(usage.free / 1024 / 1024 / 1024, 2),
        "percent": round((usage.used / usage.total) * 100, 1),
    }


def get_db_size():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    pg_database_size(current_database()),
                    pg_size_pretty(pg_database_size(current_database()))
                """
            )
            row = cur.fetchone()

    return {
        "bytes": row[0],
        "pretty": row[1],
    }


def get_service_status(service_name):
    code, stdout, stderr = _run([
        "systemctl",
        "show",
        service_name,
        "--property=LoadState,ActiveState,SubState,ExecMainStartTimestamp,MainPID",
        "--no-page",
    ])

    if code != 0:
        return {
            "name": service_name,
            "load_state": "unknown",
            "active_state": "unknown",
            "sub_state": "unknown",
            "started_at": None,
            "main_pid": None,
            "error": stderr or stdout or "systemctl unavailable",
        }

    data = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            data[key] = value or None

    return {
        "name": service_name,
        "load_state": data.get("LoadState"),
        "active_state": data.get("ActiveState"),
        "sub_state": data.get("SubState"),
        "started_at": data.get("ExecMainStartTimestamp"),
        "main_pid": data.get("MainPID"),
        "error": None,
    }


def iter_nginx_log_paths():
    found = []

    for pattern in NGINX_ACCESS_LOGS:
        matches = list(Path("/").glob(pattern.lstrip("/")))
        for path in matches:
            if path.is_file():
                found.append(path)

    return sorted(
        set(found),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )


def open_log_file(path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def parse_nginx_time(raw_value):
    return datetime.strptime(raw_value, "%d/%b/%Y:%H:%M:%S %z")


def minute_bucket(dt):
    return dt.replace(second=0, microsecond=0)


def read_nginx_access_events(since):
    events = []
    paths = iter_nginx_log_paths()

    if not paths:
        raise SystemInfoError("Aucun log HTTP lisible.")

    try:
        for path in paths:
            with open_log_file(path) as handle:
                for line in handle:
                    match = NGINX_LOG_RE.match(line)
                    if not match:
                        continue

                    try:
                        event_time = parse_nginx_time(match.group("time"))
                    except Exception:
                        continue

                    if event_time < since:
                        continue

                    raw_bytes = match.group("bytes")
                    if raw_bytes == "-":
                        sent_bytes = 0
                    else:
                        try:
                            sent_bytes = int(raw_bytes)
                        except ValueError:
                            sent_bytes = 0

                    events.append({
                        "ip": match.group("ip"),
                        "time": event_time,
                        "bytes": sent_bytes,
                        "status": match.group("status"),
                    })
    except PermissionError as exc:
        raise SystemInfoError("Logs HTTP non lisibles par l'application.") from exc
    except Exception as exc:
        raise SystemInfoError("Lecture des logs HTTP impossible.") from exc

    return events


def build_bandwidth_period(label, hours, events, now):
    since = now - timedelta(hours=hours)

    period_events = [
        event for event in events
        if event["time"] >= since
    ]

    total_bytes = sum(event["bytes"] for event in period_events)
    requests_count = len(period_events)

    by_ip = defaultdict(int)
    by_minute = defaultdict(int)

    for event in period_events:
        by_ip[event["ip"]] += event["bytes"]
        by_minute[minute_bucket(event["time"])] += event["bytes"]

    if by_ip:
        top_ip, top_ip_bytes = max(by_ip.items(), key=lambda item: item[1])
    else:
        top_ip = "-"
        top_ip_bytes = 0

    period_seconds = max(hours * 3600, 1)
    average_rate = total_bytes / period_seconds

    peak_minute_bytes = max(by_minute.values()) if by_minute else 0
    peak_rate = peak_minute_bytes / 60

    return {
        "label": label,
        "requests": requests_count,
        "total_bytes": total_bytes,
        "total_pretty": format_bytes(total_bytes),
        "avg_rate_bytes": round(average_rate, 2),
        "avg_rate_pretty": format_rate(average_rate),
        "peak_rate_bytes": round(peak_rate, 2),
        "peak_rate_pretty": format_rate(peak_rate),
        "peak_window": "1 min",
        "top_ip": top_ip,
        "top_ip_bytes": top_ip_bytes,
        "top_ip_pretty": format_bytes(top_ip_bytes),
        "top_user": "Non disponible",
    }


def get_http_bandwidth_stats():
    now = datetime.now(timezone.utc)
    max_since = now - timedelta(days=31)
    events = read_nginx_access_events(max_since)

    return {
        "periods": [
            build_bandwidth_period("1 heure", 1, events, now),
            build_bandwidth_period("1 jour", 24, events, now),
            build_bandwidth_period("1 semaine", 24 * 7, events, now),
            build_bandwidth_period("1 mois", 24 * 31, events, now),
        ],
        "note": "Trafic HTTP servi par Nginx. Le top user nécessite un lien explicite entre requêtes HTTP, session et volume envoyé.",
    }


def read_network_counters():
    path = Path("/proc/net/dev")
    if not path.exists():
        raise SystemInfoError("Compteurs réseau Linux introuvables.")

    interfaces = []

    with path.open("r", encoding="utf-8") as handle:
        for line in handle.readlines()[2:]:
            if ":" not in line:
                continue

            raw_name, raw_values = line.split(":", 1)
            name = raw_name.strip()
            values = raw_values.split()

            if name in IGNORED_INTERFACES:
                continue

            if len(values) < 16:
                raise SystemInfoError(f"Format compteur réseau invalide pour interface {name}.")

            rx_bytes = int(values[0])
            tx_bytes = int(values[8])

            interfaces.append({
                "name": name,
                "rx_bytes": rx_bytes,
                "tx_bytes": tx_bytes,
            })

    if not interfaces:
        raise SystemInfoError("Aucune interface réseau exploitable trouvée.")

    return interfaces


def get_network_live_stats(interval_seconds=1.0):
    first = read_network_counters()
    time.sleep(interval_seconds)
    second = read_network_counters()

    first_by_name = {item["name"]: item for item in first}
    rows = []

    for current in second:
        name = current["name"]
        previous = first_by_name.get(name)
        if previous is None:
            raise SystemInfoError(f"Interface réseau apparue pendant la mesure: {name}.")

        rx_delta = current["rx_bytes"] - previous["rx_bytes"]
        tx_delta = current["tx_bytes"] - previous["tx_bytes"]

        if rx_delta < 0 or tx_delta < 0:
            raise SystemInfoError(f"Compteur réseau incohérent pour interface {name}.")

        rx_rate = rx_delta / interval_seconds
        tx_rate = tx_delta / interval_seconds

        rows.append({
            "name": name,
            "rx_total_bytes": current["rx_bytes"],
            "tx_total_bytes": current["tx_bytes"],
            "rx_total_pretty": format_bytes(current["rx_bytes"]),
            "tx_total_pretty": format_bytes(current["tx_bytes"]),
            "rx_rate_bytes": round(rx_rate, 2),
            "tx_rate_bytes": round(tx_rate, 2),
            "rx_rate_pretty": format_rate(rx_rate),
            "tx_rate_pretty": format_rate(tx_rate),
        })

    total_rx = sum(row["rx_total_bytes"] for row in rows)
    total_tx = sum(row["tx_total_bytes"] for row in rows)
    total_rx_rate = sum(row["rx_rate_bytes"] for row in rows)
    total_tx_rate = sum(row["tx_rate_bytes"] for row in rows)

    return {
        "interval_seconds": interval_seconds,
        "rx_total_pretty": format_bytes(total_rx),
        "tx_total_pretty": format_bytes(total_tx),
        "rx_rate_pretty": format_rate(total_rx_rate),
        "tx_rate_pretty": format_rate(total_tx_rate),
        "interfaces": rows,
    }


def get_system_snapshot():
    return {
        "uptime": get_uptime(),
        "disk": get_disk_usage("/"),
        "db_size": get_db_size(),
        "network_live": get_network_live_stats(),
        "bandwidth": get_http_bandwidth_stats(),
        "services": [get_service_status(name) for name in SYSTEM_SERVICES],
    }
