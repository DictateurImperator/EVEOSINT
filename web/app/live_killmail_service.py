"""Explicitly authorized systemd installation via the normal Git updater."""
from pathlib import Path
import time
import uuid

SERVICE='eveosint-live-killmails.service'
UNIT_DIR=Path('/etc/systemd/system')


def privileged_install(request, nginx):
    # Fixed unit from the reviewed checkout; requests cannot supply a command or path.
    source=nginx.REPO_ROOT/'deploy'/SERVICE
    target=UNIT_DIR/SERVICE
    desired=source.read_text()
    previous=target.read_text() if target.exists() else None
    try:
        if previous!=desired:
            temporary=target.with_suffix('.tmp')
            temporary.write_text(desired);temporary.chmod(0o644);temporary.replace(target)
            result=nginx._run([nginx.SYSTEMCTL,'daemon-reload'],timeout=10)
            if result.returncode:raise RuntimeError('Live service reload failed')
        for operation in ('enable','restart'):
            result=nginx._run([nginx.SYSTEMCTL,operation,SERVICE],timeout=15)
            if result.returncode:raise RuntimeError('Live service activation failed')
    except Exception:
        if previous is None:
            nginx._run([nginx.SYSTEMCTL,'disable','--now',SERVICE],timeout=15)
            target.unlink(missing_ok=True)
        else:target.write_text(previous)
        nginx._run([nginx.SYSTEMCTL,'daemon-reload'],timeout=10)
        raise
    nginx._write_worker_status(request,phase='done',result='success',message='Sequential live killmail service installed.')
    return 0


def install_if_updater():
    try:group=Path('/proc/self/cgroup').read_text()
    except OSError:return
    if '/eveosint-code-update.service' not in group:return
    from . import nginx_config as nginx
    deadline=time.monotonic()+25
    while nginx._service_running():
        if time.monotonic()>=deadline:raise RuntimeError('Nginx worker busy during live install')
        time.sleep(.1)
    request_id=uuid.uuid4().hex
    nginx._atomic_write_json(nginx.REQUEST_FILE,dict(request_id=request_id,action='install_live_killmails',requested_by='Code update'))
    nginx._atomic_write_json(nginx.STATUS_FILE,dict(request_id=request_id,phase='queued'))
    result=nginx._run([nginx.SUDO,nginx.SYSTEMCTL,'--no-block','start',nginx.SERVICE_NAME],timeout=10)
    if result.returncode:raise RuntimeError('Cannot start live service installer')
    while time.monotonic()<deadline:
        state=nginx._read_json(nginx.STATUS_FILE,{})
        if state.get('request_id')==request_id:
            if state.get('result')=='success':return
            if state.get('result')=='failed':raise RuntimeError('Live service installation failed')
        time.sleep(.1)
    raise RuntimeError('Live service installation was not confirmed')
