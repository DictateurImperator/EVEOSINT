import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from psycopg2.errors import QueryCanceled

from .auth import has_permission, require_login, require_permission_or_redirect
from .entities import EntityError, get_hidden_killmails_page
from .killmail_filters import killmail_filters_from_request, search_killboard_filters
from .layout import app_context
from .main_objects import templates

router = APIRouter()
logger = logging.getLogger(__name__)
PERMISSION = "admin.killmail_forensics.dev"


def _data_access_error(request):
    user = require_login(request)
    if not user:
        return JSONResponse({"error": "Please log in to continue."}, status_code=401)
    if not has_permission(user, PERMISSION):
        return JSONResponse({"error": "The Killmail Forensics dev permission is required."}, status_code=403)
    return None


@router.get("/admin/killmail-forensics", response_class=HTMLResponse)
def admin_killmail_forensics(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, PERMISSION)
    if redirect:
        return redirect

    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - Killmail Forensics",
        active_module="admin",
        active_menu_key="admin.killmail_forensics",
    )
    context.update({
        "killboard_search_url": "/admin/killmail-forensics/search",
        "killboard_entity_placeholder": "Alliance, corporation…",
    })
    return templates.TemplateResponse(
        request=request, name="admin_killmail_forensics.html", context=context,
    )


@router.get("/admin/killmail-forensics/data", response_class=JSONResponse)
def admin_killmail_forensics_data(request: Request, limit: int = Query(100, ge=1, le=100)):
    denied = _data_access_error(request)
    if denied is not None:
        return denied
    try:
        page = get_hidden_killmails_page(
            per_page=limit, filters=killmail_filters_from_request(request),
        )
    except (EntityError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except QueryCanceled:
        return JSONResponse({"error": "The query time limit was reached. Narrow the filters or retry."}, status_code=503)
    except Exception:
        logger.exception("Hidden killmail search failed")
        return JSONResponse({"error": "Hidden killmails could not be loaded. Please retry."}, status_code=500)
    html = templates.env.get_template("entity_killmails_fragment.html").render(
        request=request, killmail_page=page, killmail_mode="hidden",
        killmail_base_url=None, killmail_selectable=True, error=None,
    )
    return JSONResponse({"html": html, "count": len(page["killmails"])})


@router.get("/admin/killmail-forensics/search", response_class=JSONResponse)
def admin_killmail_forensics_search(
    request: Request, kind: str = Query(...), q: str = Query(""), limit: int = Query(15),
):
    denied = _data_access_error(request)
    if denied is not None:
        return denied
    if kind not in {"ship", "entity", "zone"}:
        return JSONResponse({"error": "Invalid search kind.", "results": []}, status_code=400)
    return JSONResponse({"results": search_killboard_filters(kind, q, limit, mer_only=True)})
