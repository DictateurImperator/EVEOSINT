"""Admin publication switches and cache-only anonymous pages."""
import fcntl
import logging
import time
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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
    context.update(public_catalog=public.CATALOG, policy=public.settings(), snapshots=public.entries(),
                   prepared=_status(), timestamp=public.timestamp,
                   saved=request.query_params.get('saved') == '1')
    return templates.TemplateResponse(request=request, name='admin_public_pages.html', context=context)


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
    public.save_settings(enabled)
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


def _public_layout(request, policy=None):
    user = require_login(request)
    if user:
        return dict(app_context(request=request, user=user, title='EVEOSINT', active_module='home',
                                active_menu_key='home'), public_site=False)
    navigation = []
    for key, label in (('leaderboard', 'Leaderboard'), ('superintel', 'SuperINTEL')):
        page = (policy or {}).get(key, {})
        if not page.get('enabled'):
            continue
        for tab in public.FAMILIES[key]['tabs']:
            if tab['reason'] or tab['key'] not in page.get('tabs', []):
                continue
            url = ('/leaderboard' + ('?kind=' + tab['key'] if tab['key'] != 'character' else '')) if key == 'leaderboard' else ('/superintel' + ('/' + tab['key'] if tab['key'] != 'dashboard' else ''))
            navigation.append(dict(label=label, href='/public' + url))
            break
    return dict(show_app_layout=False, public_site=True, public_navigation=navigation)


def _public_headers(context):
    headers = {'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex'}
    if context.get('public_site'):
        headers['Content-Security-Policy'] = "connect-src 'none'; form-action 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'"
    return headers


@router.get('/public', response_class=HTMLResponse)
@router.get('/public/', response_class=HTMLResponse)
def public_home(request: Request):
    policy = public.settings()
    links = [row for row in public.entries() if row['fresh'] and
             public.published(public.resolve(row['source']), policy)]
    # Static shared Home/changelog: no report query, cache or preparation needed.
    from .public_page_renderer import content
    context = _public_layout(request, policy)
    context.update(title='EVEOSINT - Home', public_links=links, public_tabs=[], missing=False,
                   snapshot_html=public.sanitize(content('home.html', {'public_home':True}), policy, '/public'),
                   snapshot_date=None)
    return templates.TemplateResponse(request=request, name='public_page.html', context=context,
                                      headers=_public_headers(context))


# Include AFTER personal_groups_router so /public/groups/{token} remains independent.
@router.get('/public/{path:path}', response_class=HTMLResponse)
def public_page(request: Request, path: str):
    try:
        target = public.resolve('/' + path + ('?' + request.url.query if request.url.query else ''))
    except ValueError:
        return HTMLResponse(status_code=404)
    policy = public.settings()
    if not public.published(target, policy):
        return HTMLResponse(status_code=404, headers={'Cache-Control': 'no-store'})
    snapshot = public.load(target)
    tabs = []
    for entry in public.FAMILIES[target.key]['tabs']:
        if entry['reason'] or entry['key'] not in policy[target.key].get('tabs', []):
            continue
        if target.key == 'leaderboard':
            url = '/leaderboard' + ('?kind=' + entry['key'] if entry['key'] != 'character' else '')
        elif target.key == 'superintel':
            url = '/superintel' + ('/' + entry['key'] if entry['key'] != 'dashboard' else '')
        else:
            url = target.path + ('?tab=' + entry['key'] if entry['key'] not in ('overview', 'detail') else '')
        tabs.append(dict(label=entry['label'], url='/public' + url, active=entry['key'] == target.tab))
    # Small bounded list of snapshots, no database access and no public job queue.
    variants = [row for row in public.entries() if row['fresh'] and
                public.published(public.resolve(row['source']), policy)]
    context = _public_layout(request, policy)
    context.update(title='EVEOSINT - ' + public.FAMILIES[target.key]['label'], public_links=variants,
        public_tabs=tabs, missing=snapshot is None,
        snapshot_html=public.sanitize(snapshot['html'], policy, target.source) if snapshot else '',
        snapshot_date=public.timestamp(snapshot['created']) if snapshot else None)
    return templates.TemplateResponse(request=request, name='public_page.html', context=context,
                                      headers=_public_headers(context))
