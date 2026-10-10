"""Economics allocation, covered days, membership and API checks; never uses production DB."""
import unittest
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts import test_killmail_forensics as f

population = f.load('population_intelligence')
module = f.load('population_economics')
routes = f.load('routes_population_economics')
JAN, FEB, MAR = date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)


class CalculationTests(unittest.TestCase):
    def test_average_ownership_changes_regions_lost_npc_and_duplicate_coalition_scope(self):
        systems = {i: {'region_id': 1} for i in range(1, 101)}
        systems[101] = {'region_id': 2}
        systems[102] = {'region_id': 1}
        initial = [(i, 'GAIN', 10 if i <= 8 else 20, 1 if i <= 8 else 2) for i in range(1, 101)]
        initial += [(101, 'GAIN', 10, 1), (102, 'GAIN', None, None)]
        events = [(datetime(2026, 1, 16, tzinfo=UTC), 8, 'GAIN', 20, 2),
                  (datetime(2026, 1, 20, tzinfo=UTC), 101, 'LOST', None, None)]
        scope = lambda day: {('alliance', 10), ('corporation', 1)}
        share = module.daily_shares(initial, events, [JAN], systems, scope)[JAN]
        self.assertEqual(share[1]['owned_average'], Decimal(8*15+7*16)/31)
        self.assertEqual(share[1]['share'], Decimal(8*15+7*16)/3100)
        self.assertEqual(share[2]['share'], Decimal(1))
        self.assertEqual(share[2]['owned_average'], Decimal(19)/31)

    def test_coalition_membership_changes_mid_month(self):
        scope = lambda day: {('alliance', 10)} if day.day <= 15 else set()
        share = module.daily_shares([(1, 'GAIN', 10, 1)], [], [JAN], {1: {'region_id': 1}}, scope)[JAN]
        self.assertEqual(share[1]['share'], Decimal(15)/31)

    def test_economic_sums_and_missing_metric_are_not_zero(self):
        catalog = {'facts': {JAN: {1: {'mining_isk': Decimal(1000), 'production_isk': Decimal(2000)},
                                  2: {'mining_isk': Decimal(3000)}}},
                   'shares': {JAN: {1: {'share': Decimal('.08'), 'owned_average': Decimal(8), 'total_average': Decimal(100)},
                                    2: {'share': Decimal('.5'), 'owned_average': Decimal(1), 'total_average': Decimal(2)}}}}
        totals, details = module.estimates(catalog, [JAN], {})
        self.assertEqual(totals['mining_isk'], Decimal(1580))
        self.assertIsNone(totals['production_isk'])
        self.assertIsNone(totals['npc_bounties_isk'])
        self.assertEqual(len(details), 2)

    def test_population_average_covers_only_reported_days(self):
        official = [{'date': JAN, 'member_count': 100}, {'date': date(2026,1,16), 'member_count': 200},
                    {'date': MAR, 'member_count': 300}]
        dates = [r['date'] for r in official]
        self.assertEqual(module.average_population(official, dates, [JAN, MAR]), Decimal(100*15+200*16+300*31)/62)
        self.assertIsNone(module.average_population(official[1:], dates[1:], [JAN]))

    def test_activity_window_never_fills_missing_mer_month_or_future_days(self):
        self.assertEqual(module.activity_intervals([JAN, MAR], 90), [(JAN,FEB),(MAR,date(2026,4,1))])
        self.assertEqual(module.activity_intervals([JAN, MAR], 10), [(date(2026,3,22),date(2026,4,1))])

    def test_series_null_zero_denominators_and_mean_amounts(self):
        catalog = {'months': [FEB], 'rules': {}, 'facts': {}, 'shares': {}}
        @contextmanager
        def db():
            yield object()
        with patch.object(module,'_catalog',return_value=catalog), patch.object(module,'db',db), \
             patch.object(module,'_topology',return_value={'regions': {}}), \
             patch.object(module,'estimates',return_value=({'npc_bounties_isk': Decimal(280), 'mining_isk': None, 'production_isk': Decimal(560)}, [])), \
             patch.object(module,'_load_official_rows',return_value=([{'date':FEB,'member_count':100}],[FEB])), \
             patch.object(module,'_activity',return_value=(2,4,0)):
            result = module.get_series('alliance',10,'2026-02','2026-02')
        self.assertEqual(result['economic_days'],28)
        self.assertEqual(result['denominators']['member'],'100')
        self.assertEqual(result['rows'][0]['daily_average'],'10')
        self.assertEqual(result['rows'][0]['ratios']['active'],'140')
        self.assertIsNone(result['rows'][0]['ratios']['loss'])
        self.assertIsNone(result['rows'][1]['total'])
        self.assertEqual(result['activity_days'],28)

    def test_temporal_scope_counts_departed_pilots_deduplicates_kills(self):
        class Conn:
            def __init__(self): self.calls=[]
            def cursor(self): return self
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def execute(self,q,args=None): self.calls.append((q,args))
            def fetchone(self): return [101,102],[5,6],[7]
        conn=Conn()
        with patch.object(module,'scope_at',side_effect=lambda k,i,r,d:{('alliance',10 if d.day<=15 else 20)}):
            result=module._activity(conn,'coalition',1,{},[(JAN,FEB)])
        self.assertEqual(result,(2,2,1))
        queries=[(q,args) for q,args in conn.calls if args]
        self.assertEqual(len(queries),2)
        self.assertEqual(queries[0][1][0],[10])
        self.assertEqual(queries[1][1][0],[20])
        self.assertIn('ka.killmail_time >=',queries[0][0])
        self.assertIn('km.victim_character_id > 0',queries[0][0])


class RouteTests(unittest.TestCase):
    def setUp(self):
        app=FastAPI();app.include_router(routes.router);self.client=TestClient(app)

    def test_permission_and_invalid_entities(self):
        with patch.object(routes,'require_login',return_value={}), patch.object(routes,'require_permission_or_redirect',return_value=None), patch.object(routes,'get_options',return_value={'available':False,'months':[]}) as options:
            self.assertEqual(self.client.get('/api/alliance/1/population-economics/options').json()['available'],False)
            self.assertEqual(self.client.get('/api/character/1/population-economics/options').status_code,404)
            self.assertEqual(options.call_count,1)
        from fastapi.responses import RedirectResponse
        with patch.object(routes,'require_login',return_value={}), patch.object(routes,'require_permission_or_redirect',return_value=RedirectResponse('/denied')), patch.object(routes,'get_options') as options:
            self.assertEqual(self.client.get('/api/coalition/1/population-economics/options',follow_redirects=False).status_code,307)
            options.assert_not_called()


if __name__=='__main__':
    unittest.main()
