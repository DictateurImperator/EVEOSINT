"""Manual in-place killmail valuation controls."""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .audit import audit_log
from .auth import require_login
from .jobs import (
    JobError,
    killmail_values_progress,
    run_killmail_values,
    stop_killmail_values,
)

router = APIRouter()
LOG = logging.getLogger(__name__)


def allowed(request, permission):
    user = require_login(request)
    return user if permission in user.get("permissions", set()) else None


@router.get("/admin/killmail-values/status")
def status(request: Request):
    if allowed(request, "admin.jobs.view") is None:
        return JSONResponse({"error": "Access denied."}, status_code=403)
    try:
        return JSONResponse(
            killmail_values_progress(),
            headers={"Cache-Control": "no-store"},
        )
    except Exception:
        LOG.exception("Killmail valuation status failed")
        return JSONResponse(
            {"error": "Status unavailable; see the job log."}, status_code=503
        )


@router.post("/admin/killmail-values/start")
def start(
    request: Request,
):
    user = allowed(request, "admin.jobs.run")
    if user is None:
        return JSONResponse({"error": "Access denied."}, status_code=403)
    try:
        run_killmail_values()
        audit_log(
            request, "admin_job_run", user_id=user["id"], username=user["username"],
            target_type="job", target_id="calculate_killmail_values",
            details="estimated_value_cents IS NULL",
        )
        return {"started": True}
    except JobError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)


@router.post("/admin/killmail-values/stop")
def stop(request: Request):
    user = allowed(request, "admin.jobs.run")
    if user is None:
        return JSONResponse({"error": "Access denied."}, status_code=403)
    try:
        stop_killmail_values()
        audit_log(
            request, "admin_job_stop", user_id=user["id"], username=user["username"],
            target_type="job", target_id="calculate_killmail_values",
        )
        return {"stop_requested": True}
    except JobError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
