"""Account-owned groups and a deliberately read-only anonymous publication surface."""
from collections import OrderedDict
from contextlib import closing
from datetime import timedelta
from decimal import Decimal
import json
import logging
import threading
import time
from psycopg2.errors import QueryCanceled
from urllib.parse import urlencode

from fastapi import APIRouter,HTTPException,Request
from fastapi.responses import HTMLResponse,JSONResponse,RedirectResponse
from .auth import require_login,has_permission
from .layout import app_context
from .main_objects import templates
from .db import db
from . import personal_groups as store,personal_group_jobs as jobs,personal_group_queries as queries,personal_group_rankings as local_rankings
from .killmail_rankings import compact_isk,full_isk

router=APIRouter()
LOG=logging.getLogger(__name__)
_CACHE=OrderedDict();_LOCK=threading.Lock();_SLOTS=threading.BoundedSemaphore(2)
jobs.start_reaper(_LOCK,_CACHE,120)
PRIVATE='/account/groups'


def account(request):
    user=require_login(request)
    if not user:raise HTTPException(401)
    if not has_permission(user,'entities.view'):raise HTTPException(403)
    return user


def group_for(request,id):
    user=account(request)
    if not 0<id<2**63:raise HTTPException(404)
    group=store.accessible(id,user['id'])
    if not group:raise HTTPException(404)
    return user,group


def context(request,user,title):
    ctx=app_context(request=request,user=user,title=title,active_module='',active_menu_key='')
    ctx.update(context_title='Account',context_menu=[dict(label='My account',href='/account',icon='♙',active=False),dict(label='My groups',href=PRIVATE,icon='♟',active=True)])
    return ctx


def no_store(response):
    response.headers['Cache-Control']='private, no-store'
    response.headers['X-Robots-Tag']='noindex, nofollow'
    response.headers['Referrer-Policy']='no-referrer'
    return response


@router.get(PRIVATE,response_class=HTMLResponse)
def index(request:Request):
    user=account(request)
    ctx=context(request,user,'My groups');ctx.update(groups=store.listed(user['id']),delegated_groups=store.delegated(user['id']),error=None)
    return no_store(templates.TemplateResponse(request=request,name='personal_groups.html',context=ctx))


@router.get(PRIVATE+'/lookup')
def lookup(request:Request):
    account(request)
    kind=request.query_params.get('kind','member');entity_type=request.query_params.get('entity_type','all');query=request.query_params.get('q','').strip()[:100]
    if len(query)<2:return {'results':[]}
    if kind=='ship':
        from .killmail_filters import search_killboard_filters
        rows=search_killboard_filters('ship',query,limit=20)
    elif kind=='zone':
        from .entities import search_killmail_locations
        rows=search_killmail_locations(query,limit=12)
    elif kind=='member':
        from .coalitions import search_member_entities
        from .killmail_rankings import lookup as find
        if entity_type not in ('all','character','corporation','alliance','coalition'):raise HTTPException(400)
        characters=find(query,'character') if entity_type in ('all','character') else []
        others=search_member_entities(query,limit=12 if entity_type=='all' else 50) if entity_type!='character' else []
        rows=characters+[r for r in others if entity_type=='all' or r.get('entity_type')==entity_type]
    else:raise HTTPException(400)
    def display(r):
        kind=r.get('entity_type');id=r.get('entity_id')
        from .entities import image_url
        picture=r.get('image_url') or r.get('logo_url') or (image_url(kind,id,64) if kind in ('character','corporation','alliance','ship') else None)
        return dict(kind=kind,id=id,label=r.get('label') or (str(r.get('name') or '')+(f" [{r['ticker']}]" if r.get('ticker') else '')),image_url=picture,
                    section={'coalition':'COALITIONS','alliance':'ALLIANCES','corporation':'CORPORATIONS','character':'CHARACTERS','ship':'SHIPS','ship_group':'SHIP TYPES','system':'SYSTEMS','constellation':'CONSTELLATIONS','region':'REGIONS'}.get(kind,str(kind).upper()),
                    subtitle=r.get('subtitle') or ' · '.join(str(v) for v in [kind,r.get('corporation_name'),r.get('alliance_name')] if v))
    if kind=='member':
        order={'coalition':0,'alliance':1,'corporation':2,'character':3};counts={};grouped=[]
        for row in sorted(rows,key=lambda r:order.get(r.get('entity_type'),99)):
            category=row.get('entity_type');counts[category]=counts.get(category,0)+1
            if counts[category]<=6:grouped.append(row)
        rows=grouped
    return no_store(JSONResponse({'results':[display(r) for r in rows[:24]]}))


@router.get(PRIVATE+'/new',response_class=HTMLResponse)
def new(request:Request):
    user=account(request)
    ctx=context(request,user,'Create group');ctx.update(group=None,form_name='',form_description='',form_definition={'members':[],'zones':[]},error=None)
    return no_store(templates.TemplateResponse(request=request,name='personal_group_editor.html',context=ctx))


@router.post(PRIVATE+'/save')
async def save(request:Request):
    user=account(request);form=await request.form()
    id_value=form.get('group_id','');id=None
    try:
        id=int(id_value) if id_value else None
        if id is not None and not 0<id<2**63:raise HTTPException(400)
        selected=store.accessible(id,user['id']) if id else None
        if id is not None and not selected:raise HTTPException(404)
        payload=json.loads(str(form.get('definition','')))
        result=store.save(selected['owner_id'] if selected else user['id'],form.get('name'),form.get('description'),payload,id,actor_id=user['id'])
    except (ValueError,TypeError) as exc:
        ctx=context(request,user,'Edit group');ctx.update(group=store.accessible(id,user['id']) if isinstance(id,int) and 0<id<2**63 else None,
            form_name=str(form.get('name','')),form_description=str(form.get('description','')),
            form_definition=payload if 'payload' in locals() and isinstance(payload,dict) else {'members':[],'zones':[]},error=str(exc))
        return no_store(templates.TemplateResponse(request=request,name='personal_group_editor.html',context=ctx,status_code=400))
    except LookupError:raise HTTPException(404)
    return RedirectResponse(f'{PRIVATE}/{result}',status_code=303)


@router.get(PRIVATE+'/{id}/edit',response_class=HTMLResponse)
def edit(request:Request,id:int):
    user,group=group_for(request,id)
    ctx=context(request,user,'Edit group');ctx.update(group=group,form_name=group['name'],form_description=group['description'],form_definition=group['definition'],error=None)
    return no_store(templates.TemplateResponse(request=request,name='personal_group_editor.html',context=ctx))


@router.post(PRIVATE+'/{id}/delete')
def delete(request:Request,id:int):
    user,group=group_for(request,id)
    if user['id']!=group['owner_id']:raise HTTPException(403)
    store.remove(id,user['id'])
    return RedirectResponse(PRIVATE,status_code=303)


@router.post(PRIVATE+'/{id}/sharing')
async def sharing(request:Request,id:int):
    user,group=group_for(request,id);form=await request.form()
    try:store.sharing(id,group['owner_id'],form.get('enabled')=='on',form.getlist('tabs'),actor_id=user['id'])
    except ValueError as exc:return JSONResponse({'error':str(exc)},status_code=400)
    except LookupError:raise HTTPException(404)
    return RedirectResponse(f'{PRIVATE}/{id}',status_code=303)



def ranking_options(request):
    kind=request.query_params.get('kind','character');mode=request.query_params.get('mode','classic');sort=request.query_params.get('sort','destroyed')
    try:page=int(request.query_params.get('page','1'))
    except ValueError:raise HTTPException(400)
    if kind not in local_rankings.KINDS or mode not in ('classic','weighted') or sort not in local_rankings.SORTS or not 1<=page<=20:raise HTTPException(400)
    return dict(kind=kind,mode=mode,sort=sort,page=page)


@router.get(PRIVATE+'/{id}/managers/lookup')
def manager_lookup(request:Request,id:int):
    user,group=group_for(request,id)
    if user['id']!=group['owner_id']:raise HTTPException(403)
    query=request.query_params.get('q','').strip()[:100]
    return no_store(JSONResponse({'results':store.manager_accounts(id,user['id'],query) if len(query)>=2 else []}))


@router.post(PRIVATE+'/{id}/managers')
async def manage(request:Request,id:int):
    user,group=group_for(request,id)
    if user['id']!=group['owner_id']:raise HTTPException(403)
    form=await request.form()
    try:
        manager_id=int(form.get('user_id','0'))
        if not 0<manager_id<2**63:raise ValueError('Choose an account.')
        if form.get('action') not in ('add','remove'):raise ValueError('Invalid action.')
        store.set_manager(id,user['id'],manager_id,remove=form.get('action')=='remove')
    except (ValueError,TypeError) as exc:return JSONResponse({'error':str(exc)},status_code=400)
    except LookupError:raise HTTPException(404)
    return RedirectResponse(f'{PRIVATE}/{id}?view=management',status_code=303)


def selection(request,group,public=False):
    allowed={'period','view','cursor','kind','mode','sort','page'} | ({'from','through'} if not public else set())
    if set(request.query_params)-allowed:raise HTTPException(400,'Only saved-scope periods, page selection and pagination are accepted.')
    view=request.query_params.get('view') or (group['public_tabs'][0] if public and group['public_tabs'] else 'stats')
    if view not in store.TABS and not (view=='management' and not public):raise HTTPException(400)
    if view!='ranking' and set(request.query_params)&{'kind','mode','sort','page'}:raise HTTPException(400)
    if view=='ranking':ranking_options(request)
    if public and view not in group['public_tabs']:raise HTTPException(403)
    period=request.query_params.get('period','all')
    if public and period=='custom':raise HTTPException(403)
    custom=None
    try:
        if period=='custom' and not public:
            start=store.day(request.query_params.get('from'));end=store.day(request.query_params.get('through'))
            if not start or not end or start>end:raise ValueError('Choose both dates in chronological order.')
            custom=(start,end)
        elif set(request.query_params)&{'from','through'}:raise ValueError('Dates are only accepted for a private custom period.')
        result=None if view=='management' else store.period_result(group,period,custom=custom)
    except ValueError as exc:raise HTTPException(400,str(exc))
    return view,period,result


def ratio_label(totals):
    raw=Decimal(str(totals['destroyed_cents']))
    return f"{100*Decimal(str(totals['weighted_destroyed_cents']))/raw:.3f}%" if raw else '—'


def render_page(request,group,user=None,public=False):
    view,period,result=selection(request,group,public)
    available=store.periods(group) if view!='management' else []
    if public and not result:raise HTTPException(404)
    base=f"/public/groups/{group['public_token']}" if public else f"{PRIVATE}/{group['id']}"
    ctx=dict(request=request,title=group['name'],show_app_layout=False,username=None) if public else context(request,user,group['name'])
    # Never pass definitions, private rules, owner IDs or account metadata to public templates.
    ctx.update(group_name=group['name'],group_description=group['description'],public=public,
        group=None if public else group,base_url=base,view=view,period=period,result=result,is_owner=not public and user['id']==group['owner_id'],
        managers=store.managers(group['id'],user['id']) if not public and view=='management' and user['id']==group['owner_id'] else [],
        ranking=ranking_options(request) if view=='ranking' else None,
        available=available,tabs=group['public_tabs'] if public else list(store.TABS),compact_isk=compact_isk,full_isk=full_isk,
        ratio_for=ratio_label,custom_from=request.query_params.get('from',''),custom_through=request.query_params.get('through',''))
    return no_store(templates.TemplateResponse(request=request,name='personal_group.html',context=ctx))


@router.get(PRIVATE+'/{id}',response_class=HTMLResponse)
def page(request:Request,id:int):
    user,group=group_for(request,id)
    return render_page(request,group,user)


@router.get('/public/groups/{token}',response_class=HTMLResponse)
def public_page(request:Request,token:str):
    group=store.public(token)
    if not group:raise HTTPException(404)
    return render_page(request,group,public=True)


def data_result(request,group,public=False):
    view,period,result=selection(request,group,public)
    if not result:raise HTTPException(404,'This period does not overlap available group history.')
    if view!='killboard' and request.query_params.get('cursor'):raise HTTPException(400)
    if view=='ranking':
        if request.query_params.get('cursor'):raise HTTPException(400)
        options=ranking_options(request)
        state=local_rankings.state(group,result,options['kind'],options['mode'],options['sort'])
        start=(options['page']-1)*50;rows=state['rows'][start:start+50]
        base=f"/public/groups/{group['public_token']}" if public else f"{PRIVATE}/{group['id']}"
        def link(page):return base+'?'+urlencode(dict(view='ranking',period=period,**dict(options,page=page),**({'from':request.query_params['from'],'through':request.query_params['through']} if period=='custom' else {})))
        html=templates.get_template('personal_group_ranking.html').render(rows=rows,public=public,kind=options['kind'],compact_isk=compact_isk,
            full_isk=full_isk,previous=link(options['page']-1) if options['page']>1 else None,next_page=link(options['page']+1) if start+50<len(state['rows']) else None)
        return no_store(JSONResponse(dict(html=html,status=state['status'],progress=state['progress'],error=state.get('error')),status_code=202 if state['status'] in ('running','busy') else 200))
    if view in ('stats','ships'):
        can_super=False
        if view=='ships':
            from .auth import get_user_permissions
            can_super='superintel.view' in get_user_permissions(group['owner_id'])
        state=jobs.state(group,result,view,can_super=can_super)
        calculated=state['result'];html=''
        if calculated:
            if view=='stats':
                base=f"/public/groups/{group['public_token']}" if public else f"{PRIVATE}/{group['id']}"
                html=templates.get_template('personal_group_stats.html').render(result=calculated,base_url=base,compact_isk=compact_isk,full_isk=full_isk,ratio_for=ratio_label)
            else:
                with closing(db()) as conn:
                    conn.set_session(readonly=True)
                    with conn:
                        from .entities import _ship_counts_to_payload
                        ships=_ship_counts_to_payload(conn,calculated['ship_counts'])
                html=templates.get_template('personal_group_ships.html').render(result=calculated,ships=ships)
        return no_store(JSONResponse(dict(html=html,status=state['status'],progress=state['progress'],error=state.get('error')),status_code=202 if state['status'] in ('running','busy') else 200))
    key=(group['id'],group['revision'],str(result['built_at']),public,view,period,result['from_date'],result['through_date'],request.query_params.get('cursor',''))
    with _LOCK:
        hit=_CACHE.get(key)
        if hit and time.monotonic()-hit['updated']<120:
            _CACHE.move_to_end(key)
            return no_store(JSONResponse(hit['payload']))
    if not _SLOTS.acquire(blocking=False):return no_store(JSONResponse({'error':'Two result pages are loading. Please retry in a moment.'},status_code=429))
    try:
        with closing(db()) as conn:
            conn.set_session(readonly=True)
            with conn,conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout='8s'");cur.execute('SET LOCAL jit=off')
                if view!='killboard':raise HTTPException(400)
                transient=dict(group,membership_rules=queries.load_rules(conn))
                try:kb=queries.killboard(conn,transient,result,request.query_params.get('cursor',''))
                except ValueError:raise HTTPException(400,'Invalid cursor.')
                html=templates.get_template('personal_group_killboard.html').render(rows=kb['rows'],public=public)
                payload=dict(html=html,next_cursor=kb['next_cursor'],scanned=kb['scanned'],total=kb['total'])
        with _LOCK:
            _CACHE[key]=dict(updated=time.monotonic(),status='completed',payload=payload);_CACHE.move_to_end(key)
            while len(_CACHE)>32:_CACHE.popitem(last=False)
        return no_store(JSONResponse(payload))
    except QueryCanceled:
        LOG.warning('Personal group page exceeded query limit group=%s period=%s',group['id'],period)
        return no_store(JSONResponse({'error':'This historical scan reached the query time limit. Choose a month or year to narrow it.'},status_code=504))
    finally:_SLOTS.release()


@router.get(PRIVATE+'/{id}/data')
def data(request:Request,id:int):
    user,group=group_for(request,id)
    return data_result(request,group)


@router.get('/public/groups/{token}/data')
def public_data(request:Request,token:str):
    group=store.public(token)
    if not group:raise HTTPException(404)
    return data_result(request,group,public=True)
