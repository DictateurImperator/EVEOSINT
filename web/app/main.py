import logging
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


from .audit_middleware import audit_page_view_middleware
from .main_objects import templates
from .schema import ensure_tables
from .routes_account import router as account_router
from .routes_admin_audit import router as admin_audit_router
from .routes_admin_coalitions import router as admin_coalitions_router
from .routes_admin_jobs import router as admin_jobs_router
from .routes_admin_debug import router as admin_debug_router
from .routes_admin_git import router as admin_git_router
from .routes_admin_update_pipeline import router as admin_update_pipeline_router
from .routes_admin_mer import router as admin_mer_router
from .routes_admin_system import router as admin_system_router
from .routes_admin_sov_colors import router as admin_sov_colors_router
from .routes_admin_users import router as admin_users_router
from .routes_entities import router as entities_router
from .routes_home import router as home_router
from .routes_login import router as login_router
from .routes_superintel import router as superintel_router
from .routes_update_status import router as update_status_router

logger = logging.getLogger(__name__)

app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.middleware("http")(audit_page_view_middleware)


def _is_document_navigation(request: Request) -> bool:
    fetch_mode = (request.headers.get("sec-fetch-mode") or "").lower()
    fetch_dest = (request.headers.get("sec-fetch-dest") or "").lower()

    # Browsers send Sec-Fetch-* on both navigations and fetch/XHR requests.
    # If those headers are present, trust them instead of the Accept header.
    if fetch_mode or fetch_dest:
        return fetch_mode == "navigate" or fetch_dest == "document"

    # Fallback for clients/browsers that do not send Sec-Fetch-* headers.
    accept = (request.headers.get("accept") or "").lower()
    return "text/html" in accept


def _friendly_error_response(request: Request, status_code: int):
    if status_code == 401:
        title = "Authentication required"
        message = "Please log in to continue."
    elif status_code == 403:
        title = "Access denied"
        message = "You do not have permission to access this page."
    elif status_code == 404:
        title = "Page not found"
        message = "The requested page could not be found."
    elif status_code == 405:
        title = "Method not allowed"
        message = "This action is not available here."
    elif status_code == 429:
        title = "Too many requests"
        message = "Too many requests were sent. Please try again shortly."
    elif 400 <= status_code < 500:
        title = "Invalid request"
        message = "The request could not be processed."
    else:
        title = "Something went wrong"
        message = "An unexpected error occurred. Please try again later."

    public_error = f"Erreur {status_code}"

    if not _is_document_navigation(request):
        accept = (request.headers.get("accept") or "").lower()
        if "application/json" in accept or request.url.path.startswith("/api/"):
            return JSONResponse(
                {"error": public_error},
                status_code=status_code,
                headers={"Cache-Control": "no-store"},
            )
        return PlainTextResponse(
            public_error,
            status_code=status_code,
            headers={"Cache-Control": "no-store"},
        )

    return templates.TemplateResponse(
        request=request,
        name="error.html",
        context={
            "title": title,
            "error_title": title,
            "error_message": message,
            "status_code": status_code,
        },
        status_code=status_code,
    )


def _requires_csrf_check(request: Request) -> bool:
    if request.method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return False

    path = request.url.path
    return (
        path == "/logout"
        or path in {"/account/password", "/account/delete"}
        or path == "/admin"
        or path.startswith("/admin/")
    )


def _request_authority(request: Request):
    host_header = (request.headers.get("host") or "").strip()
    if not host_header:
        return None

    try:
        parsed = urlsplit("//" + host_header)
        if not parsed.hostname:
            return None
        return parsed.hostname.rstrip(".").lower(), parsed.port
    except ValueError:
        return None


def _source_authority(value: str):
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        return parsed.hostname.rstrip(".").lower(), parsed.port
    except ValueError:
        return None


@app.middleware("http")
async def csrf_protection_middleware(request: Request, call_next):
    if not _requires_csrf_check(request):
        return await call_next(request)

    request_authority = _request_authority(request)
    origin = (request.headers.get("origin") or "").strip()
    referer = (request.headers.get("referer") or "").strip()

    source_authority = None
    if origin:
        source_authority = _source_authority(origin)
    elif referer:
        source_authority = _source_authority(referer)

    if (
        request_authority is None
        or source_authority is None
        or source_authority != request_authority
    ):
        logger.warning(
            "CSRF check rejected method=%s path=%s origin=%r referer=%r",
            request.method,
            request.url.path,
            origin,
            referer,
        )
        return _friendly_error_response(request, 403)

    return await call_next(request)


@app.middleware("http")
async def normalize_error_responses(request: Request, call_next):
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "Unhandled middleware request error method=%s path=%s",
            request.method,
            request.url.path,
        )
        return _friendly_error_response(request, 500)

    if response.status_code < 400:
        return response

    # No route is allowed to expose its raw error body to a browser.
    return _friendly_error_response(request, response.status_code)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return _friendly_error_response(request, exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    safe_errors = [
        {
            "type": item.get("type"),
            "loc": item.get("loc"),
            "msg": item.get("msg"),
        }
        for item in exc.errors()
    ]
    logger.warning(
        "Request validation error method=%s path=%s errors=%s",
        request.method,
        request.url.path,
        safe_errors,
    )
    return _friendly_error_response(request, 422)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception(
        "Unhandled request error method=%s path=%s",
        request.method,
        request.url.path,
        exc_info=exc,
    )
    return _friendly_error_response(request, 500)


@app.on_event("startup")
def startup():
    ensure_tables()


app.include_router(login_router)
app.include_router(home_router)
app.include_router(account_router)
app.include_router(superintel_router)
app.include_router(entities_router)
app.include_router(update_status_router)
app.include_router(admin_users_router)
app.include_router(admin_coalitions_router)
app.include_router(admin_system_router)
app.include_router(admin_sov_colors_router)
app.include_router(admin_jobs_router)
app.include_router(admin_debug_router)
app.include_router(admin_git_router)
app.include_router(admin_update_pipeline_router)
app.include_router(admin_mer_router)
app.include_router(admin_audit_router)
