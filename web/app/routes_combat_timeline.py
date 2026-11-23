"""Admin-only timeline and authenticated, bounded desktop collector intake."""
from contextlib import closing
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from .auth import require_login, require_permission_or_redirect, get_user_permissions
from .db import db
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
def killmails(request: Request, background_tasks: BackgroundTasks, observer: str=''):
    user, redirect = admin(request)
    if redirect: return redirect
    try: payload=links.report(observer)
    except ValueError: raise HTTPException(400, 'Invalid observer')
    background_tasks.add_task(links.refresh)
    return JSONResponse(payload,headers={'Cache-Control':'no-store'})


@router.post('/collector/combat-events')
async def collect(request: Request, background_tasks: BackgroundTasks):
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
    background_tasks.add_task(links.refresh)
    return JSONResponse(ack,headers={'Cache-Control':'no-store'})
