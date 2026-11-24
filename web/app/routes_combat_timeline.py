"""Admin-only timeline and authenticated, bounded desktop collector intake."""
from contextlib import closing
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
    if not links.refresh(force=True):
        raise HTTPException(409, 'A recalculation is already running.')
    return JSONResponse({'status':'finished'},headers={'Cache-Control':'no-store'})


@router.post('/collector/combat-events')
async def collect(request: Request):
    auth=request.headers.get('authorization','')
    if not auth.startswith('Bearer '): raise HTTPException(401)
    owner=await run_in_threadpool(active_collector, auth[7:])
    if owner is None: raise HTTPException(401)
    body=bytearray()
    async for chunk in request.stream():
        if len(body)+len(chunk)>store.MAX_BODY: raise HTTPException(413)
        body.extend(chunk)
    try: ack=await run_in_threadpool(store.receive,owner,bytes(body),request.headers.get('idempotency-key',''))
    except store.EventConflict: raise HTTPException(409,'Conflicting event')
    except (ValueError,UnicodeError): raise HTTPException(422,'Invalid telemetry batch')
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
