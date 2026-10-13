"""Manual research tools and reports under Admin Report."""

import logging
from datetime import datetime, timezone
from typing import Literal
from fastapi import APIRouter, Request, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from .first_loss_pdf import make_report
from .auth import require_login
from .audit import audit_log
from .db import db
from .layout import app_context
from .main_objects import templates
from .jobs import (
    JobError,
    first_loss_progress,
    first_loss_purge_progress,
    run_first_loss_purge,
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


def report(include_model=False, known_updated_at=None):
    if known_updated_at is not None and known_updated_at.tzinfo is None:
        known_updated_at = known_updated_at.replace(tzinfo=timezone.utc)
    with db() as conn, conn.cursor() as c:
        c.execute("SELECT to_regclass('research.first_loss_runs')")
        if not c.fetchone()[0]:
            return None
        c.execute(
            "SELECT id,status,cutoff,config,CASE WHEN updated_at=%s AND status='completed' THEN NULL ELSE summary END,error,updated_at"
            + (",model" if include_model else "")
            + " FROM research.first_loss_runs ORDER BY id DESC LIMIT 1",
            (known_updated_at,),
        )
        r = c.fetchone()
    if not r:
        return None
    result = {
        "id": r[0],
        "status": r[1],
        "cutoff": r[2].isoformat(),
        "config": r[3],
        "summary": r[4] if r[1] == "completed" else None,
        "error": r[5],
        "update_token": r[6].isoformat(),
        "unchanged": r[1] == "completed" and known_updated_at == r[6],
    }
    if include_model:
        result["model"] = r[7]
    return result


@router.get("/admin/report/first-loss/report.pdf")
@router.get("/admin/debug/first-loss/report.pdf")
def download_report(
    request: Request,
    horizon: Literal["all", "7", "30", "90"] = "all",
    year: str = Query("all", pattern=r"^(all|[0-9]{4})$"),
    scope: Literal["all", "pvp", "npc"] = "all",
):
    if allowed(request, "admin.jobs.view") is None:
        return PlainTextResponse("Access denied.", status_code=403)
    try:
        snapshot = report(include_model=True)
        if not snapshot:
            return PlainTextResponse("No study report yet.", status_code=404)
        if snapshot["status"] != "completed" or not snapshot.get("summary"):
            return PlainTextResponse(
                "The study must be completed before export.", status_code=409
            )
        if year != "all" and year not in snapshot["summary"].get("years", {}):
            return PlainTextResponse(
                "Loss cohort not found in this report.", status_code=404
            )
        content = make_report(snapshot, horizon, year, scope)
        filename = f"EVEOSINT_first_loss_run_{int(snapshot['id'])}_{horizon}_{year}_{scope}.pdf"
        return Response(
            content,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )
    except Exception:
        LOG.exception("First-loss PDF export failed")
        return PlainTextResponse("The report could not be exported.", status_code=503)


@router.get("/admin/report", response_class=HTMLResponse)
@router.get("/admin/report/first-loss", response_class=HTMLResponse)
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
        active_menu_key="admin.report",
    )
    context["can_run"] = "admin.jobs.run" in user.get("permissions", set())
    return templates.TemplateResponse(
        request=request, name="admin_first_loss.html", context=context
    )


@router.get("/admin/report/first-loss/status")
@router.get("/admin/debug/first-loss/status")
def status(request: Request, known_updated_at: datetime | None = None):
    if allowed(request, "admin.jobs.view") is None:
        return JSONResponse({"error": "Access denied."}, status_code=403)
    try:
        return JSONResponse(
            {
                **first_loss_progress(),
                "report": report(known_updated_at=known_updated_at),
                "purge": first_loss_purge_progress(),
            },
            headers={"Cache-Control": "no-store"},
        )
    except Exception:
        LOG.exception("First-loss status failed")
        return JSONResponse(
            {"error": "Study status could not be loaded."}, status_code=503
        )


@router.post("/admin/report/first-loss/start")
@router.post("/admin/debug/first-loss/start")
def start(request: Request, clusters: str = Form("auto"), rebuild: bool = Form(False)):
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


@router.post("/admin/report/first-loss/stop")
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


@router.get("/admin/report/first-loss/log", response_class=PlainTextResponse)
@router.get("/admin/debug/first-loss/log", response_class=PlainTextResponse)
def log(request: Request):
    if allowed(request, "admin.jobs.view") is None:
        return PlainTextResponse("Access denied.", status_code=403)
    try:
        return PlainTextResponse(read_job_log("analyze_first_losses", lines=150))
    except JobError:
        return PlainTextResponse("No first-loss study log yet.")


@router.post("/admin/report/first-loss/purge")
def delete_study(request: Request):
    user = allowed(request, "admin.jobs.run")
    if user is None:
        return JSONResponse({"error": "Access denied."}, status_code=403)
    try:
        run_first_loss_purge()
        audit_log(
            request,
            "admin_job_run",
            user_id=user["id"],
            username=user["username"],
            target_type="job",
            target_id="purge_first_loss_study",
            details="Delete generated first-loss research data only",
        )
        return {"started": True}
    except JobError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
