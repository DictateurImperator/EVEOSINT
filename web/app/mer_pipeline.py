"""Explicit MER ALL orchestration. Never runs from a page/status GET."""
import json
import threading
import time
from . import mer,jobs

_LOCK=threading.Lock()


def running():return _LOCK.locked()


def economy():
    jobs.run_mer_economy_import_job()
    metadata=json.loads(jobs._status_path('import_mer_economy').read_text())
    expected_pid=metadata['pid']
    while True:
        state=jobs.read_mer_economy_progress()
        if not state['running']:break
        time.sleep(1)
    progress=state.get('progress') or {}
    if progress.get('pid')!=expected_pid or progress.get('phase')!='completed' or progress.get('failed',0):
        raise RuntimeError('Economic import did not complete successfully. See the economic job log.')
    return {'failed':0}


def steps():
    return [('SCAN','Scan CCP MER',mer.scan_all_mer),('DOWNLOAD','Download missing ZIPs',mer.download_mer_archives),
        ('ANALYZE','Analyze archives',mer.analyze_mer_archives),('KILL_IMPORT','Import kill dumps',mer.import_mer_kill_dumps),
        ('ECONOMY','Import economic data',economy),('ALLIANCE_HISTORY','Resolve historical alliance IDs',mer.enrich_mer_entity_ids),
        ('KILL_MATCH','Reconcile existing killmails',mer.match_mer_killmails)]


def run():
    try:
        selected=steps()
        mer._mer_log(f'[MER_ALL] PLAN total={len(selected)}')
        for index,(key,label,function) in enumerate(selected):
            mer._mer_log(f'[MER_ALL] STEP stage={key} {label}')
            result=function() or {}
            if result.get('busy') or result.get('failed') or result.get('errors') or result.get('changed'):
                raise RuntimeError(f'{label} did not finish cleanly; inspect its status/log before retrying.')
            mer._mer_log(f'[MER_ALL] STEP_DONE stage={key} processed={index+1}')
        mer._mer_log(f'[MER_ALL] END processed={len(selected)} failed=0 Economic data imported; killmails reconciled.')
    except Exception as exc:
        mer._mer_log(f'[MER_ALL] FAILED={exc}')
    finally:_LOCK.release()


def start():
    if not _LOCK.acquire(blocking=False):return False
    try:
        if any(lock.locked() for lock in (mer._download_lock,mer._analyze_lock,mer._import_kills_lock,mer._map_alliance_ids_lock,mer._match_killmails_lock)) or jobs.read_mer_economy_progress()['running']:
            _LOCK.release();return False
        mer._mer_log('[MER_ALL] START Waiting to start sequential pipeline')
        threading.Thread(target=run,name='mer-all',daemon=True).start()
        return True
    except Exception:
        _LOCK.release();raise
