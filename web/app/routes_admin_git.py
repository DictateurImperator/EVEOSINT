from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .audit import audit_log
from .auth import require_login, require_permission_or_redirect
from .code_update import (
    CodeUpdateError,
    get_code_update_snapshot,
    load_git_settings,
    queue_code_action,
    refresh_remote,
    save_git_settings,
    test_git_access,
)
from .layout import app_context
from .main_objects import templates

router = APIRouter()


def _git_redirect(*, success=None, error=None):
    if success:
        return RedirectResponse(url=f"/admin/git?success={success}", status_code=302)
    return RedirectResponse(url=f"/admin/git?error={error or 'action_failed'}", status_code=302)


@router.get("/admin/git", response_class=HTMLResponse)
def admin_git(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.system.view")
    if redirect:
        return redirect

    git_settings = load_git_settings()
    git_settings.pop("token", None)

    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - Admin Git",
        active_module="admin",
        active_menu_key="admin.git",
    )
    context.update({
        "code_update": get_code_update_snapshot(),
        "git_settings": git_settings,
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })
    return templates.TemplateResponse(request=request, name="admin_git.html", context=context)


@router.get("/admin/git/status")
def admin_git_status(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.system.view")
    if redirect:
        return JSONResponse({"error": "forbidden"}, status_code=403)

    snapshot = get_code_update_snapshot()
    return JSONResponse({
        "deployment_active": bool(snapshot.get("deployment_active")),
        "worker_running": bool(snapshot.get("worker_running")),
        "state": snapshot.get("state"),
        "current_sha": snapshot.get("current_sha"),
        "remote_sha": snapshot.get("remote_sha"),
        "previous_sha": snapshot.get("previous_sha"),
        "last_phase": snapshot.get("last_phase"),
        "last_action": snapshot.get("last_action"),
        "last_result": snapshot.get("last_result"),
        "last_message": snapshot.get("last_message"),
        "last_timestamp": snapshot.get("last_timestamp"),
    })


@router.post("/admin/git/config")
def admin_git_config(
    request: Request,
    origin_url: str = Form(...),
    branch: str = Form("main"),
    username: str = Form(""),
    token: str = Form(""),
    clear_token: str = Form("off"),
):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.system.view")
    if redirect:
        return redirect

    try:
        settings = save_git_settings(
            origin_url=origin_url,
            branch=branch,
            username=username,
            token=token,
            clear_token=(clear_token == "on"),
        )
        audit_log(
            request,
            "admin_git_config_saved",
            user_id=user["id"],
            username=user["username"],
            target_type="git_config",
            target_id=settings.get("branch"),
            details="git_config_saved",
        )
    except CodeUpdateError:
        return _git_redirect(error="config_failed")

    return _git_redirect(success="config_saved")


@router.post("/admin/git/test")
def admin_git_test(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.system.view")
    if redirect:
        return redirect

    try:
        test_git_access()
        audit_log(
            request,
            "admin_git_access_test",
            user_id=user["id"],
            username=user["username"],
            target_type="git_config",
            details="access_ok",
        )
    except CodeUpdateError as exc:
        audit_log(
            request,
            "admin_git_access_test_failed",
            user_id=user["id"],
            username=user["username"],
            target_type="git_config",
            details=str(exc),
        )
        return _git_redirect(error="access_test_failed")

    return _git_redirect(success="access_ok")


@router.post("/admin/git/check")
def admin_git_check(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.system.view")
    if redirect:
        return redirect

    try:
        remote_sha = refresh_remote()
        audit_log(
            request,
            "admin_code_update_check",
            user_id=user["id"],
            username=user["username"],
            target_type="code_update",
            target_id=remote_sha[:12],
            details="remote_checked",
        )
    except CodeUpdateError as exc:
        audit_log(
            request,
            "admin_code_update_check_failed",
            user_id=user["id"],
            username=user["username"],
            target_type="code_update",
            details=str(exc),
        )
        return _git_redirect(error="check_failed")

    return _git_redirect(success="checked")


@router.post("/admin/git/update")
def admin_git_update(request: Request, target_ref: str = Form(...)):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.system.view")
    if redirect:
        return redirect

    snapshot = get_code_update_snapshot()
    allowed_targets = {item.get("key") for item in snapshot.get("update_targets", [])}
    if not snapshot.get("update_allowed") or target_ref not in allowed_targets:
        return _git_redirect(error="update_not_allowed")

    try:
        queue_code_action("update", target_ref)
        audit_log(
            request,
            "admin_code_update_start",
            user_id=user["id"],
            username=user["username"],
            target_type="code_update",
            target_id=target_ref,
            details="update_queued",
        )
    except CodeUpdateError as exc:
        audit_log(
            request,
            "admin_code_update_start_failed",
            user_id=user["id"],
            username=user["username"],
            target_type="code_update",
            target_id=target_ref,
            details=str(exc),
        )
        return _git_redirect(error="update_start_failed")

    return _git_redirect(success="update_started")


@router.post("/admin/git/deploy")
def admin_git_deploy(request: Request, target_ref: str = Form(...)):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.system.view")
    if redirect:
        return redirect

    snapshot = get_code_update_snapshot()
    allowed_targets = {item.get("key") for item in snapshot.get("deploy_targets", [])}
    if not snapshot.get("deploy_allowed") or target_ref not in allowed_targets:
        return _git_redirect(error="deploy_not_allowed")

    try:
        queue_code_action("deploy", target_ref)
        audit_log(
            request,
            "admin_code_branch_deploy",
            user_id=user["id"],
            username=user["username"],
            target_type="code_update",
            target_id=target_ref,
            details="branch_deploy_queued",
        )
    except CodeUpdateError as exc:
        audit_log(
            request,
            "admin_code_branch_deploy_failed",
            user_id=user["id"],
            username=user["username"],
            target_type="code_update",
            target_id=target_ref,
            details=str(exc),
        )
        return _git_redirect(error="deploy_start_failed")

    return _git_redirect(success="deploy_started")


@router.post("/admin/git/rollback")
def admin_git_rollback(request: Request, target_ref: str = Form(...)):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.system.view")
    if redirect:
        return redirect

    snapshot = get_code_update_snapshot()
    allowed_targets = {item.get("key") for item in snapshot.get("rollback_targets", [])}
    if not snapshot.get("rollback_allowed") or target_ref not in allowed_targets:
        return _git_redirect(error="rollback_not_allowed")

    try:
        queue_code_action("rollback", target_ref)
        audit_log(
            request,
            "admin_code_update_rollback",
            user_id=user["id"],
            username=user["username"],
            target_type="code_update",
            target_id=target_ref,
            details="rollback_queued",
        )
    except CodeUpdateError as exc:
        audit_log(
            request,
            "admin_code_update_rollback_failed",
            user_id=user["id"],
            username=user["username"],
            target_type="code_update",
            target_id=target_ref,
            details=str(exc),
        )
        return _git_redirect(error="rollback_start_failed")

    return _git_redirect(success="rollback_started")
