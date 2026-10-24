from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from .auth import require_login, require_permission_or_redirect
from .layout import app_context
from .main_objects import templates
from . import public_traffic as traffic

router = APIRouter()


@router.get('/robots.txt', response_class=PlainTextResponse)
def robots():
    # Keep the crawl instruction consistent with the enforced public access policy.
    # This file must itself remain available to Googlebot, including when blocked.
    rule = 'Disallow: /public' if traffic.googlebot_blocked() else 'Allow: /'
    return PlainTextResponse(f'User-agent: Googlebot\n{rule}\n\nUser-agent: *\nDisallow:\n',
                             headers={'Cache-Control': 'public, max-age=300'})


def authorized(request):
    user = require_login(request)
    return user, require_permission_or_redirect(user, 'admin.system.view')


@router.get('/admin/public-traffic', response_class=HTMLResponse)
def dashboard(request: Request):
    user, redirect = authorized(request)
    if redirect:
        return redirect
    context = app_context(request=request,user=user,title='EVEOSINT - Public traffic',
                          active_module='admin',active_menu_key='admin.public_traffic')
    return templates.TemplateResponse(request=request,name='admin_public_traffic.html',context=context,
                                      headers={'Cache-Control':'no-store'})


@router.get('/admin/public-traffic/data')
def data(request: Request, day: str = ''):
    _, redirect = authorized(request)
    if redirect:
        return JSONResponse({'error':'Forbidden'},status_code=403,headers={'Cache-Control':'no-store'})
    try:
        payload = traffic.snapshot(day)
    except (OSError, traffic.sqlite3.Error):
        return JSONResponse({'error':'Traffic storage is unavailable. Check the application data directory permissions.'},
                            status_code=503,headers={'Cache-Control':'no-store'})
    return JSONResponse(payload,headers={'Cache-Control':'no-store'})


@router.post('/public/traffic/presence')
async def presence(request: Request):
    # Browser heartbeat only; no arbitrary queries, account lookup or public scans.
    if (request.headers.get('origin') or '').rstrip('/') != str(request.base_url).rstrip('/'):
        return Response(status_code=403)
    cookie = request.cookies.get(traffic.COOKIE)
    if traffic.visitor_id(cookie,'check') is None:
        return Response(status_code=204)
    source = urlsplit(request.headers.get('referer',''))
    if source.netloc != request.url.netloc or not (source.path == '/public' or source.path.startswith('/public/')):
        return Response(status_code=403)
    await run_in_threadpool(traffic.record,traffic.page_key(source.path+'?'+source.query),
                            'presence',204,0,cookie,presence=True)
    return Response(status_code=204,headers={'Cache-Control':'no-store'})


@router.post('/admin/public-traffic/googlebot')
async def googlebot_policy(request: Request):
    _, redirect = authorized(request)
    headers = {'Cache-Control': 'no-store'}
    if redirect:
        return JSONResponse({'error': 'Forbidden'}, status_code=403, headers=headers)
    if (request.headers.get('origin') or '').rstrip('/') != str(request.base_url).rstrip('/'):
        return JSONResponse({'error': 'Invalid origin'}, status_code=403, headers=headers)
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({'error': 'Invalid policy'}, status_code=400, headers=headers)
    if not isinstance(payload, dict) or type(payload.get('blocked')) is not bool:
        return JSONResponse({'error': 'Invalid policy'}, status_code=400, headers=headers)
    try:
        await run_in_threadpool(traffic.set_googlebot_blocked, payload['blocked'])
    except OSError:
        return JSONResponse({'error': 'Cannot save crawler policy.'}, status_code=503, headers=headers)
    return JSONResponse({'block_googlebot': payload['blocked']}, headers=headers)
