"""Collector tests: disposable PostgreSQL only; never production configuration."""
import ast
import types
import hashlib
import json
import os
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import psycopg2
from fastapi import FastAPI
from fastapi.testclient import TestClient
from scripts import test_killmail_forensics as f

store=f.load('killmail_generator')
routes=f.load('routes_killmail_generator')
BODY={'version':1,'status':'pending_killmail','victim':{'pilot':'Test Victim'},'loss_time':'2026-10-17T23:34:44Z','window_seconds':1800,'partial':True,'logi':[{'pilot':'Test Logi','ship':'Scythe','shield':1092}]}
def raw(p=None):return json.dumps(p or BODY,separators=(',',':')).encode()

class ValidationTests(unittest.TestCase):
    def test_minimal_candidate_and_reject_unwanted_data(self):
        self.assertEqual(store.validate_report(raw())[0]['logi'][0]['shield'],1092)
        for changed in ({'window_seconds':3600},{'version':True},{'partial':'false'},{'source':'private path'},{'status':'confirmed'}, {'logi':[{'pilot':'X','ship':'Y','shield':-1}]},{'logi':[{'pilot':'X','ship':'Y','shield':True}]}):
            with self.subTest(changed=changed),self.assertRaises(ValueError):store.validate_report(raw(dict(BODY,**changed)))
    def test_collector_key_mutations_have_csrf_protection(self):
        tree=ast.parse((Path(__file__).resolve().parents[1]/'web/app/main.py').read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_requires_csrf_check')
        ns={'Request':object};exec(compile(ast.Module(body=[fn],type_ignores=[]),'<csrf helper>','exec'),ns)
        check=ns['_requires_csrf_check']
        for path in ('/account/combat-collector/token','/account/combat-collector/revoke'):
            self.assertTrue(check(types.SimpleNamespace(method='POST',url=types.SimpleNamespace(path=path))))
        self.assertFalse(check(types.SimpleNamespace(method='POST',url=types.SimpleNamespace(path='/api/combat-repairs'))))

    def test_missing_auth_never_connects(self):
        app=FastAPI();app.include_router(routes.router)
        with TestClient(app) as client,patch.object(store,'db',side_effect=AssertionError('production forbidden')):
            self.assertEqual(client.post('/api/combat-repairs',content=raw()).status_code,401)
            self.assertEqual(client.post('/api/combat-repairs',headers={'Authorization':'Bearer invalid'},content=raw()).status_code,401)

@unittest.skipUnless(os.environ.get('KILLMAIL_GENERATOR_TEST_DSN'),'disposable PostgreSQL DSN required')
class SQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn=os.environ['KILLMAIL_GENERATOR_TEST_DSN']
        # This variable is supplied only by the disposable-cluster launcher.
        with closing(psycopg2.connect(cls.dsn)) as conn:
            with conn.cursor() as c:
                c.execute('CREATE SCHEMA web; CREATE TABLE web.users(id bigint PRIMARY KEY,username text,is_active boolean NOT NULL DEFAULT TRUE); INSERT INTO web.users VALUES(1,\'test\',TRUE),(2,\'other\',TRUE)')
                sql=(Path(__file__).resolve().parents[1]/'web/app/killmail_generator_schema.sql').read_text()
                c.execute(sql);c.execute(sql)
            conn.commit()
    def setUp(self):
        self.patch=patch.object(store,'db',lambda:psycopg2.connect(self.dsn));self.patch.start()
        with closing(psycopg2.connect(self.dsn)) as conn:
            with conn.cursor() as c:c.execute('TRUNCATE "KillmailGenerator".collector_tokens,"KillmailGenerator".repair_reports; UPDATE web.users SET is_active=TRUE')
            conn.commit()
        self.token=store.rotate_token(1)
        app=FastAPI();app.include_router(routes.router);self.client=TestClient(app)
        self.body=raw();self.id=hashlib.sha256(self.body).hexdigest()
        self.headers={'Authorization':'Bearer '+self.token,'Idempotency-Key':self.id,'Content-Type':'application/json'}
    def tearDown(self):self.client.close();self.patch.stop()
    def test_real_post_commit_ack_idempotency_and_json_columns(self):
        for _ in range(2):
            r=self.client.post('/api/combat-repairs',content=self.body,headers=self.headers)
            self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json(),{'report_id':self.id,'sha256':self.id,'status':'stored'})
        # New connection proves the row was committed before HTTP acknowledgement.
        with closing(psycopg2.connect(self.dsn)) as conn:
            with conn.cursor() as c:
                c.execute('SELECT victim_pilot,window_seconds,logi,payload,validation_status FROM "KillmailGenerator".repair_reports')
                rows=c.fetchall();self.assertEqual(len(rows),1);self.assertEqual(rows[0][0],'Test Victim');self.assertEqual(rows[0][1],1800);self.assertEqual(rows[0][2],BODY['logi']);self.assertEqual(rows[0][3],BODY);self.assertEqual(rows[0][4],'pending_validation')
    def test_bad_hash_and_payload_never_acknowledged(self):
        self.assertEqual(self.client.post('/api/combat-repairs',content=self.body,headers=dict(self.headers,**{'Idempotency-Key':'0'*64})).status_code,422)
        malformed=raw(dict(BODY,window_seconds=3600));h=dict(self.headers,**{'Idempotency-Key':hashlib.sha256(malformed).hexdigest()})
        self.assertEqual(self.client.post('/api/combat-repairs',content=malformed,headers=h).status_code,422)
        self.assertEqual(self.client.post('/api/combat-repairs',content=b'x'*65537,headers=self.headers).status_code,413)
    def test_rotation_revocation_and_inactive_user(self):
        store.rotate_token(1);self.assertIsNone(store.authenticate(self.token))
        token=store.rotate_token(1);self.assertEqual(store.authenticate(token),1)
        store.revoke_token(1);self.assertIsNone(store.authenticate(token))
        token=store.rotate_token(1)
        with closing(psycopg2.connect(self.dsn)) as conn:
            with conn.cursor() as c:c.execute('UPDATE web.users SET is_active=FALSE WHERE id=1')
            conn.commit()
        self.assertIsNone(store.authenticate(token))
    def test_failure_at_commit_cannot_return_stored(self):
        with patch.object(store,'store_report',side_effect=psycopg2.OperationalError('simulated commit failure')):
            with self.assertRaises(psycopg2.OperationalError):self.client.post('/api/combat-repairs',content=self.body,headers=self.headers)
    def test_account_route_requires_login(self):
        with patch.object(routes,'require_login',return_value=None):
            self.assertEqual(self.client.post('/account/combat-collector/token',follow_redirects=False).status_code,302)

if __name__=='__main__':unittest.main()
