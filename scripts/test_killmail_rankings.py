"""Offline ranking checks, with optional disposable PostgreSQL integration tests."""
from datetime import date, datetime, UTC
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


class ProgressTests(unittest.TestCase):
    def test_heartbeat_preserves_committed_counts_and_phase_start(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'progress.json'
            state = dict(phase='starting', processed=0, months=227, entity_rows=0)
            report = builder.ProgressReporter(path, state)
            report(phase='reading_killmails', current_month='2008-01')
            before = json.loads(path.read_text())
            report()  # Heartbeat during SQL, with no completed month yet.
            after = json.loads(path.read_text())
            self.assertEqual(after['processed'], 0)
            self.assertEqual(after['phase_started_at'], before['phase_started_at'])
            self.assertGreaterEqual(after['updated_at'], before['updated_at'])
            report(phase='writing_rankings')
            self.assertEqual(json.loads(path.read_text())['entity_rows'], 0)
            report(processed=1, entity_rows=1234)
            self.assertEqual(json.loads(path.read_text())['processed'], 1)
            report.start()
            report.close()
            self.assertFalse(report.thread.is_alive())


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.cache_dir=tempfile.TemporaryDirectory(prefix='ranking-cache-test-')
        self.addCleanup(self.cache_dir.cleanup)
        cache_patch=patch.object(rankings,'CACHE_DIR',Path(self.cache_dir.name))
        cache_patch.start()
        self.addCleanup(cache_patch.stop)
        rankings._CACHE.clear()
        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)
        self.user = {'id':1,'username':'Tester','permissions':{'admin.jobs.view','admin.jobs.run'}}
        self.auth = patch.object(routes,'require_login',return_value=self.user)
        self.auth.start()
        self.addCleanup(self.auth.stop)
        enrich_patch=patch.object(rankings,'enrich_rows',side_effect=lambda kind,rows,as_of=None: rows)
        enrich_patch.start()
        self.addCleanup(enrich_patch.stop)

    def test_uninitialized_page_never_starts_work(self):
        with patch.object(rankings,'catalog',return_value=[]),patch.object(routes,'run_killmail_rankings') as launch:
            r = self.client.get('/admin/debug/rankings')
            self.assertEqual(r.status_code,200)
            self.assertIn('Rankings have not been built yet',r.text)
            self.assertIn('Build / refresh rankings',r.text)
            launch.assert_not_called()

    def test_populated_page_modes_and_values(self):
        months=[dict(month=month,built_at='v1',killmails=3,valued=2,zero_damage=1,entity_rows=5,dirty=True) for month in (JAN,FEB)]
        rows=[dict(entity_id=42,name='<pilot>',destroyed=Decimal(123456789),lost=Decimal(100),
                   net=Decimal(123456689),efficiency=Decimal('99.99'),kills=2,losses=1,unvalued_kills=1,unvalued_losses=0)]
        with patch.object(rankings,'catalog',return_value=months),patch.object(rankings,'ranking_rows',return_value=(rows,False)) as query:
            r=self.client.get('/admin/debug/rankings?kind=character&mode=weighted&sort=net')
            self.assertEqual(r.status_code,200)
            self.assertIn('1.23 M',r.text)
            self.assertIn('1,234,567.89 ISK',r.text)
            self.assertIn('&lt;pilot&gt;',r.text)
            self.assertIn('2 awaiting refresh',r.text)
            self.assertIn('Partial results',r.text)
            self.assertEqual(query.call_args.args[:3],('character','weighted','net'))
            self.assertEqual(query.call_args.args[3:5],(JAN,FEB))
            self.assertIn('<strong>All time</strong>',r.text)
            self.assertIn('id="rankName"',r.text)
            self.assertIn('id="rankSearchBy"',r.text)
            self.assertIn('aria-label="Ranking entity type"',r.text)
            self.assertIn('data-rank-sign',r.text)
            self.assertLess(r.text.index('class="table rank-table'),r.text.index('Monthly ranking builder'))
            for url in ('?from=&through=', '?page=2'):
                response=self.client.get('/admin/debug/rankings'+url)
                self.assertEqual(response.status_code,200)
                self.assertEqual(query.call_args.args[3:5],(JAN,FEB))
                self.assertIn('<strong>All time</strong>',response.text)
            response=self.client.get('/admin/debug/rankings?from=2026-02&through=2026-02')
            self.assertEqual(query.call_args.args[3:5],(JAN,FEB))
            self.assertIn('<strong>From the beginning</strong>',response.text)


    def test_entity_tabs_keep_settings_but_reset_the_searched_entity_and_page(self):
        import html
        import re
        from urllib.parse import urlparse,parse_qs
        months=[dict(month=JAN,built_at='v',killmails=1,valued=1,zero_damage=0,entity_rows=1,dirty=False)]
        with patch.object(rankings,'catalog',return_value=months),patch.object(rankings,'scope_labels',return_value={}),patch.object(rankings,'explore_rows',return_value=dict(rows=[],offset=0,position=1,more=False)):
            for kind in ['character','corporation','alliance']:
                response=self.client.get('/admin/debug/rankings',params=dict(kind=kind,mode='weighted',sort='lost',through='2026-01',include='universe:0',exclude='alliance:999',q='Pilot',target_id=42,page=2))
                self.assertEqual(response.status_code,200)
                links=re.findall(r'<a class="nav-link[^"]*" href="([^"]*)" data-rank-kind="([^"]*)"([^>]*)>',response.text)
                self.assertEqual([tab[1] for tab in links],['character','corporation','alliance'])
                for href,tab_kind,attrs in links:
                    params=parse_qs(urlparse(html.unescape(href)).query)
                    self.assertEqual(params['kind'],[tab_kind])
                    self.assertEqual(params['mode'],['weighted'])
                    self.assertEqual(params['sort'],['lost'])
                    self.assertEqual(params['through'],['2026-01'])
                    self.assertEqual(params['include'],['universe:0'])
                    self.assertEqual(params['exclude'],['alliance:999'])
                    self.assertFalse(set(params)&{'q','target_id','position','page'})
                    self.assertEqual('aria-current="page"' in attrs,tab_kind==kind)
                self.assertNotIn('data-rank-kind="coalition"',response.text)

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
        with patch.object(rankings,'_query',return_value=[dict(entity_id=1,destroyed=Decimal(100),lost=Decimal(0),net=Decimal(100),efficiency=Decimal(100),kills=1,losses=0,unvalued_kills=0,unvalued_losses=0,name='One')]) as query:
            self.assertFalse(rankings.ranking_rows('character','classic','destroyed',JAN,FEB,months)[1])
            self.assertTrue(rankings.ranking_rows('character','classic','destroyed',JAN,FEB,months)[1])
            self.assertEqual(query.call_count,1)
            months[0]['built_at']='v2'
            self.assertFalse(rankings.ranking_rows('character','classic','destroyed',JAN,FEB,months)[1])
            self.assertEqual(query.call_count,2)

    def test_disk_cache_survives_process_cache_reset_and_data_version_invalidates(self):
        row=dict(entity_id=1,destroyed=Decimal('123.25'),lost=Decimal(10),net=Decimal('113.25'),
                 efficiency=Decimal('92.492'),kills=1,losses=1,unvalued_kills=0,unvalued_losses=0,name='One')
        months=[dict(month=JAN,built_at='version-1')]
        with patch.object(rankings,'_query',return_value=[row]) as query:
            self.assertFalse(rankings.ranking_rows('character','classic','destroyed',JAN,FEB,months)[1])
            rankings._CACHE.clear()
            result,cached=rankings.ranking_rows('character','classic','destroyed',JAN,FEB,months)
            self.assertTrue(cached)
            self.assertEqual(result,[row])
            self.assertEqual(query.call_count,1)
            months[0]['built_at']='version-2'
            self.assertFalse(rankings.ranking_rows('character','classic','destroyed',JAN,FEB,months)[1])
            self.assertEqual(query.call_count,2)

    def test_lookup_receives_selected_month_and_rejects_invalid_month(self):
        with patch.object(rankings,'lookup',return_value=[]) as lookup:
            self.assertEqual(self.client.get('/admin/debug/rankings/lookup?q=Pilot&through=2026-01').status_code,200)
            self.assertEqual(lookup.call_args.kwargs['as_of'],JAN)
            self.assertEqual(self.client.get('/admin/debug/rankings/lookup?q=Pilot').status_code,200)
            self.assertIsNone(lookup.call_args.kwargs['as_of'])
            lookup.reset_mock()
            self.assertEqual(self.client.get('/admin/debug/rankings/lookup?q=Pilot&through=invalid').status_code,400)
            lookup.assert_not_called()

    def test_cached_ranking_affiliations_are_reloaded_for_the_selected_month(self):
        months=[dict(month=JAN,built_at='v',killmails=1,valued=1,zero_damage=0,entity_rows=1,dirty=False)]
        row=dict(entity_id=1,name='Pilot',destroyed=Decimal(100),lost=Decimal(0),net=Decimal(100),efficiency=100,
                 kills=1,losses=0,unvalued_kills=0,unvalued_losses=0,corporation_id=11,corporation_name='Current Corp',alliance_id=1001,alliance_name='Current Alliance')
        def enrich(kind,rows,as_of):
            self.assertEqual(as_of,JAN)
            return [dict(r,corporation_id=10,corporation_name='Historical Corp',alliance_id=1000,alliance_name='Historical Alliance') for r in rows]
        with patch.object(rankings,'catalog',return_value=months),patch.object(rankings,'ranking_rows',return_value=([row],True)),patch.object(rankings,'enrich_rows',side_effect=enrich):
            response=self.client.get('/admin/debug/rankings?through=2026-01')
            self.assertIn('Historical Corp',response.text)
            self.assertIn('Historical Alliance',response.text)
            self.assertNotIn('Current Corp',response.text)
            self.assertIn('end of January 2026',response.text)

    def test_default_ranking_uses_last_completed_month_until_build_catches_up(self):
        months=[dict(month=JAN,built_at='v',killmails=1,valued=1,zero_damage=0,entity_rows=1,dirty=False)]
        with patch.object(rankings,'catalog',return_value=months),patch.object(rankings,'ranking_rows',return_value=([],True)),patch.object(rankings,'enrich_rows',return_value=[]) as enrich:
            response=self.client.get('/admin/debug/rankings')
            self.assertEqual(enrich.call_args.args[2],JAN)
            self.assertIn('Corporations and alliances as of end of January 2026',response.text)
            self.assertIn('data-default-through="2026-01"',response.text)
        current=datetime.now(UTC).date().replace(day=1)
        months[0]['month']=current
        with patch.object(rankings,'catalog',return_value=months),patch.object(rankings,'ranking_rows',return_value=([],True)),patch.object(rankings,'enrich_rows',return_value=[]) as enrich:
            response=self.client.get('/admin/debug/rankings')
            self.assertIsNone(enrich.call_args.args[2])
            self.assertIn('Corporations and alliances as of today',response.text)

    def test_search_route_passes_target_and_filters_to_the_same_ranking(self):
        months=[dict(month=JAN,built_at='v',killmails=1,valued=1,zero_damage=0,entity_rows=1,dirty=False)]
        row=dict(entity_id=3,name='Pilot',destroyed=Decimal(100),lost=Decimal(0),net=Decimal(100),efficiency=100,
                 kills=1,losses=0,unvalued_kills=0,unvalued_losses=0,rank=2500,highlight=True)
        with patch.object(rankings,'catalog',return_value=months),patch.object(rankings,'scope_labels',return_value={'coalition:1':'The Imperium'}),patch.object(rankings,'explore_rows',return_value=dict(rows=[row],offset=2494,position=2500,more=False)) as query:
            response=self.client.get('/admin/debug/rankings?target_id=3&q=Pilot&include=coalition:1&exclude=alliance:9&through=2026-01')
            self.assertEqual(response.status_code,200)
            self.assertIn('Around rank 2,500',response.text)
            self.assertIn('The Imperium',response.text)
            self.assertEqual(query.call_args.kwargs['target_id'],3)
            self.assertEqual(query.call_args.kwargs['as_of'],JAN)
            self.assertEqual(query.call_args.kwargs['includes'],['coalition:1'])
            self.assertEqual(query.call_args.kwargs['excludes'],['alliance:9'])

    def test_shared_lock_blocks_duplicate_work_and_file_cache_is_bounded(self):
        engine=rankings.engine()
        with engine.cache_lock(rankings.CACHE_DIR,'a'*64):
            with self.assertRaises(engine.RankingsBusy):
                with engine.cache_lock(rankings.CACHE_DIR,'a'*64):
                    self.fail('Duplicate query acquired a held lock')
        for i in range(70):
            engine.save_cache(rankings.CACHE_DIR,f'{i:064x}',[])
        self.assertEqual(len(list(rankings.CACHE_DIR.glob('*.json'))),engine.CACHE_LIMIT)

    def test_upgrade_notice_skips_the_old_expensive_query(self):
        months=[dict(month=JAN,built_at='old',killmails=1,valued=1,zero_damage=0,entity_rows=1,dirty=False,totals_ready=False)]
        with patch.object(rankings,'catalog',return_value=months),patch.object(rankings,'ranking_rows') as query:
            response=self.client.get('/admin/debug/rankings')
            self.assertEqual(response.status_code,200)
            self.assertIn('one-time totals initialization',response.text)
            query.assert_not_called()

    def test_special_scope_tokens_and_labels_do_not_need_entity_lookup(self):
        self.assertEqual(rankings.parse_scope(['universe:0','alliance:0','universe:0']),['universe:0','alliance:0'])
        self.assertEqual(rankings.scope_labels(['universe:0','alliance:0']),{'universe:0':'Universe','alliance:0':'No alliance'})
        self.assertIsNone(rankings.scope_logo('universe:0'))
        self.assertIsNone(rankings.scope_logo('alliance:0'))
        for token in ('universe:1','corporation:0','coalition:0','alliance:-1'):
            with self.assertRaises(ValueError):rankings.parse_scope([token])

    def test_explicit_universe_reuses_the_fast_ranking_query(self):
        with patch.object(rankings,'catalog',return_value=[dict(month=JAN,built_at='version',killmails=1,valued=1,zero_damage=0,entity_rows=1,dirty=False)]), \
             patch.object(rankings,'ranking_rows',return_value=([],True)) as fast, \
             patch.object(rankings,'explore_rows') as scoped:
            response=self.client.get('/admin/debug/rankings?include=universe:0')
        self.assertEqual(response.status_code,200)
        self.assertIn('Universe',response.text)
        fast.assert_called_once();scoped.assert_not_called()

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
                CREATE TABLE IF NOT EXISTS public.sde_npccharacters(sde_key text PRIMARY KEY,data jsonb);
                TRUNCATE public.sde_npccharacters;
                CREATE SCHEMA entities;
                CREATE TABLE entities.characters(character_id bigint PRIMARY KEY,name text);
                CREATE TABLE entities.corporations(corporation_id bigint PRIMARY KEY,name text);
                CREATE TABLE entities.alliances(alliance_id bigint PRIMARY KEY,name text);
                INSERT INTO entities.characters VALUES(1,'Pilot One'),(2,'Pilot Two');
                INSERT INTO entities.corporations VALUES(10,'Old Corp'),(11,'New Corp'),(30,'Corp Thirty'),(90,'Victim Corp');
                INSERT INTO entities.alliances VALUES(1000,'Old Alliance'),(1001,'New Alliance'),(3000,'Alliance Thirty'),(900,'Victim Alliance');
                CREATE TABLE entities.character_current_affiliation(character_id bigint PRIMARY KEY,corporation_id bigint,alliance_id bigint);
                INSERT INTO entities.character_current_affiliation VALUES(1,11,1001),(2,10,1000),(3,30,3000),(9,90,900);
                CREATE TABLE entities.corporation_current_affiliation(corporation_id bigint PRIMARY KEY,alliance_id bigint);
                INSERT INTO entities.corporation_current_affiliation VALUES(10,1000),(11,1001),(30,3000),(90,900);
                CREATE TABLE entities.character_corporation_history(character_id bigint,record_id bigint,corporation_id bigint,
                    start_date timestamptz,end_date timestamptz,is_deleted boolean DEFAULT false);
                INSERT INTO entities.character_corporation_history VALUES
                    (1,1,10,'2025-01-01','2026-02-01',false),(1,2,11,'2026-02-01',NULL,false),
                    (2,1,10,'2025-01-01',NULL,false),(3,1,30,'2025-01-01',NULL,false);
                CREATE TABLE entities.corporation_alliance_history(corporation_id bigint,record_id bigint,alliance_id bigint,
                    start_date timestamptz,end_date timestamptz,is_deleted boolean DEFAULT false);
                INSERT INTO entities.corporation_alliance_history VALUES
                    (10,1,1000,'2025-01-01',NULL,false),(11,1,1001,'2025-01-01',NULL,false),
                    (30,1,3000,'2025-01-01',NULL,false),(90,1,900,'2025-01-01',NULL,false);
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
        builder.ensure_current(self.conn)
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

    def test_terminal_purge_is_limited_to_generated_rankings(self):
        self.build(JAN)
        self.build(FEB)
        with self.conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM rawkm.killmails')
            before = cur.fetchone()[0]
            script = Path(builder.__file__).with_name('purge_killmail_rankings.sh').read_text()
            reset = script.split("<<'SQL'\n", 1)[1].split('\nSQL', 1)[0]
            # Exercise the exact SQL shipped in the terminal script, in this disposable DB.
            cur.execute(reset)
            for table in ('killmail_rankings_monthly', 'killmail_rankings_current',
                          'killmail_ranking_months', 'killmail_ranking_versions'):
                cur.execute('SELECT count(*) FROM analytics.' + table)
                self.assertEqual(cur.fetchone()[0], 0)
            cur.execute('SELECT ready FROM analytics.killmail_ranking_current_state')
            self.assertEqual(cur.fetchone()[0], False)
            cur.execute('SELECT count(*) FROM rawkm.killmails')
            self.assertEqual(cur.fetchone()[0], before)
        self.conn.commit()
        builder.ensure_current(self.conn)
        self.conn.commit()
        self.build(JAN)
        with self.conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM analytics.killmail_rankings_monthly')
            self.assertGreater(cur.fetchone()[0], 0)

    def test_name_autocomplete_prefix_exact_context_and_literal_wildcards(self):
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO entities.characters VALUES (4,'Pilot'),(5,'100% Pilot'),(6,'Under_score'),(7,'UnderXscore')")
        self.conn.commit()
        with patch.object(rankings,'db',return_value=self.conn):
            matches=rankings.lookup('pi','character')
            self.assertEqual([r['name'] for r in matches],['Pilot','Pilot One','Pilot Two'])
            one=next(r for r in matches if r['entity_id']==1)
            self.assertEqual((one['corporation_name'],one['alliance_name']),('New Corp','New Alliance'))
            self.assertEqual(rankings.lookup('PILOT','character')[0]['name'],'Pilot')
            self.assertEqual(rankings.lookup('One','character')[0]['name'],'Pilot One')
            self.assertEqual(rankings.lookup('100%','character')[0]['name'],'100% Pilot')
            self.assertEqual([r['name'] for r in rankings.lookup('Under_','character')],['Under_score'])
            self.assertEqual(rankings.lookup('999999999999999999999999','character'),[])
            self.assertEqual(rankings.lookup('old','corporation')[0]['alliance_name'],'Old Alliance')
            self.assertEqual(rankings.lookup('new','alliance')[0]['name'],'New Alliance')

    def test_npcs_keep_their_scores_but_are_not_ranked_or_suggested(self):
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO public.sde_npccharacters VALUES('3019582','{}')")
            cur.execute("INSERT INTO entities.characters VALUES(3019582,'Apollo Tyrannos')")
            cur.execute("INSERT INTO rawkm.killmail_attackers VALUES(100,'2026-01-02 12:00+00',3019582,1000274,NULL,500)")
        self.conn.commit()
        self.build()
        with self.conn.cursor() as cur:
            cur.execute('SELECT destroyed_cents FROM analytics.killmail_rankings_monthly WHERE entity_kind=1 AND entity_id=3019582')
            self.assertEqual(cur.fetchone()[0],10000)
        self.conn.commit()
        for mode in ['classic','weighted']:
            for sort in rankings.SORTS:
                with psycopg2.connect(self.dsn) as conn:
                    rows=rankings.engine().query(conn,'character',mode,sort,JAN,JAN)
                    self.assertNotIn(3019582,[r['entity_id'] for r in rows])
                with psycopg2.connect(self.dsn) as conn:
                    result=rankings.explorer().explore(conn,rankings.engine(),'character',mode,sort,JAN,JAN,as_of=JAN)
                    self.assertNotIn(3019582,[r['entity_id'] for r in result['rows']])
                    self.assertEqual([r['rank'] for r in result['rows']],list(range(1,len(result['rows'])+1)))
        with patch.object(rankings,'db',side_effect=lambda:psycopg2.connect(self.dsn)):
            self.assertEqual(rankings.lookup('Apollo','character'),[])
            self.assertEqual(rankings.lookup('3019582','character'),[])
        with psycopg2.connect(self.dsn) as conn:
            with self.assertRaisesRegex(ValueError,'not ranked'):
                rankings.explorer().explore(conn,rankings.engine(),'character','classic','destroyed',JAN,JAN,target_id=3019582)
        # The fallback monthly query also filters before applying the top limit.
        with self.conn.cursor() as cur:
            cur.execute('UPDATE analytics.killmail_ranking_current_state SET ready=false')
        self.conn.commit()
        with psycopg2.connect(self.dsn) as conn:
            rows=rankings.engine().query(conn,'character','classic','destroyed',JAN,JAN)
            self.assertNotIn(3019582,[r['entity_id'] for r in rows])

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

    def test_sparse_snapshots_carry_forward_and_old_correction_updates_later_totals(self):
        self.build();self.build(FEB)
        with self.conn.cursor() as cur:
            cur.execute("SELECT cumulative_destroyed_cents FROM analytics.killmail_rankings_monthly WHERE entity_kind=1 AND entity_id=1 ORDER BY month")
            self.assertEqual([r[0] for r in cur], [10000,40000])
            cur.execute("SELECT xmin::text FROM analytics.killmail_rankings_current WHERE entity_kind=1 AND entity_id=3")
            before=cur.fetchone()[0]
        self.conn.commit()
        self.build(FEB)
        with self.conn.cursor() as cur:
            cur.execute("SELECT xmin::text FROM analytics.killmail_rankings_current WHERE entity_kind=1 AND entity_id=3")
            self.assertEqual(cur.fetchone()[0],before)
            cur.execute("SELECT count(*) FROM analytics.killmail_rankings_monthly WHERE entity_kind=1 AND entity_id=3")
            self.assertEqual(cur.fetchone()[0],1)
            cur.execute("UPDATE rawkm.killmails SET estimated_value_cents=15000 WHERE killmail_id=100 AND killmail_time<'2026-02-01'")
        self.conn.commit();self.build()
        with self.conn.cursor() as cur:
            cur.execute("SELECT destroyed_cents,cumulative_destroyed_cents FROM analytics.killmail_rankings_monthly WHERE entity_kind=1 AND entity_id=1 AND month=%s",(FEB,))
            self.assertEqual(cur.fetchone(),(30000,45000))
            cur.execute("SELECT destroyed_cents,last_month FROM analytics.killmail_rankings_current WHERE entity_kind=1 AND entity_id=1")
            self.assertEqual(cur.fetchone(),(45000,FEB))

    def test_fractional_damage_credit_does_not_drift_on_repeated_rebuilds(self):
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO rawkm.killmails VALUES(104,'2026-01-06 12:00+00',1,4,40,4000)")
            cur.execute("INSERT INTO rawkm.killmail_attackers VALUES(104,'2026-01-06 12:00+00',1,10,1000,1),(104,'2026-01-06 12:00+00',2,10,1000,2)")
        self.conn.commit()
        self.build();self.build(FEB)
        for _ in range(5):
            self.build()
            with self.conn.cursor() as cur:
                cur.execute("SELECT weighted_destroyed_cents FROM analytics.killmail_rankings_current WHERE entity_kind=1 AND entity_id=1")
                self.assertEqual(cur.fetchone()[0],Decimal('33000.33333333'))
            self.conn.commit()

    def test_current_and_historical_rankings_equal_monthly_facts_for_every_sort(self):
        self.build();self.build(FEB)
        self.conn.commit()
        engine=rankings.engine()
        for last in (JAN,FEB):
            for kind in rankings.KINDS:
                for mode in ('classic','weighted'):
                    for sort in rankings.SORTS:
                        with patch.object(rankings,'db',side_effect=lambda:psycopg2.connect(self.dsn)):
                            actual=rankings._query(kind,mode,sort,JAN,last)
                            with patch.object(engine,'_current_rows',return_value=None):
                                expected=rankings._query(kind,mode,sort,JAN,last)
                        self.assertEqual(actual,expected,(kind,mode,sort,last))

    def test_future_only_entities_are_absent_from_the_historical_ranking(self):
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO rawkm.killmail_attackers VALUES(100,'2026-02-02 12:00+00',50,500,5000,100)")
        self.conn.commit();self.build();self.build(FEB)
        with patch.object(rankings,'db',side_effect=lambda:psycopg2.connect(self.dsn)):
            past=rankings._query('character','classic','destroyed',JAN,JAN)
            present=rankings._query('character','classic','destroyed',JAN,FEB)
        self.assertNotIn(50,[r['entity_id'] for r in past])
        self.assertIn(50,[r['entity_id'] for r in present])

    def test_backfill_uses_existing_months_without_new_raw_imports(self):
        self.build();self.build(FEB)
        with self.conn.cursor() as cur:
            cur.execute('UPDATE analytics.killmail_ranking_current_state SET ready=false')
            cur.execute('DELETE FROM analytics.killmail_rankings_current')
            cur.execute('UPDATE analytics.killmail_rankings_monthly SET cumulative_destroyed_cents=NULL')
        self.conn.commit()
        builder.ensure_current(self.conn)
        self.conn.commit()
        with self.conn.cursor() as cur:
            cur.execute("SELECT destroyed_cents FROM analytics.killmail_rankings_current WHERE entity_kind=1 AND entity_id=1")
            self.assertEqual(cur.fetchone()[0],40000)
            cur.execute("SELECT cumulative_destroyed_cents FROM analytics.killmail_rankings_monthly WHERE entity_kind=1 AND entity_id=1 ORDER BY month")
            self.assertEqual([r[0] for r in cur],[10000,40000])
            cur.execute('SELECT count(*) FROM rawkm.killmails')
            self.assertEqual(cur.fetchone()[0],5)

    def test_removing_activity_deletes_empty_snapshots_and_current_entities(self):
        self.build();self.build(FEB)
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM rawkm.killmails WHERE killmail_time<'2026-02-01'")
        self.conn.commit();self.build()
        with self.conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM analytics.killmail_rankings_monthly WHERE month=%s',(JAN,))
            self.assertEqual(cur.fetchone()[0],0)
            cur.execute('SELECT count(*) FROM analytics.killmail_rankings_current')
            self.assertEqual(cur.fetchone()[0],6)
            cur.execute("SELECT destroyed_cents FROM analytics.killmail_rankings_current WHERE entity_kind=1 AND entity_id=1")
            self.assertEqual(cur.fetchone()[0],30000)

    def test_search_name_target_and_rank_show_neighbours_beyond_top_1000(self):
        self.build();self.build(FEB)
        with self.conn.cursor() as cur:
            cur.execute("""INSERT INTO analytics.killmail_rankings_current(entity_kind,entity_id,destroyed_cents,kills,last_month)
                           SELECT 1,id,1000000-id,1,'2026-02-01' FROM generate_series(1000,2199) id""")
        self.conn.commit()
        engine=rankings.explorer()
        with psycopg2.connect(self.dsn) as conn:
            result=engine.explore(conn,rankings.engine(),'character','classic','destroyed',JAN,FEB,target_id=2100,around=True)
        self.assertEqual(result['position'],1101)
        self.assertEqual(len(result['rows']),11)
        self.assertEqual(result['rows'][5]['entity_id'],2100)
        self.assertTrue(result['rows'][5]['highlight'])
        with psycopg2.connect(self.dsn) as conn:
            by_rank=engine.explore(conn,rankings.engine(),'character','classic','destroyed',JAN,FEB,position=1101,around=True)
        self.assertEqual([r['entity_id'] for r in result['rows']],[r['entity_id'] for r in by_rank['rows']])

    def test_membership_filters_use_current_or_month_end_and_exclusions_win(self):
        from datetime import date, datetime, UTC
        self.build();self.build(FEB)
        engine=rankings.explorer()
        def query(**options):
            with psycopg2.connect(self.dsn) as conn:
                return engine.explore(conn,rankings.engine(),'character','classic','destroyed',JAN,FEB,scope_day=date(2026,1,31),**options)['rows']
        self.assertEqual([r['entity_id'] for r in query(includes=['alliance:1000'])],[2])
        historical=query(includes=['alliance:1000'],as_of=JAN)
        self.assertEqual({r['entity_id'] for r in historical},{1,2})
        rules={1:[dict(operation='exclude',member_type='corporation',member_id=30,valid_from=None,valid_to=None),
                  dict(operation='include',member_type='alliance',member_id=1000,valid_from=None,valid_to=None)]}
        self.assertEqual([r['entity_id'] for r in query(includes=['coalition:1'],rules=rules)],[2])
        self.assertEqual(query(includes=['coalition:1'],excludes=['corporation:10'],rules=rules),[])
        with self.assertRaises(ValueError):
            query(includes=['alliance:1000'],target_id=1,around=True)

    def test_universe_and_no_alliance_filters_at_current_and_historical_dates(self):
        from datetime import date, datetime, UTC
        self.build();self.build(FEB)
        with self.conn.cursor() as cur:
            cur.execute('UPDATE entities.character_current_affiliation SET alliance_id=0 WHERE character_id=2')
            cur.execute('UPDATE entities.corporation_current_affiliation SET alliance_id=NULL WHERE corporation_id=10')
            cur.execute('UPDATE entities.corporation_alliance_history SET alliance_id=NULL WHERE corporation_id=10')
            cur.execute("INSERT INTO analytics.killmail_rankings_current(entity_kind,entity_id,destroyed_cents,kills,last_month) VALUES(1,55,50,1,'2026-02-01')")
        self.conn.commit()
        def ids(kind='character',**options):
            with psycopg2.connect(self.dsn) as conn:
                rows=rankings.explorer().explore(conn,rankings.engine(),kind,'classic','destroyed',JAN,FEB,
                    scope_day=date(2026,1,31),**options)['rows']
            return {row['entity_id'] for row in rows}
        everyone=ids()
        self.assertIn(55,everyone)
        self.assertEqual(ids(includes=['universe:0']),everyone)
        self.assertEqual(ids(includes=['universe:0'],excludes=['corporation:30']),everyone-{3})
        self.assertEqual(ids(includes=['alliance:0']),{2})
        self.assertEqual(ids(includes=['universe:0'],excludes=['alliance:0']),everyone-{2})
        self.assertEqual(ids(includes=['alliance:0'],as_of=JAN),{1,2})
        self.assertEqual(ids(kind='corporation',includes=['alliance:0']),{10})
        self.assertEqual(ids(kind='corporation',includes=['alliance:0'],as_of=JAN),{10})
        self.assertEqual(ids(kind='alliance',includes=['alliance:0']),set())
        self.assertEqual(ids(excludes=['universe:0']),set())

    def test_affiliations_display_historical_data_without_current_fallback(self):
        rows=[dict(entity_id=1),dict(entity_id=9)]
        with psycopg2.connect(self.dsn) as conn:
            current=rankings.explorer().enrich(conn,'character',rows)
        self.assertEqual(current[0]['corporation_name'],'New Corp')
        with psycopg2.connect(self.dsn) as conn:
            historical=rankings.explorer().enrich(conn,'character',rows,JAN)
        self.assertEqual(historical[0]['corporation_name'],'Old Corp')
        self.assertEqual(historical[0]['alliance_name'],'Old Alliance')
        self.assertIsNone(historical[1]['corporation_id'])
        with patch.object(rankings,'db',side_effect=lambda:psycopg2.connect(self.dsn)):
            self.assertEqual(rankings.scope_labels(['corporation:10','alliance:1000']),{'corporation:10':'Old Corp','alliance:1000':'Old Alliance'})

    def test_lookup_historical_affiliations_use_month_end_and_never_current_fallback(self):
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO entities.characters VALUES(9,'Victim Pilot')")
            # Membership that starts at midnight the following month must not leak backwards.
            cur.execute("UPDATE entities.corporation_alliance_history SET end_date='2026-02-01' WHERE corporation_id=10")
            cur.execute("INSERT INTO entities.corporation_alliance_history VALUES(10,2,3000,'2026-02-01',NULL,false)")
        self.conn.commit()
        with patch.object(rankings,'db',return_value=self.conn):
            historical=rankings.lookup('Pilot One','character',as_of=JAN)[0]
            self.assertEqual((historical['corporation_name'],historical['alliance_name']),('Old Corp','Old Alliance'))
            later=rankings.lookup('Pilot One','character',as_of=FEB)[0]
            self.assertEqual((later['corporation_name'],later['alliance_name']),('New Corp','New Alliance'))
            corp=rankings.lookup('Old Corp','corporation',as_of=JAN)[0]
            self.assertEqual(corp['alliance_name'],'Old Alliance')
            self.assertEqual(rankings.lookup('Old Corp','corporation',as_of=FEB)[0]['alliance_name'],'Alliance Thirty')
            unknown=rankings.lookup('Victim','character',as_of=JAN)[0]
            self.assertIsNone(unknown['corporation_id'])
            self.assertIsNone(unknown['alliance_id'])

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
