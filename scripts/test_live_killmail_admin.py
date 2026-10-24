"""Admin controls, service installation and browser monitoring; no production writes."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader
from playwright.sync_api import sync_playwright,expect
from scripts import test_killmail_forensics as f
from web.app import live_killmail_service as service,nginx_config as nginx

routes=f.load('routes_admin_update_pipeline')
ADMIN=dict(id=1,username='Admin',permissions={'admin.jobs.view','admin.jobs.run'})


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        patcher=patch.object(routes.live_killmails,'ROOT',self.root);patcher.start();self.addCleanup(patcher.stop)
        app=FastAPI();app.include_router(routes.router);self.client=TestClient(app)

    def test_pause_control_requires_permission_origin_and_boolean(self):
        url='/admin/update-pipeline/api/live';headers={'origin':'http://testserver'}
        self.assertEqual(self.client.post(url,json={'enabled':False},headers=headers).status_code,403)
        with patch.object(routes,'require_login',return_value=ADMIN),patch.object(routes,'audit_log'):
            self.assertEqual(self.client.post(url,json={'enabled':False}).status_code,403)
            self.assertEqual(self.client.post(url,json={'enabled':'false'},headers=headers).status_code,400)
            response=self.client.post(url,json={'enabled':False},headers=headers)
            self.assertEqual(response.status_code,200);self.assertFalse(response.json()['enabled'])
            self.assertFalse(json.loads((self.root/'control.json').read_text())['enabled'])
            self.assertEqual(self.client.post(url,json={'enabled':True},headers=headers).status_code,200)
            self.assertTrue(routes.live_killmails.status()['enabled'])

    def test_installer_is_only_reached_by_the_normal_updater(self):
        with patch.object(Path,'read_text',return_value='0::/system.slice/eveosint-web.service'),patch.object(nginx,'_run') as run:
            service.install_if_updater();run.assert_not_called()
        with patch.object(Path,'read_text',return_value='0::/system.slice/eveosint-code-update.service'),\
            patch.object(nginx,'_service_running',return_value=False),patch.object(nginx,'_atomic_write_json') as write,\
            patch.object(nginx,'_run',return_value=SimpleNamespace(returncode=0)),\
            patch.object(nginx,'_read_json',side_effect=lambda *args:dict(request_id=write.call_args_list[0].args[1]['request_id'],result='success')):
            service.install_if_updater()
            self.assertEqual(write.call_args_list[0].args[1]['action'],'install_live_killmails')

    def test_unit_installed_as_ubuntu_and_failure_restores_previous_definition(self):
        root=Path(__file__).resolve().parents[1]
        fixture=SimpleNamespace(REPO_ROOT=root,SYSTEMCTL='/usr/bin/systemctl',_run=Mock(return_value=SimpleNamespace(returncode=0)),_write_worker_status=Mock())
        with patch.object(service,'UNIT_DIR',self.root):
            self.assertEqual(service.privileged_install({'request_id':'test'},fixture),0)
            unit=(self.root/service.SERVICE).read_text()
            self.assertIn('User=ubuntu',unit);self.assertIn('Restart=always',unit)
            self.assertIn('scripts/stream_killmails.py',unit)
            self.assertEqual(fixture._run.call_args_list[-1].args[0][-2:],['restart',service.SERVICE])
            (self.root/service.SERVICE).write_text('previous unit')
            fixture._run.return_value=SimpleNamespace(returncode=1)
            with self.assertRaises(RuntimeError):service.privileged_install({'request_id':'test'},fixture)
            self.assertEqual((self.root/service.SERVICE).read_text(),'previous unit')

    def test_admin_browser_shows_state_and_pause_resume(self):
        directory=Path(__file__).resolve().parents[1]/'web/templates'
        env=Environment(loader=ChoiceLoader([DictLoader({'base.html':'{% block content %}{% endblock %}'}),FileSystemLoader(str(directory))]))
        state=dict(pipeline_status='idle',running=False,steps=[],history=[],summary={},next_plan={},timer={},live_killmails=dict(running=True,enabled=True,phase='completed_kill',killmail_id=123,next_sequence=100,affiliations_deferred=4,valuation_deferred=2))
        html=env.get_template('admin_update_pipeline.html').render(pipeline=state,can_run=True)
        controls=[]
        with sync_playwright() as p:
            browser=p.chromium.launch(args=['--no-sandbox']);page=browser.new_page();errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            def serve(route):
                path=route.request.url
                if path.endswith('/api/live'):
                    control=json.loads(route.request.post_data);controls.append(control)
                    state['live_killmails']['enabled']=control['enabled']
                    route.fulfill(content_type='application/json',body=json.dumps(state['live_killmails']))
                elif '/api/status' in path:route.fulfill(content_type='application/json',body=json.dumps(state))
                elif '/api/log' in path:route.fulfill(body='Test log')
                else:route.fulfill(content_type='text/html',body=html)
            page.route('**/*',serve);page.goto('http://live.test/admin/update-pipeline',wait_until='networkidle')
            expect(page.locator('#liveState')).to_contain_text('completed_kill')
            expect(page.locator('#liveMetrics')).to_contain_text('Next sequence 100')
            page.get_by_role('button',name='Pause live',exact=True).click()
            expect(page.locator('#liveState')).to_contain_text('finishing the current KM')
            self.assertEqual(controls,[dict(enabled=False)])
            page.reload();page.get_by_role('button',name='Resume live',exact=True).click()
            self.assertEqual(controls[-1],dict(enabled=True))
            self.assertFalse(errors,errors);browser.close()


if __name__=='__main__':unittest.main()
