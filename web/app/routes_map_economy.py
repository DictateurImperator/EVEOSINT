from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from .auth import require_login, require_permission_or_redirect
from .map_economy import EconomyMapError, get_economy_options, get_economy_series

router = APIRouter()


@router.get('/api/map/eve-2d/economy/options')
def economy_options(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, 'entities.view')
    if redirect:
        return redirect
    try:
        return JSONResponse(get_economy_options(), headers={'Cache-Control': 'no-store'})
    except EconomyMapError as exc:
        return JSONResponse({'error': str(exc)}, status_code=503)


@router.get('/api/map/eve-2d/economy')
def economy_series(request: Request, from_month: Annotated[str, Query(alias='from')],
                   to_month: Annotated[str, Query(alias='to')],
                   metric: Annotated[list[str] | None, Query()] = None,
                   evolution: bool = False):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, 'entities.view')
    if redirect:
        return redirect
    try:
        return JSONResponse(get_economy_series(from_month, to_month, metric, evolution),
                            headers={'Cache-Control': 'no-store'})
    except EconomyMapError as exc:
        return JSONResponse({'error': str(exc)}, status_code=400)
