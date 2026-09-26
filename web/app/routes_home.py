from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .auth import require_login, require_permission_or_redirect
from .layout import app_context
from .main_objects import templates

router = APIRouter()


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
