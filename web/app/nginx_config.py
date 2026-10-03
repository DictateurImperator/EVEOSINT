import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path("/home/ubuntu/eveosint")
RUN_DIR = REPO_ROOT / "data" / "run" / "nginx_config"
REQUEST_FILE = RUN_DIR / "request.json"
STATUS_FILE = RUN_DIR / "status.json"

CONFIG_FILE = Path("/etc/nginx/sites-available/eveosint")
BACKUP_DIR = Path("/var/backups/eveosint-nginx")

SERVICE_NAME = "eveosint-nginx-config.service"
SYSTEMCTL = "/usr/bin/systemctl"
SUDO = "/usr/bin/sudo"
NGINX = "/usr/sbin/nginx"

MAX_CONFIG_BYTES = 512 * 1024


class NginxConfigError(RuntimeError):
    pass


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _ensure_run_dir():
    RUN_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write_json(path, payload):
    _ensure_run_dir()
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {} if default is None else default


def _run(command, *, timeout=30, check=False):
    try:
        return subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=check,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NginxConfigError(f"command_failed:{command[0]}") from exc


def _service_running():
    result = _run([SYSTEMCTL, "is-active", SERVICE_NAME], timeout=5)
    return result.returncode == 0 and result.stdout.strip() in {"active", "activating"}


def _validate_content(content):
    if not isinstance(content, str):
        raise NginxConfigError("invalid_content")
    raw = content.encode("utf-8")
    if not raw:
        raise NginxConfigError("empty_config")
    if len(raw) > MAX_CONFIG_BYTES:
        raise NginxConfigError("config_too_large")
    if b"\x00" in raw:
        raise NginxConfigError("invalid_content")
    return raw


def load_nginx_config():
    try:
        return CONFIG_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        raise NginxConfigError("config_read_failed") from exc


def get_nginx_snapshot():
    status_data = _read_json(STATUS_FILE, {})
    try:
        config = load_nginx_config()
        read_error = None
    except NginxConfigError as exc:
        config = ""
        read_error = str(exc)

    return {
        "path": str(CONFIG_FILE),
        "config": config,
        "read_error": read_error,
        "worker_running": _service_running(),
        "phase": status_data.get("phase"),
        "result": status_data.get("result"),
        "message": status_data.get("message"),
        "request_id": status_data.get("request_id"),
        "requested_by": status_data.get("requested_by"),
        "timestamp": status_data.get("timestamp"),
        "backup_path": status_data.get("backup_path"),
        "nginx_test_output": status_data.get("nginx_test_output"),
    }


def queue_nginx_save(content, requested_by):
    raw = _validate_content(content)
    if _service_running():
        raise NginxConfigError("worker_busy")

    request_id = uuid.uuid4().hex
    request = {
        "request_id": request_id,
        "requested_at": _utc_now(),
        "requested_by": (requested_by or "").strip()[:128],
        "content": raw.decode("utf-8"),
    }
    _atomic_write_json(REQUEST_FILE, request)
    _atomic_write_json(
        STATUS_FILE,
        {
            "phase": "queued",
            "result": None,
            "message": "Configuration save queued.",
            "request_id": request_id,
            "requested_by": request["requested_by"],
            "timestamp": _utc_now(),
        },
    )

    result = _run(
        [SUDO, SYSTEMCTL, "--no-block", "start", SERVICE_NAME],
        timeout=10,
    )
    if result.returncode != 0:
        _atomic_write_json(
            STATUS_FILE,
            {
                "phase": "failed",
                "result": "failed",
                "message": "Unable to start the Nginx configuration worker.",
                "request_id": request_id,
                "requested_by": request["requested_by"],
                "timestamp": _utc_now(),
            },
        )
        raise NginxConfigError("worker_start_failed")

    return request_id


def _combined_output(result):
    parts = []
    if result.stdout.strip():
        parts.append(result.stdout.strip())
    if result.stderr.strip():
        parts.append(result.stderr.strip())
    return "\n".join(parts)[-20000:]


def _replace_config(raw, original_stat):
    parent = CONFIG_FILE.parent
    fd, tmp_name = tempfile.mkstemp(prefix=".eveosint-nginx-", dir=str(parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, stat.S_IMODE(original_stat.st_mode))
        os.chown(tmp, original_stat.st_uid, original_stat.st_gid)
        os.replace(tmp, CONFIG_FILE)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _write_worker_status(request, **updates):
    payload = {
        "request_id": request.get("request_id"),
        "requested_by": request.get("requested_by"),
        "timestamp": _utc_now(),
    }
    payload.update(updates)
    _atomic_write_json(STATUS_FILE, payload)


def run_request():
    request = _read_json(REQUEST_FILE, {})
    request_id = request.get("request_id")
    if not request_id or not isinstance(request_id, str):
        raise NginxConfigError("request_missing")

    raw = _validate_content(request.get("content"))

    if not CONFIG_FILE.exists() or not CONFIG_FILE.is_file() or CONFIG_FILE.is_symlink():
        raise NginxConfigError("config_path_invalid")

    original_stat = CONFIG_FILE.stat()
    original = CONFIG_FILE.read_bytes()

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = BACKUP_DIR / f"eveosint.{backup_stamp}.{request_id[:8]}.conf"
    shutil.copy2(CONFIG_FILE, backup_path)

    _write_worker_status(
        request,
        phase="testing",
        result=None,
        message="Testing new Nginx configuration.",
        backup_path=str(backup_path),
    )

    _replace_config(raw, original_stat)
    test_result = _run([NGINX, "-t"], timeout=30)
    test_output = _combined_output(test_result)

    if test_result.returncode != 0:
        _replace_config(original, original_stat)
        restore_test = _run([NGINX, "-t"], timeout=30)
        restore_output = _combined_output(restore_test)
        _write_worker_status(
            request,
            phase="failed",
            result="failed",
            message="nginx -t failed. Previous configuration restored; Nginx was not reloaded.",
            backup_path=str(backup_path),
            nginx_test_output=test_output,
            restore_test_output=restore_output,
        )
        return 1

    _write_worker_status(
        request,
        phase="reloading",
        result=None,
        message="Configuration test passed. Reloading Nginx.",
        backup_path=str(backup_path),
        nginx_test_output=test_output,
    )

    reload_result = _run([SYSTEMCTL, "reload", "nginx.service"], timeout=30)
    if reload_result.returncode != 0:
        reload_output = _combined_output(reload_result)
        _replace_config(original, original_stat)
        restore_test = _run([NGINX, "-t"], timeout=30)
        restore_reload = _run([SYSTEMCTL, "reload", "nginx.service"], timeout=30)
        _write_worker_status(
            request,
            phase="failed",
            result="failed",
            message="Nginx reload failed. Previous configuration restored.",
            backup_path=str(backup_path),
            nginx_test_output=test_output,
            reload_output=reload_output,
            restore_test_output=_combined_output(restore_test),
            restore_reload_output=_combined_output(restore_reload),
        )
        return 1

    _write_worker_status(
        request,
        phase="done",
        result="success",
        message="Configuration saved, nginx -t passed, and Nginx reloaded.",
        backup_path=str(backup_path),
        nginx_test_output=test_output,
    )
    return 0


def main():
    if len(sys.argv) != 2 or sys.argv[1] != "run-request":
        raise SystemExit("usage: nginx_config.py run-request")

    exit_code = 1
    try:
        exit_code = run_request()
    except NginxConfigError as exc:
        request = _read_json(REQUEST_FILE, {})
        _write_worker_status(
            request,
            phase="failed",
            result="failed",
            message=str(exc),
        )
        exit_code = 1
    except Exception:
        request = _read_json(REQUEST_FILE, {})
        _write_worker_status(
            request,
            phase="failed",
            result="failed",
            message="unexpected_worker_error",
        )
        exit_code = 1
    finally:
        try:
            REQUEST_FILE.unlink()
        except FileNotFoundError:
            pass

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
