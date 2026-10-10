"""Offline checks for manual MER economics launch and passive progress."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts import test_killmail_forensics as f

config = sys.modules[f.PACKAGE + '.config']
with patch.object(config, 'JOBS_CONFIG_PATH', f.ROOT / 'config/offline-jobs.json', create=True):
    jobs = f.load('jobs')
f.load('mer')
routes = f.load('routes_admin_mer')


class AdminTests(unittest.TestCase):
    def test_registered_job_only_starts_on_manual_action(self):
        with patch.object(jobs, '_read_config_file', return_value={'jobs': []}), patch.object(jobs, '_start_process') as launch:
            job = jobs.get_job('import_mer_economy')
            self.assertEqual(job['type'], 'mer_economy')
            self.assertTrue(job['command'][1].endswith('scripts/import_mer_economy.py'))
            launch.assert_not_called()
            jobs.run_mer_economy_import_job()
            launch.assert_called_once_with(job, job['command'])

    def test_status_does_not_display_previous_run_while_new_worker_starts(self):
        with patch.object(jobs, '_read_runtime_status', return_value={'running': True, 'pid': 123}), patch.object(Path, 'read_text', return_value=json.dumps({'pid': 122, 'phase': 'completed'})):
            self.assertEqual(jobs.read_mer_economy_progress()['progress']['phase'], 'starting')
        with patch.object(jobs, '_read_runtime_status', return_value={'running': False, 'pid': None}), patch.object(Path, 'read_text', return_value=json.dumps({'pid': 123, 'phase': 'running'})):
            self.assertEqual(jobs.read_mer_economy_progress()['progress']['phase'], 'interrupted')

    def test_routes_are_permission_checked_and_progress_is_passive(self):
        app = FastAPI()
        app.include_router(routes.router)
        client = TestClient(app)
        user = {'id': 1, 'username': 'admin', 'permissions': {'admin.mer.view'}}
        with patch.object(routes, 'require_login', return_value=user), patch.object(routes, 'run_mer_economy_import_job') as launch, patch.object(routes, 'read_mer_economy_progress', return_value={'running': True, 'progress': {'processed': 5}}) as read, patch.object(routes, 'audit_log'), patch.object(routes, 'read_job_log', return_value='DONE') as log:
            response = client.get('/admin/mer/economy-status')
            self.assertEqual(response.json()['progress']['processed'], 5)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(client.get('/admin/mer/economy-log').text, 'DONE')
            launch.assert_not_called()
            response = client.post('/admin/mer/import-economy', follow_redirects=False)
            self.assertIn('economy_started', response.headers['location'])
            launch.assert_called_once()
            launch.side_effect = jobs.JobError('job_already_running:import_mer_economy')
            self.assertIn('economy_busy', client.post('/admin/mer/import-economy', follow_redirects=False).headers['location'])
            launch.reset_mock(); read.reset_mock(); log.reset_mock()
            user['permissions'] = set()
            for path in ('/admin/mer/economy-status', '/admin/mer/economy-log'):
                self.assertEqual(client.get(path, follow_redirects=False).status_code, 302)
            self.assertEqual(client.post('/admin/mer/import-economy', follow_redirects=False).status_code, 302)
            launch.assert_not_called(); read.assert_not_called(); log.assert_not_called()


if __name__ == '__main__':
    unittest.main()
