"""Offline privacy logging changes; never changes production configuration."""
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen
from web.app import public_security as security, nginx_config as nginx


class SecurityTests(unittest.TestCase):
    def test_crawler_sources_and_user_agents(self):
        for ip in security.GOOGLE_IPS:
            self.assertTrue(security.blocked_crawler(ip,'Chrome'))
            self.assertTrue(security.blocked_crawler('::ffff:'+ip,'Safari'))
        self.assertTrue(security.blocked_crawler('8.8.8.8','Chrome GoogleOther'))
        self.assertFalse(security.blocked_crawler('8.8.8.8','Chrome'))
        value=security.crawler_config('server {\n listen 80;\n}\nserver {\n listen 443;\n}\n')
        self.assertEqual(security.crawler_config(value),value)
        self.assertEqual(value.count('return 403;'),2)

    def test_configuration_is_idempotent_and_keeps_proxy_maintenance(self):
        source='server {\n access_log /tmp/site.log;\n error_log /tmp/error.log;\n location / { proxy_pass http://127.0.0.1:8000; }\n}\nserver {\n listen 80;\n return 301 https://example.test$request_uri;\n}\n'
        value=security.logging_config(source)
        self.assertEqual(security.logging_config(value),value)
        self.assertEqual(value.count('access_log off;'),2)
        self.assertEqual(value.count('error_log /dev/null;'),2)
        self.assertIn('proxy_pass http://127.0.0.1:8000;',value)
        self.assertIn('return 301',value)
        self.assertNotIn('/tmp/site.log',value)

    def test_installer_only_runs_from_code_updater_and_waits_for_checked_worker(self):
        source='server {\n listen 80;\n}\n'
        with patch.object(Path,'read_text',return_value='0::/system.slice/eveosint-web.service'),patch.object(nginx,'queue_nginx_save') as queue:
            security.install_logging_if_updater();queue.assert_not_called()
        with patch.object(Path,'read_text',return_value='0::/system.slice/eveosint-code-update.service'),\
             patch.object(nginx,'load_nginx_config',return_value=source),\
             patch.object(nginx,'queue_nginx_save',return_value='request') as queue,\
             patch.object(nginx,'_read_json',return_value={'request_id':'request','result':'success'}):
            security.install_logging_if_updater()
            self.assertIn('access_log off;',queue.call_args.args[0])
        with patch.object(Path,'read_text',return_value='0::/system.slice/eveosint-code-update.service'),\
             patch.object(nginx,'load_nginx_config',return_value=source),\
             patch.object(nginx,'queue_nginx_save',return_value='request'),\
             patch.object(nginx,'_read_json',return_value={'request_id':'request','result':'failed'}):
            with self.assertRaises(RuntimeError):security.install_logging_if_updater()

    @unittest.skipUnless(shutil.which('nginx'),'Nginx needed for isolated log check')
    def test_requests_and_upstream_errors_do_not_write_routine_ip_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            server=f'server {{\n listen 127.0.0.1:{port};\n location / {{proxy_pass http://127.0.0.1:1;}}\n}}\n'
            config=root/'nginx.conf'
            server=security.crawler_config(security.logging_config(server))
            config.write_text(f'daemon off; master_process off; error_log {root}/error.log; pid {root}/nginx.pid; events {{worker_connections 32;}} http {{access_log {root}/access.log;\n'+server+'}')
            process=subprocess.Popen([shutil.which('nginx'),'-p',str(root),'-c',str(config)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            try:
                deadline=time.monotonic()+3
                while True:
                    try:urlopen(f'http://127.0.0.1:{port}/example',timeout=1)
                    except HTTPError as error:
                        self.assertEqual(error.code,502);break
                    except OSError:
                        if time.monotonic()>deadline:raise AssertionError('Nginx did not start')
                        time.sleep(.03)
                from urllib.request import Request
                for agent in ('GoogleOther','Googlebot'):
                    with self.assertRaises(HTTPError) as denied:
                        urlopen(Request(f'http://127.0.0.1:{port}/expensive',headers={'User-Agent':agent}),timeout=1)
                    self.assertEqual(denied.exception.code,403)
                body=urlopen(Request(f'http://127.0.0.1:{port}/robots.txt',headers={'User-Agent':'GoogleOther'}),timeout=1).read()
                self.assertIn(b'Disallow: /',body)
                self.assertFalse((root/'access.log').exists() and (root/'access.log').stat().st_size)
                self.assertNotIn('client: 127.0.0.1',(root/'error.log').read_text())
            finally:
                process.terminate()
                try:process.communicate(timeout=3)
                except subprocess.TimeoutExpired:process.kill();process.communicate()


if __name__=='__main__':unittest.main()
