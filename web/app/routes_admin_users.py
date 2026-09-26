from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .audit import audit_log
from .auth import has_permission, require_login, require_permission_or_redirect
from .layout import app_context
from .main_objects import templates
from .security import validate_password
from .users import (
    count_admin_users,
    create_or_update_user,
    delete_user_by_id,
    get_admin_user_detail,
    is_user_admin,
    list_admin_users,
    list_permission_groups,
    list_roles,
    reset_user_password,
    update_user_account,
    update_user_direct_permissions,
)

router = APIRouter()


@router.get("/admin", response_class=HTMLResponse)
def admin_users(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.users.view")
    if redirect:
        return redirect

    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - Admin Users",
        active_module="admin",
        active_menu_key="admin.users",
    )
    context.update({
        "users": list_admin_users(),
        "roles": list_roles(),
        "can_create_users": has_permission(user, "admin.users.create"),
        "can_delete_users": has_permission(user, "admin.users.delete"),
        "error": request.query_params.get("error"),
        "success": request.query_params.get("success"),
    })

    return templates.TemplateResponse(request=request, name="admin_users.html", context=context)


@router.post("/admin/users/create")
def admin_users_create(request: Request, username: str = Form(...), password: str = Form(...), role_key: str = Form(...)):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.users.create")
    if redirect:
        return redirect

    username = username.strip()
    role_key = role_key.strip().lower()

    if not username:
        return RedirectResponse(url="/admin?error=username_empty", status_code=302)

    password_error = validate_password(password)
    if password_error:
        return RedirectResponse(url=f"/admin?error={password_error}", status_code=302)

    try:
        create_or_update_user(username, password, role_key)
        audit_log(request, "admin_user_saved", user_id=user["id"], username=user["username"], target_type="user", target_id=username)
    except Exception:
        return RedirectResponse(url="/admin?error=create_failed", status_code=302)

    return RedirectResponse(url="/admin?success=user_saved", status_code=302)


@router.get("/admin/users/{target_user_id}", response_class=HTMLResponse)
def admin_user_detail(request: Request, target_user_id: int):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.users.view")
    if redirect:
        return redirect

    target_user = get_admin_user_detail(target_user_id)
    if not target_user:
        return RedirectResponse(url="/admin?error=user_not_found", status_code=302)

    context = app_context(
        request=request,
        user=user,
        title=f"EVEOSINT - User {target_user['username']}",
        active_module="admin",
        active_menu_key="admin.users",
    )
    context.update({
        "target_user": target_user,
        "roles": list_roles(),
        "permission_groups": list_permission_groups(),
        "direct_permissions": target_user["direct_permissions"],
        "effective_permissions": {permission["permission_key"] for permission in target_user["permissions"]},
        "can_edit_users": has_permission(user, "admin.users.edit"),
        "can_delete_users": has_permission(user, "admin.users.delete"),
        "error": request.query_params.get("error"),
        "success": request.query_params.get("success"),
    })

    return templates.TemplateResponse(request=request, name="admin_user_detail.html", context=context)


@router.post("/admin/users/{target_user_id}/update")
def admin_user_update(request: Request, target_user_id: int, role_key: str = Form(...), is_active: str = Form("off")):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.users.edit")
    if redirect:
        return redirect

    role_key = role_key.strip().lower()
    active_value = is_active == "on"

    if target_user_id == user["id"] and not active_value:
        return RedirectResponse(url=f"/admin/users/{target_user_id}?error=cannot_disable_self", status_code=302)

    if is_user_admin(target_user_id) and count_admin_users() <= 1 and role_key != "admin":
        return RedirectResponse(url=f"/admin/users/{target_user_id}?error=cannot_remove_last_admin", status_code=302)

    try:
        update_user_account(target_user_id, active_value, role_key)
        audit_log(request, "admin_user_updated", user_id=user["id"], username=user["username"], target_type="user", target_id=target_user_id)
    except Exception:
        return RedirectResponse(url=f"/admin/users/{target_user_id}?error=update_failed", status_code=302)

    return RedirectResponse(url=f"/admin/users/{target_user_id}?success=user_updated", status_code=302)


@router.post("/admin/users/{target_user_id}/permissions")
async def admin_user_permissions(request: Request, target_user_id: int):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.users.edit")
    if redirect:
        return redirect

    form = await request.form()
    permission_keys = form.getlist("permission_keys")

    try:
        update_user_direct_permissions(target_user_id, permission_keys)
        audit_log(request, "admin_user_permissions_updated", user_id=user["id"], username=user["username"], target_type="user", target_id=target_user_id)
    except Exception:
        return RedirectResponse(url=f"/admin/users/{target_user_id}?error=permissions_update_failed", status_code=302)

    return RedirectResponse(url=f"/admin/users/{target_user_id}?success=permissions_updated", status_code=302)


@router.post("/admin/users/{target_user_id}/password")
def admin_user_password(request: Request, target_user_id: int, password: str = Form(...)):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.users.edit")
    if redirect:
        return redirect

    password_error = validate_password(password)
    if password_error:
        return RedirectResponse(url=f"/admin/users/{target_user_id}?error={password_error}", status_code=302)

    reset_user_password(target_user_id, password)
    audit_log(request, "admin_user_password_reset", user_id=user["id"], username=user["username"], target_type="user", target_id=target_user_id)

    return RedirectResponse(url=f"/admin/users/{target_user_id}?success=password_updated", status_code=302)


@router.post("/admin/users/delete")
def admin_users_delete(request: Request, user_id: int = Form(...)):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.users.delete")
    if redirect:
        return redirect

    if user_id == user["id"]:
        return RedirectResponse(url="/admin?error=cannot_delete_self", status_code=302)

    if is_user_admin(user_id) and count_admin_users() <= 1:
        return RedirectResponse(url="/admin?error=cannot_delete_last_admin", status_code=302)

    delete_user_by_id(user_id)
    audit_log(request, "admin_user_deleted", user_id=user["id"], username=user["username"], target_type="user", target_id=user_id)

    return RedirectResponse(url="/admin?success=user_deleted", status_code=302)


@router.post("/admin/users/{target_user_id}/delete")
def admin_user_detail_delete(request: Request, target_user_id: int):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, "admin.users.delete")
    if redirect:
        return redirect

    if target_user_id == user["id"]:
        return RedirectResponse(url=f"/admin/users/{target_user_id}?error=cannot_delete_self", status_code=302)

    if is_user_admin(target_user_id) and count_admin_users() <= 1:
        return RedirectResponse(url=f"/admin/users/{target_user_id}?error=cannot_delete_last_admin", status_code=302)

    delete_user_by_id(target_user_id)
    audit_log(request, "admin_user_deleted", user_id=user["id"], username=user["username"], target_type="user", target_id=target_user_id)

    return RedirectResponse(url="/admin?success=user_deleted", status_code=302)
