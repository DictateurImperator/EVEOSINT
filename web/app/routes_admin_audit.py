from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from .audit import (
    get_audit_user,
    get_user_actions_since,
    list_audit_users,
    list_recent_actions,
    list_user_actions,
    parse_since_date,
    render_actions_csv,
)
from .auth import require_login, require_permission_or_redirect
from .layout import app_context
from .main_objects import templates

router = APIRouter()


@router.get("/admin/audit", response_class=HTMLResponse)
def admin_audit(
    request: Request,
    users_page_number: int = Query(1, alias="users_page"),
):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.audit.view")
    if redirect:
        return redirect

    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - Admin Audit",
        active_module="admin",
        active_menu_key="admin.audit",
    )
    context.update({
        "users_page": list_audit_users(page=users_page_number, per_page=50),
        "latest_actions": list_recent_actions(limit=50),
    })

    return templates.TemplateResponse(request=request, name="admin_audit.html", context=context)


@router.get("/admin/audit/users/{target_user_id}", response_class=HTMLResponse)
def admin_audit_user(
    request: Request,
    target_user_id: int,
    page: int = 1,
):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.audit.view")
    if redirect:
        return redirect

    target_user = get_audit_user(target_user_id)
    if not target_user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")

    context = app_context(
        request=request,
        user=user,
        title=f"EVEOSINT - Audit {target_user['username']}",
        active_module="admin",
        active_menu_key="admin.audit",
    )
    context.update({
        "target_user": target_user,
        "actions_page": list_user_actions(target_user_id, page=page, per_page=50),
    })

    return templates.TemplateResponse(request=request, name="admin_audit_user.html", context=context)


@router.get("/admin/audit/users/{target_user_id}/download")
def admin_audit_user_download(
    request: Request,
    target_user_id: int,
    since: str,
):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.audit.view")
    if redirect:
        return redirect

    target_user = get_audit_user(target_user_id)
    if not target_user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")

    try:
        since_dt = parse_since_date(since)
    except ValueError:
        raise HTTPException(status_code=400, detail="Date invalide. Format attendu: YYYY-MM-DD")

    rows = get_user_actions_since(target_user_id, since_dt)
    csv_data = render_actions_csv(rows)
    safe_username = "".join(
        char if char.isalnum() or char in ("-", "_") else "_"
        for char in target_user["username"]
    )
    filename = f"audit_{safe_username}_since_{since}.csv"

    return Response(
        content=csv_data,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        },
    )
