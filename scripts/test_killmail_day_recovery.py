"""One-day backfill validation, permissions, and disposable DB replay checks."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts import recover_killmail_day as recovery
from scripts import test_killmail_archive_refresh as fixtures
from scripts import test_killmail_forensics as f


class ValidationTests(unittest.TestCase):
    def test_raw_id_day_and_hash_must_match(self):
        payload = fixtures.payload(stamp='2026-07-13T12:00:00Z')
        result = recovery.normalize(payload, recovery.DAY, str(payload['killmail_id']), {str(payload['killmail_id']):'a'*40})
        self.assertEqual(result['killmail_hash'], 'a'*40)
        for changed in [dict(payload,killmail_time='2026-07-14T00:00:00Z'),
                        dict(payload,killmail_time='2026-07-13T12:00:00'),
                        dict(payload,killmail_hash='bad'), dict(payload,victim={})]:
            with self.assertRaises(ValueError): recovery.normalize(changed, recovery.DAY)
        with self.assertRaises(ValueError): recovery.normalize(payload,recovery.DAY,'999')

    def test_bad_cached_archive_never_opens_import_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root/'cache';cache.mkdir()
            (cache/'killmails.json').write_text(json.dumps({'1':fixtures.payload(1,'2026-07-14T00:00:00Z')}))
            (cache/'hashes.json').write_text('{}')
            importer = Mock(ARCHIVE_DIR=root/'archives')
            session = Mock()
            with self.assertRaises(ValueError):
                recovery.recover_day(Mock(),importer,session,Mock(),root/'state.json',Mock(),cache=cache)
            importer.ensure_tables.assert_not_called()
            session.get.assert_not_called()
            session.head.assert_not_called()
            self.assertEqual(list((root/'archives').iterdir()),[])

    def test_job_is_registered_once_and_is_not_in_daily_pipeline(self):
        import sys
        from scripts import update_pipeline
        config = sys.modules[f.PACKAGE+'.config']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'jobs.json'
            path.write_text('{"jobs":[]}')
            with patch.object(config,'JOBS_CONFIG_PATH',path,create=True):
                jobs = f.load('jobs')
            with patch.object(jobs,'JOBS_CONFIG_PATH',path):
                rows = jobs.load_jobs_config()
            matches = [row for row in rows if row['key']=='recover_killmail_day']
            self.assertEqual(len(matches),1)
            self.assertEqual(matches[0]['type'],'killmail_day_recovery')
            self.assertTrue(matches[0]['command'][1].endswith('/scripts/recover_killmail_day.py'))
            self.assertNotIn('recover_killmail_day',[row['id'] for row in update_pipeline.weekly_steps()])

    def test_admin_controls_require_view_or_run_and_status_never_starts_work(self):
        import sys
        config = sys.modules[f.PACKAGE+'.config']
        with patch.object(config,'JOBS_CONFIG_PATH',f.ROOT/'config/offline-jobs.json',create=True):
            f.load('jobs');routes=f.load('routes_admin_jobs')
        app=FastAPI();app.include_router(routes.router)
        user={'id':1,'username':'admin','permissions':{'admin.jobs.view'}}
        with TestClient(app) as client, patch.object(routes,'require_login',return_value=user), \
             patch.object(routes,'killmail_day_recovery_progress',return_value={'running':False,'progress':{'phase':'completed','added':10}}), \
             patch.object(routes,'run_killmail_day_recovery',return_value=(True,'started')) as launch, \
             patch.object(routes,'audit_log'):
            self.assertEqual(client.get('/admin/jobs/day-recovery/progress').json()['progress']['added'],10)
            self.assertEqual(client.post('/admin/jobs/day-recovery/run',follow_redirects=False).status_code,302)
            launch.assert_not_called()
            user['permissions'].add('admin.jobs.run')
            self.assertEqual(client.post('/admin/jobs/day-recovery/run',follow_redirects=False).status_code,302)
            launch.assert_called_once()
            user['permissions']=set()
            self.assertEqual(client.get('/admin/jobs/day-recovery/progress').status_code,403)


@unittest.skipUnless(fixtures.SOCKET.startswith('/tmp/'), 'Requires disposable archive test database')
class DayRecoveryIntegrationTests(unittest.TestCase):
    connect=fixtures.RefreshIntegrationTests.connect
    setUp=fixtures.RefreshIntegrationTests.setUp
    scalar=fixtures.RefreshIntegrationTests.scalar

    def test_replay_preserves_values_and_items_and_matches_only_requested_month(self):
        cache=self.base/'cache';cache.mkdir()
        new=fixtures.payload()
        (cache/'killmails.json').write_text(json.dumps({str(p['killmail_id']):p for p in [self.old,new]}))
        (cache/'hashes.json').write_text(json.dumps({str(new['killmail_id']):'a'*40}))
        checkpoint=self.base/'day_checkpoint.json'
        events=[]
        with self.connect() as conn:
            self.importer.mark_day(conn,fixtures.DAY,'missing',files_count=0,archive_count=0,error='404')
            with conn.cursor() as cur:
                cur.execute('ALTER TABLE rawkm.killmails ADD COLUMN IF NOT EXISTS estimated_value_cents numeric')
                cur.execute('UPDATE rawkm.killmails SET estimated_value_cents=999')
            conn.commit()
        def run():
            with self.connect() as conn, patch.object(fixtures.mer,'db',self.connect), patch.object(fixtures.mer,'_mer_log'):
                return recovery.recover_day(conn,self.importer,Mock(),fixtures.mer.match_mer_killmails,
                    checkpoint,lambda **kw:events.append(kw),day=fixtures.DAY,cache=cache)
        self.assertEqual(run()['added'],1)
        self.assertEqual(self.scalar('SELECT count(*) FROM rawkm.killmails'),2)
        self.assertEqual(self.scalar('SELECT estimated_value_cents FROM rawkm.killmails WHERE killmail_id=138072305'),999)
        self.assertEqual(self.scalar('SELECT count(*) FROM rawkm.killmail_attackers'),2)
        self.assertEqual(self.scalar('SELECT resolved_km[1] FROM mer.killmails WHERE source_row=1'),138072306)
        self.assertIsNone(self.scalar('SELECT resolved_km FROM mer.killmails WHERE source_row=2'))
        self.assertEqual(self.scalar('SELECT status FROM rawkm.killmail_import_days'),'missing')
        self.assertEqual(run()['added'],0)
        self.assertEqual(self.scalar('SELECT count(*) FROM rawkm.killmails'),2)
        self.assertEqual(self.scalar('SELECT count(*) FROM rawkm.killmail_archive_recoveries'),1)
        self.assertEqual(events[-1]['phase'],'completed')
        self.assertEqual(json.loads(checkpoint.read_text())['pending_months'],[])


if __name__=='__main__': unittest.main()
