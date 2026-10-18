"""Permission-checked, explicit on-request Debug economics calculations."""
from contextlib import closing
from datetime import date
from secrets import token_urlsafe
import threading
import logging
import time
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from .auth import require_login, require_permission_or_redirect
from .db import db
from . import economic_indices as economy

LOG=logging.getLogger(__name__)
router=APIRouter()
_LOCK=threading.Lock()
_RUNS={}
_SLOTS=threading.BoundedSemaphore(2)


def access(request):
    user=require_login(request)
    redirect=require_permission_or_redirect(user,'admin.jobs.view')
    return user,redirect


def reply(value,status=200):return JSONResponse(value,status_code=status,headers={'Cache-Control':'no-store'})


def prune():
    now=time.monotonic()
    for id,run in list(_RUNS.items()):
        if run['status']!='running' and now-run['updated']>120:del _RUNS[id]


@router.get('/admin/debug/economy/catalog')
def catalog(request:Request):
    user,redirect=access(request)
    if redirect:return redirect
    try:
        with closing(db()) as conn:
            conn.set_session(readonly=True)
            with conn,conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout='10s'")
                available=economy.catalog(conn)
                cur.execute('SELECT min(price_date),max(price_date) FROM market.zkill_daily_prices')
                first,last=cur.fetchone()
        return reply({'first':first.isoformat() if first else None,'last':last.isoformat() if last else None,
            'baskets':[{'key':b['key'],'label':b['label'],'count':len(b['ids'])} for b in available['baskets'].values()],
            'items':[{'id':i['id'],'name':i['name'],'group':i['group_name']} for i in available['items'].values()]})
    except Exception:return reply({'error':'SDE or local zKill prices are unavailable. Check their imports.'},503)


@router.post('/admin/debug/economy/calculate')
async def start(request:Request):
    user,redirect=access(request)
    if redirect:return redirect
    try:
        payload=await request.json()
        if not isinstance(payload,dict):raise ValueError('Invalid basket.')
        if len(str(payload))>40000:raise ValueError('Basket is too large.')
        with closing(db()) as conn:
            conn.set_session(readonly=True)
            with conn:
                available=economy.catalog(conn);economy.validate(payload,available)
        owner=user['id']
        # Superseding a selection cancels the owner's previous query; other users retain theirs.
        with _LOCK:
            prune();previous=[r for r in _RUNS.values() if r['owner']==owner and r['status']=='running']
            for run in previous:run['control']['stop'].set()
        for run in previous:
            connection=run['control'].get('connection')
            if connection:
                try:connection.cancel()
                except Exception:pass
        if not _SLOTS.acquire(blocking=False):return reply({'error':'Two calculations are active. Retry shortly.'},429)
        id=token_urlsafe(18);control={'stop':threading.Event(),'connection':None}
        run={'owner':owner,'status':'running','progress':{},'result':None,'updated':time.monotonic(),'last_requested':time.monotonic(),'control':control}
        with _LOCK:
            # Bound retained results as well as concurrent workers.
            for old in [k for k,r in _RUNS.items() if r['status']!='running'][:-2]:del _RUNS[old]
            _RUNS[id]=run
        def report(**values):
            with _LOCK:
                if time.monotonic()-run['last_requested']>60:
                    control['stop'].set();raise ValueError('Stopped: this page has not requested progress for 60 seconds.')
                run['progress']=values;run['updated']=time.monotonic()
        def worker():
            try:
                result=economy.calculate(payload,report,control)
                with _LOCK:run.update(status='cancelled' if control['stop'].is_set() else 'completed',result=None if control['stop'].is_set() else result)
            except Exception as exc:
                if not control['stop'].is_set():LOG.exception('Debug economic index calculation failed')
                with _LOCK:run.update(status='cancelled' if control['stop'].is_set() else 'failed',error=str(exc) if isinstance(exc,ValueError) else 'Price scan failed or exceeded its time limit. Choose a smaller basket.')
            finally:
                control['connection']=None;run['updated']=time.monotonic();_SLOTS.release()
        try:threading.Thread(target=worker,name='debug-economics',daemon=True).start()
        except Exception:
            with _LOCK:_RUNS.pop(id,None)
            _SLOTS.release();raise
        return reply({'id':id},202)
    except (ValueError,TypeError) as exc:return reply({'error':str(exc)},400)


@router.get('/admin/debug/economy/status/{id}')
def status(request:Request,id:str):
    user,redirect=access(request)
    if redirect:return redirect
    with _LOCK:
        prune();run=_RUNS.get(id)
        if not run or run['owner']!=user['id']:return reply({'error':'Calculation expired. Calculate again.'},404)
        run['last_requested']=time.monotonic()
        return reply({key:value for key,value in run.items() if key not in ('control','owner','updated','last_requested')})


@router.post('/admin/debug/economy/stop/{id}')
def stop(request:Request,id:str):
    user,redirect=access(request)
    if redirect:return redirect
    with _LOCK:
        run=_RUNS.get(id)
        if not run or run['owner']!=user['id']:return reply({'error':'Calculation not found.'},404)
        run['control']['stop'].set();connection=run['control'].get('connection')
    if connection:
        try:connection.cancel()
        except Exception:pass
    return reply({'status':'cancelled'})
