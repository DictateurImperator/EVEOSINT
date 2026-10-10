import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from psycopg2.errors import QueryCanceled

from .auth import require_login, require_permission_or_redirect
from .map_economy import EconomyMapError
from .population_economics import get_options, get_series

router = APIRouter()
logger = logging.getLogger(__name__)


def _response(request, kind, entity_id, fn):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, 'entities.view')
    if redirect:
        return redirect
    if kind not in {'alliance', 'coalition'} or entity_id <= 0:
        return JSONResponse({'error': 'Invalid entity.'}, status_code=404)
    try:
        return JSONResponse(fn(), headers={'Cache-Control': 'no-store'})
    except (ValueError, EconomyMapError) as exc:
        return JSONResponse({'error': str(exc)}, status_code=400)
    except QueryCanceled:
        return JSONResponse({'error': 'Economic analysis timed out. Try a shorter period.'}, status_code=504)
    except Exception:
        logger.exception('Population economics failed: %s %s', kind, entity_id)
        return JSONResponse({'error': 'Economic analysis could not be loaded.'}, status_code=500)


@router.get('/api/{kind}/{entity_id}/population-economics/options')
def economics_options(request: Request, kind: str, entity_id: int):
    return _response(request, kind, entity_id, lambda: get_options(kind, entity_id))


@router.get('/api/{kind}/{entity_id}/population-economics')
def economics_series(request: Request, kind: str, entity_id: int,
                     from_month: str = Query(alias='from'), to_month: str = Query(alias='to'),
                     window: int = Query(90)):
    return _response(request, kind, entity_id, lambda: get_series(kind, entity_id, from_month, to_month, window))
