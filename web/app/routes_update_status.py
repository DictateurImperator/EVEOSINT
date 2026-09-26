from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .auth import require_login
from .update_pipeline import get_public_update_status


router = APIRouter()


@router.get("/api/update-status")
def update_status(request: Request):
    user = require_login(request)
    if not user:
        return JSONResponse({"running": False}, status_code=401)
    return JSONResponse(get_public_update_status())
