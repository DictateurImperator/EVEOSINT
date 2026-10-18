"""Small passive MER status snapshots; never starts or alters a job."""
from contextlib import closing
from contextvars import ContextVar
from datetime import datetime,UTC
from pathlib import Path
import json
import os
import re
import threading

PATH=Path.home()/'eveosint/data/mer/operations_progress.json'
LABELS={'MER_ALL':'MER ALL','SCAN':'CCP archive catalog','DOWNLOAD':'Download ZIPs','ANALYZE':'Analyze archives','KILL_IMPORT':'Import kill dumps','ALLIANCE_HISTORY':'Historical alliance IDs','KILL_MATCH':'Match existing killmails'}
_LOCK=threading.RLock()
_CURRENT=ContextVar('mer_operation',default=None)


def _load():
    try:
        data=json.loads(PATH.read_text())
        return data if isinstance(data,dict) else {}
    except (OSError,ValueError):return {}


def _save(data):
    PATH.parent.mkdir(parents=True,exist_ok=True)
    temporary=PATH.with_suffix('.tmp')
    temporary.write_text(json.dumps(data))
    temporary.replace(PATH)


def record(message):
    match=re.match(r'^\[([A-Z_]+)\]\s*(.*)',message)
    if not match or match[1] not in LABELS:return
    key,text=match.groups()
    if text=='BUSY':return # A duplicate launch must not overwrite the active run.
    _CURRENT.set(key)
    now=datetime.now(UTC).isoformat()
    numbers={k:int(v) for k,v in re.findall(r'\b([a-z_]+)=(\d+)',text)}
    start=text.startswith(('START','PINNED_START'))
    end=text.startswith(('END','PINNED_END'))
    month=re.match(r'(?:PINNED )?(\d{4}-\d{2})\b',text)
    part=re.search(r'partition=(\S+)',text)
    with _LOCK:
        data=_load();state=data.get(key,{})
        if start and (state.get('status')!='running' or state.get('pid')!=os.getpid()):
            state=dict(status='running',started_at=now,processed=0,metrics={},backends=[],pid=os.getpid())
        if not state:return
        state.update(updated_at=now,message=text[:700])
        if start:
            state['total']=numbers.get('total',numbers.get('files',numbers.get('rows',numbers.get('months',state.get('total')))))
        if text.startswith('PLAN'):state['total']=numbers.get('total')
        if key=='SCAN' and text.startswith('DYNAMIC'):state['total']=(state.get('total') or 0)+numbers.get('months',0)
        if text.startswith('STEP_DONE'):state['processed']=numbers.get('processed',state.get('processed',0))
        stage=re.search(r'stage=([A-Z_]+)',text)
        if stage:state['current']=LABELS.get(stage[1],'Economic data import' if stage[1]=='ECONOMY' else stage[1])
        if month:state['current']=month[1]
        if part:state['current']=part[1]
        done=(month and (key=='SCAN' or 'state=' in text or 'status=' in text or 'FAILED=' in text)) or (part and 'rows=' in text)
        if done:
            state['processed']=state.get('processed',0)+1
            for name,value in numbers.items():state['metrics'][name]=state['metrics'].get(name,0)+value
        if text.startswith('FAILED='):
            state.update(status='failed',finished_at=now)
        elif end:
            state.update(status='completed_with_errors' if numbers.get('failed',numbers.get('errors',0)) else 'completed',finished_at=now)
            state['metrics'].update(numbers)
            state['processed']=numbers.get('processed',state.get('total') or state.get('processed',0))
        data[key]=state;_save(data)


def bind_current(connection):
    key=_CURRENT.get()
    if not key or not hasattr(connection,'get_backend_pid'):return
    try:
        with _LOCK:
            data=_load();state=data.get(key,{})
            if state.get('status')!='running':return
            pid=connection.get_backend_pid()
            state['backends']=(state.get('backends',[])+[pid])[-8:]
            data[key]=state;_save(data)
    except Exception:pass


def snapshot():
    with _LOCK:data=_load()
    now=datetime.now(UTC)
    for key,state in data.items():
        if state.get('status')=='running' and not Path(f"/proc/{state.get('pid',0)}").exists():
            state['status']='interrupted';state['finished_at']=state.get('updated_at',state.get('started_at'))
        try:state['elapsed_seconds']=int(((datetime.fromisoformat(state.get('finished_at')) if state.get('finished_at') else now)-datetime.fromisoformat(state['started_at'])).total_seconds())
        except (ValueError,TypeError,KeyError):state['elapsed_seconds']=0
    pids=sorted({pid for state in data.values() if state.get('status')=='running' for pid in state.get('backends',[])})
    if pids:
        try:
            from .db import db
            with closing(db()) as conn:
                conn.set_session(readonly=True)
                with conn,conn.cursor() as cur:
                    cur.execute("SET LOCAL statement_timeout='1000ms'")
                    cur.execute("SELECT pid,state,wait_event_type,wait_event,pg_blocking_pids(pid) FROM pg_stat_activity WHERE pid=ANY(%s)",(pids,))
                    activity={row[0]:row[1:] for row in cur}
            for state in data.values():
                if state.get('status')!='running':continue
                entries=[activity[pid] for pid in state.get('backends',[]) if pid in activity]
                blocked=[r for r in entries if r[1]=='Lock']
                state['waiting_for_lock']=bool(blocked)
                if blocked:
                    blockers={pid for row in blocked for pid in row[3]}
                    state['waiting_for']=[LABELS[k] for k,v in data.items() if blockers.intersection(v.get('backends',[]))]
                state['sql_active']=any(r[0]=='active' for r in entries)
        except Exception:
            for state in data.values():
                if state.get('status')=='running':state['sql_status_unavailable']=True
    return {'operations':[dict(key=key,label=label,**data.get(key,{'status':'not_started'})) for key,label in LABELS.items()]}


def log_tail(path,lines=80):
    try:
        with path.open('rb') as handle:
            handle.seek(0,2);size=handle.tell();handle.seek(max(0,size-65536))
            return '\n'.join(handle.read().decode('utf-8',errors='replace').splitlines()[-lines:])
    except OSError:return 'No MER log available yet.'
