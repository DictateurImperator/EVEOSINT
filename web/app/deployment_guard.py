"""Deployment-only maintenance and web request interruption.

The existing privileged Nginx worker installs the gate and stops only the web
unit. A separate temporary systemd monitor clears maintenance after the updater
exits and a fresh web process passes health checks, including on old rollbacks.
No daily job or database process is stopped.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

RUNTIME = Path('/run/eveosint-maintenance')
WEB_SERVICE = 'eveosint-web.service'
UPDATE_SERVICE = 'eveosint-code-update.service'
SYSTEMCTL = '/usr/bin/systemctl'
BEGIN = '# BEGIN EVEOSINT DEPLOYMENT MAINTENANCE'
END = '# END EVEOSINT DEPLOYMENT MAINTENANCE'
PAGE = ('<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="refresh" content="5"><title>EVEOSINT maintenance</title>'
        '<body style="background:#080e16;color:#d9e7f6;font:18px sans-serif;padding:10vh 8vw">'
        '<h1>EVEOSINT is undergoing maintenance</h1>'
        '<p>The site will return after the update has been checked.</p>'
        '<p>This page refreshes automatically.</p></body></html>')


class MaintenanceError(RuntimeError):
    pass


def state():
    try:
        path=RUNTIME/'state.json'
        if path.is_symlink():return {}
        return json.loads(path.read_text())
    except (OSError,ValueError):
        return {}


def atomic_runtime_file(path, content, mode):
    fd, temporary=tempfile.mkstemp(prefix='.maintenance-',dir=RUNTIME)
    try:
        with os.fdopen(fd,'wb') as handle:
            handle.write(content)
            os.fchmod(handle.fileno(),mode)
            if os.geteuid()==0:
                os.fchown(handle.fileno(),RUNTIME.stat().st_uid,RUNTIME.stat().st_gid)
        os.replace(temporary,path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_state(**fields):
    payload=state();payload.update(fields,updated=datetime.now(timezone.utc).isoformat())
    atomic_runtime_file(RUNTIME/'state.json',json.dumps(payload).encode(),0o600)


def web_pid():
    r=subprocess.run([SYSTEMCTL,'show',WEB_SERVICE,'-p','MainPID','--value'],
                     capture_output=True,text=True,timeout=3,check=False)
    if r.returncode != 0 or not r.stdout.strip().isdigit():
        raise MaintenanceError('web_state_unavailable')
    return int(r.stdout.strip())


def update_active():
    r=subprocess.run([SYSTEMCTL,'show',UPDATE_SERVICE,'-p','ActiveState','--value'],
                     capture_output=True,text=True,timeout=3,check=False)
    if r.returncode != 0:
        raise MaintenanceError('update_state_unavailable')
    return r.stdout.strip() in ('active','activating','deactivating','reloading')


def updater_pid():
    r=subprocess.run([SYSTEMCTL,'show',UPDATE_SERVICE,'-p','MainPID','--value'],
                     capture_output=True,text=True,timeout=3,check=False)
    if r.returncode != 0 or not r.stdout.strip().isdigit():
        raise MaintenanceError('update_state_unavailable')
    return int(r.stdout.strip())


def nginx_with_gate(content, flag):
    """Insert into proxying server blocks, ignoring braces in comments/quotes."""
    if BEGIN in content:
        if END not in content or str(flag) not in content:
            raise MaintenanceError('incomplete_maintenance_configuration')
        return content
    # Preserve positions while masking comments and quoted strings for brace matching.
    masked=list(content);quote=None;comment=False;escaped=False
    for i,ch in enumerate(content):
        if comment:
            if ch=='\n':comment=False
            else:masked[i]=' '
        elif quote:
            masked[i]=' '
            if escaped:escaped=False
            elif ch=='\\':escaped=True
            elif ch==quote:quote=None
        elif ch=='#':comment=True;masked[i]=' '
        elif ch in ('"',"'"):quote=ch;masked[i]=' '
    mask=''.join(masked)
    starts=[]
    for match in re.finditer(r'\bserver\s*\{',mask):
        opening=mask.index('{',match.start());depth=1;end=opening+1
        while depth and end<len(mask):
            depth+=(mask[end]=='{')-(mask[end]=='}');end+=1
        if depth:
            raise MaintenanceError('invalid_nginx_server_block')
        block=mask[opening+1:end-1]
        if re.search(r'\bproxy_pass\s+http://(?:127\.0\.0\.1|localhost):8000(?:/|\s|;)',block):
            starts.append(opening+1)
    if not starts:
        raise MaintenanceError('web_proxy_configuration_missing')
    # Flag path comes from the application constant, never from an HTTP request.
    quoted=str(flag).replace('\\','\\\\').replace('"','\\"').replace('$','\\$')
    addition=(f'\n    {BEGIN}\n'
              f'    if (-f "{quoted}") {{ return 503; }}\n'
              '    error_page 503 =503 @eveosint_deployment_maintenance;\n'
              '    location @eveosint_deployment_maintenance {\n'
              '        default_type text/html;\n'
              '        add_header Retry-After 5 always;\n'
              '        add_header X-EVEOSINT-Maintenance 1 always;\n'
              '        add_header Cache-Control "no-store" always;\n'
              f"        return 503 '{PAGE}';\n"
              f'    }}\n    {END}\n')
    for pos in reversed(starts):content=content[:pos]+addition+content[pos:]
    return content


def start_monitor(root, request_id, runner):
    if not re.fullmatch(r'[a-f0-9]{32}',request_id):
        raise MaintenanceError('invalid_maintenance_request')
    source=RUNTIME/'monitor.py'
    atomic_runtime_file(source,Path(__file__).read_bytes(),0o644)
    unit='eveosint-deployment-maintenance-'+request_id
    result=runner(['/usr/bin/systemd-run','--quiet','--collect','--unit='+unit,
                   '--property=User='+str(root.stat().st_uid),
                   '--property=Group='+str(root.stat().st_gid),
                   '--property=RuntimeMaxSec=1800','--property=Type=exec',
                   str(root/'venv/bin/python'),str(source),'monitor',str(root),request_id],timeout=10)
    if result.returncode != 0:
        raise MaintenanceError('maintenance_monitor_start_failed')


def stop_web_requests(runner, grace=3):
    """Stop precisely the web unit, never the updater or daily pipeline units."""
    result=runner([SYSTEMCTL,'--no-block','stop',WEB_SERVICE],timeout=5)
    if result.returncode != 0:
        raise MaintenanceError('web_stop_failed')
    deadline=time.monotonic()+grace
    while web_pid() and time.monotonic()<deadline:time.sleep(.1)
    if web_pid():
        result=runner([SYSTEMCTL,'kill','--signal=SIGKILL','--kill-whom=all',WEB_SERVICE],timeout=5)
        if result.returncode != 0 and web_pid():
            raise MaintenanceError('web_requests_interruption_failed')
    deadline=time.monotonic()+3
    while web_pid() and time.monotonic()<deadline:time.sleep(.1)
    if web_pid():
        raise MaintenanceError('web_did_not_stop')


def privileged_prepare(request, nginx):
    """Runs only in the existing root Nginx configuration systemd worker."""
    root=nginx.REPO_ROOT
    if RUNTIME.is_symlink():raise MaintenanceError('invalid_maintenance_directory')
    RUNTIME.mkdir(mode=0o755,parents=True,exist_ok=True)
    os.chown(RUNTIME,root.stat().st_uid,root.stat().st_gid);RUNTIME.chmod(0o755)
    request_id=request['request_id']
    old_pid=web_pid()
    worker=updater_pid()
    if not worker or not update_active():
        raise MaintenanceError('deployment_worker_not_running')
    content=nginx.load_nginx_config()
    gated=nginx_with_gate(content,RUNTIME/'active')
    if gated!=content:
        # Reuse the existing test/reload/backup/restore implementation.
        result=nginx.apply_config(dict(request,content=gated))
        if result != 0:return result
    start_monitor(root,request_id,nginx._run)
    write_state(request_id=request_id,worker_pid=worker,old_web_pid=old_pid,phase='interrupting_requests')
    atomic_runtime_file(RUNTIME/'active',request_id.encode(),0o644)
    try:
        # The flag is visible to Nginx before touching the upstream process.
        stop_web_requests(nginx._run)
        write_state(phase='maintenance_ready')
        nginx._write_worker_status(request,phase='done',result='success',message='Maintenance enabled; web requests stopped. Daily jobs are untouched.')
        return 0
    except Exception:
        write_state(phase='failed',message='Web interruption failed; maintenance remains enabled.')
        raise


def prepare(root, force=False):
    """Called by the updater before checkout, or by the legacy setup hook."""
    current=state()
    if not force and (RUNTIME/'active').exists() and current.get('worker_pid')==updater_pid() and current.get('phase')=='maintenance_ready':
        return
    if __package__:
        from . import nginx_config as nginx
    else:
        import nginx_config as nginx
    request_id=nginx.queue_deployment_maintenance()
    deadline=time.monotonic()+35
    while time.monotonic()<deadline:
        result=nginx._read_json(nginx.STATUS_FILE,{})
        if result.get('request_id')==request_id:
            if result.get('result')=='success' and state().get('phase')=='maintenance_ready':return
            if result.get('result')=='failed':raise MaintenanceError('maintenance_preparation_failed')
        time.sleep(.15)
    raise MaintenanceError('maintenance_preparation_failed')


def prepare_if_updater(root):
    # Prevent schema setup / ordinary application startup from stopping the site.
    try:group=Path('/proc/self/cgroup').read_text()
    except OSError:return
    if '/eveosint-code-update.service' in group:prepare(root)


def healthy():
    try:
        req=urllib.request.Request('http://127.0.0.1:8000/login',headers={'User-Agent':'EVEOSINT-DeploymentHealth/1.0'})
        with urllib.request.urlopen(req,timeout=2) as response:return response.status==200
    except Exception:return False


def monitor(root,request_id):
    deadline=time.monotonic()+1750
    while time.monotonic()<deadline:
        current=state()
        if current.get('request_id')==request_id:break
        time.sleep(.2)
    else:return 1
    while time.monotonic()<deadline:
        current=state()
        if current.get('request_id')!=request_id:return 0
        if not update_active():break
        time.sleep(.25)
    else:
        write_state(phase='failed',message='Deployment exceeded its supervision window; maintenance remains enabled.');return 1
    # Works even if an old updater restored a version without this module.
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        pid=web_pid()
        if pid and pid!=current.get('old_web_pid') and healthy():
            if state().get('request_id')!=request_id:return 0
            (RUNTIME/'active').unlink(missing_ok=True)
            write_state(phase='complete',message='Fresh web service passed health checks; maintenance removed.')
            return 0
        time.sleep(.25)
    write_state(phase='failed',message='Web health check failed; maintenance remains enabled.')
    return 1


if __name__=='__main__':
    if len(sys.argv)!=4 or sys.argv[1]!='monitor':raise SystemExit('deployment monitor arguments required')
    raise SystemExit(monitor(Path(sys.argv[2]),sys.argv[3]))
