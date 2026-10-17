"""Local abyssal policy must win over corrupt/current/future zKill valuations."""
from datetime import date
from decimal import Decimal
import os
import unittest
from unittest.mock import Mock, MagicMock, patch
from scripts import abyssal_pricing as policy
from scripts import calculate_killmail_values as values
from scripts import recover_killmail_prices as recovery
from scripts import test_killmail_values as fixture
from scripts import reprice_abyssal_killmails as orchestration

DAY=date(2026,1,1)


class PolicyTests(unittest.TestCase):
    def engine(self, mode='loss-day'):
        return values.Pricing({52227:(60,7,'Abyssal Damage Control'),2048:(60,7,'Damage Control II')}, {},{},
            valuation_mode=mode,abyssal_equivalents={52227:(2048,)})

    def test_corrupt_quote_and_quote_cache_never_override_the_local_policy(self):
        for mode in ('loss-day','zkill'):
            engine=self.engine(mode)
            engine.add_day(DAY,{'52227':Decimal('999999999999'),'2048':Decimal('650000')})
            engine.quotes.market_put(52227,DAY,(Decimal('999999999999'),False))
            self.assertEqual(engine.price(52227,DAY,DAY),(Decimal('650000'),False))
            engine.add_day(date(2026,1,2),{'52227':Decimal('0.01'),'2048':Decimal('700000')})
            self.assertEqual(engine.price(52227,date(2026,1,2),date(2026,1,2)),(Decimal('700000'),False))

    def test_loss_day_fallback_uses_the_t2_history_not_abyssal_history(self):
        engine=self.engine()
        engine.add_day(DAY,{'2048':123,'52227':999999999})
        self.assertEqual(engine.price(52227,date(2026,1,3),date(2026,1,3))[0],Decimal(123))
        future=self.engine();future.first_prices[2048]=(date(2026,2,1),Decimal(456))
        self.assertEqual(future.price(52227,DAY,DAY)[0],Decimal(456))

    def test_ambiguous_plates_use_cheapest_compatible_t2(self):
        engine=values.Pricing({}, {},{},valuation_mode='loss-day',abyssal_equivalents={47812:(20345,20347)})
        engine.add_day(DAY,{'47812':999999999,'20345':500,'20347':1000})
        self.assertEqual(engine.price(47812,DAY,DAY),(Decimal(500),False))

    def test_missing_ordinary_price_cannot_reuse_an_abyssal_quote(self):
        engine=self.engine();engine.add_day(DAY,{'52227':999999999})
        with self.assertRaises(values.HistoricalRecipeUnavailable):engine.price(52227,DAY,DAY)
        diagnostic=recovery.DiagnosticPricing(engine.metadata,{}, {},valuation_mode='loss-day',abyssal_equivalents=engine.abyssal_equivalents)
        diagnostic.add_day(DAY,{'52227':999999999})
        with self.assertRaises(values.HistoricalRecipeUnavailable):diagnostic.price(52227,DAY,DAY)
        self.assertNotIn((DAY,52227),diagnostic.issues)
        self.assertIn((DAY,2048),diagnostic.issues)

    def test_non_module_mutaplasmid_retains_its_own_quote(self):
        engine=values.Pricing({999:(1964,34,'Decayed Damage Control Mutaplasmid')},{},{},valuation_mode='loss-day')
        engine.add_day(DAY,{'999':1000})
        self.assertEqual(engine.price(999,DAY,DAY),(Decimal(1000),False))

    def test_ccp_inputs_select_t2_then_true_t1_and_approved_ore_exception(self):
        rows=[(52227,60,7,'Abyssal Damage Control',15,1),(2048,60,7,'Damage Control II',2,2),
              (2,60,7,'Damage Control I',1,1),(47836,1156,7,'Medium Abyssal Ancillary Shield Booster',15,1),
              (32772,1156,7,'Medium Ancillary Shield Booster',None,1),(88,1156,7,'Faction Booster',4,1),
              (90498,464,7,'Abyssal Deep Core Strip Miner',15,1),(87562,464,7,'ORE Deep Core Strip Miner',4,1),
              (17482,464,7,'Strip Miner I',1,1)]
        maps=[[{'resultingType':52227,'applicableTypes':[2,2048]},
               {'resultingType':47836,'applicableTypes':[32772,88]},
               {'resultingType':90498,'applicableTypes':[87562]}]]
        self.assertEqual(policy.derive_equivalents(rows,maps),{52227:(2048,),47836:(32772,),90498:(17482,)})
        with self.assertRaises(ValueError):policy.derive_equivalents(rows,[])

    def test_absent_rankings_do_not_trigger_an_initial_build(self):
        conn=MagicMock();cursor=conn.cursor.return_value.__enter__.return_value;cursor.fetchone.return_value=(None,)
        run=Mock()
        self.assertEqual(orchestration.refresh_rankings(conn,run,Mock(),Mock()),'awaiting_initial_build')
        run.assert_not_called()


    def test_initialized_rankings_are_refreshed_after_value_changes(self):
        import threading
        conn=MagicMock();cursor=conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect=[('analytics.killmail_ranking_current_state',),(True,),(True,),(0,)]
        run=Mock()
        self.assertEqual(orchestration.refresh_rankings(conn,run,Mock(),threading.Event()),'updated')
        run.assert_called_once_with('rankings','build_killmail_rankings.py',['--incremental-only'])

    def test_manual_job_is_registered_and_start_requires_run_permission(self):
        import sys,tempfile
        from pathlib import Path
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from scripts import test_killmail_forensics as f
        config=sys.modules[f.PACKAGE+'.config']
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'jobs.json';path.write_text('{"jobs":[]}')
            with patch.object(config,'JOBS_CONFIG_PATH',path,create=True):
                jobs=f.load('jobs');routes=f.load('routes_admin_jobs')
            with patch.object(jobs,'JOBS_CONFIG_PATH',path):
                found=[r for r in jobs.load_jobs_config() if r['key']=='reprice_abyssal_killmails']
            self.assertEqual(len(found),1)
            self.assertTrue(found[0]['command'][1].endswith('/reprice_abyssal_killmails.py'))
        app=FastAPI();app.include_router(routes.router)
        user={'id':1,'username':'Test','permissions':{'admin.jobs.view'}}
        with TestClient(app) as client,patch.object(routes,'require_login',return_value=user), \
             patch.object(routes,'run_abyssal_repricing',return_value=(True,'started')) as run, \
             patch.object(routes,'abyssal_repricing_progress',return_value={'running':False,'progress':None}), \
             patch.object(routes,'audit_log'):
            self.assertEqual(client.get('/admin/jobs/abyssal-repricing/progress').status_code,200)
            client.post('/admin/jobs/abyssal-repricing/start',follow_redirects=False)
            run.assert_not_called()
            user['permissions'].add('admin.jobs.run')
            self.assertEqual(client.post('/admin/jobs/abyssal-repricing/start',follow_redirects=False).status_code,302)
            run.assert_called_once()


@unittest.skipUnless(os.environ.get('KILLMAIL_VALUES_TEST_DSN'),'Disposable valuation DB required')
class RepricingDatabaseTests(unittest.TestCase):
    setUp=fixture.DatabaseTests.setUp
    tearDown=fixture.DatabaseTests.tearDown

    def test_only_abyssal_kills_are_repriced_without_erasing_rows_or_other_values(self):
        from scripts import build_killmail_rankings as ranks
        with self.conn.cursor() as cur:
            cur.execute(ranks.SCHEMA.read_text())
            cur.execute("INSERT INTO rawkm.killmails VALUES(1,'2026-01-01 12:00Z',1,99999999999),(2,'2026-01-01 13:00Z',1,333),(3,'2026-01-01 14:00Z',1,NULL)")
            cur.execute("INSERT INTO rawkm.killmail_items VALUES(1,'2026-01-01 12:00Z',1,52227,11,1,2,0),(2,'2026-01-01 13:00Z',1,2048,11,1,0,0)")
        wanted=values.needed_prices(self.conn,DAY,'loss-day',(52227,))
        self.assertEqual(set(wanted[DAY]),{(DAY,1),(DAY,52227)})
        # Full fitted stack, not a subtraction from the formerly inflated total.
        prices=[(DAY,DAY,1,Decimal(100),False),(DAY,DAY,52227,Decimal(650000),False)]
        self.assertEqual(values.update_month(self.conn,DAY,prices,'loss-day',(52227,))[0],1)
        with self.conn.cursor() as cur:
            cur.execute('SELECT killmail_id,estimated_value_cents FROM rawkm.killmails ORDER BY killmail_id')
            self.assertEqual(cur.fetchall(),[(1,195010000),(2,333),(3,None)])
            cur.execute('SELECT count(*) FROM rawkm.killmail_items');self.assertEqual(cur.fetchone()[0],2)
            cur.execute('SELECT revision FROM analytics.killmail_ranking_versions');self.assertEqual(cur.fetchone()[0],1)
            cur.execute('DROP TABLE valuation_stage; DROP TABLE valuation_prices')
        self.assertEqual(values.update_month(self.conn,DAY,prices,'loss-day',(52227,))[0],0)
        with self.conn.cursor() as cur:
            cur.execute('SELECT revision FROM analytics.killmail_ranking_versions');self.assertEqual(cur.fetchone()[0],1)

    def test_unavailable_equivalent_price_preserves_existing_kill_value(self):
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO rawkm.killmails VALUES(1,'2026-01-01 12:00Z',1,12345)")
            cur.execute("INSERT INTO rawkm.killmail_items VALUES(1,'2026-01-01 12:00Z',1,52227,11,1,0,0)")
        self.assertEqual(values.update_month(self.conn,DAY,[(DAY,DAY,1,Decimal(100),False),(DAY,DAY,52227,None,True)],'loss-day',(52227,))[0],0)
        with self.conn.cursor() as cur:
            cur.execute('SELECT estimated_value_cents FROM rawkm.killmails');self.assertEqual(cur.fetchone()[0],12345)
        events=[]
        self.assertEqual(values.abyssal_target_months(self.conn,(52227,),lambda *args,**kw:events.append(kw)),[(DAY,DAY)])
        self.assertTrue(events)


if __name__=='__main__':unittest.main()
