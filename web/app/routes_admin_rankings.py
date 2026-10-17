"""Experimental rankings page and explicit monthly builder controls."""
import logging
from datetime import datetime, UTC
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
    import time
    user=require_login(request)
    redirect=require_permission_or_redirect(user,'admin.jobs.view')
    if redirect:
        return redirect
    context=app_context(request=request,user=user,title='EVEOSINT - ISK rankings',active_module='admin',active_menu_key='admin.debug')
    params=request.query_params
    kind=params.get('kind','character');mode=params.get('mode','classic');sort=params.get('sort','destroyed')
    view='ranking'  # Search shares this page; old view=search links remain valid.
    q=params.get('q','').strip()[:100]
    custom_period=bool(params.get('through'))
    months=[];rows=[];first=last=None;error=None;cached=False;elapsed=0;page_number=1;offset=0
    includes=[];excludes=[];labels={};more=False;found_position=None;affiliation_month=None;lookup_default_through=''
    try:
        if kind not in rankings.KINDS or mode not in ('classic','weighted') or sort not in rankings.SORTS or view not in ('ranking','search'):
            raise ValueError('Invalid ranking filter.')
        page_number=int(params.get('page','1'))
        if not 1<=page_number<=20000000:
            raise ValueError('Invalid page.')
        offset=(page_number-1)*rankings.PAGE_SIZE
        includes=rankings.parse_scope(params.getlist('include'))
        excludes=rankings.parse_scope(params.getlist('exclude'))
        labels=rankings.scope_labels(includes+excludes)
        months=rankings.catalog()
        if months:
            first=months[0]['month']
            current_month=datetime.now(UTC).date().replace(day=1)
            if months[-1]['month']<current_month:
                lookup_default_through=months[-1]['month'].strftime('%Y-%m')
            last=rankings.month_input(params.get('through') or months[-1]['month'].strftime('%Y-%m'))
            affiliation_month=last if custom_period or last<current_month else None
            if first>last:
                raise ValueError(f'Choose a month on or after {first:%Y-%m}, the first available month.')
            started=time.monotonic()
            if months[0].get('totals_ready',True):
                target_id=None;position=offset+1
                searching=bool(q or params.get('target_id') or params.get('position'))
                if searching:
                    if params.get('target_id'):
                        target_id=int(params['target_id'])
                        if not 0<target_id<2**63:
                            raise ValueError('Invalid entity.')
                    elif q:
                        matches=rankings.lookup(q,kind,as_of=affiliation_month)
                        exact=[r for r in matches if str(r['name']).casefold()==q.casefold()]
                        chosen=exact or matches
                        if len(chosen)!=1:
                            raise ValueError('Select a name from the suggestions to identify the entity.')
                        target_id=int(chosen[0]['entity_id'])
                    else:
                        position=int(params['position'])
                        if not 1<=position<=1000000000:
                            raise ValueError('Choose a positive rank.')
                # An explicit Universe inclusion keeps the fast unfiltered ranking path.
                effective_includes=[] if 'universe:0' in includes else includes
                if searching or effective_includes or excludes or page_number>20:
                    result=rankings.explore_rows(kind,mode,sort,first,last,as_of=affiliation_month,
                        includes=effective_includes,excludes=excludes,position=position,target_id=target_id,around=searching)
                    rows=result['rows'];offset=result['offset'];more=result['more'];found_position=result['position'] if searching else None
                else:
                    all_rows,cached=rankings.ranking_rows(kind,mode,sort,first,last,months)
                    rows=[dict(r,rank=offset+i+1) for i,r in enumerate(all_rows[offset:offset+rankings.PAGE_SIZE])]
                    more=offset+rankings.PAGE_SIZE<len(all_rows) or len(all_rows)==rankings.TOP_LIMIT and page_number==20
                rows=rankings.enrich_rows(kind,rows,affiliation_month)
            elapsed=time.monotonic()-started
    except (ValueError,rankings.RankingsBusy) as exc:
        error=str(exc)
    except QueryCanceled:
        error='This historical ranking exceeded the query time limit. Try a more recent month.'
    except Exception:
        LOG.exception('Ranking page failed')
        error='Rankings unavailable. See the application log.'
    selected=[m for m in months if first and last and first<=m['month']<=last]
    missing=max(0,(last.year-first.year)*12+last.month-first.month+1-len(selected)) if first and last else 0
    base=dict(kind=kind,mode=mode,sort=sort)
    if custom_period:
        base['through']=params.get('through','')
    if includes:
        base['include']=includes
    if excludes:
        base['exclude']=excludes
    def link(**changes):
        return '/admin/debug/rankings?'+urlencode(dict(base,**changes),doseq=True)
    context.update(months=months,rows=rows,kind=kind,mode=mode,sort=sort,view=view,q=q,
        target_id=params.get('target_id',''),position=params.get('position',''),found_position=found_position,
        include_terms=[dict(token=v,label=labels.get(v,v),logo_url=rankings.scope_logo(v)) for v in includes],
        exclude_terms=[dict(token=v,label=labels.get(v,v),logo_url=rankings.scope_logo(v)) for v in excludes],
        ranking_link=link(view='ranking'),search_link=link(view='search'),
        all_time_link='/admin/debug/rankings?'+urlencode({k:v for k,v in base.items() if k!='through'},doseq=True),
        last=params.get('through',''),custom_period=custom_period,
        period_from=first.strftime('%Y-%m') if first else '',period_through=last.strftime('%Y-%m') if last else '',
        membership_date=('end of '+affiliation_month.strftime('%B %Y')) if affiliation_month else 'today',
        lookup_default_through=lookup_default_through,
        totals_ready=bool(months and months[0].get('totals_ready',True)),error=error,can_run='admin.jobs.run' in user.get('permissions',set()),
        selected=selected,missing=missing,cached=cached,elapsed=elapsed,page_number=page_number,offset=offset,
        previous=link(page=page_number-1) if not found_position and page_number>1 else None,
        next_page=link(page=page_number+1) if not found_position and more else None,
        compact_isk=rankings.compact_isk,full_isk=rankings.full_isk,
        coverage_kills=sum(m['killmails'] for m in selected),coverage_valued=sum(m['valued'] for m in selected),
        coverage_zero=sum(m['zero_damage'] for m in selected),dirty=sum(m['dirty'] for m in selected))
    return templates.TemplateResponse(request=request,name='admin_rankings.html',context=context)


@router.get('/admin/debug/rankings/lookup')
def lookup(request: Request):
    user=require_login(request)
    if 'admin.jobs.view' not in user.get('permissions',set()):
        return JSONResponse({'error':'Access denied.'},status_code=403)
    kind=request.query_params.get('kind','character')
    purpose=request.query_params.get('purpose','target')
    if kind not in rankings.KINDS or purpose not in ('target','scope'):
        return JSONResponse({'error':'Invalid lookup.'},status_code=400)
    try:
        through=request.query_params.get('through','').strip()
        as_of=rankings.month_input(through) if through else None
        return {'results':rankings.lookup(request.query_params.get('q',''),kind,purpose,as_of=as_of)}
    except ValueError as exc:
        return JSONResponse({'error':str(exc)},status_code=400)
    except Exception:
        LOG.exception('Ranking lookup failed')
        return JSONResponse({'error':'Search unavailable.'},status_code=503)


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
