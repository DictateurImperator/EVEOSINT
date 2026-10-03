from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .audit import audit_log
from .auth import require_login, require_permission_or_redirect
from .layout import app_context
from .main_objects import templates
from .nginx_config import (
    NginxConfigError,
    get_nginx_snapshot,
    queue_nginx_save,
)
from .system_info import get_system_snapshot

router = APIRouter()


@router.get("/admin/system", response_class=HTMLResponse)
def admin_system(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.system.view")
    if redirect:
        return redirect

    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - Admin System",
        active_module="admin",
        active_menu_key="admin.system",
    )
    context.update({"system": get_system_snapshot()})

    return templates.TemplateResponse(request=request, name="admin_system.html", context=context)



def _nginx_redirect(*, success=None, error=None):
    if success:
        return RedirectResponse(url=f"/admin/nginx?success={success}", status_code=302)
    return RedirectResponse(url=f"/admin/nginx?error={error or 'action_failed'}", status_code=302)


@router.get("/admin/nginx", response_class=HTMLResponse)
def admin_nginx(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.system.view")
    if redirect:
        return redirect

    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - Admin Nginx",
        active_module="admin",
        active_menu_key="admin.system",
    )
    context.update({
        "nginx": get_nginx_snapshot(),
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })
    return templates.TemplateResponse(request=request, name="admin_nginx.html", context=context)


@router.get("/admin/nginx/status")
def admin_nginx_status(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.system.view")
    if redirect:
        return JSONResponse({"error": "forbidden"}, status_code=403)

    snapshot = get_nginx_snapshot()
    return JSONResponse({
        "worker_running": bool(snapshot.get("worker_running")),
        "phase": snapshot.get("phase"),
        "result": snapshot.get("result"),
        "message": snapshot.get("message"),
        "timestamp": snapshot.get("timestamp"),
        "backup_path": snapshot.get("backup_path"),
        "nginx_test_output": snapshot.get("nginx_test_output"),
    })


@router.post("/admin/nginx/save")
def admin_nginx_save(
    request: Request,
    config_content: str = Form(...),
):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.system.view")
    if redirect:
        return redirect

    try:
        request_id = queue_nginx_save(
            config_content,
            requested_by=user.get("username") or "",
        )
        audit_log(
            request,
            "admin_nginx_config_save",
            user_id=user["id"],
            username=user["username"],
            target_type="nginx_config",
            target_id=request_id,
            details="nginx_config_save_queued",
        )
    except NginxConfigError as exc:
        audit_log(
            request,
            "admin_nginx_config_save_failed",
            user_id=user["id"],
            username=user["username"],
            target_type="nginx_config",
            details=str(exc),
        )
        return _nginx_redirect(error=str(exc))

    return _nginx_redirect(success="save_started")
