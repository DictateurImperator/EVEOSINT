"""Manual in-place killmail valuation controls."""

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from .audit import audit_log
from .auth import require_login
from .db import db
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


def summary():
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('market.killmail_valuation_months')")
        if not cur.fetchone()[0]:
            return None
        cur.execute(
            """SELECT count(*),coalesce(sum(killmails_total),0),
                      coalesce(sum(killmails_valued),0),
                      coalesce(sum(fallback_price_pairs),0),max(calculated_at)
               FROM market.killmail_valuation_months"""
        )
        months, total, valued, fallback, updated = cur.fetchone()
    return {
        "months": int(months), "killmails": int(total), "valued": int(valued),
        "fallback_price_pairs": int(fallback),
        "updated_at": updated.isoformat() if updated else None,
    }


@router.get("/admin/killmail-values/status")
def status(request: Request):
    if allowed(request, "admin.jobs.view") is None:
        return JSONResponse({"error": "Access denied."}, status_code=403)
    try:
        return JSONResponse(
            {**killmail_values_progress(), "summary": summary()},
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
    month_from: str = Form(""),
    month_to: str = Form(""),
    recalculate: str = Form(""),
):
    user = allowed(request, "admin.jobs.run")
    if user is None:
        return JSONResponse({"error": "Access denied."}, status_code=403)
    try:
        replace = recalculate == "1"
        run_killmail_values(month_from, month_to, replace)
        audit_log(
            request, "admin_job_run", user_id=user["id"], username=user["username"],
            target_type="job", target_id="calculate_killmail_values",
            details=f"from={month_from}; through={month_to}; recalculate={replace}",
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
