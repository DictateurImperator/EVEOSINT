"""Experimental rankings page and explicit monthly builder controls."""
import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, HTMLResponse
from psycopg2.errors import QueryCanceled

from .auth import require_login, require_permission_or_redirect
from .audit import audit_log
from .layout import app_context
from .main_objects import templates
from .jobs import JobError, run_killmail_rankings, stop_killmail_rankings, killmail_rankings_progress
from . import killmail_rankings as rankings

router = APIRouter()
LOG = logging.getLogger(__name__)


@router.get('/admin/debug/rankings', response_class=HTMLResponse)
def page(request: Request):
    user = require_login(request)
    redirect = require_permission_or_redirect(user,'admin.jobs.view')
    if redirect:
        return redirect
    context = app_context(request=request,user=user,title='EVEOSINT - ISK rankings',
                          active_module='admin',active_menu_key='admin.debug')
    params = request.query_params
    kind = params.get('kind','character')
    mode = params.get('mode','classic')
    sort = params.get('sort','destroyed')
    custom_period = bool(params.get('from') or params.get('through'))
    error = None
    months = []
    rows = []
    first = last = None
    cached = False
    elapsed = 0
    page_number = 1
    try:
        if kind not in rankings.KINDS or mode not in ('classic','weighted') or sort not in rankings.SORTS:
            raise ValueError('Invalid ranking filter.')
        page_number = int(params.get('page','1'))
        if not 1<=page_number<=rankings.TOP_LIMIT//rankings.PAGE_SIZE:
            raise ValueError('Page must be between 1 and 20.')
        months = rankings.catalog()
        if months:
            first = rankings.month_input(params.get('from') or months[0]['month'].strftime('%Y-%m'))
            last = rankings.month_input(params.get('through') or months[-1]['month'].strftime('%Y-%m'))
            if first>last:
                raise ValueError('From must precede through.')
            import time
            start = time.monotonic()
            rows,cached = rankings.ranking_rows(kind,mode,sort,first,last,months)
            elapsed = time.monotonic()-start
    except (ValueError,rankings.RankingsBusy) as exc:
        error = str(exc)
    except QueryCanceled:
        error = 'This period exceeded the 25-second query limit. Choose fewer months.'
    except Exception:
        LOG.exception('Ranking page failed')
        error = 'Rankings unavailable. See the application log.'
    selected = [m for m in months if first and last and first<=m['month']<=last]
    missing = 0
    if first and last:
        missing = max(0,(last.year-first.year)*12+last.month-first.month+1-len(selected))
    base = dict(kind=kind,mode=mode,sort=sort)
    if custom_period and first and last:
        base.update({'from':first.strftime('%Y-%m'),'through':last.strftime('%Y-%m')})
    def link(number):
        return '/admin/debug/rankings?'+urlencode(dict(base,page=number))
    offset = (page_number-1)*rankings.PAGE_SIZE
    context.update(months=months,rows=rows[offset:offset+rankings.PAGE_SIZE],kind=kind,mode=mode,sort=sort,
        first=params.get('from',''),last=params.get('through',''),custom_period=custom_period,
        period_from=first.strftime('%Y-%m') if first else '',
        period_through=last.strftime('%Y-%m') if last else '',
        error=error,can_run='admin.jobs.run' in user.get('permissions',set()),
        selected=selected,missing=missing,cached=cached,elapsed=elapsed,page_number=page_number,offset=offset,
        previous=link(page_number-1) if page_number>1 else None,
        next_page=link(page_number+1) if offset+rankings.PAGE_SIZE<len(rows) else None,
        compact_isk=rankings.compact_isk,full_isk=rankings.full_isk,
        coverage_kills=sum(m['killmails'] for m in selected),coverage_valued=sum(m['valued'] for m in selected),
        coverage_zero=sum(m['zero_damage'] for m in selected),dirty=sum(m['dirty'] for m in selected))
    return templates.TemplateResponse(request=request,name='admin_rankings.html',context=context)


@router.get('/admin/debug/rankings/status')
def status(request: Request):
    user = require_login(request)
    if 'admin.jobs.view' not in user.get('permissions',set()):
        return JSONResponse({'error':'Access denied.'},status_code=403)
    return JSONResponse(killmail_rankings_progress(),headers={'Cache-Control':'no-store'})


@router.post('/admin/debug/rankings/{action}')
def control(request: Request, action: str):
    user = require_login(request)
    if 'admin.jobs.run' not in user.get('permissions',set()):
        return JSONResponse({'error':'Access denied.'},status_code=403)
    if action not in ('start','stop'):
        return JSONResponse({'error':'Unknown action.'},status_code=404)
    try:
        result = run_killmail_rankings() if action=='start' else stop_killmail_rankings()
        audit_log(request,'admin_job_'+action,user_id=user['id'],username=user['username'],
                  target_type='job',target_id='build_killmail_rankings')
        return {'ok':True,'result':result}
    except JobError as exc:
        return JSONResponse({'error':str(exc)},status_code=409)
