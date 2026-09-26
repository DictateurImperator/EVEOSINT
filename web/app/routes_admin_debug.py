from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from .audit import audit_log
from .auth import require_login, require_permission_or_redirect
from .debug_runs import (
    DebugRunError,
    cleanup_debug_data,
    get_debug_status,
    read_debug_log,
    start_affiliation_debug,
    stop_debug_run,
)
from .layout import app_context
from .main_objects import templates

router = APIRouter()


def _redirect(code, kind="error"):
    return RedirectResponse(url=f"/admin/debug?{kind}={code}", status_code=302)


@router.get("/admin/debug", response_class=HTMLResponse)
def admin_debug(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.jobs.view")
    if redirect:
        return redirect
    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - Debug",
        active_module="admin",
        active_menu_key="admin.debug",
    )
    context.update({
        "debug": get_debug_status(include_history=True),
        "can_run": "admin.jobs.run" in user.get("permissions", set()),
        "error": request.query_params.get("error"),
        "success": request.query_params.get("success"),
    })
    return templates.TemplateResponse(request=request, name="admin_debug.html", context=context)


@router.get("/admin/debug/api/status")
def admin_debug_status(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.jobs.view")
    if redirect:
        return redirect
    return JSONResponse(get_debug_status(include_history=True))


@router.get("/admin/debug/api/log")
def admin_debug_log(request: Request, run_id: str | None = None, lines: int = 800):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.jobs.view")
    if redirect:
        return redirect
    try:
        content = read_debug_log(run_id=run_id, lines=lines)
    except DebugRunError as exc:
        return PlainTextResponse(str(exc), status_code=404)
    return PlainTextResponse(content, media_type="text/plain; charset=utf-8")


@router.post("/admin/debug/run/affiliation-1-4")
def admin_debug_run_affiliation(
    request: Request,
    source_table: str = Form("entities.recent_killmail_pilots"),
    source_column: str = Form("character_id"),
    limit: str = Form(""),
    workers: int = Form(4),
    batch_size: int = Form(999),
    rate_limit: int = Form(280),
    store_results: str | None = Form(None),
    use_etag_cache: str | None = Form(None),
    skip_bulk: str | None = Form(None),
):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.jobs.run")
    if redirect:
        return redirect
    parsed_limit = None
    if str(limit).strip():
        parsed_limit = int(str(limit).strip())
    try:
        pid = start_affiliation_debug(
            source_table=source_table,
            source_column=source_column,
            limit=parsed_limit,
            workers=workers,
            batch_size=batch_size,
            esi_max_calls_per_minute=rate_limit,
            store_results=store_results is not None,
            use_etag_cache=use_etag_cache is not None,
            skip_bulk=skip_bulk is not None,
        )
        audit_log(
            request,
            "admin_debug_run",
            user_id=user["id"],
            username=user["username"],
            target_type="debug_affiliation_steps_1_4",
            target_id="optimized",
            details=f"started pid={pid} source={source_table}.{source_column} limit={parsed_limit}",
        )
    except (DebugRunError, ValueError) as exc:
        code = "already_running" if str(exc) == "debug_already_running" else "start_failed"
        return _redirect(code)
    return _redirect("started", kind="success")


@router.post("/admin/debug/stop")
def admin_debug_stop(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.jobs.run")
    if redirect:
        return redirect
    try:
        pid = stop_debug_run()
        audit_log(
            request,
            "admin_debug_stop",
            user_id=user["id"],
            username=user["username"],
            target_type="debug_run",
            target_id=str(pid),
            details="SIGTERM requested",
        )
    except DebugRunError:
        return _redirect("stop_failed")
    return _redirect("stop_requested", kind="success")


@router.post("/admin/debug/cleanup")
def admin_debug_cleanup(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.jobs.run")
    if redirect:
        return redirect
    try:
        cleanup_debug_data()
    except DebugRunError:
        return _redirect("cleanup_failed")
    return _redirect("cleaned", kind="success")
