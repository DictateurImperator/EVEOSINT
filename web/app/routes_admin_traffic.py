from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from .auth import require_login, require_permission_or_redirect
from .layout import app_context
from .main_objects import templates
from . import public_traffic as traffic

router = APIRouter()


@router.get('/legal', response_class=HTMLResponse)
@router.get('/public/legal', response_class=HTMLResponse)
def legal(request: Request):
    # Available before a choice, after refusal, and to blocked clients.
    from .routes_public_pages import _public_layout
    from . import public_pages
    context=_public_layout(request,public_pages.settings())
    context.update(title='EVEOSINT · Legal & Privacy',public_notice=True)
    return templates.TemplateResponse(request=request,name='public_legal.html',context=context,
                                      headers={'Cache-Control':'no-store'})


@router.get('/robots.txt', response_class=PlainTextResponse)
def robots():
    return PlainTextResponse('User-agent: Googlebot\nUser-agent: GoogleOther\nUser-agent: Google-InspectionTool\nUser-agent: Storebot-Google\nUser-agent: Google-Extended\nDisallow: /\n\nUser-agent: *\nDisallow:\n', headers={'Cache-Control': 'public, max-age=300'})


def authorized(request):
    user = require_login(request)
    return user, require_permission_or_redirect(user, 'admin.system.view')


@router.get('/admin/public-traffic', response_class=HTMLResponse)
def dashboard(request: Request, background_tasks: BackgroundTasks):
    user, redirect = authorized(request)
    if redirect:
        return redirect
    # Initial install/monthly refresh only from authenticated Admin, off the request path.
    background_tasks.add_task(traffic.traffic_countries.ensure_database)
    context = app_context(request=request,user=user,title='EVEOSINT - Public traffic',
                          active_module='admin',active_menu_key='admin.public_traffic')
    return templates.TemplateResponse(request=request,name='admin_public_traffic.html',context=context,
                                      headers={'Cache-Control':'no-store'})


@router.get('/admin/public-traffic/data')
def data(request: Request, day: str = '', country: str = ''):
    _, redirect = authorized(request)
    if redirect:
        return JSONResponse({'error':'Forbidden'},status_code=403,headers={'Cache-Control':'no-store'})
    try:
        payload = traffic.snapshot(day,country)
    except ValueError:
        return JSONResponse({'error':'Choose a two-letter country code.'},status_code=400,headers={'Cache-Control':'no-store'})
    except (OSError, traffic.sqlite3.Error):
        return JSONResponse({'error':'Traffic storage is unavailable. Check the application data directory permissions.'},
                            status_code=503,headers={'Cache-Control':'no-store'})
    return JSONResponse(payload,headers={'Cache-Control':'no-store'})


@router.post('/public/traffic/presence')
async def presence(request: Request):
    # Browser heartbeat only; no arbitrary queries, account lookup or public scans.
    if (request.headers.get('origin') or '').rstrip('/') != str(request.base_url).rstrip('/'):
        return Response(status_code=403)
    cookie = None
    if not traffic.access_accepted(request):
        return Response(status_code=204)
    source = urlsplit(request.headers.get('referer',''))
    if source.netloc != request.url.netloc or not (source.path == '/public' or source.path.startswith('/public/')):
        return Response(status_code=403)
    await run_in_threadpool(traffic.record,traffic.page_key(source.path+'?'+source.query),
                            'presence',204,0,cookie,presence=True,client_ip=request.client.host if request.client else None)
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
    if not payload['blocked']:
        return JSONResponse({'error': 'Emergency GOOGLE BOT block is active in Nginx.'}, status_code=409, headers=headers)
    try:
        await run_in_threadpool(traffic.set_googlebot_blocked, payload['blocked'])
    except OSError:
        return JSONResponse({'error': 'Cannot save crawler policy.'}, status_code=503, headers=headers)
    return JSONResponse({'block_googlebot': payload['blocked']}, headers=headers)


@router.post('/admin/public-traffic/countries')
def refresh_countries(request: Request, background_tasks: BackgroundTasks):
    _, redirect = authorized(request)
    headers = {'Cache-Control':'no-store'}
    if redirect or (request.headers.get('origin') or '').rstrip('/') != str(request.base_url).rstrip('/'):
        return JSONResponse({'error':'Forbidden'},status_code=403,headers=headers)
    background_tasks.add_task(traffic.traffic_countries.ensure_database,force=True)
    return JSONResponse({'state':'downloading'},status_code=202,headers=headers)


@router.post('/public/access/choice')
async def access_choice(request: Request):
    if (request.headers.get('origin') or '').rstrip('/')!=str(request.base_url).rstrip('/'):
        return Response(status_code=403)
    try:choice=(await request.json()).get('choice')
    except (ValueError,AttributeError):return Response(status_code=400)
    if choice not in ('accepted','declined'):return Response(status_code=400)
    value=await run_in_threadpool(traffic.access_choice,choice)
    response=Response(status_code=204,headers={'Cache-Control':'no-store'})
    response.set_cookie(traffic.ACCESS_COOKIE,value,max_age=180*86400,httponly=True,
                        secure=request.url.scheme=='https',samesite='lax',path='/public')
    return response


@router.post('/admin/public-traffic/ip-block')
async def ip_block(request: Request):
    _,redirect=authorized(request)
    if redirect or (request.headers.get('origin') or '').rstrip('/')!=str(request.base_url).rstrip('/'):
        return Response(status_code=403)
    try:
        payload=await request.json()
        address=str(traffic.ipaddress.ip_address(payload['ip']))
        blocked=payload['blocked'];reason=payload.get('reason','').strip()
        if type(blocked) is not bool or (blocked and not 1<=len(reason)<=500):raise ValueError()
    except (ValueError,KeyError,TypeError,AttributeError):return Response(status_code=400)
    def save():
        with traffic.connection() as conn:
            return conn.execute('UPDATE security_ips SET blocked=?,reason=? WHERE ip=?',
                                (int(blocked),reason if blocked else '',address)).rowcount
    count=await run_in_threadpool(save)
    return JSONResponse({'updated':bool(count)},status_code=200 if count else 404,headers={'Cache-Control':'no-store'})
