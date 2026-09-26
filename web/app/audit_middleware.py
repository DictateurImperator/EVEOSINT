from .audit import audit_log
from .auth import get_current_user


EXCLUDED_PATHS = {
    "/login",
    "/logout",
    "/favicon.ico",
}

EXCLUDED_PREFIXES = (
    "/api/",
    "/static/",
)


def should_audit_page_view(request, response):
    path = request.url.path

    if request.method != "GET":
        return False

    if response.status_code >= 400:
        return False

    if path in EXCLUDED_PATHS:
        return False

    return not path.startswith(EXCLUDED_PREFIXES)


async def audit_page_view_middleware(request, call_next):
    response = await call_next(request)

    if should_audit_page_view(request, response):
        user = get_current_user(request)

        if user:
            details = request.url.path
            if request.url.query:
                details = f"{details}?{request.url.query}"

            audit_log(
                request,
                "page_view",
                user_id=user["id"],
                username=user["username"],
                target_type="page",
                target_id=request.url.path,
                details=details,
            )

    return response
