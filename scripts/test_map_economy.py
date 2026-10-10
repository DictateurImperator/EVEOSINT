"""Economic heatmap arithmetic, read-only queries and permission checks."""
import os
import unittest
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts import test_killmail_forensics as f

module = f.load('map_economy')
routes = f.load('routes_map_economy')
REGIONS = {10000001: {'name': 'Derelik'}, 10000002: {'name': 'The Forge'}}
JAN, FEB = date(2026, 1, 1), date(2026, 2, 1)


def row(day, metric, value, region=10000001, kind='region', name='Derelik'):
    return day, region, kind, name, metric, Decimal(value)


class ArithmeticTests(unittest.TestCase):
    def test_period_ratio_is_ratio_of_sums_and_combined_is_sum_of_indicators(self):
        rows = [row(JAN, 'production_isk', '100'), row(JAN, 'mining_isk', '100'), row(JAN, 'trade_isk', '20'),
                row(FEB, 'production_isk', '900'), row(FEB, 'mining_isk', '0'), row(FEB, 'trade_isk', '30')]
        frames, missing = module.build_frames(rows, REGIONS, [JAN, FEB], ['mining_isk', 'trade_isk'])
        entry = frames[0]['regions'][0]
        self.assertEqual(entry['production'], '1000')
        self.assertEqual(entry['metrics'][0]['ratio'], .1)
        self.assertEqual(entry['combined']['ratio'], .15)
        self.assertEqual(entry['combined']['value'], '150')
        self.assertEqual(missing, [])

    def test_monthly_animation_keeps_dates_and_zero_distinct_from_missing(self):
        rows = [row(JAN, 'production_isk', '100'), row(JAN, 'mining_isk', '0'), row(FEB, 'production_isk', '100')]
        frames, _ = module.build_frames(rows, REGIONS, [JAN, FEB], ['mining_isk'], True)
        self.assertEqual([frame['month'] for frame in frames], ['2026-01', '2026-02'])
        self.assertEqual(frames[0]['regions'][0]['metrics'][0]['ratio'], 0)
        self.assertIsNone(frames[1]['regions'][0]['metrics'][0]['ratio'])
        self.assertIsNone(frames[1]['regions'][0]['combined']['value'])

    def test_missing_month_does_not_produce_a_partial_period_total(self):
        frames, _ = module.build_frames([row(JAN, 'production_isk', '10'), row(JAN, 'mining_isk', '5')], REGIONS, [JAN, FEB], ['mining_isk'])
        entry = frames[0]['regions'][0]
        self.assertIsNone(entry['production'])
        self.assertIsNone(entry['metrics'][0]['value'])
        self.assertIn('1/2 months', entry['metrics'][0]['missing_reason'])

    def test_zero_or_missing_production_still_allows_absolute_value(self):
        for rows in ([row(JAN, 'production_isk', '0'), row(JAN, 'mining_isk', '25')], [row(JAN, 'mining_isk', '25')]):
            frames, _ = module.build_frames(rows, REGIONS, [JAN], ['mining_isk'])
            metric = frames[0]['regions'][0]['metrics'][0]
            self.assertEqual(metric['value'], '25')
            self.assertIsNone(metric['ratio'])
            self.assertIsNotNone(metric['missing_reason'])

    def test_unresolved_region_names_resolve_but_wormhole_aggregates_do_not(self):
        frames, missing = module.build_frames([row(JAN, 'production_isk', '10', None, 'region_name', 'derelik'),
                                               row(JAN, 'mining_isk', '10', None, 'space_group', 'Wormhole'),
                                               row(JAN, 'mining_isk', '10', None, 'region_name', 'Future Region')],
                                              REGIONS, [JAN], ['production_isk'])
        self.assertEqual(frames[0]['regions'][0]['region_id'], 10000001)
        self.assertEqual(missing, ['Future Region', 'Wormhole'])

    def test_invalid_periods_and_metrics_are_rejected_before_any_database_query(self):
        for value in ('2026-13', '2026-1', '2026-01-01'):
            with self.assertRaises(module.EconomyMapError):
                module.month(value)
        self.assertEqual(module.month_range(date(2025,12,1), FEB), [date(2025,12,1), JAN, FEB])
        for start,end in ((FEB,JAN),(JAN,date(2046,1,1))):
            with self.assertRaises(module.EconomyMapError):
                module.month_range(start,end)
        with self.assertRaises(module.EconomyMapError):
            module.get_economy_series('2026-01', '2026-02', ['anything; DROP TABLE'])

    def test_api_permissions_and_passive_data_loading(self):
        app = FastAPI(); app.include_router(routes.router)
        client = TestClient(app)
        user = {'id': 1, 'username': 'reader', 'permissions': {'entities.view'}}
        with patch.object(routes, 'require_login', return_value=user), patch.object(routes, 'get_economy_options', return_value={'months': ['2026-01']}) as options, patch.object(routes, 'get_economy_series', return_value={'frames': []}) as series:
            self.assertEqual(client.get('/api/map/eve-2d/economy/options').json()['months'], ['2026-01'])
            response = client.get('/api/map/eve-2d/economy?from=2026-01&to=2026-02&metric=mining_isk&metric=trade_isk&evolution=true')
            self.assertEqual(response.headers['cache-control'], 'no-store')
            series.assert_called_once_with('2026-01','2026-02',['mining_isk','trade_isk'],True)
            user['permissions'] = set(); options.reset_mock(); series.reset_mock()
            self.assertEqual(client.get('/api/map/eve-2d/economy/options',follow_redirects=False).status_code,302)
            self.assertEqual(client.get('/api/map/eve-2d/economy?from=2026-01&to=2026-02',follow_redirects=False).status_code,302)
            options.assert_not_called(); series.assert_not_called()


@unittest.skipUnless(os.environ.get('EVEOSINT_FORENSICS_TEST_SOCKET'), 'Explicit local PostgreSQL test socket required')
class QueryTests(unittest.TestCase):
    def test_queries_use_regional_totals_without_adding_overlapping_datasets(self):
        import psycopg2

        from scripts.import_mer_economy import SCHEMA
        conn = psycopg2.connect(dbname='postgres', user='codex', host=os.environ['EVEOSINT_FORENSICS_TEST_SOCKET'], port=55444)
        try:
            with conn.cursor() as cur:
                cur.execute(SCHEMA.read_text())
                cur.execute('TRUNCATE mer.region_economy_monthly')
                for dataset, metric, value in [('regional','mining_isk',10),('regional','production_isk',100),('mining_region','mining_isk',500),('moon_region','moon_mining_isk',300)]:
                    cur.execute("""INSERT INTO mer.region_economy_monthly(period_start,scope_kind,scope_key,scope_name,region_id,dataset,metric,value,unit,source_month,source_member)
                                   VALUES (%s,'region','10000001','Derelik',10000001,%s,%s,%s,'ISK',%s,'test.csv')""", (JAN,dataset,metric,value,JAN))
            conn.commit()
            @contextmanager
            def connection():
                yield conn
            conn.set_session(readonly=True)
            with patch.object(module,'db',connection), patch.object(module,'_topology', return_value={'regions':REGIONS}):
                self.assertEqual(module.get_economy_options()['months'], ['2026-01'])
                result = module.get_economy_series('2026-01','2026-01',['mining_isk'])
                self.assertEqual(result['frames'][0]['regions'][0]['metrics'][0]['value'],'10')
                self.assertEqual(result['frames'][0]['regions'][0]['metrics'][0]['ratio'],.1)
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
