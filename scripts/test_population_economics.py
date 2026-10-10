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
    def setUp(self):
        module._ACTIVITY_CACHE.clear()

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
        result=result['months'][0]
        self.assertEqual(result['economic_days'],28)
        self.assertEqual(result['denominators']['member'],'100')
        self.assertNotIn('daily_average',result['rows'][0])
        self.assertEqual(result['rows'][0]['ratios']['active'],'140')
        self.assertNotIn('loss',result['rows'][0]['ratios'])
        self.assertNotIn('kill',result['rows'][0]['ratios'])
        self.assertIsNone(result['rows'][1]['total'])
        self.assertEqual(result['activity_days'],28)

    def test_months_are_separate_instead_of_summed(self):
        catalog={'months':[JAN,MAR], 'rules':{}, 'facts':{}, 'shares':{}}
        @contextmanager
        def db(): yield object()
        def estimates(catalog, months, regions):
            value=Decimal(100 if months[0]==JAN else 300)
            return {key:value for key in module.METRICS},[]
        with patch.object(module,'_catalog',return_value=catalog), patch.object(module,'db',db), \
             patch.object(module,'_topology',return_value={'regions':{}}),patch.object(module,'estimates',side_effect=estimates), \
             patch.object(module,'_load_official_rows',return_value=([{'date':JAN,'member_count':100}],[JAN])), \
             patch.object(module,'_activity',return_value=(2,4,1)):
            data=module.get_series('alliance',10,'2026-01','2026-03')
        self.assertEqual([r['month'] for r in data['months']],['2026-01','2026-03'])
        self.assertEqual([r['rows'][0]['total'] for r in data['months']],['100','300'])
        self.assertNotIn('rows',data)

    def test_temporal_scope_counts_departed_pilots_deduplicates_kills(self):
        class Conn:
            def __init__(self): self.calls=[]
            def cursor(self): return self
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def execute(self,q,args=None): self.calls.append((q,args))
            def fetchone(self):
                return ([101,102],2) if 'ARRAY_AGG' in self.calls[-1][0] else (1,)
        conn=Conn()
        with patch.object(module,'scope_at',side_effect=lambda k,i,r,d:{('alliance',10 if d.day<=15 else 20)}):
            result=module._activity(conn,'coalition',1,{},[(JAN,FEB)])
        self.assertEqual(result,(2,4,0))
        queries=[(q,args) for q,args in conn.calls if args]
        self.assertEqual(len(queries),2)
        self.assertEqual(queries[0][1][4],[10])
        self.assertEqual(queries[1][1][4],[20])
        self.assertIn('ka.killmail_time >=',queries[0][0])
        self.assertIn('km.victim_character_id > 0',queries[0][0])
        count=len(conn.calls)
        with patch.object(module,'scope_at',side_effect=lambda k,i,r,d:{('alliance',10 if d.day<=15 else 20)}):
            self.assertEqual(module._activity(conn,'coalition',1,{},[(JAN,FEB)]),result)
        self.assertEqual(len(conn.calls),count)


class EvolutionTests(unittest.TestCase):
    def test_monthly_values_missing_month_and_mean_population(self):
        catalog={'months':[JAN,MAR],'rules':{}}
        @contextmanager
        def db():yield object()
        with patch.object(module,'_catalog',return_value=catalog), patch.object(module,'db',db), \
             patch.object(module,'estimates',return_value=({key:Decimal(3100) for key in module.METRICS}, [])), \
             patch.object(module,'_load_official_rows',return_value=([{'date':JAN,'member_count':100}],[JAN])), \
             patch.object(module,'_activity') as pvp:
            data=module.get_evolution('alliance',10,'2026-01','2026-03','member')
        self.assertEqual([p['month'] for p in data['points']],['2026-01','2026-02','2026-03'])
        self.assertEqual(data['points'][0]['values']['mining_isk'],'31')
        self.assertIsNone(data['points'][1]['values']['mining_isk'])
        pvp.assert_not_called()

    def test_pvp_rolling_window_uses_original_range_across_batches_and_null_denominator(self):
        catalog={'months':[JAN,MAR],'rules':{}}
        @contextmanager
        def db():yield object()
        with patch.object(module,'_catalog',return_value=catalog), patch.object(module,'db',db), \
             patch.object(module,'estimates',return_value=({key:Decimal(3100) for key in module.METRICS}, [])), \
             patch.object(module,'_activity',return_value=(0,1,2)) as pvp:
            data=module.get_evolution('alliance',10,'2026-01','2026-03','active',90,2)
        self.assertEqual(data['points'][0]['month'],'2026-03')
        self.assertEqual(pvp.call_args.args[-1],[(JAN,FEB),(MAR,date(2026,4,1))])
        self.assertIsNone(data['points'][0]['values']['mining_isk'])
        self.assertIsNone(data['next_offset'])

    def test_month_comparison_is_independent_of_display_range(self):
        catalog={'months':[JAN,MAR],'rules':{}}
        @contextmanager
        def db():yield object()
        with patch.object(module,'_catalog',return_value=catalog), patch.object(module,'db',db), \
             patch.object(module,'estimates',return_value=({key:Decimal(100) for key in module.METRICS},[])), \
             patch.object(module,'_activity',return_value=(1,1,1)) as pvp:
            module.get_evolution('alliance',10,'2026-03','2026-03','active',90)
        self.assertEqual(pvp.call_args.args[-1],[(JAN,FEB),(MAR,date(2026,4,1))])

    def test_expensive_ratios_return_one_month_immediately(self):
        catalog={'months':[JAN,FEB,MAR],'rules':{}}
        @contextmanager
        def db():yield object()
        with patch.object(module,'_catalog',return_value=catalog), patch.object(module,'db',db), \
             patch.object(module,'estimates',return_value=({key:Decimal(100) for key in module.METRICS},[])), \
             patch.object(module,'_activity',return_value=(2,4,1)) as pvp:
            data=module.get_evolution('alliance',10,'2026-01','2026-03','active',90)
        self.assertEqual(len(data['points']),1)
        self.assertEqual(data['next_offset'],1)
        self.assertEqual(data['points'][0]['values']['mining_isk'],'50')
        self.assertEqual(pvp.call_count,1)

    def test_invalid_basis_does_not_query_database(self):
        with patch.object(module,'_catalog') as catalog:
            with self.assertRaises(ValueError):module.get_evolution('alliance',10,'2026-01','2026-03','bad')
            catalog.assert_not_called()


class ComparisonTests(unittest.TestCase):
    def test_changes_preserve_decimal_precision_and_sign(self):
        data=module.change('3100123456789.123456','3000000000000')
        self.assertEqual(data['delta'],'100123456789.123456')
        self.assertEqual(data['direction'],'increase')
        self.assertEqual(module.change('50','100')['percent'],'-50.0')
        self.assertEqual(module.change('0','100')['direction'],'decrease')
        self.assertEqual(module.change('100','100')['percent'],'0')

    def test_missing_or_zero_baseline_has_no_infinite_percentage(self):
        self.assertIsNone(module.change('100',None)['delta'])
        self.assertIsNone(module.change(None,'100')['percent'])
        self.assertEqual(module.change('100','0')['delta'],'100')
        self.assertIsNone(module.change('100','0')['percent'])

    def test_comparison_contains_one_row_per_indicator_and_separate_months(self):
        def series(kind,eid,start,end,window):
            amount='200' if start=='2026-03' else '100'
            measurement={'month':start,'rows':[{'metric':key,'total':amount,'ratios':{'member':amount}} for key in module.METRICS],
                         'denominators':{'member':amount}}
            return {'months':[measurement]}
        with patch.object(module,'get_series',side_effect=series) as load,patch.object(module,'get_series_regions',return_value=[]):
            data=module.get_comparison('alliance',10,'2026-01','2026-03')
        self.assertEqual(len(data['rows']),3)
        self.assertEqual(data['rows'][0]['values']['total']['value'],'200')
        self.assertEqual(data['rows'][0]['values']['total']['delta'],'100')
        self.assertEqual(data['rows'][0]['values']['total']['percent'],'100')
        self.assertEqual([call.args[2:4] for call in load.call_args_list],[('2026-01','2026-01'),('2026-03','2026-03')])

    def test_unknown_baseline_keeps_observed_values(self):
        with patch.object(module,'get_series',side_effect=[{'months':[]},{'months':[{'rows':[{'metric':'mining_isk','total':'10','ratios':{}}],'denominators':{}}]}]), \
             patch.object(module,'get_series_regions',return_value=[]):
            data=module.get_comparison('alliance',10,'2026-01','2026-03')
        mining=next(row for row in data['rows'] if row['metric']=='mining_isk')
        self.assertEqual(mining['values']['total']['value'],'10')
        self.assertIsNone(mining['values']['total']['delta'])

    def test_daily_chart_basis_is_removed(self):
        with self.assertRaises(ValueError):module.get_evolution('alliance',10,'2026-01','2026-03','daily')


class PurchasingPowerTests(unittest.TestCase):
    def test_factor_direction_and_missing_indices(self):
        levels={JAN:Decimal(100),MAR:Decimal(110)}
        self.assertEqual(module._power(levels,JAN,JAN),Decimal(1))
        self.assertEqual(module._power(levels,JAN,MAR),Decimal(100)/110)
        self.assertIsNone(module._power(levels,JAN,FEB))
        self.assertIsNone(module._power({JAN:Decimal(0),MAR:Decimal(100)},JAN,MAR))

    def test_ppa_is_added_without_changing_nominal_indicators(self):
        def series(kind,eid,start,end,window):
            return {'months':[{'month':start,'rows':[{'metric':key,'total':'100','ratios':{'member':'10','active':'20'}} for key in module.METRICS],
                               'denominators':{'member':10,'active':5}}]}
        with patch.object(module,'get_series',side_effect=series),patch.object(module,'get_series_regions',return_value=[]), \
             patch.object(module,'_cpi_levels',return_value={JAN:Decimal(100),MAR:Decimal(110)}):
            data=module.get_comparison('alliance',10,'2026-01','2026-03',90,'2026-01')
        self.assertEqual(len(data['rows']),6)
        self.assertEqual([r['metric'] for r in data['rows']],['npc_bounties_isk','npc_bounties_ppa_isk','mining_isk','mining_ppa_isk','production_isk','production_ppa_isk'])
        nominal=data['rows'][0]['values']['total'];ppa=data['rows'][1]['values']['total']
        self.assertEqual(nominal['value'],'100')
        self.assertEqual(nominal['percent'],'0')
        self.assertLess(Decimal(ppa['value']),Decimal(100))
        self.assertEqual(ppa['direction'],'decrease')
        self.assertAlmostEqual(float(ppa['percent']),-9.09090909)
        self.assertAlmostEqual(float(data['purchasing_power']['price_index']['value']),110)
        self.assertEqual(data['purchasing_power']['reference'],'2026-01')
        self.assertNotIn('loss',data['denominators'])
        self.assertNotIn('kill',data['rows'][0]['values'])

    def test_chart_ppa_uses_the_same_reference_and_keeps_raw_series(self):
        @contextmanager
        def db():yield object()
        with patch.object(module,'_catalog',return_value={'months':[JAN,MAR],'rules':{}}),patch.object(module,'db',db), \
             patch.object(module,'estimates',return_value=({key:Decimal(100) for key in module.METRICS},[])), \
             patch.object(module,'_cpi_levels',return_value={JAN:Decimal(100),MAR:Decimal(200)}):
            data=module.get_evolution('alliance',10,'2026-01','2026-03',reference='2026-01')
        self.assertEqual(data['points'][0]['values']['mining_isk'],'100')
        self.assertEqual(data['points'][0]['values']['mining_ppa_isk'],'100')
        self.assertEqual(data['points'][2]['values']['mining_isk'],'100')
        self.assertEqual(data['points'][2]['values']['mining_ppa_isk'],'50.0')
        self.assertIsNone(data['points'][1]['values']['mining_ppa_isk'])

    def test_removed_bases_are_rejected(self):
        for basis in ['loss','kill']:
            with self.assertRaises(ValueError):module.get_evolution('alliance',10,'2026-01','2026-03',basis)


class RankingTests(unittest.TestCase):
    def test_ranking_keeps_decimal_precision_omits_missing_and_breaks_ties_by_month(self):
        series={'points':[{'month':'2026-04','values':{'mining_ppa_isk':'10000000000000000.01'}},
                          {'month':'2026-03','values':{'mining_ppa_isk':'10000000000000000.02'}},
                          {'month':'2026-02','values':{'mining_ppa_isk':'10000000000000000.02'}},
                          {'month':'2026-01','values':{'mining_ppa_isk':None}}], 'total_months':4,'next_offset':None}
        with patch.object(module,'_catalog',return_value={'months':[JAN,MAR]}),patch.object(module,'get_evolution',return_value=series):
            result=module.get_best_month('alliance',10,'mining_ppa_isk','total','2026-03')
        self.assertEqual(result['best']['month'],'2026-02')
        self.assertEqual([r['month'] for r in result['ranked_months']],['2026-02','2026-03','2026-04'])
        self.assertEqual(result['available'],3)
        self.assertEqual(result['checked'],4)

    def test_only_supported_ppa_indicators_are_ranked(self):
        with patch.object(module,'_catalog') as catalog:
            with self.assertRaises(ValueError):module.get_best_month('alliance',10,'mining_isk','total','2026-03')
            with self.assertRaises(ValueError):module.get_best_month('alliance',10,'mining_ppa_isk','kill','2026-03')
            catalog.assert_not_called()


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
