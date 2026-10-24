"""Traffic accounting and admin access tests; never connect to production."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.testclient import TestClient
from scripts import test_killmail_forensics as f

traffic = f.load('public_traffic')
routes = f.load('routes_admin_traffic')
ADMIN = dict(id=1,username='Admin',permissions={'admin.system.view'})


class TrafficTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = patch.object(traffic,'ROOT',Path(self.tmp.name));self.root.start()
        app = FastAPI()
        app.middleware('http')(traffic.traffic_middleware)
        app.include_router(routes.router)
        @app.get('/public/character/{id}')
        def page(id: int):
            return HTMLResponse('<h1>Public</h1>')
        @app.get('/public/read')
        def read():
            return JSONResponse({'answer':1})
        @app.get('/public/broken')
        def broken():
            return HTMLResponse('error',status_code=500)
        @app.get('/private')
        def private():
            return HTMLResponse('Private')
        self.client = TestClient(app)

    def tearDown(self):
        self.root.stop();self.tmp.cleanup()

    def test_daily_and_per_page_uniques_and_requests_are_distinct(self):
        self.client.get('/public/character/1')
        self.client.get('/public/character/1')
        self.client.get('/public/character/2')
        self.client.get('/public/read?source=/character/1&path=/api/example')
        self.client.get('/public/broken')
        self.client.get('/private')
        s=traffic.snapshot()
        self.assertEqual(s['summary']['unique_visitors'],1)
        self.assertEqual(s['summary']['views'],3)
        self.assertEqual(s['summary']['requests'],5)
        self.assertEqual(s['summary']['errors'],1)
        self.assertEqual([(p['views'],p['unique_visitors']) for p in s['pages']],[(2,1),(1,1)])
        self.client.cookies.clear();self.client.get('/public/character/1')
        self.assertEqual(traffic.snapshot()['summary']['unique_visitors'],2)
        self.assertEqual(sum(row['kind']=='data' for row in s['recent']),1)

    def test_separate_utc_days_and_retention(self):
        now=time.time();yesterday=now-86400
        traffic.record('/public/character/1','page',200,5,'a'*32,now=yesterday)
        traffic.record('/public/character/1','page',200,8,'a'*32,now=now)
        self.assertEqual(traffic.snapshot()['summary']['unique_visitors'],1)
        day=datetime.fromtimestamp(yesterday,timezone.utc).strftime('%Y-%m-%d')
        self.assertEqual(traffic.snapshot(day)['summary']['unique_visitors'],1)
        self.assertEqual(len(traffic.snapshot()['days']),2)
        traffic.record('/public/old','page',200,5,'a'*32,now=now-40*86400)
        # Force periodic cleanup on the next current request.
        with traffic.connection() as c:c.execute("DELETE FROM metadata WHERE key='cleaned'")
        traffic.record('/public/character/1','page',200,5,'a'*32,now=now)
        self.assertEqual(len(traffic.snapshot()['days']),2)

    def test_presence_same_browser_deduplicates_and_expires(self):
        self.client.get('/public/character/1')
        headers={'origin':'http://testserver','referer':'http://testserver/public/character/1'}
        for _ in range(2):self.assertEqual(self.client.post('/public/traffic/presence',headers=headers).status_code,204)
        s=traffic.snapshot();self.assertEqual(s['summary']['active'],1)
        self.assertEqual(s['active'][0]['visitors'],1)
        self.assertEqual(s['summary']['requests'],1)  # Heartbeats aren't page views or data calls.
        headers['referer']='http://testserver/public/character/2'
        self.client.post('/public/traffic/presence',headers=headers)
        self.assertEqual(traffic.snapshot()['summary']['active'],1)
        self.assertEqual(len(traffic.snapshot()['active']),2)
        with traffic.connection() as c:c.execute('UPDATE presence SET seen=?',(time.time()-91,))
        self.assertEqual(traffic.snapshot()['summary']['active'],0)
        headers['origin']='https://foreign.example'
        self.assertEqual(self.client.post('/public/traffic/presence',headers=headers).status_code,403)

    def test_only_admin_can_access_page_and_data(self):
        for user in (None,dict(id=2,username='User',permissions=set())):
            with patch.object(routes,'require_login',return_value=user):
                self.assertEqual(self.client.get('/admin/public-traffic',follow_redirects=False).status_code,302)
                self.assertEqual(self.client.get('/admin/public-traffic/data').status_code,403)
        with patch.object(routes,'require_login',return_value=ADMIN):
            r=self.client.get('/admin/public-traffic');self.assertEqual(r.status_code,200)
            self.assertIn('Most viewed pages',r.text)
            self.assertEqual(r.headers['cache-control'],'no-store')
            self.assertEqual(self.client.get('/admin/public-traffic/data').status_code,200)

    def test_no_raw_secrets_or_public_tokens_in_metrics(self):
        token='sensitive_group_link'
        key=traffic.page_key('/public/groups/'+token+'?view=stats&q=secret&token=secret')
        self.assertNotIn(token,key);self.assertNotIn('secret',key)
        self.assertIn('view=stats',key)
        self.assertEqual(traffic.page_key('/public/do-not-store-this-secret'),'/public/other')
        self.assertIsNone(traffic.visitor_id('malformed','day'))
        self.client.get('/public/character/1?q=password')
        with traffic.connection() as c:
            dump='\n'.join(c.iterdump())
        self.assertNotIn(self.client.cookies.get(traffic.COOKIE),dump)
        self.assertNotIn('password',dump)

    def test_all_real_public_route_paths_keep_their_identity(self):
        public = f.load('public_pages')
        paths = ['/public/killboard', '/public/map/eve-2d',
                 '/public/kill/mer/2026-09-01/649525/hidden',
                 '/public/character/90037483?tab=top-kills']
        for family in public.CATALOG:
            paths.append('/public'+family['example'])
            for tab in family['tabs']:
                paths.append('/public'+public.tab_path(family['key'],tab['key'],family['example']))
        paths += ['/public/entities/'+kind for kind in public.ENTITY_INDEX_KEYS]
        for url in paths:
            with self.subTest(url=url):
                self.assertEqual(traffic.page_key(url),url)

    def test_requested_paths_repetition_and_client_labels(self):
        for _ in range(3):
            r=self.client.get('/public/wp-login.php?password=not-for-logs',headers={'user-agent':'curl/8.0 credential-not-for-logs'})
            self.assertEqual(r.status_code,404)
        snapshot=traffic.snapshot()
        row=snapshot['diagnostics'][0]
        self.assertEqual(row['target'],'/public/wp-login.php')
        self.assertEqual(row['requests'],3)
        self.assertEqual(row['client'],'curl')
        self.assertEqual(row['signal'],'Scanner-like path')
        self.assertEqual(row['method'],'GET')
        self.assertEqual(snapshot['recent'][0]['target'],'/public/wp-login.php')
        self.assertEqual(snapshot['recent'][0]['status'],404)
        self.assertNotIn('not-for-logs',str(snapshot))
        self.assertEqual(traffic.client_type('Mozilla/5.0 Chrome/120 Googlebot/2.1'),'Googlebot (declared)')
        self.assertEqual(traffic.client_type('Mozilla/5.0 Firefox/120'),'Firefox')
        self.assertEqual(traffic.snapshot('2026-99-99')['day'],snapshot['today'])

    def test_request_path_redaction_and_data_endpoint_context(self):
        for url,secret in [('/public/token/private-value','private-value'),
                           ('/public/groups/private-group-token?view=stats','private-group-token'),
                           ('/public/path/'+('a'*64),'a'*64),
                           ('/public/path/'+('abcd1234'*4),'abcd1234'*4),
                           ('/public/path/aabbccdd-1122-3344-5566-aabbccddeeff','aabbccdd-1122-3344-5566-aabbccddeeff')]:
            self.assertNotIn(secret,traffic.requested_path(url))
        self.client.get('/public/read?source=/character/1&path=/api/example?token=not-for-logs')
        row=traffic.snapshot()['recent'][0]
        self.assertEqual(row['page'],'/public/character/1')
        self.assertEqual(row['target'],'/public/read → /api/example')
        self.assertNotIn('not-for-logs',str(row))

    def test_existing_traffic_records_are_preserved_and_details_are_bounded(self):
        traffic.record('/public/character/1','page',200,5,'a'*32)
        self.assertIsNone(traffic.snapshot()['recent'][0]['target'])
        details=dict(target='/public/wp-login.php',method='GET',client='curl',signal='Scanner-like path')
        for _ in range(1005):
            traffic.record('/public/other','data',404,1,details=details)
        with traffic.connection() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM request_details').fetchone()[0],1000)
        self.assertEqual(traffic.snapshot()['summary']['requests'],1006)
        self.assertEqual(traffic.snapshot()['summary']['unique_visitors'],1)
        self.assertEqual(traffic.snapshot()['diagnostics'][0]['requests'],1000)

    def test_metrics_failure_never_breaks_public_page(self):
        with patch.object(traffic,'connection',side_effect=OSError('unavailable')):
            self.assertEqual(self.client.get('/public/character/1').status_code,200)

    def test_recent_records_are_bounded(self):
        with traffic.connection() as c:
            c.executemany('INSERT INTO recent(at,page,kind,status,elapsed) VALUES (?,?,?,?,?)',
                          [(time.time(),'/public','page',200,1)]*1010)
        traffic.record('/public','page',200,1)
        with traffic.connection() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM recent').fetchone()[0],1000)

    def test_googlebot_blocked_before_handler_and_without_visitor_cookie(self):
        for path in ('/public/character/1', '/public/read', '/public/traffic/presence', '/public'):
            response = self.client.get(path, headers={'user-agent':'Mozilla/5.0 (compatible; GoogleBot/2.1)'})
            self.assertEqual(response.status_code,403)
            self.assertNotIn('set-cookie',response.headers)
            self.assertEqual(response.headers['cache-control'],'no-store')
        snapshot = traffic.snapshot()
        self.assertEqual(snapshot['summary']['views'],0)
        self.assertEqual(snapshot['summary']['unique_visitors'],0)
        self.assertTrue(all(row['kind']=='blocked' for row in snapshot['recent']))
        self.assertTrue(all(row['client']=='Googlebot (declared)' for row in snapshot['recent']))
        # A direct middleware invocation proves no page handler is entered.
        import asyncio
        from starlette.requests import Request
        from unittest.mock import AsyncMock
        request = Request({'type':'http','method':'GET','path':'/public/read','query_string':b'',
                           'headers':[(b'user-agent',b'Googlebot')],'scheme':'http','server':('testserver',80)})
        handler = AsyncMock()
        self.assertEqual(asyncio.run(traffic.traffic_middleware(request,handler)).status_code,403)
        handler.assert_not_called()
        self.assertEqual(self.client.get('/private',headers={'user-agent':'Googlebot'}).status_code,200)
        for agent in ('Safari','bingbot','Mozilla/5.0'):
            self.assertEqual(self.client.get('/public/character/1',headers={'user-agent':agent}).status_code,200)

    def test_crawler_policy_admin_only_validated_and_persistent(self):
        url='/admin/public-traffic/googlebot'
        headers={'origin':'http://testserver'}
        for user in (None,dict(id=2,username='User',permissions=set())):
            with patch.object(routes,'require_login',return_value=user):
                self.assertEqual(self.client.post(url,json={'blocked':False},headers=headers).status_code,403)
        with patch.object(routes,'require_login',return_value=ADMIN):
            self.assertEqual(self.client.post(url,json={'blocked':False}).status_code,403)
            self.assertEqual(self.client.post(url,json={'blocked':False},headers={'origin':'https://other.example'}).status_code,403)
            for value in ({'blocked':'false'},[],{}):
                self.assertEqual(self.client.post(url,json=value,headers=headers).status_code,400)
            self.assertEqual(self.client.post(url,json={'blocked':False},headers=headers).status_code,200)
            self.assertFalse(traffic.googlebot_blocked())
            self.assertFalse(self.client.get('/admin/public-traffic/data').json()['block_googlebot'])
            self.assertEqual(self.client.get('/public/character/1',headers={'user-agent':'Googlebot'}).status_code,200)
            self.assertEqual(self.client.post(url,json={'blocked':True},headers=headers).status_code,200)
            self.assertEqual(self.client.get('/public/character/1',headers={'user-agent':'Googlebot'}).status_code,403)
        self.assertTrue((traffic.ROOT/'crawler_policy.json').exists())
        (traffic.ROOT/'crawler_policy.json').write_text('invalid')
        self.assertTrue(traffic.googlebot_blocked())


if __name__ == '__main__':unittest.main()
