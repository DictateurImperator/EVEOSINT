"""Admin publication switches and bounded, on-demand public views."""
import fcntl
import logging
import time
from functools import lru_cache
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from .auth import require_login, require_permission_or_redirect
from .layout import app_context
from .main_objects import templates
from . import public_pages as public

router = APIRouter()
LOG = logging.getLogger(__name__)


def _admin(request):
    user = require_login(request)
    return user, require_permission_or_redirect(user, 'admin.system.view')


def _status():
    import json
    try:
        state = json.loads((public.ROOT / 'prepare-status.json').read_text())
        if state.get('state') == 'running':
            with (public.ROOT / 'prepare.lock').open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    state['seconds'] = round(time.time() - state.get('updated', time.time()), 1)
                else:
                    return dict(state='interrupted', message='Preparation interrupted. You can retry.')
        return state
    except (OSError, ValueError):
        return dict(state='idle', message='No snapshot preparation running.')


@router.get('/admin/public-pages', response_class=HTMLResponse)
def admin_page(request: Request):
    user, redirect = _admin(request)
    if redirect:
        return redirect
    context = app_context(request=request, user=user, title='EVEOSINT - Public pages', active_module='admin', active_menu_key='admin.public_pages')
    context.update(public_catalog=public.admin_catalog(), policy=public.settings(), snapshot_policy=public.effective_policy(public.settings()), snapshots=public.entries(),
                   prepared=_status(), timestamp=public.timestamp,
                   saved=request.query_params.get('saved') == '1', cache_limit=public.settings().get('_cache',{}).get('max_entries',public.MAX_ENTRIES))
    return templates.TemplateResponse(request=request, name='admin_public_pages.html', context=context,headers={'Cache-Control':'no-store'})


@router.get('/admin/public-pages/preview', response_class=HTMLResponse)
def preview(request: Request):
    _, redirect = _admin(request)
    if redirect:
        return redirect
    try:
        target = public.resolve(request.query_params.get('source', ''))
    except ValueError:
        return HTMLResponse(status_code=404)
    snapshot = public.load(target)
    return templates.TemplateResponse(request=request, name='public_page.html', context=dict(
        title='EVEOSINT - Private preview', public_links=[], public_tabs=[],
        missing=snapshot is None, snapshot_date=public.timestamp(snapshot['created']) if snapshot else None,
        snapshot_html=public.sanitize(snapshot['html'], public.settings(), target.source) if snapshot else '',
        show_app_layout=False, public_site=True, private_preview=True), headers={'Cache-Control':'no-store',
        'Content-Security-Policy': "connect-src 'none'; form-action 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'"})


@router.post('/admin/public-pages/settings')
async def change_settings(request: Request):
    _, redirect = _admin(request)
    if redirect:
        return redirect
    form = await request.form()
    enabled = set(form.getlist('enabled'))
    try:
        public.save_settings(enabled, max_entries=form.get('cache_entries', public.MAX_ENTRIES))
    except (ValueError,TypeError):
        return JSONResponse({'error':'Choose a whole number of cached results.'},status_code=400)
    return RedirectResponse('/admin/public-pages?saved=1', status_code=303)


def _prepare(request, target, lock):
    started = time.monotonic()
    try:
        from .public_page_renderer import render
        html = render(request, target)
        public.store(target, html)
        state = dict(state='complete', message='Snapshot ready.', source=target.source, public=target.public)
    except Exception:
        LOG.exception('Public snapshot preparation failed source=%s', target.source)
        state = dict(state='failed', message='Preparation failed. See the application log.', source=target.source)
    finally:
        state.update(updated=time.time(), seconds=round(time.monotonic() - started, 1))
        try:
            with public.locked():
                public._atomic(public.ROOT / 'prepare-status.json', state)
        finally:
            lock.close()


@router.post('/admin/public-pages/prepare')
async def prepare(request: Request, background_tasks: BackgroundTasks):
    _, redirect = _admin(request)
    if redirect:
        return redirect
    try:
        payload = await request.json()
        target = public.resolve(payload.get('source', ''))
    except (ValueError, AttributeError) as exc:
        return JSONResponse(dict(error=str(exc)), status_code=400)
    public.ROOT.mkdir(parents=True, exist_ok=True)
    lock = (public.ROOT / 'prepare.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return JSONResponse(dict(error='Another snapshot is being prepared. Wait for completion.'), status_code=409)
    try:
        with public.locked():
            public._atomic(public.ROOT / 'prepare-status.json',
                dict(state='running', message='Reading the selected page…', source=target.source, updated=time.time()))
        background_tasks.add_task(_prepare, request, target, lock)
    except Exception:
        lock.close()
        raise
    return JSONResponse(dict(state='running', source=target.source), status_code=202)


@router.get('/admin/public-pages/status')
def status(request: Request):
    _, redirect = _admin(request)
    if redirect:
        return redirect
    return JSONResponse(_status(), headers={'Cache-Control': 'no-store'})


@router.post('/admin/public-pages/clear')
def clear_cache(request: Request):
    _, redirect = _admin(request)
    if redirect:
        return redirect
    public.clear()
    return RedirectResponse('/admin/public-pages', status_code=303)


@lru_cache(maxsize=16)
def shared_context_menu(module, bucket):
    """Use the same configured menu labels, icons and order as private pages."""
    from .menus import build_context_menu
    from .public_read import PUBLIC_READER
    return build_context_menu(PUBLIC_READER,module,'')


def _public_layout(request, policy=None, target=None):
    user = require_login(request)
    raw_policy=policy or {}
    policy=public.effective_policy(raw_policy)
    navigation=[dict(label='Home',href='/public',active=target is None)]
    entities=public.entity_navigation(raw_policy)
    if entities:navigation.append(dict(label='EntitiesINTEL',href='/public/entities'))
    for key,label in (('superintel','SuperINTEL'),('killboard','Killboard'),('tools','Tools'),('leaderboard','Leaderboard')):
        page=policy.get(key,{})
        if not page.get('enabled') or not page.get('tabs'):continue
        if key=='killboard':url='/killboard'
        elif key=='tools':url='/tools'
        elif key=='leaderboard':url='/leaderboard'+('' if page['tabs'][0]=='character' else '?kind='+page['tabs'][0])
        else:url='/superintel'+('' if page['tabs'][0]=='dashboard' else '/'+page['tabs'][0])
        navigation.append(dict(label=label,href='/public'+url))
    order={label:index for index,label in enumerate(('Home','SuperINTEL','EntitiesINTEL','Killboard','Tools','Leaderboard'))}
    navigation.sort(key=lambda item:order[item['label']])
    is_entities=target and (target.tab=='index' or target.key=='map')
    module='entities' if is_entities else ('tools' if target and target.key=='tools' else None)
    context_menu=[];context_title='Menu'
    if module:
        configured=shared_context_menu(module,int(time.time()//60))
        context_title=configured['title']
        for item in configured['items']:
            path=item.get('href','')
            if path=='/map':
                destination=next((link['href'] for link in entities if link['key']=='map'),None)
            elif path.startswith('/entities/'):
                destination=next((link['href'] for link in entities if link['href']=='/public'+path),None)
            else:destination=public.link_url(path,raw_policy)
            if not destination:continue
            active=(target.key=='map' and path=='/map') or (target.tab=='index' and path==target.path) or (module=='tools' and destination=='/public'+target.path)
            context_menu.append(dict(item,href=destination,active=active))
    for item in navigation:
        item['active']=(item['label']=='EntitiesINTEL' and bool(is_entities)) or (item['label']=='Tools' and module=='tools') or item.get('active',False)
    search_source=next((item['href'].removeprefix('/public') for item in entities if item['key']!='map'),None)
    return dict(request=request,user=user,username=user.get('username','') if user else '',
                show_app_layout=True,public_site=True,public_navigation=navigation,top_menu=navigation,
                context_title=context_title,context_menu=context_menu,public_shared_layout=True,
                public_search=bool(search_source or public.killboards_enabled(raw_policy)),
                public_search_source=search_source or '/killboard')


def _public_headers(context):
    headers = {'Cache-Control': 'no-store', 'Vary':'Cookie'}
    if context.get('public_site') and not context.get('user'):
        headers['Cache-Control'] = f'private, max-age={public.BROWSER_MAX_AGE}, must-revalidate'
        headers['Content-Security-Policy'] = "connect-src 'self'; form-action 'self'; frame-src 'none'; object-src 'none'; base-uri 'none'"
    return headers


@router.get('/public', response_class=HTMLResponse)
@router.get('/public/', response_class=HTMLResponse)
def public_home(request: Request):
    policy = public.settings()
    links = [row for row in public.entries() if not row.get('internal') and row['fresh'] and
             public.published(public.resolve(row['source']), policy)]
    # Static shared Home/changelog: no report query, cache or preparation needed.
    from .public_page_renderer import content
    context = _public_layout(request, policy)
    context.update(title='Welcome to EVE OSINT', public_links=links, public_tabs=[], missing=False,
                   snapshot_html=public.sanitize(content('home.html', {'public_home':True}), policy, '/public'),
                   snapshot_date=None)
    return _page_response(request,context)


def _conditional(request, body, media_type, headers, status=200):
    from hashlib import sha256
    if status == 200 and headers.get('Cache-Control') != 'no-store':
        etag = '"' + sha256(body).hexdigest() + '"'
        headers = dict(headers, ETag=etag)
        if request.headers.get('if-none-match') in (etag,'*'):
            return Response(status_code=304,headers=headers)
    return Response(body,status_code=status,media_type=media_type,headers=headers)


def _page_response(request,context):
    response=templates.TemplateResponse(request=request,name='public_page.html',context=context)
    return _conditional(request,response.body,'text/html',_public_headers(context))


def _rewrite_data(data,policy,source):
    if isinstance(data,dict):
        return {k:public.sanitize(v,policy,source,interactive=True) if k=='html' and isinstance(v,str)
                else _rewrite_data(v,policy,source) for k,v in data.items()}
    if isinstance(data,list):
        return [_rewrite_data(v,policy,source) for v in data]
    return data


@router.post('/public/compute')
async def compute(request: Request):
    """Owner-published tool calculations only; no management/job endpoints."""
    import json
    from hashlib import sha256
    from .public_read import read_scope, read_get
    # Public read computations carry no account/session credentials.
    origin=request.headers.get('origin')
    if origin:
        from urllib.parse import urlsplit
        if urlsplit(origin).netloc!=request.headers.get('host'):
            return Response(status_code=403,headers={'Cache-Control':'no-store'})
    if request.headers.get('content-length','0').isdigit() and int(request.headers.get('content-length','0'))>65536:
        return Response(status_code=413,headers={'Cache-Control':'no-store'})
    raw=await request.body()
    if len(raw)>65536:return Response(status_code=413,headers={'Cache-Control':'no-store'})
    try:
        payload=json.loads(raw)
        source=public.resolve(payload['source'])
        body=json.dumps(payload['payload'],sort_keys=True,separators=(',',':')).encode()
        path=payload['path']
        target=public.compute_target(source,path,sha256(body).hexdigest())
    except (ValueError,KeyError,TypeError):
        return Response(status_code=404,headers={'Cache-Control':'no-store'})
    if not public.published(target,public.settings()):
        return Response(status_code=404,headers={'Cache-Control':'no-store'})
    cached=public.load(target)
    try:
        if not cached:
            with public.render_slot(target):
                cached=public.load(target)
                if not cached:
                    with read_scope():result=await read_get(request.app,path,method='POST',request_body=body)
                    if result.status_code!=200:
                        return Response(result.content,status_code=result.status_code,media_type=result.headers.get('content-type'),headers={'Cache-Control':'no-store'})
                    value=json.dumps(dict(body=result.text,media_type=result.headers.get('content-type','application/json'),
                        download=result.headers.get('content-disposition')))
                    try:public.store(target,value)
                    except ValueError:pass
                    cached={'html':value}
    except public.CacheBusy:
        return JSONResponse({'error':'Another result is loading. Please retry shortly.'},status_code=503,headers={'Cache-Control':'no-store'})
    except Exception:
        LOG.exception('Published tool computation failed')
        return JSONResponse({'error':'Computation failed. Please retry.'},status_code=503,headers={'Cache-Control':'no-store'})
    if not public.published(target,public.settings()):
        return Response(status_code=404,headers={'Cache-Control':'no-store'})
    value=json.loads(cached['html']);headers={'Cache-Control':'no-store'}
    if value.get('download'):headers['Content-Disposition']=value['download']
    return Response(value['body'],media_type=value['media_type'].split(';')[0],headers=headers)


@router.get('/public/read')
async def read_data(request: Request):
    import json
    from .public_read import read_scope, read_get
    try:
        source=public.resolve(request.query_params.get('source',''))
        target=public.read_target(request.query_params.get('path',''),source)
    except (ValueError,KeyError):
        return Response(status_code=404,headers={'Cache-Control':'no-store'})
    policy=public.settings()
    if not public.published(source,policy) or not public.published(target,policy):
        return Response(status_code=404,headers={'Cache-Control':'no-store'})
    cached=public.load(target)
    transient=False
    try:
        if not cached:
            with public.render_slot(target):
                cached=public.load(target)
                if not cached:
                    from urllib.parse import parse_qsl
                    private_url=dict(parse_qsl(target.query))['path']
                    # In-process ASGI only: no remote HTTP, cookies or credentials.
                    # read_target accepted an explicit GET data endpoint for this entity/tab.
                    with read_scope():
                        result=await read_get(request.app,private_url,request.headers.get('accept','*/*'))
                    if result.status_code != 200:
                        return Response(result.content,status_code=result.status_code,media_type=result.headers.get('content-type'),headers={'Cache-Control':'no-store'})
                    payload=dict(body=result.text,media_type=result.headers.get('content-type','text/html'),
                                 headers={k:v for k,v in result.headers.items() if k.lower().startswith('x-kb-')})
                    if 'json' in payload['media_type']:
                        data=json.loads(payload['body'])
                        transient=isinstance(data,dict) and data.get('status') in ('running','started','pending','failed')
                    else:
                        transient='data-pilotable-refresh="1"' in payload['body'] or 'alert-danger' in payload['body']
                    if not transient:
                        try:public.store(target,json.dumps(payload))
                        except ValueError:pass
                    cached=dict(html=json.dumps(payload))
    except public.CacheBusy:
        return JSONResponse({'error':'Another result is loading. Please retry shortly.'},status_code=503,headers={'Retry-After':'2','Cache-Control':'no-store'})
    except Exception:
        LOG.exception('Published data request failed')
        return JSONResponse({'error':'This data could not be loaded. Please retry.'},status_code=503,headers={'Cache-Control':'no-store'})
    policy=public.settings()
    if not public.published(source,policy) or not public.published(target,policy):
        return Response(status_code=404,headers={'Cache-Control':'no-store'})
    payload=json.loads(cached['html'])
    if 'json' in payload['media_type']:
        data=json.loads(payload['body'])
        from urllib.parse import urlsplit, parse_qs
        data_path=parse_qs(target.query).get('path',[''])[0]
        if urlsplit(data_path).path=='/api/entity-search' and (source.key=='killboard' or source.tab=='index'):
            results=[]
            for item in data.get('results',[]):
                try:entity=public.resolve(item.get('url',''))
                except ValueError:continue
                if entity.key not in public.ENTITY_INDEX_KEYS.values():continue
                destination=public.entity_destination(entity,policy)
                if destination:results.append(dict(item,url=destination))
            data=dict(data,results=results)
        body=json.dumps(_rewrite_data(data,policy,source.source)).encode()
    else:
        html=payload['body']
        if source.tab=='index' and source.key=='ship':html=html.replace('/ship-category?', '/entities/ship?')
        body=public.sanitize(html,policy,source.source,interactive=True).encode()
    headers=dict(payload['headers'], **{'Cache-Control':'no-store' if transient else f'private, max-age={public.BROWSER_MAX_AGE}, must-revalidate'})
    return _conditional(request,body,payload['media_type'].split(';')[0],headers)


# Include AFTER personal_groups_router so /public/groups/{token} remains independent.
@router.get('/public/{path:path}', response_class=HTMLResponse)
def public_page(request: Request, path: str):
    if path=='entities':
        links=public.entity_navigation(public.settings())
        return RedirectResponse(links[0]['href'],status_code=302,headers={'Cache-Control':'no-store'}) if links else HTMLResponse(status_code=404,headers={'Cache-Control':'no-store'})
    try:
        target = public.resolve('/' + path + ('?' + request.url.query if request.url.query else ''))
    except ValueError:
        return HTMLResponse(status_code=404)
    policy = public.settings()
    if not public.published(target,policy) and target.key in public.ENTITY_INDEX_KEYS.values() and (target.tab=='overview' or (target.key=='coalition' and target.tab=='killboard' and not target.query)):
        destination=public.entity_destination(target,policy)
        if destination:return RedirectResponse(destination,status_code=302,headers={'Cache-Control':'no-store'})
    if not public.published(target, policy):
        return HTMLResponse(status_code=404, headers={'Cache-Control': 'no-store'})
    from .public_page_renderer import render
    try:
        snapshot, hit = public.cached_result(target, lambda: render(request,target))
    except public.CacheBusy as exc:
        return HTMLResponse('<p>'+str(exc)+'</p>', status_code=503, headers={'Retry-After':'2','Cache-Control':'no-store'})
    except Exception:
        LOG.exception('Published page failed source=%s',target.source)
        return HTMLResponse('<p>This view could not be loaded. Please retry.</p>',status_code=503,headers={'Cache-Control':'no-store'})
    # Publication may have changed while the query was running.
    policy = public.settings()
    if not public.published(target,policy):
        return HTMLResponse(status_code=404,headers={'Cache-Control':'no-store'})
    tabs = []
    effective=public.effective_policy(policy)
    for entry in ([] if target.tab=='index' else public.FAMILIES[target.key]['tabs']):
        if entry['key'] not in effective.get(target.key,{}).get('tabs', []):
            continue
        if target.key == 'leaderboard':
            url = '/leaderboard' + ('?kind=' + entry['key'] if entry['key'] != 'character' else '')
        elif target.key == 'superintel':
            url = '/superintel' + ('/' + entry['key'] if entry['key'] != 'dashboard' else '')
        elif target.key in ('map','tools'):
            url = public.tab_path(target.key,entry['key'],target.path)
        else:
            base_path=f'/{target.key}/{target.entity_id}' if target.key in ('coalition','character','corporation','alliance') else target.path
            url = base_path + ('?tab=' + entry['key'] if entry['key'] not in ('overview', 'detail') else '')
        tabs.append(dict(label=entry['label'], url='/public' + url, active=entry['key'] == target.tab))
    if target.tab=='index':tabs=[]
    # Small bounded list of snapshots, no database access and no public job queue.
    variants = [row for row in public.entries() if not row.get('internal') and row['fresh'] and
                public.published(public.resolve(row['source']), policy)]
    context = _public_layout(request, policy, target)
    context.update(title='EVEOSINT - ' + public.FAMILIES[target.key]['label'], public_links=variants,
        public_tabs=tabs, missing=snapshot is None,
        snapshot_html=public.sanitize(snapshot['html'], policy, target.source, interactive=True),
        public_target=target, public_policy=public.effective_policy(policy), extra_public_pages=dict(public.EXTRA_PAGES,**{'/entities/'+kind:(key,'index') for kind,key in public.ENTITY_INDEX_KEYS.items()}), snapshot_date=public.timestamp(snapshot['created']))
    return _page_response(request,context)
