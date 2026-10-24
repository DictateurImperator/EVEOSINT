"""Maintenance orchestration, rollback recovery and actual Nginx gate tests.
No production configuration, service, database or daily job is modified.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import importlib.util
import io
import sys
import types
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error

from web.app import deployment_guard as guard, nginx_config as nginx, code_update


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.runtime=Path(self.temp.name)/'runtime';self.runtime.mkdir()
        self.patch=patch.object(guard,'RUNTIME',self.runtime);self.patch.start();self.addCleanup(self.patch.stop)

    def test_only_web_unit_is_interrupted_and_stuck_requests_are_killed(self):
        commands=[]
        def run(args,**kwargs):commands.append(args);return SimpleNamespace(returncode=0)
        with patch.object(guard,'web_pid',side_effect=[123,123,0,0]):
            guard.stop_web_requests(run,grace=0)
        self.assertEqual(commands,[[guard.SYSTEMCTL,'--no-block','stop',guard.WEB_SERVICE],
                                   [guard.SYSTEMCTL,'kill','--signal=SIGKILL','--kill-whom=all',guard.WEB_SERVICE]])
        self.assertTrue(all(args[-1]=='eveosint-web.service' for args in commands))

    def test_clean_stop_does_not_force_kill(self):
        calls=[]
        with patch.object(guard,'web_pid',return_value=0):
            guard.stop_web_requests(lambda args,**kw:calls.append(args) or SimpleNamespace(returncode=0))
        self.assertEqual(len(calls),1)

    def test_gate_precedes_web_interruption_and_monitor_survives_checkout(self):
        root=Path(self.temp.name);root.joinpath('venv/bin').mkdir(parents=True)
        config='server { listen 443 ssl; location / { proxy_pass http://127.0.0.1:8000; } }'
        def apply(request):
            self.assertIn(guard.BEGIN,request['content'])
            self.assertFalse((self.runtime/'active').exists())
            return 0
        def stop(_):self.assertTrue((self.runtime/'active').is_file())
        fake=SimpleNamespace(REPO_ROOT=root,load_nginx_config=lambda:config,apply_config=apply,
                             _run=lambda *a,**kw:SimpleNamespace(returncode=0),_write_worker_status=lambda *a,**kw:None)
        with patch.object(guard,'web_pid',return_value=123),patch.object(guard,'updater_pid',return_value=456), \
             patch.object(guard,'update_active',return_value=True),patch.object(guard,'stop_web_requests',side_effect=stop):
            self.assertEqual(guard.privileged_prepare({'request_id':'a'*32},fake),0)
        self.assertTrue((self.runtime/'monitor.py').is_file())
        self.assertEqual(guard.state()['phase'],'maintenance_ready')

    def test_success_and_old_version_rollback_both_remove_maintenance_only_after_health(self):
        for old_status in ('success','failed_rolled_back','failed_rollback_failed'):
            with self.subTest(status=old_status):
                (self.runtime/'active').write_text('a'*32)
                guard.write_state(request_id='a'*32,old_web_pid=123,phase='maintenance_ready')
                with patch.object(guard,'update_active',return_value=False),patch.object(guard,'web_pid',return_value=456), \
                     patch.object(guard,'healthy',return_value=True):
                    self.assertEqual(guard.monitor(Path(self.temp.name),'a'*32),0)
                self.assertFalse((self.runtime/'active').exists())
                self.assertEqual(guard.state()['phase'],'complete')

    def test_failed_health_keeps_maintenance(self):
        (self.runtime/'active').write_text('a'*32)
        guard.write_state(request_id='a'*32,old_web_pid=123)
        with patch.object(guard,'update_active',return_value=False),patch.object(guard,'web_pid',return_value=456), \
             patch.object(guard,'healthy',return_value=False),patch.object(guard.time,'sleep'), \
             patch.object(guard.time,'monotonic',side_effect=[0,0,0,0,31]):
            self.assertEqual(guard.monitor(Path(self.temp.name),'a'*32),1)
        self.assertTrue((self.runtime/'active').exists())

    def test_stale_monitor_does_not_clear_another_deployment(self):
        (self.runtime/'active').write_text('b'*32)
        guard.write_state(request_id='a'*32,old_web_pid=123)
        with patch.object(guard,'update_active',side_effect=lambda:guard.write_state(request_id='b'*32) or False), \
             patch.object(guard,'web_pid',return_value=456),patch.object(guard,'healthy',return_value=True):
            self.assertEqual(guard.monitor(Path(self.temp.name),'a'*32),0)
        self.assertTrue((self.runtime/'active').exists())

    def test_schema_script_does_not_stop_site_outside_update_service(self):
        for unit in ('eveosint-web.service','eveosint-update.service'):
            with patch.object(Path,'read_text',return_value='0::/system.slice/'+unit),patch.object(guard,'prepare') as prepare:
                guard.prepare_if_updater(Path('/example'))
                prepare.assert_not_called()
        with patch.object(Path,'read_text',return_value='0::/system.slice/eveosint-code-update.service'),patch.object(guard,'prepare') as prepare:
            guard.prepare_if_updater(Path('/example'))
            prepare.assert_called_once_with(Path('/example'))

    def test_nginx_gate_preserves_non_web_servers_and_quoted_braces(self):
        source='server { listen 80; return 301 https://example.test; }\nserver {\n# { ignored\nset $a "}";\nlocation / { proxy_pass http://127.0.0.1:8000; }\n}'
        output=guard.nginx_with_gate(source,self.runtime/'active')
        self.assertEqual(output.count(guard.BEGIN),1)
        self.assertTrue(output.startswith('server { listen 80; return 301 https://example.test; }'))
        self.assertEqual(guard.nginx_with_gate(output,self.runtime/'active'),output)
        with self.assertRaises(guard.MaintenanceError):guard.nginx_with_gate('server { return 200; }',self.runtime/'active')

    def test_nginx_worker_dispatch_uses_same_checked_configuration_pipeline(self):
        request={'request_id':'a'*32,'action':'prepare_deployment'}
        with patch.object(nginx,'_read_json',return_value=request),patch.object(guard,'privileged_prepare',return_value=0) as prep:
            self.assertEqual(nginx.run_request(),0)
        self.assertEqual(prep.call_args.args[0],request)

    def test_root_worker_status_is_readable_by_application_owner(self):
        root=Path(self.temp.name)
        with patch.object(nginx,'REPO_ROOT',root),patch.object(nginx,'RUN_DIR',root), \
             patch.object(nginx.os,'geteuid',return_value=0):
            nginx._atomic_write_json(root/'worker-status.json',{'result':'success'})
        status=root/'worker-status.json'
        self.assertEqual(status.stat().st_uid,root.stat().st_uid)
        self.assertEqual(status.stat().st_mode & 0o777,0o600)
        self.assertEqual(json.loads(status.read_text())['result'],'success')

    def test_nginx_checked_apply_and_failed_validation_restore_original(self):
        root=Path(self.temp.name);config=root/'nginx.conf';original='server { return 200; }'
        request={'request_id':'a'*32,'content':'server { return 503; }'}
        for valid in (True,False):
            config.write_text(original)
            result=SimpleNamespace(returncode=0 if valid else 1,stdout='',stderr='')
            with patch.object(nginx,'CONFIG_FILE',config),patch.object(nginx,'BACKUP_DIR',root/'backups'), \
                 patch.object(nginx,'RUN_DIR',root),patch.object(nginx,'STATUS_FILE',root/'status.json'), \
                 patch.object(nginx,'_run',return_value=result) as run:
                self.assertEqual(nginx.apply_config(request),0 if valid else 1)
            self.assertEqual(config.read_text(),request['content'] if valid else original)
            self.assertTrue(list((root/'backups').glob('*.conf')))
            if not valid:self.assertFalse(any('reload' in args.args[0] for args in run.call_args_list))

    def test_legacy_setup_entrypoint_enters_maintenance_before_database_setup(self):
        setup=Path(__file__).resolve().parents[1]/'web/setup_acl.py'
        spec=importlib.util.spec_from_file_location('legacy_setup_fixture',setup)
        module=importlib.util.module_from_spec(spec)
        with patch.object(Path,'open',return_value=io.StringIO('{}')):spec.loader.exec_module(module)
        steps=[]
        class Connection:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def cursor(self):return self
            def execute(self,*args):pass
            def commit(self):pass
        app=types.ModuleType('app');app.__path__=[]
        privacy=types.ModuleType('app.public_security')
        privacy.install_logging_if_updater=lambda:steps.append('privacy')
        live=types.ModuleType('app.live_killmail_service')
        live.install_if_updater=lambda:steps.append('live')
        with patch.dict(sys.modules,{'app':app,'app.deployment_guard':guard,'app.public_security':privacy,'app.live_killmail_service':live}), \
             patch.object(Path,'read_text',return_value='0::/system.slice/eveosint-code-update.service'), \
             patch.object(guard,'prepare',side_effect=lambda root:steps.append('maintenance')), \
             patch.object(module,'db',side_effect=lambda:steps.append('schema') or Connection()),patch('builtins.print'):
            module.main()
        self.assertEqual(steps,['maintenance','privacy','schema','live'])

    def test_existing_setup_hook_bootstraps_old_updater_and_timeout_stays_thirty(self):
        setup=(Path(__file__).resolve().parents[1]/'web/setup_acl.py').read_text()
        self.assertIn('prepare_if_updater',setup)
        with patch.object(code_update,'_run',return_value=SimpleNamespace(returncode=0)) as run:
            code_update._restart_web_service()
        self.assertEqual(run.call_args.kwargs['timeout'],30)


@unittest.skipUnless(shutil.which('nginx'),'Nginx required for isolated HTTP maintenance test')
class NginxHTTPTests(unittest.TestCase):
    def test_static_maintenance_survives_upstream_stop_and_only_reopens_after_gate_removal(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200);self.end_headers();self.wfile.write(b'healthy app')
            def log_message(self,*args):pass
        backend=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        upstream=backend.server_address[1]
        threading.Thread(target=backend.serve_forever,daemon=True).start()
        self.addCleanup(backend.server_close)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);flag=root/'active'
            server=f'server {{listen 127.0.0.1:{port}; location / {{proxy_pass http://127.0.0.1:8000;}}}}'
            server=guard.nginx_with_gate(server,flag).replace('http://127.0.0.1:8000;',f'http://127.0.0.1:{upstream};')
            config=root/'nginx.conf';config.write_text(f'daemon off; master_process off; error_log stderr; pid {root}/nginx.pid; events {{worker_connections 32;}} http {{access_log off; {server}}}')
            process=subprocess.Popen([shutil.which('nginx'),'-p',str(root),'-c',str(config)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            try:
                url=f'http://127.0.0.1:{port}/public/anything'
                deadline=time.monotonic()+3
                while True:
                    try:
                        with urllib.request.urlopen(url,timeout=1) as r:self.assertEqual(r.status,200)
                        break
                    except OSError:
                        if time.monotonic()>deadline:raise AssertionError(process.stderr.read().decode())
                        time.sleep(.03)
                flag.write_text('active')
                backend.shutdown();backend.server_close()
                for _ in range(2):
                    with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(url,timeout=2)
                    self.assertEqual(caught.exception.code,503)
                    self.assertEqual(caught.exception.headers['Retry-After'],'5')
                    self.assertEqual(caught.exception.headers['X-EVEOSINT-Maintenance'],'1')
                    self.assertIn(b'EVEOSINT is undergoing maintenance',caught.exception.read())
                backend=ThreadingHTTPServer(('127.0.0.1',upstream),Handler)
                threading.Thread(target=backend.serve_forever,daemon=True).start()
                flag.unlink()
                with urllib.request.urlopen(url,timeout=2) as r:self.assertEqual(r.read(),b'healthy app')
            finally:
                backend.shutdown();backend.server_close()
                process.terminate()
                try:process.communicate(timeout=3)
                except subprocess.TimeoutExpired:process.kill();process.communicate()


if __name__=='__main__':unittest.main()
