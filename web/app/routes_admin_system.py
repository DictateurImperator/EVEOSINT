from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .auth import require_login, require_permission_or_redirect
from .layout import app_context
from .main_objects import templates
from .system_info import get_system_snapshot

router = APIRouter()


@router.get("/admin/system", response_class=HTMLResponse)
def admin_system(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.system.view")
    if redirect:
        return redirect

    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - Admin System",
        active_module="admin",
        active_menu_key="admin.system",
    )
    context.update({"system": get_system_snapshot()})

    return templates.TemplateResponse(request=request, name="admin_system.html", context=context)
