"""Admin-only timeline and authenticated, bounded desktop collector intake."""
from contextlib import closing
import zlib
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from .auth import require_login, require_permission_or_redirect, get_user_permissions
from .db import db
from .main_objects import templates
from . import combat_timeline as store
from . import combat_killmail_links as links

router = APIRouter()


def admin(request, permission='admin.jobs.view'):
    user = require_login(request)
    redirect = require_permission_or_redirect(user, permission)
    return user, redirect


@router.get('/admin/debug/combat/events')
def events(request: Request, observer: str='', kind: str='', before: str='', date_from: str='', date_to: str=''):
    user, redirect = admin(request)
    if redirect: return redirect
    try: payload=links.attach_links(store.timeline(observer,kind,before,date_from,date_to))
    except ValueError: raise HTTPException(400, 'Invalid timeline filter')
    return JSONResponse(payload,headers={'Cache-Control':'no-store'})


def active_collector(token):
    owner=store.token_owner(token)
    if owner is None: return None
    with closing(db()) as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT is_active FROM web.users WHERE id=%s',(owner,));row=cur.fetchone()
    if not row or not row[0] or 'admin.jobs.run' not in get_user_permissions(owner): return None
    return owner


@router.get('/admin/debug/combat/killmails')
def killmails(request: Request, observer: str='', page: int=1, date_from: str='', date_to: str='', month: str='', side: str='both', killmail_id: str=''):
    user, redirect = admin(request)
    if redirect: return redirect
    try:
        payload=links.report(observer,page,date_from,date_to,month,side,killmail_id)
        kb=links.killboard_page(payload,side)
        payload['html']=templates.env.get_template('entity_killmails_fragment.html').render(killmail_page=kb,killmail_public=False)
    except ValueError: raise HTTPException(400, 'Invalid observer')
    return JSONResponse(payload,headers={'Cache-Control':'no-store'})


@router.post('/admin/debug/combat/recalculate')
def recalculate(request: Request):
    user, redirect = admin(request, 'admin.jobs.run')
    if redirect: return redirect
    try:
        if not links.refresh(force=True): raise HTTPException(409, 'A recalculation is already running.')
    except store.Maintenance: return maintenance_response()
    return JSONResponse({'status':'finished'},headers={'Cache-Control':'no-store'})


def maintenance_response():
    return JSONResponse({'status':'paused'},status_code=503,
        headers={'Cache-Control':'no-store','Retry-After':'5','X-EVEOSINT-Maintenance':'1'})


@router.get('/collector/status')
async def collector_status(request: Request):
    if store.MAINTENANCE.exists(): return maintenance_response()
    auth=request.headers.get('authorization','')
    if not auth.startswith('Bearer ') or await run_in_threadpool(active_collector,auth[7:]) is None:
        raise HTTPException(401)
    return JSONResponse({'version':2,'status':'ready'},headers={'Cache-Control':'no-store'})


@router.post('/collector/combat-events')
@router.post('/collector/death-summaries')
async def collect(request: Request):
    if store.MAINTENANCE.exists(): return maintenance_response()
    auth=request.headers.get('authorization','')
    if not auth.startswith('Bearer '): raise HTTPException(401)
    owner=await run_in_threadpool(active_collector, auth[7:])
    if owner is None: raise HTTPException(401)
    body=bytearray()
    encoding=request.headers.get('content-encoding','identity').lower()
    if encoding not in ('identity','gzip'): raise HTTPException(415)
    decoder=zlib.decompressobj(16+zlib.MAX_WBITS) if encoding=='gzip' else None
    wire_size=0
    try:
        async for chunk in request.stream():
            if store.MAINTENANCE.exists(): return maintenance_response()
            wire_size+=len(chunk)
            if wire_size>store.MAX_BODY: raise HTTPException(413)
            body.extend(decoder.decompress(chunk,store.MAX_BODY+1-len(body)) if decoder else chunk)
            if len(body)>store.MAX_BODY or (decoder and decoder.unconsumed_tail): raise HTTPException(413)
        if decoder and (not decoder.eof or decoder.unused_data): raise ValueError('Invalid gzip')
        ack=await run_in_threadpool(store.receive,owner,bytes(body),request.headers.get('idempotency-key',''),request.url.path.endswith('/death-summaries'))
    except store.Maintenance: return maintenance_response()
    except store.EventConflict: raise HTTPException(409,'Conflicting observation')
    except (ValueError,UnicodeError,zlib.error): raise HTTPException(422,'Invalid telemetry batch')
    return JSONResponse(ack,headers={'Cache-Control':'no-store'})


@router.get('/admin/debug/combat/kill/{killmail_id}')
def killmail_test(request: Request, killmail_id: int):
    user, redirect = admin(request)
    if redirect: return redirect
    from .routes_entities import kill_profile
    response=kill_profile(request,killmail_id)
    if not hasattr(response,'context'): return response
    return templates.TemplateResponse(request=request,name='killmail_detail.html',
        context=dict(response.context,combat_debug=True))
