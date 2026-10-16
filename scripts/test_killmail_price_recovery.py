"""Safety and precision checks for sparse zKill price recovery."""
import os
import threading
import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from scripts import recover_killmail_prices as recovery
from scripts import calculate_killmail_values as values
from scripts import test_killmail_values as fixtures
jobs = fixtures.jobs

DAY = date(2007, 12, 5)


def item_html(type_id=4318, current='0.01', rows=''):
    return f'<link rel="canonical" href="https://zkillboard.com/item/{type_id}/"><h4>Market Prices - The Forge</h4><h5>zKillboard Price: {current} ISK</h5><table>{rows}</table>'


def parse_item(**kwargs):
    page=recovery.ItemPage();page.feed(item_html(**kwargs));return page


class ParserTests(unittest.TestCase):
    def test_dated_history_uses_previous_before_first_later(self):
        page=parse_item(current='999', rows='<tr><td>2010-01-01</td><td>1,234.56</td></tr><tr><td>2010-01-03</td><td>200</td></tr>')
        self.assertEqual(page.quote(date(2010,1,2)),(Decimal('1234.56'),'item_history',date(2010,1,1)))
        self.assertEqual(page.quote(date(2007,1,1)),(Decimal('1234.56'),'item_history',date(2010,1,1)))
        self.assertEqual(page.quote(date(2010,1,3))[0],Decimal('200'))

    def test_explicit_cent_is_valid_and_missing_price_defaults_to_cent(self):
        self.assertEqual(parse_item().quote(DAY),(Decimal('.01'),'item_current',None))
        for current in ('0','NaN','unknown'):
            self.assertEqual(parse_item(current=current).quote(DAY),(Decimal('.01'),'fallback',None))

    def test_rejects_wrong_identity(self):
        session=MagicMock()
        session.get.return_value=MagicMock(status_code=200,content=b'x',text=item_html(type_id=1))
        with self.assertRaises(ValueError):
            recovery.PageClient(session,threading.Event()).fetch(4318)

    def test_http_404_cached_and_only_item_url_requested(self):
        session=MagicMock();session.get.return_value.status_code=404
        client=recovery.PageClient(session,threading.Event())
        self.assertIsNone(client.fetch(123));self.assertIsNone(client.fetch(123))
        session.get.assert_called_once_with('https://zkillboard.com/item/123/',timeout=(15,60),allow_redirects=False)

    def test_rate_limit_wait_and_then_success(self):
        session=MagicMock()
        limited=MagicMock(status_code=429,headers={'Retry-After':'125'})
        ok=MagicMock(status_code=200,content=b'x',text=item_html(type_id=248))
        session.get.side_effect=[limited,ok]
        stopped=MagicMock();stopped.wait.return_value=False
        client=recovery.PageClient(session,stopped)
        self.assertIsNotNone(client.fetch(248))
        self.assertTrue(any(call.args==(125.0,) for call in stopped.wait.call_args_list))

    def test_diagnostics_include_missing_component(self):
        pricing = recovery.DiagnosticPricing({1:(None,None,'Root')}, {1:(1,{2:3})}, {}, valuation_mode='loss-day')
        self.assertTrue(pricing.price(1,DAY,DAY)[1])
        self.assertEqual(pricing.issues[(DAY,2)][0], 'type_missing_from_sde')
        self.assertEqual(pricing.issues[(DAY,1)][0], 'recipe_component_unpriced')

    def test_job_is_passive(self):
        with patch.object(jobs, '_read_config_file', return_value={'jobs':[]}), patch.object(jobs, '_start_process') as launch:
            job=jobs.get_job('recover_killmail_prices')
            self.assertEqual(job['type'],'killmail_price_recovery')
            launch.assert_not_called()
            jobs.run_killmail_price_recovery()
            launch.assert_called_once_with(job, job['command'])


class RecalculationTests(unittest.TestCase):
    def test_reuses_regular_null_only_calculator_and_releases_valuation_lock(self):
        from pathlib import Path
        from types import SimpleNamespace
        import tempfile
        conn = MagicMock()
        process = MagicMock(returncode=0, pid=123)
        process.poll.return_value = 0
        with tempfile.TemporaryDirectory() as tmp, patch.object(recovery.subprocess, 'Popen', return_value=process) as launch:
            args = SimpleNamespace(config=Path('/safe/db.json'), progress=Path(tmp)/'progress.json')
            recovery.recalculate(conn, args, threading.Event(), MagicMock())
            command = launch.call_args.args[0]
            self.assertIn(str(Path(values.__file__).resolve()), command)
            self.assertEqual(command[command.index('--db-config')+1], '/safe/db.json')
            self.assertNotIn('--valuation-mode', command)  # Normal loss-day defaults.
            conn.cursor.return_value.__enter__.return_value.execute.assert_called_once_with(
                'SELECT pg_advisory_unlock(%s)', (values.LOCK,))

    def test_stopping_recovery_stops_child_calculator(self):
        from pathlib import Path
        from types import SimpleNamespace
        conn = MagicMock()
        process = MagicMock(pid=123)
        process.poll.return_value = None
        stopped = MagicMock()
        stopped.is_set.return_value = False
        stopped.wait.return_value = True
        with patch.object(recovery.subprocess, 'Popen', return_value=process):
            with self.assertRaises(recovery.StopRequested):
                recovery.recalculate(conn, SimpleNamespace(config=Path('/safe/db.json'), progress=Path('/tmp/unread-progress.json')), stopped, MagicMock())
        process.terminate.assert_called()
        process.wait.assert_called()


@unittest.skipUnless(os.environ.get('KILLMAIL_VALUES_TEST_DSN'), 'Use isolated valuation test database')
class RecoveryDatabaseTests(unittest.TestCase):
    def setUp(self):
        fixtures.DatabaseTests.setUp(self)
        with self.conn.cursor() as cur:
            cur.execute('CREATE SCHEMA market; CREATE TABLE market.zkill_daily_prices(price_date date PRIMARY KEY,prices jsonb)')
            cur.execute(recovery.SCHEMA.read_text())
            cur.execute("INSERT INTO market.zkill_daily_prices VALUES('2026-01-01','{\"1\":10,\"2\":0}'),('2026-01-02','{}')")

    def tearDown(self):
        fixtures.DatabaseTests.tearDown(self)

    def test_official_prices_preserved_and_supplementary_quote_idempotent(self):
        day=date(2026,1,1)
        self.assertEqual(recovery.save_quote(self.conn,day,1,Decimal(999),123),0)
        self.assertEqual(recovery.save_quote(self.conn,day,2,Decimal('12.3456'),123),1)
        self.assertEqual(recovery.save_quote(self.conn,day,2,Decimal(999),999),0)
        with self.conn.cursor() as cur:
            cur.execute('SELECT prices FROM market.zkill_daily_prices WHERE price_date=%s',(day,))
            self.assertEqual(cur.fetchone()[0], {'1':10,'2':0})
        first=values.load_first_prices(self.conn, date(2026,1,2))
        self.assertEqual(first[2],(day, Decimal('12.3456')))
        with self.conn.cursor() as cur:
            cur.execute(values.price_stream_query(self.conn),(day,day))
            prices=cur.fetchone()[1]
            self.assertEqual(prices['1'],10)
            self.assertEqual(Decimal(str(prices['2'])),Decimal('12.3456'))

    def test_later_official_price_supersedes_recovered_same_day(self):
        day=date(2026,1,1)
        recovery.save_quote(self.conn,day,2,Decimal(12),123)
        with self.conn.cursor() as cur:
            cur.execute("UPDATE market.zkill_daily_prices SET prices=prices || '{\"2\":20}' WHERE price_date=%s",(day,))
            cur.execute(values.price_stream_query(self.conn),(day,day))
            self.assertEqual(cur.fetchone()[1]['2'],20)
        self.assertEqual(values.load_first_prices(self.conn,day)[2],(day,Decimal(20)))

    def test_recovered_quote_fills_null_kill_without_touching_existing_value_or_raw_items(self):
        day=date(2026,1,1)
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO rawkm.killmails VALUES(1,'2026-01-01 12:00+00',2,NULL),(2,'2026-01-01 12:00+00',2,987654)")
            cur.execute("INSERT INTO rawkm.killmail_items VALUES(1,'2026-01-01 12:00+00',0,1,5,3,0,0)")
            cur.execute('SELECT * FROM rawkm.killmail_items'); before=cur.fetchall()
        recovery.save_quote(self.conn,day,2,Decimal('12.3456'),123)
        pricing=values.Pricing({}, {}, {},valuation_mode='loss-day',first_prices=values.load_first_prices(self.conn,day))
        rows=values.boundary_prices(day,[(day,1),(day,2)],pricing)
        self.assertEqual(values.update_month(self.conn,day,rows,'loss-day')[0],1)
        with self.conn.cursor() as cur:
            cur.execute('SELECT killmail_id,estimated_value_cents FROM rawkm.killmails ORDER BY 1')
            self.assertEqual(cur.fetchall(),[(1,4235),(2,987654)])
            cur.execute('SELECT * FROM rawkm.killmail_items');self.assertEqual(cur.fetchall(),before)

    def test_call_estimate_sums_actual_missing_item_days(self):
        day=date(2026,1,1)
        recovery.save_gap(self.conn,day,1,'One','no_price_or_recipe','Missing')
        recovery.save_gap(self.conn,day,2,'Two','no_price_or_recipe','Missing')
        recovery.save_gap(self.conn,date(2026,1,2),1,'One','no_price_or_recipe','Missing')
        recovery.save_gap(self.conn,day,0,'Missing hull','missing_victim_ship','Raw data problem')
        self.assertEqual(recovery.gap_counts(self.conn),dict(missing_items=2,missing_item_days=3,missing_dates=2,estimated_calls_max=2))
        with self.conn.cursor() as cur:
            cur.execute("UPDATE market.killmail_price_gaps SET status='recovered' WHERE item_type_id=2")
        self.assertEqual(recovery.gap_counts(self.conn)['missing_item_days'],2)


    def test_full_diagnosis_and_recovery_with_real_sql_stream(self):
        day=date(2026,1,1)
        cfg={k:self.conn.get_dsn_parameters()[k] for k in ('dbname','user','host','port')}
        stopped=threading.Event()
        try:
            with self.conn.cursor() as cur:
                cur.execute('CREATE TABLE public.sde_groups(sde_key text PRIMARY KEY,data jsonb)')
                cur.execute("INSERT INTO rawkm.killmails VALUES(1,'2026-01-01 12:00+00',2,NULL)")
            self.conn.commit()
            with patch.object(values,'RecipeHistory',return_value=None):
                recovery.diagnose(self.conn,cfg,stopped,MagicMock())
            self.assertEqual(recovery.gap_counts(self.conn)['missing_item_days'],1)
            with self.conn.cursor() as cur:
                cur.execute('SELECT item_type_id,reason FROM market.killmail_price_gaps WHERE active')
                self.assertEqual(cur.fetchall(),[(2,'type_missing_from_sde')])
            page=parse_item(type_id=2,current='4.12')
            client=MagicMock(requests=1)
            client.fetch.return_value=page
            progress=MagicMock()
            recovery.recover(self.conn,stopped,client,progress)
            client.fetch.assert_called_once_with(2)
            with self.conn.cursor() as cur:
                cur.execute('SELECT unit_price_isk,source_killmail_id FROM market.zkill_recovered_prices')
                self.assertEqual(cur.fetchone(),(Decimal('4.12'),None))
            self.assertTrue(any(call.kwargs.get('planned_calls_max')==1 for call in progress.call_args_list))
        finally:
            # This fixture commits deliberately; cleanup is confined to the guarded test DB.
            self.conn.rollback()
            with self.conn.cursor() as cur:
                cur.execute('DROP SCHEMA rawkm CASCADE; DROP SCHEMA market CASCADE; DROP TABLE public.sde_types; DROP TABLE IF EXISTS public.sde_groups')
            self.conn.commit()


    def test_one_item_request_serves_multiple_dates_and_404_uses_cent(self):
        day=date(2026,1,1)
        try:
            recovery.save_gap(self.conn,day,2,'Two','no_price_or_recipe','Missing')
            recovery.save_gap(self.conn,date(2026,1,2),2,'Two','no_price_or_recipe','Missing')
            recovery.save_gap(self.conn,day,99,'Removed type','no_price_or_recipe','Missing')
            self.conn.commit()
            page=parse_item(type_id=2,current='4.12')
            client=MagicMock(requests=2)
            client.fetch.side_effect=lambda type_id: page if type_id==2 else None
            progress=MagicMock()
            recovery.recover(self.conn,threading.Event(),client,progress)
            self.assertEqual([call.args for call in client.fetch.call_args_list],[(2,),(99,)])
            with self.conn.cursor() as cur:
                cur.execute('SELECT item_type_id,price_date,unit_price_isk,source_kind,source_killmail_id FROM market.zkill_recovered_prices ORDER BY item_type_id,price_date')
                self.assertEqual(cur.fetchall(),[(2,day,Decimal('4.12'),'item_current',None),
                    (2,date(2026,1,2),Decimal('4.12'),'item_current',None),
                    (99,day,Decimal('.01'),'fallback',None)])
                cur.execute("SELECT status FROM market.killmail_price_gaps WHERE item_type_id=99")
                self.assertEqual(cur.fetchone()[0],'fallback')
                cur.execute(recovery.SCHEMA.read_text())  # Migration is safe to rerun.
            self.assertEqual(progress.call_args.kwargs['fallback_pairs'],1)
            self.assertEqual(progress.call_args.kwargs['items_processed'],2)
        finally:
            self.conn.rollback()
            with self.conn.cursor() as cur:
                cur.execute('DROP SCHEMA rawkm CASCADE; DROP SCHEMA market CASCADE; DROP TABLE public.sde_types')
            self.conn.commit()


class AdminControlTests(unittest.TestCase):
    def test_complete_jobs_page_with_default_recovery_job(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from scripts.test_first_loss_admin import f
        routes = f.load('routes_admin_jobs')
        app = FastAPI()
        app.include_router(routes.router)
        user = {'id': 1, 'username': 'admin', 'permissions': {'admin.jobs.view', 'admin.jobs.run'}}
        with patch.object(jobs, '_read_config_file', return_value={'jobs': []}), \
             patch.object(routes, 'require_login', return_value=user), \
             patch.object(routes, 'require_permission_or_redirect', return_value=None), \
             patch.object(jobs, '_start_process') as launch:
            listed = jobs.list_jobs_with_logs()
            recovery_job = next(job for job in listed if job['key'] == 'recover_killmail_prices')
            for field in ('log_exists', 'running', 'started_at', 'last_lines'):
                self.assertIn(field, recovery_job)
            response = TestClient(app).get('/admin/jobs')
            self.assertEqual(response.status_code, 200)
            self.assertIn('Diagnose and recover missing item prices', response.text)
            self.assertIn('id="recovery-status"', response.text)
            self.assertIn('id="values-status"', response.text)
            launch.assert_not_called()

    def test_permissions_passive_status_and_manual_controls(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from scripts.test_first_loss_admin import f
        routes = f.load('routes_admin_killmail_price_recovery')
        app=FastAPI();app.include_router(routes.router)
        client=TestClient(app)
        user={'id':1,'username':'admin','permissions':{'admin.jobs.view'}}
        with patch.object(routes,'require_login',return_value=user), patch.object(routes,'killmail_price_recovery_progress',return_value={'running':False,'progress':None}), patch.object(routes,'run_killmail_price_recovery') as launch, patch.object(routes,'stop_killmail_price_recovery') as stop, patch.object(routes,'audit_log'):
            self.assertEqual(client.get('/admin/killmail-price-recovery/status').status_code,200)
            launch.assert_not_called()
            self.assertEqual(client.post('/admin/killmail-price-recovery/start').status_code,403)
            self.assertEqual(client.post('/admin/killmail-price-recovery/stop').status_code,403)
            stop.assert_not_called()
            user['permissions'].add('admin.jobs.run')
            self.assertEqual(client.post('/admin/killmail-price-recovery/start').json(),{'started':True})
            launch.assert_called_once()
            self.assertEqual(client.post('/admin/killmail-price-recovery/stop').json(),{'stop_requested':True})
            stop.assert_called_once()
            launch.side_effect=jobs.JobError('Already running')
            self.assertEqual(client.post('/admin/killmail-price-recovery/start').status_code,409)
