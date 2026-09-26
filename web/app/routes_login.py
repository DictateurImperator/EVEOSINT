from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .auth import create_session, delete_session, find_user_by_username, get_current_user, verify_password
from .audit import audit_log
from .config import SESSION_COOKIE
from .main_objects import templates

router = APIRouter()

DUMMY_PASSWORD_HASH = "$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6Ttx7gQvLBO0vtqtqzh45fQj0yC.e"


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/home", status_code=302)
    return RedirectResponse(url="/login", status_code=302)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/home", status_code=302)

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"title": "EVEOSINT - Login", "error": None, "show_app_layout": False},
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    clean_username = username.strip()
    user_row = find_user_by_username(clean_username)

    password_hash = user_row[2] if user_row else DUMMY_PASSWORD_HASH
    password_ok = verify_password(password, password_hash)

    if not user_row:
        audit_log(request, "login_failed", username=clean_username, details="unknown_user")
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"title": "EVEOSINT - Login", "error": "Identifiants invalides.", "show_app_layout": False},
            status_code=401,
        )

    user_id, db_username, password_hash, is_active = user_row

    if not is_active or not password_ok:
        audit_log(request, "login_failed", user_id=user_id, username=db_username, details="bad_password_or_inactive")
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"title": "EVEOSINT - Login", "error": "Identifiants invalides.", "show_app_layout": False},
            status_code=401,
        )

    token, expires_at = create_session(user_id)
    audit_log(request, "login_success", user_id=user_id, username=db_username)

    response = RedirectResponse(url="/home", status_code=302)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        secure=True,
        samesite="lax",
        expires=expires_at,
    )
    return response


@router.post("/logout")
def logout(request: Request):
    user = get_current_user(request)
    token = request.cookies.get(SESSION_COOKIE)
    delete_session(token)

    if user:
        audit_log(request, "logout", user_id=user["id"], username=user["username"])

    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response
