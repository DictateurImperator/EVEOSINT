from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .auth import require_login, require_permission_or_redirect
from .layout import app_context
from .main_objects import templates

router = APIRouter()


@router.get("/admin/killmail-forensics", response_class=HTMLResponse)
def admin_killmail_forensics(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.killmail_forensics.dev")
    if redirect:
        return redirect

    return templates.TemplateResponse(
        request=request,
        name="admin_killmail_forensics.html",
        context=app_context(
            request=request,
            user=user,
            title="EVEOSINT - Killmail Forensics",
            active_module="admin",
            active_menu_key="admin.killmail_forensics",
        ),
    )
