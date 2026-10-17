"""Offline ranking checks, with optional disposable PostgreSQL integration tests."""
from datetime import date
from decimal import Decimal
import os
import json
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import psycopg2
from fastapi import FastAPI
from fastapi.testclient import TestClient
from scripts import build_killmail_rankings as builder
from scripts.killmail_ranking_state import mark_months_dirty, month_of
from scripts import test_first_loss_admin as fixture

jobs = fixture.jobs
rankings = fixture.f.load('killmail_rankings')
routes = fixture.f.load('routes_admin_rankings')
JAN = date(2026,1,1)
FEB = date(2026,2,1)


class ControlTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)
        self.user = {'id':1,'username':'Tester','permissions':{'admin.jobs.view','admin.jobs.run'}}
        self.auth = patch.object(routes,'require_login',return_value=self.user)
        self.auth.start()
        self.addCleanup(self.auth.stop)

    def test_uninitialized_page_never_starts_work(self):
        with patch.object(rankings,'catalog',return_value=[]),patch.object(routes,'run_killmail_rankings') as launch:
            r = self.client.get('/admin/debug/rankings')
            self.assertEqual(r.status_code,200)
            self.assertIn('Rankings have not been built yet',r.text)
            self.assertIn('Build / refresh rankings',r.text)
            launch.assert_not_called()

    def test_populated_page_modes_and_values(self):
        months=[dict(month=JAN,built_at='v1',killmails=3,valued=2,zero_damage=1,entity_rows=5,dirty=True)]
        rows=[dict(entity_id=42,name='<pilot>',destroyed=Decimal(123456789),lost=Decimal(100),
                   net=Decimal(123456689),efficiency=Decimal('99.99'),kills=2,losses=1,unvalued_kills=1,unvalued_losses=0)]
        with patch.object(rankings,'catalog',return_value=months),patch.object(rankings,'ranking_rows',return_value=(rows,False)) as query:
            r=self.client.get('/admin/debug/rankings?kind=character&mode=weighted&sort=net')
            self.assertEqual(r.status_code,200)
            self.assertIn('1.23 M',r.text)
            self.assertIn('1,234,567.89 ISK',r.text)
            self.assertIn('&lt;pilot&gt;',r.text)
            self.assertIn('1 awaiting refresh',r.text)
            self.assertIn('Partial results',r.text)
            self.assertEqual(query.call_args.args[:3],('character','weighted','net'))

    def test_controls_permissions_and_no_side_effect_on_status(self):
        with patch.object(routes,'killmail_rankings_progress',return_value={'running':False,'progress':None}),patch.object(routes,'run_killmail_rankings') as launch:
            self.assertEqual(self.client.get('/admin/debug/rankings/status').status_code,200)
            launch.assert_not_called()
            self.user['permissions']={'admin.jobs.view'}
            self.assertEqual(self.client.post('/admin/debug/rankings/start').status_code,403)
            launch.assert_not_called()
            self.user['permissions'].add('admin.jobs.run')
            launch.return_value=(True,'started')
            with patch.object(routes,'audit_log'):
                self.assertEqual(self.client.post('/admin/debug/rankings/start').status_code,200)
            launch.assert_called_once()

    def test_bad_filters_never_query(self):
        with patch.object(rankings,'catalog',return_value=[]),patch.object(rankings,'ranking_rows') as query:
            for url in ['?kind=oops','?sort=destroyed;DROP','?page=-1']:
                self.assertEqual(self.client.get('/admin/debug/rankings'+url).status_code,200)
            query.assert_not_called()

    def test_job_is_normalized_and_manual(self):
        with patch.object(jobs,'_read_config_file',return_value={'jobs':[]}),patch.object(jobs,'_start_process',return_value=(True,'started')) as launch:
            job=jobs.get_job('build_killmail_rankings')
            self.assertEqual(job['type'],'killmail_rankings')
            self.assertIn('log_exists',job)
            launch.assert_not_called()
            jobs.run_killmail_rankings()
            launch.assert_called_once_with(job,job['command'])

    def test_cache_reuses_period_and_invalidates_rebuild(self):
        rankings._CACHE.clear()
        months=[dict(month=JAN,built_at='v1')]
        with patch.object(rankings,'_query',return_value=[{'id':1}]) as query:
            self.assertFalse(rankings.ranking_rows('character','classic','destroyed',JAN,FEB,months)[1])
            self.assertTrue(rankings.ranking_rows('character','classic','destroyed',JAN,FEB,months)[1])
            self.assertEqual(query.call_count,1)
            months[0]['built_at']='v2'
            self.assertFalse(rankings.ranking_rows('character','classic','destroyed',JAN,FEB,months)[1])
            self.assertEqual(query.call_count,2)

    def test_month_is_utc_and_validated(self):
        self.assertEqual(month_of('2026-02-01T00:30:00+02:00'),JAN)
        for value in ['2026-99','2002-01','2026-1','2026-01;SELECT 1']:
            with self.assertRaises(ValueError):
                rankings.month_input(value)


@unittest.skipUnless(os.environ.get('KILLMAIL_VALUES_TEST_DSN'),'Disposable SQL database not configured')
class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.dsn=os.environ['KILLMAIL_VALUES_TEST_DSN']
        if 'dbname=killmail_values_test' not in self.dsn:
            raise RuntimeError('Only disposable killmail_values_test is allowed')
        self.conn=psycopg2.connect(self.dsn)
        self.addCleanup(self.conn.close)
        with self.conn.cursor() as cur:
            cur.execute('DROP SCHEMA IF EXISTS analytics CASCADE; DROP SCHEMA IF EXISTS rawkm CASCADE; DROP SCHEMA IF EXISTS entities CASCADE')
            cur.execute('''CREATE SCHEMA rawkm;
                CREATE TABLE rawkm.killmails(killmail_id bigint,killmail_time timestamptz,
                  estimated_value_cents bigint,victim_character_id bigint,victim_corporation_id bigint,victim_alliance_id bigint)
                  PARTITION BY RANGE(killmail_time);
                CREATE TABLE rawkm.killmail_attackers(killmail_id bigint,killmail_time timestamptz,
                  character_id bigint,corporation_id bigint,alliance_id bigint,damage_done integer)
                  PARTITION BY RANGE(killmail_time);
                CREATE TABLE rawkm.killmails_2026_01 PARTITION OF rawkm.killmails FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');
                CREATE TABLE rawkm.killmails_2026_02 PARTITION OF rawkm.killmails FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');
                CREATE TABLE rawkm.killmail_attackers_2026_01 PARTITION OF rawkm.killmail_attackers FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');
                CREATE TABLE rawkm.killmail_attackers_2026_02 PARTITION OF rawkm.killmail_attackers FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');
                CREATE SCHEMA entities;
                CREATE TABLE entities.characters(character_id bigint PRIMARY KEY,name text);
                CREATE TABLE entities.corporations(corporation_id bigint PRIMARY KEY,name text);
                CREATE TABLE entities.alliances(alliance_id bigint PRIMARY KEY,name text);
                INSERT INTO entities.characters VALUES(1,'Pilot One'),(2,'Pilot Two');
                INSERT INTO rawkm.killmails VALUES
                  (100,'2026-01-02 12:00+00',10000,9,90,900),
                  (101,'2026-01-03 12:00+00',20000,1,10,1000),
                  (102,'2026-01-04 12:00+00',NULL,2,10,1000),
                  (103,'2026-01-05 12:00+00',1000,NULL,90,900),
                  (100,'2026-02-02 12:00+00',30000,9,90,900);
                INSERT INTO rawkm.killmail_attackers VALUES
                  (100,'2026-01-02 12:00+00',1,10,1000,20),
                  (100,'2026-01-02 12:00+00',1,10,1000,10),
                  (100,'2026-01-02 12:00+00',2,10,1000,20),
                  (100,'2026-01-02 12:00+00',NULL,1000001,NULL,50),
                  (101,'2026-01-03 12:00+00',3,30,3000,0),
                  (102,'2026-01-04 12:00+00',1,11,1001,40),
                  (103,'2026-01-05 12:00+00',NULL,1000001,NULL,-1),
                  (100,'2026-02-02 12:00+00',1,11,1001,100);
            ''')
            cur.execute(builder.SCHEMA.read_text())
        self.conn.commit()
        self.conn.set_session(isolation_level='REPEATABLE READ')

    def tearDown(self):
        self.conn.rollback()
        with self.conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS analytics CASCADE; DROP SCHEMA IF EXISTS rawkm CASCADE; DROP SCHEMA IF EXISTS entities CASCADE")
        self.conn.commit()

    def build(self,month=JAN):
        result=builder.build_month(self.conn,month)
        self.conn.commit()
        return result

    def facts(self,kind,id,month=JAN):
        with self.conn.cursor() as cur:
            cur.execute('''SELECT destroyed_cents,weighted_destroyed_cents,lost_cents,kills,losses,unvalued_kills,unvalued_losses
                           FROM analytics.killmail_rankings_monthly WHERE entity_kind=%s AND entity_id=%s AND month=%s''',(kind,id,month))
            return cur.fetchone()

    def test_credit_dedup_damage_npc_losses_and_no_empty_rows(self):
        result=self.build()
        self.assertEqual(self.facts(1,1),(10000,3000,20000,2,1,1,0))
        self.assertEqual(self.facts(2,10),(10000,5000,20000,1,2,0,1))
        self.assertEqual(self.facts(3,1000),(10000,5000,20000,1,2,0,1))
        self.assertEqual(self.facts(1,3),(20000,0,0,1,0,0,0))
        self.assertIsNone(self.facts(1,999))
        self.assertEqual(result['zero_damage'],2)
        self.assertEqual((result['killmails'],result['valued']),(4,3))

    def test_cli_initialization_resume_force_and_progress(self):
        self.conn.commit()
        cfg = self.conn.get_dsn_parameters()
        with tempfile.TemporaryDirectory(prefix='ranking-cli-test-') as directory:
            root = Path(directory)
            config = root/'db.json'
            config.write_text(json.dumps(dict(db_name=cfg['dbname'], db_user=cfg['user'], db_password='',
                                             db_host=cfg['host'], db_port=cfg['port'])))
            progress = root/'progress.json'
            command = [sys.executable, str(builder.__file__), '--db-config',str(config),
                       '--progress',str(progress),'--min-free-gb','0']
            def run(*extra):
                result = subprocess.run(command+list(extra),capture_output=True,text=True,timeout=15)
                self.assertEqual(result.returncode,0,result.stderr)
            run('--incremental-only')
            with self.conn.cursor() as cur:
                cur.execute('SELECT count(*) FROM analytics.killmail_ranking_months')
                self.assertEqual(cur.fetchone()[0],0)
            self.conn.commit()
            run()
            self.assertEqual(json.loads(progress.read_text())['processed'],2)
            run()
            self.assertEqual(json.loads(progress.read_text())['months'],0)
            run('--force','--from-month','2026-01','--through-month','2026-01')
            state=json.loads(progress.read_text())
            self.assertEqual((state['phase'],state['months'],state['processed']),('completed',1,1))

    def test_rebuild_replaces_month_and_never_changes_raw(self):
        self.build()
        with self.conn.cursor() as cur:
            cur.execute('UPDATE rawkm.killmails SET estimated_value_cents=15000 WHERE killmail_id=100 AND killmail_time<\'2026-02-01\'')
        mark_months_dirty(self.conn,[JAN])
        self.conn.commit()
        self.build()
        self.assertEqual(self.facts(1,1)[0],15000)
        self.build()
        self.assertEqual(self.facts(1,1)[0],15000)
        with self.conn.cursor() as cur:
            cur.execute('SELECT count(*),sum(estimated_value_cents) FROM rawkm.killmails')
            self.assertEqual(cur.fetchone(),(5,66000))

    def test_months_are_separate_and_period_is_sum(self):
        self.build();self.build(FEB)
        self.assertEqual(self.facts(1,1,FEB),(30000,30000,0,1,0,0,0))
        self.assertEqual(self.facts(2,11,JAN),(0,0,0,1,0,1,0))
        self.assertEqual(builder.source_months(self.conn),[JAN,FEB])
        self.conn.commit()
        with patch.object(rankings,'db',side_effect=lambda:psycopg2.connect(self.dsn)):
            rows=rankings._query('character','classic','destroyed',JAN,FEB)
            one=next(r for r in rows if r['entity_id']==1)
            self.assertEqual((one['destroyed'],one['lost'],one['kills']),(40000,20000,3))
            weighted=rankings._query('character','weighted','destroyed',JAN,FEB)
            self.assertEqual(next(r for r in weighted if r['entity_id']==1)['destroyed'],33000)

    def test_transaction_rollback_preserves_previous_month(self):
        self.build()
        with self.conn.cursor() as cur:
            cur.execute('UPDATE rawkm.killmails SET estimated_value_cents=99999 WHERE killmail_id=100')
        builder.build_month(self.conn,JAN)
        self.conn.rollback()
        self.assertEqual(self.facts(1,1)[0],10000)

    def test_dirty_revision_is_atomic_and_concurrent_change_is_not_lost(self):
        self.build()
        mark_months_dirty(self.conn,[JAN]);self.conn.rollback()
        with self.conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM analytics.killmail_ranking_versions')
            self.assertEqual(cur.fetchone()[0],0)
        self.conn.commit()
        second=psycopg2.connect(self.dsn)
        self.addCleanup(second.close)
        def during_build(**progress):
            if progress.get('phase')=='aggregating_attackers':
                mark_months_dirty(second,[JAN]);second.commit()
        builder.build_month(self.conn,JAN,during_build)
        self.conn.commit()
        with self.conn.cursor() as cur:
            cur.execute('''SELECT m.built_revision,v.revision FROM analytics.killmail_ranking_months m
                           JOIN analytics.killmail_ranking_versions v USING(month)''')
            self.assertEqual(cur.fetchone(),(0,1))
        self.conn.commit()
        self.build()
        with self.conn.cursor() as cur:
            cur.execute('SELECT built_revision FROM analytics.killmail_ranking_months WHERE month=%s',(JAN,))
            self.assertEqual(cur.fetchone()[0],1)


if __name__=='__main__':
    unittest.main()
