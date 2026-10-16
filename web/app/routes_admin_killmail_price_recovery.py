"""Missing item price diagnosis and recovery controls."""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from .db import db
import csv
import io

from .audit import audit_log
from .auth import require_login
from .jobs import (
    JobError,
    killmail_price_recovery_progress,
    run_killmail_price_recovery,
    stop_killmail_price_recovery,
)

router = APIRouter()
LOG = logging.getLogger(__name__)


def allowed(request, permission):
    user = require_login(request)
    return user if permission in user.get("permissions", set()) else None


@router.get("/admin/killmail-price-recovery/status")
def status(request: Request):
    if allowed(request, "admin.jobs.view") is None:
        return JSONResponse({"error": "Access denied."}, status_code=403)
    try:
        return JSONResponse(
            killmail_price_recovery_progress(),
            headers={"Cache-Control": "no-store"},
        )
    except Exception:
        LOG.exception("Price recovery status failed")
        return JSONResponse(
            {"error": "Status unavailable; see the job log."}, status_code=503
        )


@router.post("/admin/killmail-price-recovery/start")
def start(
    request: Request,
):
    user = allowed(request, "admin.jobs.run")
    if user is None:
        return JSONResponse({"error": "Access denied."}, status_code=403)
    try:
        run_killmail_price_recovery()
        audit_log(
            request, "admin_job_run", user_id=user["id"], username=user["username"],
            target_type="job", target_id="recover_killmail_prices",
            details="Diagnose NULL killmail values and read item pages and use 0.01 ISK for missing prices",
        )
        return {"started": True}
    except JobError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)


@router.post("/admin/killmail-price-recovery/stop")
def stop(request: Request):
    user = allowed(request, "admin.jobs.run")
    if user is None:
        return JSONResponse({"error": "Access denied."}, status_code=403)
    try:
        stop_killmail_price_recovery()
        audit_log(
            request, "admin_job_stop", user_id=user["id"], username=user["username"],
            target_type="job", target_id="recover_killmail_prices",
        )
        return {"stop_requested": True}
    except JobError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)


@router.get("/admin/killmail-price-recovery/report.csv")
def report(request: Request):
    if allowed(request, "admin.jobs.view") is None:
        return JSONResponse({"error": "Access denied."}, status_code=403)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['UTC day', 'Item ID', 'Item name', 'Reason', 'Explanation', 'Status', 'Unit price ISK', 'Source kill', 'Price source', 'Source price date', 'Last error'])
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('market.killmail_price_gaps')")
        if cur.fetchone()[0]:
            cur.execute("""SELECT price_date,item_type_id,item_name,reason,detail,status,unit_price_isk,
                           source_killmail_id,to_jsonb(g)->>'source_kind',to_jsonb(g)->>'source_price_date',last_error FROM market.killmail_price_gaps g
                           WHERE active ORDER BY price_date,item_type_id""")
            for row in cur:
                # Prevent spreadsheet formula evaluation in externally sourced names/errors.
                writer.writerow([("'" + x) if isinstance(x, str) and x.startswith(('=', '+', '-', '@')) else x for x in row])
    return Response(output.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": 'attachment; filename="killmail-price-gaps.csv"', "Cache-Control": "no-store"})
