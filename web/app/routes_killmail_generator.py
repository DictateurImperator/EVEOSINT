from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from psycopg2.errors import UndefinedTable, InvalidSchemaName
from starlette.concurrency import run_in_threadpool

from .auth import require_login
from .layout import app_context
from .main_objects import templates
from . import killmail_generator as store

router = APIRouter()


def page(request, user, token=None):
    context = app_context(request=request, user=user, title='EVEOSINT - Combat collector', active_module='', active_menu_key='')
    context['collector_token'] = token
    return templates.TemplateResponse(request=request, name='combat_collector.html', context=context,
                                      headers={'Cache-Control': 'no-store'})


@router.get('/account/combat-collector', response_class=HTMLResponse)
def collector_page(request: Request):
    user = require_login(request)
    return page(request, user) if user else RedirectResponse('/login', status_code=302)


@router.post('/account/combat-collector/token', response_class=HTMLResponse)
def create_token(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse('/login', status_code=302)
    try:
        token = store.rotate_token(user['id'])
    except (UndefinedTable, InvalidSchemaName):
        raise HTTPException(503, 'Collector setup required')
    return page(request, user, token)


@router.post('/account/combat-collector/revoke')
def revoke(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse('/login', status_code=302)
    try:
        store.revoke_token(user['id'])
    except (UndefinedTable, InvalidSchemaName):
        raise HTTPException(503, 'Collector setup required')
    return RedirectResponse('/account/combat-collector', status_code=303)


@router.post('/api/combat-repairs')
async def receive_report(request: Request):
    auth = request.headers.get('authorization', '')
    if not auth.startswith('Bearer '):
        raise HTTPException(401)
    try:
        owner = await run_in_threadpool(store.authenticate, auth[7:])
    except (UndefinedTable, InvalidSchemaName):
        raise HTTPException(503, 'Collector setup required')
    if owner is None:
        raise HTTPException(401)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > store.MAX_BODY:
            raise HTTPException(413)
    try:
        ack = await run_in_threadpool(store.store_report, owner, request.headers.get('idempotency-key', ''), bytes(body))
    except ValueError:
        raise HTTPException(422, 'Invalid report')
    except (UndefinedTable, InvalidSchemaName):
        raise HTTPException(503, 'Collector setup required')
    return JSONResponse(ack, headers={'Cache-Control': 'no-store'})
