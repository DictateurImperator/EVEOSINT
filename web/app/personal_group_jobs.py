"""On-request group calculations, with bounded short-lived RAM results and no stored analysis."""
from collections import OrderedDict
from contextlib import closing
from datetime import timedelta
from decimal import Decimal
import logging
import threading
import time
from .db import db
from . import personal_group_scans as scans
from . import personal_group_queries as q

LOG=logging.getLogger(__name__)
LOCK_ID=920134801
FIELDS=('kills','losses','destroyed_cents','weighted_destroyed_cents','lost_cents','unvalued_kills','unvalued_losses','matched')
_LOCK=threading.Lock()
_RUNS=OrderedDict()
TTL=120
MAX_RESULTS=8


class NoDemand(Exception):pass


def prune(lock,runs,ttl,now=None):
    now=time.monotonic() if now is None else now
    with lock:
        for key,run in list(runs.items()):
            if run['status']!='running' and now-run['updated']>=ttl:del runs[key]


def start_reaper(lock,runs,ttl):
    def reap():
        while True:
            time.sleep(15)
            prune(lock,runs,ttl)
    threading.Thread(target=reap,name='group-cache-expiry',daemon=True).start()


start_reaper(_LOCK,_RUNS,TTL)

def _sum(rows):
    return {key:sum((Decimal(str(row.get(key,0))) for row in rows),Decimal(0)) for key in FIELDS}


def calculate(group,period,view,can_super,report):
    with closing(db()) as source:
        control=getattr(report,'control',None)
        if control:control.bind(source)
        source.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with source,source.cursor() as cur:
            while True:
                if control:control.check()
                cur.execute('SELECT pg_try_advisory_lock(%s)',(LOCK_ID,))
                if cur.fetchone()[0]:break
                report(phase='Waiting for previous scan to stop')
                time.sleep(.1)
            cur.execute("SET LOCAL timezone='UTC';SET LOCAL jit=off;SET LOCAL statement_timeout='120s';SET LOCAL work_mem='32MB'")
            start=period['from_date'];end=period['through_date']+timedelta(days=1)
            rules=q.load_rules(source);config=group['definition']
            q.actor_predicate(config,rules,'a')
            if view=='ships':
                report(phase='Counting all known members and pilotable ships',checked=0,total=1)
                data=q.pilotable_counts(source,config,rules,start,end,check=lambda:report(phase='Counting all known members and pilotable ships'))
                if not can_super and data['counts']:
                    from .entities import _ship_counts_to_payload
                    metadata=_ship_counts_to_payload(source,data['counts'])
                    denied={str(s['entity_id']) for s in metadata if (s.get('group_name') or '').lower() in ('titan','supercarrier') or (s.get('ship_sde_band') or '').lower()=='supercapital ships'}
                    data['counts']={key:value for key,value in data['counts'].items() if key not in denied}
                result=dict(period,totals={'ships_ready':data['ready']},ship_counts=data['counts'],roster_count=data['roster_count'],skills_count=data['skills_count'])
                report(checked=1,total=1,result=result)
                return result
            parts=[r for r in q.relations(source) if r[0]<end and q.next_month(r[0])>start]
            zones=q.zone_ids(source,config);monthly=[];top={'raw':[],'weighted':[]}
            group=dict(group,membership_rules=rules)
            for index,relation in enumerate(parts):
                month=relation[0]
                report(phase='Scanning killmails',checked=index,total=len(parts),current_month=month.strftime('%Y-%m'))
                if view=='top-kills':
                    candidates=q.month_top_kills(source,group,relation,max(start,month),min(end,q.next_month(month)),zones)
                    for mode,field in [('raw','raw_cents'),('weighted','true_cents')]:
                        top[mode]=sorted(top[mode]+[r for r in candidates if r['mode']==mode],key=lambda r:(r[field],r['killmail_time'],r['killmail_id']),reverse=True)[:20]
                    report(checked=index+1,result=dict(period,top_kills=q.hydrate_top_kills(source,top)))
                    continue
                totals=q.month_totals(source,config,rules,relation,max(start,month),min(end,q.next_month(month)),zones)
                monthly.append(dict(period=month.strftime('%Y-%m'),totals=totals))
                result=dict(period,totals=_sum([m['totals'] for m in monthly]),monthly=list(monthly))
                report(checked=index+1,result=result)
            if view=='top-kills':return dict(period,top_kills=q.hydrate_top_kills(source,top))
            return dict(period,totals=_sum([m['totals'] for m in monthly]),monthly=monthly)


def state(group,period,view,can_super=False):
    if view not in ('stats','ships','top-kills'):raise ValueError('Invalid group result.')
    key=(group['id'],group['revision'],period['from_date'],period['through_date'],view,can_super)
    with _LOCK:
        now=time.monotonic()
        for old,value in list(_RUNS.items()):
            if value['status']!='running' and now-value['updated']>=TTL:del _RUNS[old]
        previous=_RUNS.get(key)
        if previous and previous.get('control') and previous['control'].cancelled.is_set():previous=None
        if previous:
            previous['control'].subscribe()
            previous['last_requested']=now
            _RUNS.move_to_end(key)
            return dict(previous,progress=dict(previous['progress']))
        if any(run['status']=='running' and not run['control'].cancelled.is_set() for run in _RUNS.values()):
            return dict(status='busy',progress={},result=None,error='Another group result is loading. Please retry shortly.')
        while len(_RUNS)>=MAX_RESULTS:_RUNS.popitem(last=False)
        run=dict(status='running',progress=dict(phase='Starting',checked=0,total=0),result=None,updated=now,last_requested=now)
        control=scans.Control();control.subscribe();run['control']=control
        _RUNS[key]=run
        def report(**values):
            with _LOCK:
                control.check()
                if time.monotonic()-run['last_requested']>60:raise NoDemand()
                if 'result' in values:run['result']=values.pop('result')
                run['progress'].update(values);run['updated']=time.monotonic()
        report.control=control
        def worker():
            try:
                result=calculate(group,period,view,can_super,report)
                with _LOCK:
                    control.check()
                    control.unbind()
                    run.update(status='completed',result=result,updated=time.monotonic())
            except (NoDemand,scans.Cancelled):
                with _LOCK:
                    if _RUNS.get(key) is run:_RUNS.pop(key,None)
            except Exception as exc:
                if control.cancelled.is_set():
                    with _LOCK:
                        if _RUNS.get(key) is run:_RUNS.pop(key,None)
                    return
                LOG.exception('On-request group result failed group=%s',group['id'])
                with _LOCK:run.update(status='failed',error=str(exc) if isinstance(exc,ValueError) else 'Could not complete this scan. Choose a month or narrower period.',updated=time.monotonic()-TTL+10)
            finally:control.unbind()
        threading.Thread(target=worker,name=f"group-result-{group['id']}",daemon=True).start()
        return dict(run,progress=dict(run['progress']))
