from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .audit import audit_log
from .auth import create_session, delete_session, find_user_by_username, require_login, verify_password
from .config import SESSION_COOKIE
from .layout import app_context
from .main_objects import templates
from .security import validate_password, pwd_context
from .users import count_admin_users, delete_user_by_id, is_user_admin, reset_user_password

router = APIRouter()


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    context = app_context(
        request=request,
        user=user,
        title="EVEOSINT - Account",
        active_module="",
        active_menu_key="",
    )
    context.update({
        "error": request.query_params.get("error"),
        "success": request.query_params.get("success"),
    })

    return templates.TemplateResponse(request=request, name="account.html", context=context)


@router.post("/account/password")
def account_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    user = require_login(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    if new_password != confirm_password:
        return RedirectResponse(url="/account?error=password_confirm_mismatch", status_code=302)

    password_error = validate_password(new_password)
    if password_error:
        return RedirectResponse(url=f"/account?error={password_error}", status_code=302)

    user_row = find_user_by_username(user["username"])
    if not user_row or not verify_password(current_password, user_row[2]):
        audit_log(request, "account_password_failed", user_id=user["id"], username=user["username"])
        return RedirectResponse(url="/account?error=current_password_invalid", status_code=302)

    reset_user_password(user["id"], new_password)
    audit_log(request, "account_password_changed", user_id=user["id"], username=user["username"])

    token, expires_at = create_session(user["id"])
    response = RedirectResponse(url="/account?success=password_updated", status_code=302)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        expires=expires_at,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@router.post("/account/delete")
def account_delete(
    request: Request,
    username_confirm: str = Form(...),
    delete_confirm: str = Form(...),
):
    user = require_login(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    if username_confirm.strip() != user["username"] or delete_confirm.strip() != "DELETE":
        return RedirectResponse(url="/account?error=delete_confirm_invalid", status_code=302)

    if is_user_admin(user["id"]) and count_admin_users() <= 1:
        return RedirectResponse(url="/account?error=cannot_delete_last_admin", status_code=302)

    token = request.cookies.get(SESSION_COOKIE)
    audit_log(request, "account_deleted_self", user_id=user["id"], username=user["username"], target_type="user", target_id=user["id"])
    delete_user_by_id(user["id"])
    delete_session(token)

    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response
