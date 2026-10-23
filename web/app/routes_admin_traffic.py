from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .auth import require_login, require_permission_or_redirect
from .layout import app_context
from .main_objects import templates
from . import public_traffic as traffic

router = APIRouter()


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
