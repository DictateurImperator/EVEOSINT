from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from .audit import audit_log
from .auth import require_login, require_permission_or_redirect
from .layout import app_context
from .main_objects import templates
from .update_pipeline import (
    UpdatePipelineError,
    get_pipeline_status,
    read_pipeline_log,
    start_update_pipeline,
)


router = APIRouter()


def _redirect_error(code):
    return RedirectResponse(url=f"/admin/update-pipeline?error={code}", status_code=302)


@router.get("/admin/update-pipeline", response_class=HTMLResponse)
def admin_update_pipeline(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.jobs.view")
    if redirect:
        return redirect

    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - Update Pipeline",
        active_module="admin",
        active_menu_key="admin.update_pipeline",
    )
    context.update({
        "pipeline": get_pipeline_status(include_history=True),
        "can_run": "admin.jobs.run" in user.get("permissions", set()),
        "error": request.query_params.get("error"),
        "success": request.query_params.get("success"),
    })
    return templates.TemplateResponse(
        request=request,
        name="admin_update_pipeline.html",
        context=context,
    )


@router.get("/admin/update-pipeline/api/status")
def admin_update_pipeline_status(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.jobs.view")
    if redirect:
        return redirect
    return JSONResponse(get_pipeline_status(include_history=True, include_next_plan=False))


@router.get("/admin/update-pipeline/api/log")
def admin_update_pipeline_log(request: Request, run_id: str | None = None, lines: int = 800):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.jobs.view")
    if redirect:
        return redirect
    try:
        content = read_pipeline_log(run_id=run_id, lines=lines)
    except UpdatePipelineError as exc:
        return PlainTextResponse(str(exc), status_code=404)
    return PlainTextResponse(content, media_type="text/plain; charset=utf-8")


@router.post("/admin/update-pipeline/run/weekly")
def admin_update_pipeline_run_weekly(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.jobs.run")
    if redirect:
        return redirect
    try:
        pid = start_update_pipeline(mode="weekly", trigger="admin")
        audit_log(
            request,
            "admin_update_pipeline_run",
            user_id=user["id"],
            username=user["username"],
            target_type="update_pipeline",
            target_id="weekly",
            details=f"started pid={pid}",
        )
    except UpdatePipelineError as exc:
        audit_log(
            request,
            "admin_update_pipeline_run_failed",
            user_id=user["id"],
            username=user["username"],
            target_type="update_pipeline",
            target_id="weekly",
            details=str(exc),
        )
        code = "already_running" if str(exc) == "pipeline_already_running" else "start_failed"
        return _redirect_error(code)
    return RedirectResponse(url="/admin/update-pipeline?success=weekly_started", status_code=302)
