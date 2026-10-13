"""Manual historical zKill valuation import controls."""

import logging
from fastapi import APIRouter, Request, Form
from fastapi.responses import JSONResponse, PlainTextResponse
from .auth import require_login
from .audit import audit_log
from .db import db
from .layout import app_context
from .main_objects import templates
from .jobs import JobError, run_zkill_prices, stop_zkill_prices, zkill_prices_progress

router = APIRouter()
LOG = logging.getLogger(__name__)


def allowed(request, permission):
    user = require_login(request)
    return user if permission in user.get("permissions", set()) else None


def summary():
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('market.zkill_daily_prices')")
        if not cur.fetchone()[0]:
            return None
        cur.execute(
            """SELECT count(*), count(*) FILTER (WHERE status='empty'),
            coalesce(sum(item_count),0), min(price_date), max(price_date),
            pg_total_relation_size('market.zkill_daily_prices') FROM market.zkill_daily_prices"""
        )
        days, empty, items, first, last, size = cur.fetchone()
    return {
        "days": days,
        "empty": empty,
        "items": int(items),
        "first": first.isoformat() if first else None,
        "last": last.isoformat() if last else None,
        "bytes": size,
    }


@router.get("/admin/prices")
def page(request: Request):
    user = allowed(request, "admin.jobs.view")
    if user is None:
        return PlainTextResponse("Access denied.", status_code=403)
    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - Historical item prices",
        active_module="admin",
        active_menu_key="admin.prices",
    )
    context["can_run"] = "admin.jobs.run" in user.get("permissions", set())
    return templates.TemplateResponse(
        request=request, name="admin_prices.html", context=context
    )


@router.get("/admin/prices/status")
def status(request: Request):
    if allowed(request, "admin.jobs.view") is None:
        return JSONResponse({"error": "Access denied."}, status_code=403)
    try:
        return JSONResponse(
            {**zkill_prices_progress(), "summary": summary()},
            headers={"Cache-Control": "no-store"},
        )
    except Exception:
        LOG.exception("Price import status failed")
        return JSONResponse(
            {"error": "Status unavailable; see the job log."}, status_code=503
        )


@router.post("/admin/prices/start")
def start(request: Request, date_from: str = Form(""), date_to: str = Form("")):
    user = allowed(request, "admin.jobs.run")
    if user is None:
        return JSONResponse({"error": "Access denied."}, status_code=403)
    try:
        run_zkill_prices(date_from, date_to)
        audit_log(
            request,
            "admin_job_run",
            user_id=user["id"],
            username=user["username"],
            target_type="job",
            target_id="import_zkill_prices",
            details=f"from={date_from}; through={date_to}",
        )
        return {"started": True}
    except JobError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)


@router.post("/admin/prices/stop")
def stop(request: Request):
    user = allowed(request, "admin.jobs.run")
    if user is None:
        return JSONResponse({"error": "Access denied."}, status_code=403)
    try:
        stop_zkill_prices()
        audit_log(
            request,
            "admin_job_stop",
            user_id=user["id"],
            username=user["username"],
            target_type="job",
            target_id="import_zkill_prices",
        )
        return {"stop_requested": True}
    except JobError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
