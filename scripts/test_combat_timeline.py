"""Offline admin telemetry tests; no production configuration or game logs."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from scripts import test_killmail_forensics as f

store=f.load('combat_timeline')
routes=f.load('routes_combat_timeline')
ADMIN={'id':1,'username':'Tester','permissions':{'admin.jobs.view','admin.jobs.run'}}


def event(n=1, observer='Test Pilot', kind='shield_in'):
    return {'event_id':hashlib.sha256(str(n).encode()).hexdigest(),'occurred_at':'2026-10-20T19:22:13Z',
            'observer':observer,'kind':kind,'amount':488,'source':{'name':'Test Logi','ship':'Scythe'},'target':{'name':observer},'weapon':'Test Remote Shield Booster'}


def body(events=None):
    return json.dumps({'version':1,'events':events or [event()]},separators=(',',':')).encode()


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        p=patch.object(store,'ROOT',Path(self.tmp.name));p.start();self.addCleanup(p.stop)

    def test_token_hash_rotation_and_revocation(self):
        key=store.rotate_token(1);self.assertEqual(store.token_owner(key),1)
        with store.connection() as conn:
            self.assertNotEqual(conn.execute('SELECT digest FROM tokens').fetchone()[0],key)
        other=store.rotate_token(1);self.assertIsNone(store.token_owner(key));self.assertEqual(store.token_owner(other),1)
        store.revoke_token(1);self.assertIsNone(store.token_owner(other))
        self.assertIsNone(store.token_owner('invalid'))

    def test_ack_after_commit_retry_is_idempotent_and_persists(self):
        raw=body();batch=hashlib.sha256(raw).hexdigest()
        self.assertEqual(store.receive(1,raw,batch)['inserted'],1)
        self.assertEqual(store.receive(1,raw,batch)['inserted'],0)
        store._ready.clear()
        result=store.timeline();self.assertEqual(result['total'],1);self.assertEqual(len(result['events']),1)
        self.assertEqual(result['streams'][0]['events'],1)

    def test_different_observers_and_close_loss_signals_are_preserved(self):
        events=[event(1,'Receiver'),event(2,'Logi','shield_out'),event(3,'Receiver','loss_signal'),event(4,'Receiver','loss_signal')]
        raw=body(events);store.receive(1,raw,hashlib.sha256(raw).hexdigest())
        self.assertEqual(store.timeline()['total'],4)
        self.assertEqual(len(store.timeline(observer='Receiver',kind='loss_signal')['events']),2)

    def test_conflicting_duplicate_rolls_back_whole_batch(self):
        raw=body();store.receive(1,raw,hashlib.sha256(raw).hexdigest())
        altered=event();altered['amount']=999;raw=body([event(2),altered])
        with self.assertRaises(store.EventConflict):store.receive(1,raw,hashlib.sha256(raw).hexdigest())
        self.assertEqual(store.timeline()['total'],1)

    def test_equal_timestamps_cursor_and_filters_do_not_skip(self):
        raw=body([event(i, 'A' if i%2 else 'B') for i in range(120)])
        store.receive(1,raw,hashlib.sha256(raw).hexdigest())
        first=store.timeline(limit=50);second=store.timeline(before=first['next'],limit=50);third=store.timeline(before=second['next'],limit=50)
        ids=[e['event_id'] for p in [first,second,third] for e in p['events']]
        self.assertEqual(len(ids),120);self.assertEqual(len(set(ids)),120);self.assertFalse(third['next'])
        self.assertEqual(len(store.timeline(observer='A')['events']),60)
        self.assertFalse(store.timeline(date_from='2026-10-21T00:00:00Z')['events'])

    def test_unexpected_private_fields_types_and_oversized_requests_rejected(self):
        for changes in [{'kind':[]},{'kind':'chat'},{'path':'private/file'},{'amount':True},{'observer':''},{'source':{'password':'secret'}},{'amount':10**15},{'occurred_at':'9999-01-01T00:00:00Z'}]:
            raw=body([dict(event(),**changes)])
            with self.subTest(changes=changes),self.assertRaises(ValueError):store.validate(raw)
        with self.assertRaises(ValueError):store.receive(1,body(),'0'*64)
        with self.assertRaises(ValueError):store.receive(1,b'x'*(store.MAX_BODY+1),'0'*64)

    def test_concurrent_retry_inserts_once(self):
        raw=body();batch=hashlib.sha256(raw).hexdigest()
        with ThreadPoolExecutor(max_workers=4) as pool:
            acks=list(pool.map(lambda _:store.receive(1,raw,batch),range(4)))
        self.assertEqual(sum(x['inserted'] for x in acks),1)
        self.assertEqual(store.timeline()['total'],1)


class RouteTests(StorageTests):
    def setUp(self):
        super().setUp();self.app=FastAPI();self.app.include_router(routes.router);self.client=TestClient(self.app);self.addCleanup(self.client.close)

    def test_anonymous_and_non_admin_cannot_read_or_create_keys(self):
        with patch.object(routes,'require_login',return_value=None),patch.object(routes,'require_permission_or_redirect',return_value=RedirectResponse('/login',302)):
            self.assertEqual(self.client.get('/admin/debug/combat/events',follow_redirects=False).status_code,302)
            self.assertEqual(self.client.post('/admin/debug/combat/token',follow_redirects=False).status_code,404)
        with patch.object(routes,'require_login',return_value={'id':2,'permissions':set()}),patch.object(routes,'require_permission_or_redirect',return_value=RedirectResponse('/forbidden',302)):
            self.assertEqual(self.client.get('/admin/debug/combat/events',follow_redirects=False).status_code,302)
        self.assertEqual(self.client.post('/collector/combat-events',content=body()).status_code,401)

    def test_intake_checks_current_account_state_and_permissions(self):
        class Connection:
            def __init__(self, active): self.active=active; self.closed=False
            def cursor(self): return self
            def __enter__(self): return self
            def __exit__(self,*args): return False
            def execute(self,*args): pass
            def fetchone(self): return (self.active,)
            def close(self): self.closed=True
        for active, permissions, expected in [(True, {'admin.jobs.run'}, 1), (True, set(), None), (False, {'admin.jobs.run'}, None)]:
            conn=Connection(active)
            with patch.object(store,'token_owner',return_value=1), patch.object(routes,'db',return_value=conn), patch.object(routes,'get_user_permissions',return_value=permissions):
                self.assertEqual(routes.active_collector('test'),expected)
                self.assertTrue(conn.closed)

    def test_ingest_requires_private_token_and_preserves_timeline(self):
        with patch.object(routes,'require_login',return_value=ADMIN),patch.object(routes,'require_permission_or_redirect',return_value=None):
            key=store.rotate_token(1)
            raw=body();headers={'Authorization':'Bearer '+key,'Idempotency-Key':hashlib.sha256(raw).hexdigest()}
            with patch.object(routes,'active_collector',side_effect=store.token_owner):
                response=self.client.post('/collector/combat-events',content=raw,headers=headers)
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.json()['accepted'],1)
                self.assertEqual(self.client.post('/collector/combat-events',content=raw,headers=headers).json()['inserted'],0)
            self.assertEqual(len(self.client.get('/admin/debug/combat/events').json()['events']),1)
            self.assertEqual(self.client.get('/admin/debug/combat/events?kind=unknown').status_code,400)


    def test_reader_login_distribution_and_token_management_routes_absent(self):
        for path in ['/admin/debug/combat/token','/admin/debug/combat/revoke','/admin/debug/combat/download','/admin/debug/combat/connect','/collector/combat-connect']:
            self.assertEqual(self.client.get(path).status_code,404)
            self.assertEqual(self.client.post(path).status_code,404)
