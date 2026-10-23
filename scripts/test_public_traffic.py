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

    def test_metrics_failure_never_breaks_public_page(self):
        with patch.object(traffic,'connection',side_effect=OSError('unavailable')):
            self.assertEqual(self.client.get('/public/character/1').status_code,200)

    def test_recent_records_are_bounded(self):
        with traffic.connection() as c:
            c.executemany('INSERT INTO recent(at,page,kind,status,elapsed) VALUES (?,?,?,?,?)',
                          [(time.time(),'/public','page',200,1)]*1010)
        traffic.record('/public','page',200,1)
        with traffic.connection() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM recent').fetchone()[0],1000)


if __name__ == '__main__':unittest.main()
