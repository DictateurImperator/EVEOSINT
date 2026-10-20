"""Offline ESI validation, access, replay and concurrent import tests."""
import copy
import logging
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from scripts import test_killmail_forensics as f

service=f.load('killmail_esi_import')
routes=f.load('routes_admin_killmail_import')
debug=f.load('routes_admin_debug')
URL='https://esi.evetech.net/latest/killmails/139240959/'+'a'*40+'/?datasource=tranquility'
PAYLOAD={'killmail_time':'2026-10-17T23:34:43Z','solar_system_id':30002537,
 'victim':{'character_id':94036773,'corporation_id':98370861,'ship_type_id':12019,'damage_taken':18199,
           'items':[{'item_type_id':34,'quantity_dropped':10,'flag':5,'singleton':0}]},
 'attackers':[{'character_id':1,'corporation_id':2,'damage_done':18199,'final_blow':True,'ship_type_id':12005}]}
STAMP=datetime(2026,10,17,23,34,43,tzinfo=timezone.utc)
def fetched():return dict(copy.deepcopy(PAYLOAD),killmail_id=139240959,killmail_hash='a'*40),STAMP

class ValidationTests(unittest.TestCase):
 def test_accepts_official_urls_and_rejects_other_targets(self):
  self.assertEqual(service.parse_link(URL),(139240959,'a'*40))
  self.assertEqual(service.parse_link(URL.replace('/latest/','/v1/')),(139240959,'a'*40))
  for value in [URL.replace('https:','http:'),URL.replace('esi.evetech.net','evil.invalid'),URL.replace('esi.evetech.net','esi.evetech.net@127.0.0.1'),URL.replace('esi.evetech.net','esi.evetech.net.evil.invalid'),URL+'&access_token=private',URL.replace('tranquility','singularity'),URL+'#fragment',URL.replace('a'*40,'bad')]:
   with self.subTest(value=value),self.assertRaises(service.ImportError):service.parse_link(value)
 def test_invalid_link_never_connects_or_fetches(self):
  with patch.object(service,'fetch_killmail') as fetch,patch.object(service,'db') as connect:
   with self.assertRaises(service.ImportError):service.import_link('https://127.0.0.1/private')
   fetch.assert_not_called();connect.assert_not_called()
 def test_fetch_pins_ccp_host_and_disables_redirects(self):
  with patch.object(service.requests,'Session') as session:
   response=session.return_value.__enter__.return_value.get.return_value.__enter__.return_value
   response.status_code=200;response.headers={};response.iter_content.return_value=[__import__('json').dumps(PAYLOAD).encode()]
   p,stamp=service.fetch_killmail(139240959,'a'*40)
   self.assertEqual(p['killmail_hash'],'a'*40);self.assertEqual(stamp,STAMP)
   args,kwargs=session.return_value.__enter__.return_value.get.call_args
   self.assertTrue(args[0].startswith('https://esi.evetech.net/latest/killmails/'));self.assertFalse(kwargs['allow_redirects'])
 def test_esi_errors_never_open_database(self):
  for error in [service.ImportError('404'),requests.Timeout()]:
   with patch.object(service,'fetch_killmail',side_effect=error),patch.object(service,'db') as connect:
    with self.assertRaises((service.ImportError,requests.Timeout)):service.import_link(URL)
    connect.assert_not_called()
 def test_importer_does_not_replace_web_logger_handlers(self):
  before=list(logging.getLogger().handlers);service.importer_module();self.assertEqual(logging.getLogger().handlers,before)

class RouteTests(unittest.TestCase):
 def setUp(self):
  app=FastAPI();app.include_router(routes.router);app.include_router(debug.router);self.client=TestClient(app)
  self.user={'id':1,'username':'admin','permissions':{'admin.jobs.view','admin.jobs.run'}}
 def test_post_requires_run_permission(self):
  for user,code in [(None,401),({'id':1,'permissions':{'admin.jobs.view'}},403)]:
   with patch.object(routes,'require_login',return_value=user),patch.object(routes,'import_link') as work:
    self.assertEqual(self.client.post('/admin/debug/killmail/import',data={'esi_url':URL}).status_code,code);work.assert_not_called()
 def test_get_tab_does_not_start_work_and_preserves_economics(self):
  with patch.object(debug,'require_login',return_value=self.user),patch.object(debug,'db') as connect,patch.object(routes,'import_link') as work:
   normal=self.client.get('/admin/debug').text;tab=self.client.get('/admin/debug?tab=killmail').text
   self.assertIn('economicForm',normal);self.assertIn('Import killmail',normal)
   self.assertIn('killmailImportForm',tab);self.assertNotIn('id="economicForm"',tab);self.assertIn('Indices économiques',tab)
   connect.assert_not_called();work.assert_not_called()
 def test_post_returns_import_result_or_visible_error(self):
  with patch.object(routes,'require_login',return_value=self.user),patch.object(routes,'audit_log'),patch.object(routes,'import_link',return_value={'status':'imported','killmail_id':139240959,'url':'/kill/139240959'}):
   self.assertEqual(self.client.post('/admin/debug/killmail/import',data={'esi_url':URL}).json()['status'],'imported')
  with patch.object(routes,'require_login',return_value=self.user),patch.object(routes,'import_link',side_effect=service.ImportError('Lien invalide.')):
   self.assertEqual(self.client.post('/admin/debug/killmail/import',data={'esi_url':URL}).status_code,422)

@unittest.skipUnless(os.environ.get('KILLMAIL_ESI_IMPORT_TEST_DSN'),'Requires disposable PostgreSQL')
class SQLTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.dsn=os.environ['KILLMAIL_ESI_IMPORT_TEST_DSN'];cfg=psycopg2.extensions.parse_dsn(cls.dsn)
  if not cfg.get('host','').startswith('/tmp/'):
   raise AssertionError('Only disposable /tmp PostgreSQL is permitted')
  cls.importer=service.importer_module()
 def setUp(self):
  with closing(psycopg2.connect(self.dsn)) as conn:
   with conn.cursor() as cur:cur.execute('DROP SCHEMA IF EXISTS rawkm CASCADE; DROP SCHEMA IF EXISTS analytics CASCADE')
   conn.commit();self.importer.ensure_tables(conn);self.importer.ensure_month_partitions(conn,STAMP.date().replace(day=1),STAMP.date().replace(day=1))
  self.connect=patch.object(service,'db',lambda:psycopg2.connect(self.dsn));self.connect.start()
  self.fetch=patch.object(service,'fetch_killmail',side_effect=lambda *_:fetched());self.fetch.start()
 def tearDown(self):self.fetch.stop();self.connect.stop()
 def scalar(self,query):
  with closing(psycopg2.connect(self.dsn)) as conn:
   with conn.cursor() as cur:cur.execute(query);return cur.fetchone()[0]
 def test_insert_replay_preserves_items_attackers_and_prices(self):
  self.assertEqual(service.import_link(URL)['status'],'imported')
  with closing(psycopg2.connect(self.dsn)) as conn:
   with conn.cursor() as cur:cur.execute('UPDATE rawkm.killmails SET estimated_value_cents=123456')
   conn.commit()
  self.assertEqual(service.import_link(URL)['status'],'existing')
  self.assertEqual(self.scalar('SELECT count(*) FROM rawkm.killmails'),1)
  self.assertEqual(self.scalar('SELECT count(*) FROM rawkm.killmail_attackers'),1)
  self.assertEqual(self.scalar('SELECT count(*) FROM rawkm.killmail_items'),1)
  self.assertEqual(self.scalar('SELECT estimated_value_cents FROM rawkm.killmails'),123456)
 def test_default_archive_import_still_refreshes_existing_items(self):
  original,_=fetched()
  with closing(psycopg2.connect(self.dsn)) as conn:
   self.importer.insert_killmail(conn,original);conn.commit()
   changed=copy.deepcopy(original);changed['victim']['items'][0]['quantity_dropped']=20
   self.importer.insert_killmail(conn,changed);conn.commit()
  self.assertEqual(self.scalar('SELECT quantity_dropped FROM rawkm.killmail_items'),20)
 def test_concurrent_import_is_one_insert(self):
  with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:service.import_link(URL)['status'],range(2)))
  self.assertEqual(sorted(results),['existing','imported']);self.assertEqual(self.scalar('SELECT count(*) FROM rawkm.killmail_items'),1)
 def test_import_failure_rolls_back_entire_killmail(self):
  with patch.object(self.importer,'insert_item_recursive',side_effect=RuntimeError('simulated failure')):
   with self.assertRaises(RuntimeError):service.import_link(URL)
  self.assertEqual(self.scalar('SELECT count(*) FROM rawkm.killmails'),0);self.assertEqual(self.scalar('SELECT count(*) FROM rawkm.killmail_attackers'),0)

if __name__=='__main__':unittest.main()
