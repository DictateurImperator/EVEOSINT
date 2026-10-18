"""Offline fixed-basket maths, classification, access and disposable SQL checks."""
from datetime import date
from decimal import Decimal
from pathlib import Path
from contextlib import closing
import json
import os
import threading
import unittest
from unittest.mock import patch
import psycopg2
from fastapi import FastAPI
from fastapi.testclient import TestClient
from scripts import test_killmail_forensics as f

e=f.load('economic_indices');routes=f.load('routes_economic_indices')
DAY=date(2026,1,15);END=date(2026,2,28)


def item(id=1,**changes):
    return dict(id=id,name='Hull '+str(id),group=25,category=6,meta=1,race=1,faction=500001,group_name='Frigate',**changes)


class MathsTests(unittest.TestCase):
    def test_fixed_quantities_use_reference_cost_not_average_percentages(self):
        items={1:item(),2:item(2),3:item(3)}
        before={1:(Decimal(100),DAY),2:(Decimal(10),DAY)}
        after={1:(Decimal(140),END),2:(Decimal(20),END),3:(Decimal(999),END)}
        basket={'name':'Fleet','quantities':{1:Decimal(2),2:Decimal(1),3:Decimal(1)}}
        data=e.build_result([basket],{date(2025,12,31):{},DAY:before,END:after},DAY,END,items)['series'][0]
        self.assertEqual(data['base_cost'],'210');self.assertEqual(data['points'][1]['index'],100)
        self.assertAlmostEqual(data['points'][2]['index'],100*300/210)
        self.assertIsNone(data['points'][0]['index']);self.assertEqual(data['excluded'],[{'id':3,'name':'Hull 3'}])
        self.assertEqual(data['items'][2]['included'],False)

    def test_size_technology_and_triglavian_precedence(self):
        for group in (659,30):
            row=item();row['group']=group
            self.assertEqual(e.classify(row,[])[0],'Supercapital')
        for group in (513,902,4594,5120):
            row=item();row['group']=group
            self.assertEqual(e.classify(row,[])[0],'Capital')
        for group in (28,380,1202,463,543):
            row=item();row['group']=group
            self.assertEqual(e.classify(row,[])[0],'Subcapital')
        for group in (324,358,832,900,1534):
            row=item();row.update(group=group,race=135,meta=2)
            result=e.classify(row,[]);self.assertEqual(result[0],'Triglavian');self.assertEqual(result[2],'T2')
        self.assertEqual(e.classify(item(),['Navy Faction'])[2],'Navy')
        self.assertEqual(e.classify(item(),['Pirate Faction'])[2],'Pirate')
        for changed in ({'meta':15},{'name':'Abyssal Damage Control'},{'group':29}):
            row=item();row.update(changed);self.assertIsNone(e.classify(row,[]))
        self.assertIsNone(e.classify(item(),['Special Edition Ships']))

    def test_validation_dates_quantities_unknown_and_empty(self):
        available={'items':{1:item()},'baskets':{'a':{'label':'Frigates','ids':[1]}}}
        payload={'start':'2026-01-01','base':'2026-01-15','end':'2026-02-28','presets':['a'],'items':[{'id':1,'quantity':'2.5'}]}
        self.assertEqual(e.validate(payload,available)[3][1]['quantities'],{1:Decimal('2.5')})
        for changes in ({'base':'2025-01-01'},{'end':'2099-01-01'},{'presets':['missing']},{'items':[{'id':1,'quantity':'NaN'}]}, {'items':[{'id':1,'quantity':'-1'}]},{'items':[{'id':2,'quantity':'1'}]},{'presets':[],'items':[]}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):e.validate(dict(payload,**changes),available)


class RouteTests(unittest.TestCase):
    def setUp(self):
        app=FastAPI();app.include_router(routes.router);self.client=TestClient(app)

    def test_permission_and_private_status_never_start_queries(self):
        with patch.object(routes,'require_login',return_value={'id':1,'permissions':set()}),patch.object(routes,'db') as connect:
            for path in ('catalog','status/secret'):
                self.assertEqual(self.client.get('/admin/debug/economy/'+path,follow_redirects=False).status_code,302)
            self.assertEqual(self.client.post('/admin/debug/economy/calculate',json={},follow_redirects=False).status_code,302)
            connect.assert_not_called()
        with routes._LOCK:routes._RUNS['secret']={'owner':99,'status':'running','control':{},'updated':0}
        try:
            with patch.object(routes,'require_login',return_value={'id':1,'permissions':{'admin.jobs.view'}}),patch.object(routes,'db') as connect:
                self.assertEqual(self.client.get('/admin/debug/economy/status/secret').status_code,404)
                self.assertEqual(self.client.post('/admin/debug/economy/stop/secret').status_code,404)
                connect.assert_not_called()
        finally:routes._RUNS.clear()

    def test_stop_cancels_only_owned_connection(self):
        from unittest.mock import MagicMock
        connection=MagicMock();stop=threading.Event()
        routes._RUNS['owned']={'owner':1,'status':'running','control':{'stop':stop,'connection':connection},'updated':0}
        try:
            with patch.object(routes,'require_login',return_value={'id':1,'permissions':{'admin.jobs.view'}}):
                response=self.client.post('/admin/debug/economy/stop/owned')
                self.assertEqual(response.status_code,200);self.assertTrue(stop.is_set());connection.cancel.assert_called_once()
        finally:routes._RUNS.clear()

    def test_debug_template_has_no_automatic_calculation(self):
        page=f.templates.env.get_template('admin_debug_index.html').render(request=f.Request({'type':'http','method':'GET','path':'/admin/debug','headers':[],'scheme':'http','server':('test',80),'query_string':b''}),username='Admin',top_menu=[],context_menu=[],context_title='Admin',show_app_layout=True)
        self.assertIn('Reference date = 100%',page);self.assertIn('Save in this browser',page)
        self.assertIn("api('catalog')",page)
        self.assertIn("$('economicForm').onsubmit",page)
        self.assertIn('Abyssal modules are excluded',page)
        # Main application registration and CSRF protection cover the new POSTs.
        source=(f.ROOT/'web/app/main.py').read_text()
        self.assertIn('app.include_router(economic_indices_router)',source)


@unittest.skipUnless(os.environ.get('EVEOSINT_FORENSICS_TEST_SOCKET','').startswith('/tmp/'),'Disposable PostgreSQL socket required')
class SQLTests(unittest.TestCase):
    def connect(self):return psycopg2.connect(host=os.environ['EVEOSINT_FORENSICS_TEST_SOCKET'],port=55444,user='codex',dbname='postgres')

    def setUp(self):
        e._CACHE=None
        with closing(self.connect()) as conn,conn,conn.cursor() as cur:
            cur.execute('DROP SCHEMA IF EXISTS market CASCADE;CREATE SCHEMA market;DROP TABLE IF EXISTS public.sde_types,public.sde_groups,public.sde_marketgroups CASCADE')
            for table in ('sde_types','sde_groups','sde_marketgroups'):cur.execute(f'CREATE TABLE public.{table}(sde_key text PRIMARY KEY,data jsonb)')
            cur.execute("INSERT INTO public.sde_groups VALUES('25','{\"categoryID\":6,\"name\":{\"en\":\"Frigate\"}}')")
            cur.execute("INSERT INTO public.sde_marketgroups VALUES('1','{\"name\":{\"en\":\"Frigates\"}}')")
            for id in (1,2,3):
                cur.execute('INSERT INTO public.sde_types VALUES(%s,%s)',(str(id),json.dumps({'name':{'en':'Hull '+str(id)},'groupID':25,'published':True,'marketGroupID':1})))
            cur.execute((f.ROOT/'scripts/zkill_prices_schema.sql').read_text())
            for day,prices in [('2026-01-01',{'1':100,'2':10}),('2026-01-20',{'1':999}),('2026-02-28',{'1':140,'2':20,'3':5}),('2026-03-01',{'1':10000})]:
                cur.execute("INSERT INTO market.zkill_daily_prices(price_date,prices,item_count,status,source_url,source_sha256) VALUES(%s,%s,%s,'imported','test','test')",(day,json.dumps(prices),len(prices)))

    def tearDown(self):e._CACHE=None

    def test_real_sparse_history_carry_forward_exact_base_no_future_fill_no_writes(self):
        payload={'start':'2026-01-01','base':'2026-01-15','end':'2026-02-28','items':[{'id':1,'quantity':2},{'id':2,'quantity':1},{'id':3,'quantity':1}]}
        progress=[]
        with patch.object(e,'db',self.connect):data=e.calculate(payload,lambda **p:progress.append(p),{'stop':threading.Event()})
        series=data['series'][0]
        self.assertEqual(series['base_cost'],'210');self.assertEqual(series['points'][1]['index'],100)
        self.assertAlmostEqual(series['points'][-1]['index'],100*300/210)
        self.assertEqual(series['included'],2);self.assertEqual(series['items'][0]['base_quote_date'],'2026-01-01')
        self.assertEqual(series['excluded'],[{'id':3,'name':'Hull 3'}]);self.assertEqual(progress[-1]['checked'],2)
        with closing(self.connect()) as conn,conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM market.zkill_daily_prices');self.assertEqual(cur.fetchone()[0],4)

    def test_catalog_and_manual_start_completed_status_with_owner_access(self):
        app=FastAPI();app.include_router(routes.router);client=TestClient(app)
        user={'id':1,'permissions':{'admin.jobs.view'}}
        with patch.object(routes,'require_login',return_value=user),patch.object(routes,'db',self.connect),patch.object(e,'db',self.connect):
            payload={'start':'2026-01-01','base':'2026-01-15','end':'2026-02-28','items':[{'id':1,'quantity':2}]}
            catalog=client.get('/admin/debug/economy/catalog').json();self.assertEqual(len(catalog['items']),3)
            response=client.post('/admin/debug/economy/calculate',json=payload);self.assertEqual(response.status_code,202)
            import time
            for _ in range(100):
                state=client.get('/admin/debug/economy/status/'+response.json()['id']).json()
                if state['status']!='running':break
                time.sleep(.02)
            self.assertEqual(state['status'],'completed',state)
            self.assertEqual(state['result']['series'][0]['points'][-1]['index'],140)
        routes._RUNS.clear()


if __name__=='__main__':unittest.main()
