from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import logging

from .coalitions import search_member_entities

from .auth import require_login, require_permission_or_redirect
from .layout import app_context
from .main_objects import templates
from .menus import visible_tools

router = APIRouter()


@router.get("/tools", response_class=HTMLResponse)
def tools(request: Request):
    user = require_login(request)
    items = visible_tools(user)
    if not items:
        return RedirectResponse(url="/home", status_code=302)
    context = app_context(request=request, user=user, title="EVEOSINT - Tools",
                          active_module="tools", active_menu_key="tools")
    context["tools"] = items
    return templates.TemplateResponse(request=request, name="tools.html", context=context)


@router.get("/home", response_class=HTMLResponse)
def home(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    redirect = require_permission_or_redirect(user, "home.view")
    if redirect:
        return redirect

    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context=app_context(
            request=request,
            user=user,
            title="EVEOSINT - Home",
            active_module="home",
            active_menu_key="home",
        ),
    )


@router.get("/tools/population", response_class=HTMLResponse)
@router.get("/tools/economics", response_class=HTMLResponse)
def comparison_tool(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "entities.view")
    if redirect:
        return redirect
    kind = "economics" if request.url.path.endswith("economics") else "population"
    context = app_context(request=request, user=user, title="EVEOSINT - " + kind.title() + " comparison",
                          active_module="tools", active_menu_key="tools." + kind)
    context["comparison_kind"] = kind
    return templates.TemplateResponse(request=request, name="tools_comparison.html", context=context)


@router.get("/api/tools/entity-search")
def tools_entity_search(request: Request, q: str = ""):
    user = require_login(request)
    if "entities.view" not in user.get("permissions", set()):
        return JSONResponse({"error": "Access denied."}, status_code=403)
    try:
        results = search_member_entities(q[:200], limit=50)
        return {"results": [item for item in results if item["entity_type"] in {"alliance", "coalition"}]}
    except Exception:
        logging.getLogger(__name__).exception("Analysis tools entity search failed")
        return JSONResponse({"error": "Entity search could not be loaded."}, status_code=503)
