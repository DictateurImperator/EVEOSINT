"""Collector protocol and access controls; no production configuration."""
import hashlib
import json
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
import unittest
from unittest.mock import patch
from scripts.test_combat_dev import store,routes,event as raw_event
ADMIN={'id':1,'username':'Tester','permissions':{'admin.jobs.view','admin.jobs.run'}}

def event(n=1,observer='Test Pilot',kind='shield_in'):
    return dict(raw_event(n,kind),observer=observer)
def body(events=None):
    return b'\n'.join(json.dumps(e).encode() for e in (events or [event()]))

class RouteTests(unittest.TestCase):
    def setUp(self):
        app=FastAPI();app.include_router(routes.router);self.client=TestClient(app)
    def test_admin_routes_require_permission(self):
        with patch.object(routes,'require_login',return_value=None),patch.object(routes,'require_permission_or_redirect',return_value=RedirectResponse('/login',302)):
            for path in ('/admin/debug/combat/events','/admin/debug/combat/killmails'):
                self.assertEqual(self.client.get(path,follow_redirects=False).status_code,302)
            self.assertEqual(self.client.post('/admin/debug/combat/recalculate',follow_redirects=False).status_code,302)
    def test_reader_distribution_and_login_routes_absent(self):
        for path in ('/admin/debug/combat/token','/admin/debug/combat/revoke','/admin/debug/combat/download','/admin/debug/combat/connect','/collector/combat-connect'):
            self.assertEqual(self.client.get(path).status_code,404)
            self.assertEqual(self.client.post(path).status_code,404)
    def test_active_owner_requires_enabled_admin(self):
        class Connection:
            def __init__(self,active):self.active=active
            def cursor(self):return self
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def execute(self,*args):pass
            def fetchone(self):return (self.active,)
            def close(self):pass
        for active,permissions,expected in ((True,{'admin.jobs.run'},1),(False,{'admin.jobs.run'},None),(True,set(),None)):
            with patch.object(store,'token_owner',return_value=1),patch.object(routes,'db',return_value=Connection(active)),patch.object(routes,'get_user_permissions',return_value=permissions):
                self.assertEqual(routes.active_collector('test'),expected)
    def test_reading_debug_does_not_scan(self):
        with patch.object(routes,'require_login',return_value=ADMIN),patch.object(routes,'require_permission_or_redirect',return_value=None),patch.object(routes.links,'refresh',side_effect=AssertionError('No scan on GET')),patch.object(store,'timeline',return_value=dict(events=[],streams=[],total=0,next='')):
            self.assertEqual(self.client.get('/admin/debug/combat/events').status_code,200)
    def test_invalid_summary_and_old_protocol(self):
        with self.assertRaises(ValueError):store.validate_summary({'version':1})
        with self.assertRaises(ValueError):store.validate_event({'version':1,'events':[]})
