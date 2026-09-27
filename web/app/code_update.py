import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


REPO_ROOT = Path.home() / "eveosint"
GIT_CONFIG_FILE = REPO_ROOT / "data" / "config" / "git.json"
RUN_DIR = REPO_ROOT / "data" / "run" / "code_update"
REQUEST_FILE = RUN_DIR / "request.json"
STATUS_FILE = RUN_DIR / "status.json"
HISTORY_FILE = RUN_DIR / "history.jsonl"
LOCK_FILE = RUN_DIR / "update.lock"
WEB_SERVICE = "eveosint-web.service"
UPDATE_SERVICE = "eveosint-code-update.service"
SYSTEMCTL = "/usr/bin/systemctl"
DEFAULT_BRANCH = "main"
HEALTH_URL = "http://127.0.0.1:8000/login"

# Test-server overlay: these two tracked files contain the Admin > Git test wiring.
# They are allowed locally during the test, but every other tracked change still blocks updates.
TEST_ALLOWED_TRACKED_CHANGES = {
    "web/app/main.py",
    "web/app/menus.py",
}


class CodeUpdateError(RuntimeError):
    pass


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _ensure_run_dir():
    RUN_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write_json(path, payload):
    _ensure_run_dir()
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, path)


def _read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {} if default is None else default


def _append_history(entry):
    _ensure_run_dir()
    with HISTORY_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def _read_history(limit=10):
    try:
        lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    entries = []
    for line in reversed(lines[-max(limit * 3, limit):]):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        entries.append(item)
        if len(entries) >= limit:
            break
    return entries


def _run(command, *, cwd=REPO_ROOT, timeout=60, check=True, extra_env=None):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    if extra_env:
        env.update(extra_env)
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CodeUpdateError(f"command_failed:{command[0]}") from exc

    if check and result.returncode != 0:
        raise CodeUpdateError(f"command_failed:{command[0]}")
    return result


def _git(args, *, timeout=60, check=True, authenticated=False):
    extra_env = _git_auth_env() if authenticated else None
    return _run(["git", *args], timeout=timeout, check=check, extra_env=extra_env)


def load_git_settings():
    raw = _read_json(GIT_CONFIG_FILE, {})
    return {
        "origin_url": (raw.get("origin_url") or "").strip(),
        "branch": (raw.get("branch") or DEFAULT_BRANCH).strip() or DEFAULT_BRANCH,
        "username": (raw.get("username") or "").strip(),
        "token": raw.get("token") or "",
        "token_configured": bool(raw.get("token")),
    }


def save_git_settings(origin_url, branch, username, token=None, clear_token=False):
    origin_url = (origin_url or "").strip()
    branch = (branch or DEFAULT_BRANCH).strip() or DEFAULT_BRANCH
    username = (username or "").strip()

    if not origin_url:
        raise CodeUpdateError("git_origin_missing")
    try:
        parsed_origin = urlsplit(origin_url)
    except ValueError as exc:
        raise CodeUpdateError("invalid_origin_url") from exc
    if parsed_origin.scheme in {"http", "https"} and (parsed_origin.username or parsed_origin.password):
        raise CodeUpdateError("credentials_in_origin_url")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch):
        raise CodeUpdateError("invalid_branch")

    existing = _read_json(GIT_CONFIG_FILE, {})
    stored_token = existing.get("token") or ""
    if clear_token:
        stored_token = ""
    elif token:
        stored_token = token.strip()

    payload = {
        "origin_url": origin_url,
        "branch": branch,
        "username": username,
        "token": stored_token,
        "updated_at": _utc_now(),
    }
    GIT_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = GIT_CONFIG_FILE.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, GIT_CONFIG_FILE)
    os.chmod(GIT_CONFIG_FILE, 0o600)
    return load_git_settings()


def _git_auth_env():
    settings = load_git_settings()
    token = settings.get("token") or ""
    if not token:
        return None

    _ensure_run_dir()
    askpass = RUN_DIR / "git-askpass.sh"
    askpass.write_text(
        '#!/bin/sh\ncase "$1" in\n  *Username*) printf "%s\\n" "$EVEOSINT_GIT_USERNAME" ;;;\n  *) printf "%s\\n" "$EVEOSINT_GIT_TOKEN" ;;;\nesac\n'.replace(';;;', ';;'),
        encoding="utf-8",
    )
    os.chmod(askpass, 0o700)
    return {
        "GIT_ASKPASS": str(askpass),
        "GIT_ASKPASS_REQUIRE": "force",
        "EVEOSINT_GIT_USERNAME": settings.get("username") or "x-access-token",
        "EVEOSINT_GIT_TOKEN": token,
    }


def _configured_origin():
    settings = load_git_settings()
    return settings.get("origin_url") or _origin_url()


def _configured_branch():
    settings = load_git_settings()
    return settings.get("branch") or _current_branch()


def test_git_access():
    settings = load_git_settings()
    origin = settings.get("origin_url") or (_origin_url() if _is_git_repo() else "")
    branch = settings.get("branch") or DEFAULT_BRANCH
    if not origin:
        raise CodeUpdateError("git_origin_missing")

    result = _git(
        ["ls-remote", "--heads", origin, f"refs/heads/{branch}"],
        timeout=60,
        check=False,
        authenticated=True,
    )
    if result.returncode != 0:
        raise CodeUpdateError("git_access_failed")
    if not result.stdout.strip():
        raise CodeUpdateError("git_branch_missing")
    return True


def _is_git_repo():
    if not REPO_ROOT.exists():
        return False
    result = _git(["rev-parse", "--is-inside-work-tree"], check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def _current_sha():
    return _git(["rev-parse", "HEAD"]).stdout.strip()


def _current_branch():
    branch = _git(["branch", "--show-current"]).stdout.strip()
    return branch or DEFAULT_BRANCH


def _origin_url():
    result = _git(["remote", "get-url", "origin"], check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def _sanitize_remote_url(value):
    value = (value or "").strip()
    if not value:
        return ""

    try:
        parsed = urlsplit(value)
    except ValueError:
        return value

    if parsed.scheme in {"http", "https"} and parsed.hostname:
        host = parsed.hostname
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))
    return value


def _version_sort_key(value):
    match = re.fullmatch(r"(?:v)?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", value or "")
    if not match:
        return (-1, -1, -1, value or "")
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), value or "")


def _discover_remote_targets():
    origin = _configured_origin()
    branch = _configured_branch()
    if not origin:
        raise CodeUpdateError("git_origin_missing")

    result = _git(
        ["ls-remote", "--heads", origin],
        timeout=60,
        check=False,
        authenticated=True,
    )
    if result.returncode != 0:
        raise CodeUpdateError("git_access_failed")

    refs = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        sha, ref = parts
        if re.fullmatch(r"[0-9a-fA-F]{40}", sha):
            refs[ref] = sha.lower()

    targets = []
    latest_ref = f"refs/heads/{branch}"
    latest_sha = refs.get(latest_ref)
    if latest_sha:
        targets.append({
            "key": "latest",
            "ref": latest_ref,
            "label": f"latest ({branch})",
            "kind": "latest",
            "sha": latest_sha,
            "short": latest_sha[:12],
        })

    stable = []
    prefix = "refs/heads/stable/"
    for ref, sha in refs.items():
        if not ref.startswith(prefix):
            continue
        version = ref[len(prefix):]
        if not version:
            continue
        stable.append({
            "key": f"stable/{version}",
            "ref": ref,
            "label": f"stable {version}",
            "kind": "stable",
            "version": version,
            "sha": sha,
            "short": sha[:12],
        })

    stable.sort(key=lambda item: _version_sort_key(item["version"]), reverse=True)
    targets.extend(stable)
    return targets


def _target_by_key(target_key, targets=None):
    target_key = (target_key or "").strip()
    targets = targets if targets is not None else _discover_remote_targets()
    for item in targets:
        if item.get("key") == target_key:
            return item
    raise CodeUpdateError("target_not_found")


def _fetch_remote_target(target_key):
    target = _target_by_key(target_key)
    origin = _configured_origin()
    _git(
        ["fetch", "--quiet", "--prune", origin, target["ref"]],
        timeout=90,
        authenticated=True,
    )
    fetched_sha = _git(["rev-parse", "FETCH_HEAD"]).stdout.strip().lower()
    if not _commit_exists(fetched_sha):
        raise CodeUpdateError("remote_commit_missing")
    if fetched_sha != target["sha"]:
        raise CodeUpdateError("remote_target_changed")
    return target, fetched_sha


def _tracked_changed_paths():
    result = _git(["diff", "--name-only", "HEAD", "--"])
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _tracked_change_state():
    changed = _tracked_changed_paths()
    test_overlay = changed & TEST_ALLOWED_TRACKED_CHANGES
    blocking = changed - TEST_ALLOWED_TRACKED_CHANGES
    return {
        "changed": sorted(changed),
        "test_overlay": sorted(test_overlay),
        "blocking": sorted(blocking),
    }


def _tracked_dirty():
    return bool(_tracked_change_state()["blocking"])


def _capture_test_overlay(paths):
    overlay = {}
    for relative_path in paths:
        path = REPO_ROOT / relative_path
        if path.exists() and path.is_file():
            overlay[relative_path] = {
                "exists": True,
                "content": path.read_bytes(),
                "mode": path.stat().st_mode & 0o777,
            }
        else:
            overlay[relative_path] = {"exists": False, "content": b"", "mode": None}
    return overlay


def _restore_test_overlay(overlay):
    for relative_path, item in overlay.items():
        path = REPO_ROOT / relative_path
        if item["exists"]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(item["content"])
            if item.get("mode") is not None:
                os.chmod(path, item["mode"])
        elif path.exists():
            path.unlink()


def _git_blob_bytes(sha, relative_path):
    result = _git(["show", f"{sha}:{relative_path}"], check=False)
    if result.returncode != 0:
        return None
    return result.stdout.encode("utf-8")


def _overlay_restore_plan(from_sha, to_sha, overlay):
    if not overlay or from_sha == to_sha:
        return overlay, []

    paths = sorted(overlay)
    result = _git(["diff", "--name-only", f"{from_sha}..{to_sha}", "--", *paths])
    changed_by_target = {line.strip() for line in result.stdout.splitlines() if line.strip()}

    restore_overlay = {}
    conflicts = []
    for relative_path, item in overlay.items():
        if relative_path not in changed_by_target:
            restore_overlay[relative_path] = item
            continue

        target_bytes = _git_blob_bytes(to_sha, relative_path)
        local_bytes = item["content"] if item.get("exists") else None
        if target_bytes == local_bytes:
            continue
        conflicts.append(relative_path)

    return restore_overlay, sorted(conflicts)


def _commit_exists(def _commit_exists(sha):
    if not sha or not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
        return False
    result = _git(["cat-file", "-e", f"{sha}^{{commit}}"], check=False)
    return result.returncode == 0


def _is_ancestor(ancestor, descendant):
    result = _git(["merge-base", "--is-ancestor", ancestor, descendant], check=False)
    return result.returncode == 0


def _rev_count(range_spec):
    result = _git(["rev-list", "--count", range_spec], check=False)
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def _service_running():
    result = _run([SYSTEMCTL, "is-active", UPDATE_SERVICE], cwd=REPO_ROOT, timeout=5, check=False)
    return result.returncode == 0 and result.stdout.strip() in {"active", "activating"}


def _status_base():
    return {
        "timestamp": _utc_now(),
        "phase": "idle",
        "action": None,
        "result": None,
        "message": None,
    }


def _write_status(**updates):
    current = _read_json(STATUS_FILE, {})
    current.update(updates)
    current["timestamp"] = _utc_now()
    _atomic_write_json(STATUS_FILE, current)
    return current


def refresh_remote():
    if not _is_git_repo():
        raise CodeUpdateError("git_repository_not_initialized")

    targets = _discover_remote_targets()
    latest = _target_by_key("latest", targets)
    origin = _configured_origin()

    _git(
        ["fetch", "--quiet", "--prune", origin, latest["ref"]],
        timeout=90,
        authenticated=True,
    )
    remote_sha = _git(["rev-parse", "FETCH_HEAD"]).stdout.strip().lower()
    if not _commit_exists(remote_sha):
        raise CodeUpdateError("remote_commit_missing")
    if remote_sha != latest["sha"]:
        raise CodeUpdateError("remote_target_changed")

    _write_status(
        last_remote_sha=remote_sha,
        last_remote_branch=_configured_branch(),
        last_remote_check_at=_utc_now(),
        remote_targets=targets,
    )
    return remote_sha


def _decorate_targets(targets, current_sha):
    decorated = []
    for item in targets or []:
        entry = dict(item)
        sha = entry.get("sha")
        entry["is_current"] = bool(sha and current_sha and sha == current_sha)
        entry["can_update"] = False
        entry["can_rollback"] = False
        if sha and current_sha and _commit_exists(sha) and sha != current_sha:
            entry["can_update"] = _is_ancestor(current_sha, sha)
            entry["can_rollback"] = _is_ancestor(sha, current_sha)
        decorated.append(entry)
    return decorated


def get_code_update_snapshot(refresh=False):
    status = _read_json(STATUS_FILE, _status_base())
    snapshot = {
        "git_repo": False,
        "current_sha": None,
        "current_short": None,
        "current_version": None,
        "branch": None,
        "origin": None,
        "configured_origin": None,
        "configured_branch": None,
        "git_username": None,
        "git_token_configured": False,
        "remote_sha": status.get("last_remote_sha"),
        "remote_short": (status.get("last_remote_sha") or "")[:12] or None,
        "last_remote_check_at": status.get("last_remote_check_at"),
        "remote_targets": status.get("remote_targets") or [],
        "update_targets": [],
        "rollback_targets": [],
        "dirty": False,
        "tracked_changes": [],
        "test_overlay_active": False,
        "test_overlay_files": [],
        "blocking_tracked_files": [],
        "worker_running": False,
        "deployment_active": status.get("phase") in {"queued", "running"},
        "state": "unavailable",
        "behind": None,
        "ahead": None,
        "update_allowed": False,
        "rollback_allowed": False,
        "previous_sha": status.get("previous_sha"),
        "previous_short": (status.get("previous_sha") or "")[:12] or None,
        "last_phase": status.get("phase"),
        "last_action": status.get("action"),
        "last_result": status.get("result"),
        "last_message": status.get("message"),
        "last_timestamp": status.get("timestamp"),
        "history": _read_history(limit=10),
    }

    settings = load_git_settings()
    snapshot["configured_origin"] = _sanitize_remote_url(settings.get("origin_url"))
    snapshot["configured_branch"] = settings.get("branch")
    snapshot["git_username"] = settings.get("username")
    snapshot["git_token_configured"] = settings.get("token_configured", False)

    try:
        snapshot["worker_running"] = _service_running()
    except CodeUpdateError:
        snapshot["worker_running"] = False

    try:
        if not _is_git_repo():
            return snapshot

        snapshot["git_repo"] = True
        snapshot["current_sha"] = _current_sha()
        snapshot["current_short"] = snapshot["current_sha"][:12]
        snapshot["branch"] = _configured_branch()
        snapshot["origin"] = _sanitize_remote_url(_configured_origin())
        tracked_state = _tracked_change_state()
        snapshot["tracked_changes"] = tracked_state["changed"]
        snapshot["test_overlay_files"] = tracked_state["test_overlay"]
        snapshot["test_overlay_active"] = bool(tracked_state["test_overlay"])
        snapshot["blocking_tracked_files"] = tracked_state["blocking"]
        snapshot["dirty"] = bool(tracked_state["blocking"])

        if refresh:
            snapshot["remote_sha"] = refresh_remote()
            snapshot["remote_short"] = snapshot["remote_sha"][:12]
            refreshed_status = _read_json(STATUS_FILE, {})
            snapshot["last_remote_check_at"] = refreshed_status.get("last_remote_check_at")
            snapshot["remote_targets"] = refreshed_status.get("remote_targets") or []

        remote_sha = snapshot.get("remote_sha")
        current_sha = snapshot.get("current_sha")
        targets = _decorate_targets(snapshot.get("remote_targets"), current_sha)
        snapshot["remote_targets"] = targets
        snapshot["update_targets"] = [item for item in targets if item.get("can_update")]
        snapshot["rollback_targets"] = [item for item in targets if item.get("can_rollback")]
        for item in targets:
            if item.get("is_current"):
                snapshot["current_version"] = item.get("label")
                break

        deployment_active = snapshot["worker_running"] or status.get("phase") in {"queued", "running"}
        snapshot["deployment_active"] = deployment_active

        if deployment_active:
            snapshot["state"] = "running"
        elif snapshot["dirty"]:
            snapshot["state"] = "dirty"
        elif not remote_sha or not _commit_exists(remote_sha):
            snapshot["state"] = "not_checked"
        elif current_sha == remote_sha:
            snapshot["state"] = "up_to_date"
            snapshot["behind"] = 0
            snapshot["ahead"] = 0
        else:
            snapshot["behind"] = _rev_count(f"{current_sha}..{remote_sha}")
            snapshot["ahead"] = _rev_count(f"{remote_sha}..{current_sha}")
            if _is_ancestor(current_sha, remote_sha):
                snapshot["state"] = "update_available"
            elif _is_ancestor(remote_sha, current_sha):
                snapshot["state"] = "local_ahead"
            else:
                snapshot["state"] = "diverged"

        snapshot["update_allowed"] = (
            bool(snapshot["update_targets"])
            and not snapshot["dirty"]
            and not deployment_active
        )
        snapshot["rollback_allowed"] = (
            bool(snapshot["rollback_targets"])
            and not snapshot["dirty"]
            and not deployment_active
        )
        return snapshot
    except CodeUpdateError:
        snapshot["state"] = "error"
        return snapshot


def queue_code_action(action, target_ref):
    if action not in {"update", "rollback"}:
        raise CodeUpdateError("invalid_action")

    if _service_running():
        raise CodeUpdateError("update_already_running")

    target_ref = (target_ref or "").strip()
    if not target_ref:
        raise CodeUpdateError("target_missing")
    _target_by_key(target_ref)

    request_payload = {
        "action": action,
        "target_ref": target_ref,
        "requested_at": _utc_now(),
    }
    _atomic_write_json(REQUEST_FILE, request_payload)
    _write_status(
        phase="queued",
        action=action,
        result=None,
        message="queued",
        target_ref=target_ref,
    )

    result = _run(
        ["sudo", "-n", SYSTEMCTL, "--no-block", "start", UPDATE_SERVICE],
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        try:
            REQUEST_FILE.unlink()
        except OSError:
            pass
        _write_status(
            phase="idle",
            action=action,
            result="failed",
            message="update_service_start_failed",
            target_ref=target_ref,
        )
        raise CodeUpdateError("update_service_start_failed")


def _restart_web_service(def _restart_web_service():
    result = _run(
        ["sudo", "-n", SYSTEMCTL, "restart", WEB_SERVICE],
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise CodeUpdateError("web_restart_failed")


def _health_check(timeout_seconds=30):
    deadline = time.monotonic() + timeout_seconds
    last_error = None
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request(HEALTH_URL, headers={"User-Agent": "EVEOSINT-CodeUpdater/1.0"})
            with urllib.request.urlopen(request, timeout=3) as response:
                if 200 <= response.status < 400:
                    return True
        except Exception as exc:
            last_error = exc
        time.sleep(1)
    if last_error:
        raise CodeUpdateError("health_check_failed") from last_error
    raise CodeUpdateError("health_check_failed")


def _run_post_checkout_steps():
    python_bin = REPO_ROOT / "venv" / "bin" / "python"
    if not python_bin.exists():
        raise CodeUpdateError("venv_python_missing")

    _run(
        [str(python_bin), "-m", "compileall", "-q", "web/app", "scripts"],
        cwd=REPO_ROOT,
        timeout=120,
    )

    requirements = REPO_ROOT / "requirements.txt"
    if requirements.exists():
        pip_bin = REPO_ROOT / "venv" / "bin" / "pip"
        if not pip_bin.exists():
            raise CodeUpdateError("venv_pip_missing")
        _run(
            [str(pip_bin), "install", "-r", str(requirements)],
            cwd=REPO_ROOT,
            timeout=600,
        )

    setup_acl = REPO_ROOT / "web" / "setup_acl.py"
    if setup_acl.exists():
        _run(
            [str(python_bin), str(setup_acl)],
            cwd=REPO_ROOT / "web",
            timeout=120,
        )


def _rollback_to(sha, test_overlay=None):
    _git(["reset", "--hard", sha], timeout=60)
    if test_overlay:
        _restore_test_overlay(test_overlay)
    _run_post_checkout_steps()
    _restart_web_service()
    _health_check()


def _perform_action(action, target_ref):
    if not _is_git_repo():
        raise CodeUpdateError("git_repository_not_initialized")

    tracked_state = _tracked_change_state()
    if tracked_state["blocking"]:
        raise CodeUpdateError("tracked_local_changes")

    test_overlay = _capture_test_overlay(tracked_state["test_overlay"])
    previous_sha = _current_sha()
    status_before = _read_json(STATUS_FILE, {})

    target, target_sha = _fetch_remote_target(target_ref)
    if target_sha == previous_sha:
        raise CodeUpdateError("target_is_current")

    if action == "update":
        if not _is_ancestor(previous_sha, target_sha):
            raise CodeUpdateError("target_not_forward")
    elif action == "rollback":
        if not _is_ancestor(target_sha, previous_sha):
            raise CodeUpdateError("target_not_rollback")
    else:
        raise CodeUpdateError("invalid_action")

    restore_overlay, overlay_conflicts = _overlay_restore_plan(
        previous_sha,
        target_sha,
        test_overlay,
    )
    if overlay_conflicts:
        raise CodeUpdateError("test_overlay_conflicts_remote")

    _write_status(
        phase="running",
        action=action,
        result=None,
        message="deploying",
        from_sha=previous_sha,
        target_sha=target_sha,
        target_ref=target_ref,
        target_label=target.get("label"),
    )

    changed_checkout = False
    try:
        _git(["reset", "--hard", target_sha], timeout=60)
        changed_checkout = True
        if restore_overlay:
            _restore_test_overlay(restore_overlay)
        _run_post_checkout_steps()
        _restart_web_service()
        _health_check()
    except Exception as exc:
        if changed_checkout:
            try:
                _rollback_to(previous_sha, test_overlay=test_overlay)
            except Exception:
                _write_status(
                    phase="failed",
                    action=action,
                    result="failed",
                    message="deploy_failed_and_rollback_failed",
                    current_sha=_current_sha() if _is_git_repo() else None,
                    previous_sha=status_before.get("previous_sha"),
                    target_ref=target_ref,
                )
                _append_history({
                    "timestamp": _utc_now(),
                    "action": action,
                    "target_ref": target_ref,
                    "target_label": target.get("label"),
                    "from_sha": previous_sha,
                    "to_sha": target_sha,
                    "result": "failed_rollback_failed",
                })
                raise CodeUpdateError("deploy_failed_and_rollback_failed") from exc

            _write_status(
                phase="idle",
                action=action,
                result="failed",
                message="deploy_failed_rolled_back",
                current_sha=previous_sha,
                previous_sha=status_before.get("previous_sha"),
                target_ref=target_ref,
            )
            _append_history({
                "timestamp": _utc_now(),
                "action": action,
                "target_ref": target_ref,
                "target_label": target.get("label"),
                "from_sha": previous_sha,
                "to_sha": target_sha,
                "result": "failed_rolled_back",
            })
            raise CodeUpdateError("deploy_failed_rolled_back") from exc
        raise

    _write_status(
        phase="idle",
        action=action,
        result="success",
        message="deploy_success",
        current_sha=target_sha,
        previous_sha=previous_sha,
        target_sha=target_sha,
        target_ref=target_ref,
        target_label=target.get("label"),
    )
    _append_history({
        "timestamp": _utc_now(),
        "action": action,
        "target_ref": target_ref,
        "target_label": target.get("label"),
        "from_sha": previous_sha,
        "to_sha": target_sha,
        "result": "success",
    })


def run_pending_request():
    _ensure_run_dir()
    with LOCK_FILE.open("a+", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CodeUpdateError("update_already_running") from exc

        request_payload = _read_json(REQUEST_FILE, {})
        action = request_payload.get("action")
        target_ref = request_payload.get("target_ref")
        if action not in {"update", "rollback"} or not target_ref:
            raise CodeUpdateError("no_valid_update_request")

        try:
            REQUEST_FILE.unlink()
        except OSError:
            pass

        try:
            _perform_action(action, target_ref)
        except Exception as exc:
            current = _read_json(STATUS_FILE, {})
            if current.get("phase") in {"queued", "running"}:
                _write_status(
                    phase="failed",
                    action=action,
                    result="failed",
                    message=str(exc) if isinstance(exc, CodeUpdateError) else "deploy_failed",
                    target_ref=target_ref,
                )
            raise


def self_test():def self_test():
    checks = []

    checks.append(("repository_path", REPO_ROOT.exists(), str(REPO_ROOT)))
    git_repo = False
    try:
        git_repo = _is_git_repo()
    except CodeUpdateError:
        git_repo = False
    checks.append(("git_repository", git_repo, "git rev-parse"))

    if git_repo:
        try:
            checks.append(("current_commit", True, _current_sha()))
            checks.append(("branch", True, _current_branch()))
            origin = _configured_origin()
            checks.append(("origin", bool(origin), _sanitize_remote_url(origin) or "missing"))
            checks.append(("tracked_tree_clean", not _tracked_dirty(), "clean required for real update"))
        except CodeUpdateError:
            checks.append(("git_status", False, "failed"))

    for label, command in (
        (
            "sudo_start_update_service",
            ["sudo", "-n", "-l", SYSTEMCTL, "--no-block", "start", UPDATE_SERVICE],
        ),
        (
            "sudo_restart_web_service",
            ["sudo", "-n", "-l", SYSTEMCTL, "restart", WEB_SERVICE],
        ),
    ):
        try:
            result = _run(command, timeout=10, check=False)
            checks.append((label, result.returncode == 0, "sudoers permission"))
        except CodeUpdateError:
            checks.append((label, False, "sudoers permission"))

    ok = True
    for label, passed, detail in checks:
        state = "OK" if passed else "FAIL"
        print(f"[{state}] {label}: {detail}")
        if not passed:
            ok = False
    return 0 if ok else 1


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {"run-request", "self-test"}:
        print("Usage: code_update.py {run-request|self-test}", file=sys.stderr)
        return 2

    command = sys.argv[1]
    try:
        if command == "self-test":
            return self_test()
        run_pending_request()
        return 0
    except CodeUpdateError as exc:
        print(f"Code update failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())