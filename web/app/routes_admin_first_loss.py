"""Experimental first-loss study, kept under Admin Debug."""

import logging
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from .auth import require_login
from .audit import audit_log
from .db import db
from .layout import app_context
from .main_objects import templates
from .jobs import (
    JobError,
    first_loss_progress,
    run_first_loss_study,
    stop_first_loss_study,
    read_job_log,
)

router = APIRouter()
LOG = logging.getLogger(__name__)


def allowed(request, permission):
    user = require_login(request)
    if permission not in user.get("permissions", set()):
        return None
    return user


def report():
    with db() as conn, conn.cursor() as c:
        c.execute("SELECT to_regclass('research.first_loss_runs')")
        if not c.fetchone()[0]:
            return None
        c.execute(
            "SELECT id,status,cutoff,config,summary,error FROM research.first_loss_runs ORDER BY id DESC LIMIT 1"
        )
        r = c.fetchone()
    if not r:
        return None
    return {
        "id": r[0],
        "status": r[1],
        "cutoff": r[2].isoformat(),
        "config": r[3],
        "summary": r[4] if r[1] == "completed" else None,
        "error": r[5],
    }


@router.get("/admin/debug/first-loss", response_class=HTMLResponse)
def page(request: Request):
    user = allowed(request, "admin.jobs.view")
    if user is None:
        return PlainTextResponse("Access denied.", status_code=403)
    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - First-loss return study",
        active_module="admin",
        active_menu_key="admin.debug",
    )
    context["can_run"] = "admin.jobs.run" in user.get("permissions", set())
    return templates.TemplateResponse(
        request=request, name="admin_first_loss.html", context=context
    )


@router.get("/admin/debug/first-loss/status")
def status(request: Request):
    if allowed(request, "admin.jobs.view") is None:
        return JSONResponse({"error": "Access denied."}, status_code=403)
    try:
        return JSONResponse(
            {**first_loss_progress(), "report": report()},
            headers={"Cache-Control": "no-store"},
        )
    except Exception:
        LOG.exception("First-loss status failed")
        return JSONResponse(
            {"error": "Study status could not be loaded."}, status_code=503
        )


@router.post("/admin/debug/first-loss/start")
def start(request: Request, clusters: int = Form(8), rebuild: bool = Form(False)):
    user = allowed(request, "admin.jobs.run")
    if user is None:
        return JSONResponse({"error": "Access denied."}, status_code=403)
    try:
        run_first_loss_study(clusters, rebuild)
        audit_log(
            request,
            "admin_job_run",
            user_id=user["id"],
            username=user["username"],
            target_type="job",
            target_id="analyze_first_losses",
            details=f"clusters={clusters}; rebuild={rebuild}",
        )
        return {"started": True}
    except JobError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)


@router.post("/admin/debug/first-loss/stop")
def stop(request: Request):
    user = allowed(request, "admin.jobs.run")
    if user is None:
        return JSONResponse({"error": "Access denied."}, status_code=403)
    try:
        stop_first_loss_study()
        audit_log(
            request,
            "admin_job_stop",
            user_id=user["id"],
            username=user["username"],
            target_type="job",
            target_id="analyze_first_losses",
        )
        return {"stop_requested": True}
    except JobError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)


@router.get("/admin/debug/first-loss/log", response_class=PlainTextResponse)
def log(request: Request):
    if allowed(request, "admin.jobs.view") is None:
        return PlainTextResponse("Access denied.", status_code=403)
    try:
        return PlainTextResponse(read_job_log("analyze_first_losses", lines=150))
    except JobError:
        return PlainTextResponse("No first-loss study log yet.")
