from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from psycopg2.errors import QueryCanceled

from .auth import has_permission, require_login, require_permission_or_redirect
from .killmail_statistics import monthly_statistics
from .layout import app_context
from .main_objects import templates

router = APIRouter()
PERMISSION = "admin.jobs.view"


@router.get("/admin/killmail-statistics", response_class=HTMLResponse)
def admin_killmail_statistics(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, PERMISSION)
    if redirect:
        return redirect
    context = app_context(request=request, user=user, title="EVEOSINT - Killmail Statistics",
                          active_module="admin", active_menu_key="admin.killmail_statistics")
    context["can_view_forensics"] = has_permission(user, "admin.killmail_forensics.dev")
    return templates.TemplateResponse(request=request, name="admin_killmail_statistics.html", context=context)


@router.get("/admin/killmail-statistics/data")
def admin_killmail_statistics_data(request: Request, year: int | None = Query(None, ge=2003, le=9998)):
    user = require_login(request)
    if not user:
        return JSONResponse({"error": "Please log in to continue."}, status_code=401)
    if not has_permission(user, PERMISSION):
        return JSONResponse({"error": "The admin.jobs.view permission is required."}, status_code=403)
    try:
        return monthly_statistics(year)
    except QueryCanceled:
        return JSONResponse({"error": "Counting this year took too long. Please retry."}, status_code=503)
