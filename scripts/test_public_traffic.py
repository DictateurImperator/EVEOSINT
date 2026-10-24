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
        traffic.set_googlebot_blocked(True)
        from importlib import import_module
        self.security=import_module(f.PACKAGE+'.public_security')
        self.security._clients.clear()
        self.geo = patch.object(traffic.traffic_countries,'ensure_database');self.geo.start()
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
        self.client = TestClient(app,client=('8.8.8.8',5000))
        self.client.cookies.set(traffic.ACCESS_COOKIE,traffic.access_choice('accepted'))

    def tearDown(self):
        self.geo.stop();self.root.stop();self.tmp.cleanup()

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
        self.assertEqual(traffic.snapshot()['summary']['unique_visitors'],1)
        self.assertEqual(sum(row['kind']=='data' for row in s['recent']),1)

    def test_separate_utc_days_and_retention(self):
        now=time.time();yesterday=now-86400
        traffic.record('/public/character/1','page',200,5,'a'*32,now=yesterday,client_ip='8.8.8.8')
        traffic.record('/public/character/1','page',200,8,'a'*32,now=now,client_ip='8.8.8.8')
        self.assertEqual(traffic.snapshot()['summary']['unique_visitors'],1)
        day=datetime.fromtimestamp(yesterday,timezone.utc).strftime('%Y-%m-%d')
        self.assertEqual(traffic.snapshot(day)['summary']['unique_visitors'],1)
        self.assertEqual(len(traffic.snapshot()['days']),2)
        traffic.record('/public/old','page',200,5,'a'*32,now=now-40*86400,client_ip='8.8.8.8')
        # Force periodic cleanup on the next current request.
        with traffic.connection() as c:c.execute("DELETE FROM metadata WHERE key='cleaned'")
        traffic.record('/public/character/1','page',200,5,'a'*32,now=now,client_ip='8.8.8.8')
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
        with traffic.connection() as c:c.execute('UPDATE ip_presence SET seen=?',(time.time()-91,))
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
        self.assertNotIn(self.client.cookies.get(traffic.ACCESS_COOKIE),dump)
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
        traffic.record('/public/character/1','page',200,5,'a'*32,client_ip='8.8.8.8')
        self.assertIsNone(traffic.snapshot()['recent'][0]['target'])
        details=dict(target='/public/wp-login.php',method='GET',client='curl',signal='Scanner-like path')
        for _ in range(1005):
            traffic.record('/public/other','data',404,1,details=details,client_ip='8.8.8.8')
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
            c.execute("INSERT INTO recent_countries SELECT id,'FR' FROM recent")
        traffic.record('/public','page',200,1,client_ip='8.8.8.8')
        with traffic.connection() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM recent').fetchone()[0],1000)
            self.assertLessEqual(c.execute('SELECT COUNT(*) FROM recent_countries').fetchone()[0],1000)

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
        self.assertEqual(self.client.get('/private',headers={'user-agent':'Googlebot'}).status_code,403)
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
            self.assertEqual(self.client.post(url,json={'blocked':False},headers=headers).status_code,409)
            self.assertEqual(self.client.post(url,json={'blocked':True},headers=headers).status_code,200)
            self.assertEqual(self.client.get('/public/character/1',headers={'user-agent':'Googlebot'}).status_code,403)
        self.assertTrue((traffic.ROOT/'crawler_policy.json').exists())
        (traffic.ROOT/'crawler_policy.json').write_text('invalid')
        self.assertTrue(traffic.googlebot_blocked())

    def test_robots_is_available_to_blocked_bot_and_follows_policy(self):
        from urllib.robotparser import RobotFileParser
        for blocked in (True,False,True):
            traffic.set_googlebot_blocked(blocked)
            response=self.client.get('/robots.txt',headers={'user-agent':'Googlebot'})
            self.assertEqual(response.status_code,200)
            self.assertTrue(response.headers['content-type'].startswith('text/plain'))
            self.assertEqual(response.headers['cache-control'],'public, max-age=300')
            parser=RobotFileParser();parser.parse(response.text.splitlines())
            for path in ('/public','/public/','/public/character/1'):
                self.assertEqual(parser.can_fetch('Googlebot',path),False)
                self.assertTrue(parser.can_fetch('bingbot',path))
            self.assertFalse(parser.can_fetch('Googlebot','/public/read'))
            self.assertFalse(parser.can_fetch('Googlebot','/robots.txt'))
        self.assertEqual(traffic.snapshot()['summary']['requests'],0)

    def test_cache_indexing_default_preserves_explicit_block_and_fail_closed(self):
        policy=traffic.ROOT/'crawler_policy.json'
        policy.unlink()
        self.assertFalse(traffic.googlebot_blocked())
        traffic.set_googlebot_blocked(True)
        self.assertTrue(traffic.googlebot_blocked())
        policy.write_text('broken')
        self.assertTrue(traffic.googlebot_blocked())

    def test_country_daily_counts_dedupe_browsers_and_separate_bots(self):
        with patch.object(traffic.traffic_countries,'country',return_value='FR'):
            self.client.get('/public/character/1');self.client.get('/public/character/2')
        with patch.object(traffic.traffic_countries,'country',return_value='DE'):
            self.client.get('/public/character/1')
            self.client.get('/public/read')
            self.client.get('/public/character/1',headers={'user-agent':'Googlebot'})
        s=traffic.snapshot();countries={r['country']:r for r in s['countries']}
        self.assertEqual(countries['FR']['unique_visitors'],1)
        self.assertEqual(countries['FR']['views'],2)
        self.assertEqual(countries['DE']['unique_visitors'],1)
        self.assertEqual(countries['DE']['views'],1)
        self.assertEqual(countries['DE']['requests'],2)
        self.assertEqual(countries['DE']['blocked'],0)
        self.assertEqual(s['summary']['unique_visitors'],1)
        self.assertEqual(s['recent'][0]['country'],'DE')
        self.assertEqual(self.client.get('/admin/public-traffic/data').status_code,403)
        with traffic.connection() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM ip_country_visitors').fetchone()[0],2)
            # Only country codes are stored, never client addresses.
            columns={r[1] for r in c.execute('PRAGMA table_info(recent_countries)')}
            self.assertEqual(columns,{'id','country'})

    def test_country_refresh_is_admin_only_and_background(self):
        url='/admin/public-traffic/countries'
        self.assertEqual(self.client.post(url,headers={'origin':'http://testserver'}).status_code,403)
        with patch.object(routes,'require_login',return_value=ADMIN):
            self.assertEqual(self.client.post(url).status_code,403)
            self.assertEqual(self.client.post(url,headers={'origin':'https://foreign.example'}).status_code,403)
            self.assertEqual(self.client.post(url,headers={'origin':'http://testserver'}).status_code,202)
        traffic.traffic_countries.ensure_database.assert_called_once_with(force=True)

    def test_country_uses_asgi_client_not_arbitrary_forwarded_header(self):
        from starlette.requests import Request
        request=Request({'type':'http','method':'GET','path':'/public','query_string':b'',
                         'headers':[(b'x-forwarded-for',b'1.1.1.1')],
                         'scheme':'http','server':('testserver',80),'client':('8.8.8.8',12345)})
        with patch.object(traffic.traffic_countries,'country',return_value='US') as lookup:
            self.assertEqual(traffic.request_details(request)['country'],'US')
        lookup.assert_called_once_with('8.8.8.8')

    def test_country_metrics_follow_thirty_day_retention(self):
        details=dict(target='/public',method='GET',client='Safari',signal='Unclassified',country='FR')
        traffic.record('/public','page',200,1,'a'*32,now=time.time()-40*86400,details=details,client_ip='8.8.8.8')
        with traffic.connection() as c:c.execute("DELETE FROM metadata WHERE key='cleaned'")
        traffic.record('/public','page',200,1,'a'*32,details=dict(details,country='DE'),client_ip='8.8.8.8')
        with traffic.connection() as c:
            self.assertEqual([r[0] for r in c.execute('SELECT country FROM country_requests')],['DE'])
            self.assertEqual([r[0] for r in c.execute('SELECT country FROM ip_country_visitors')],['DE'])

    def test_ip_uniques_independent_of_cookie_and_daily_keys(self):
        self.client.get('/public/character/1')
        self.client.cookies.clear()
        self.client.cookies.set(traffic.ACCESS_COOKIE,traffic.access_choice('accepted'))
        self.client.get('/public/character/1')
        self.assertEqual(traffic.snapshot()['summary']['unique_visitors'],1)
        second=TestClient(self.client.app,client=('1.1.1.1',5010))
        second.cookies.set(traffic.ACCESS_COOKIE,traffic.access_choice('accepted'))
        second.get('/public/character/1')
        self.assertEqual(traffic.snapshot()['summary']['unique_visitors'],2)
        today=datetime.now(timezone.utc).strftime('%Y-%m-%d')
        a=traffic.ip_visitor_id('8.8.8.8',today)
        self.assertEqual(a,traffic.ip_visitor_id('::ffff:8.8.8.8',today))
        with traffic.connection() as c:
            pseudonyms=[r[0] for r in c.execute('SELECT visitor FROM ip_visitors')]
            self.assertTrue(all(isinstance(v,bytes) and len(v)==32 for v in pseudonyms))
            self.assertEqual(c.execute('SELECT COUNT(*) FROM security_ips').fetchone()[0],0)

    def test_country_page_rankings_are_daily_indexed_and_not_recent_sample(self):
        with patch.object(traffic.traffic_countries,'country',return_value='FR'):
            self.client.get('/public/character/1');self.client.get('/public/character/1')
        with patch.object(traffic.traffic_countries,'country',return_value='DE'):
            self.client.get('/public/character/2')
        self.assertEqual([(r['page'],r['views'],r['unique_visitors']) for r in traffic.snapshot(country='fr')['pages']],
                         [('/public/character/1',2,1)])
        self.assertEqual(traffic.snapshot(country='DE')['pages'][0]['page'],'/public/character/2')
        self.assertEqual(traffic.snapshot(country='US')['pages'],[])
        with traffic.connection() as c:c.execute('DELETE FROM recent')
        self.assertEqual(traffic.snapshot(country='FR')['pages'][0]['views'],2)
        with patch.object(routes,'require_login',return_value=ADMIN):
            self.assertEqual(self.client.get('/admin/public-traffic/data?country=fr').json()['country'],'FR')
            self.assertEqual(self.client.get('/admin/public-traffic/data?country=invalid').status_code,400)

    def test_notice_accept_decline_tamper_and_expiry(self):
        self.client.cookies.clear()
        response=self.client.get('/public/character/1')
        self.assertIn('Privacy, cookies and security',response.text)
        self.assertEqual(traffic.snapshot()['summary']['views'],0)
        self.assertEqual(self.client.get('/public/read').status_code,403)
        url='/public/access/choice';headers={'origin':'http://testserver'}
        self.assertEqual(self.client.post(url,json={'choice':'accepted'}).status_code,403)
        self.assertEqual(self.client.post(url,json={'choice':'invalid'},headers=headers).status_code,400)
        response=self.client.post(url,json={'choice':'declined'},headers=headers)
        self.assertEqual(response.status_code,204)
        self.assertIn('Privacy, cookies and security',self.client.get('/public/access-required').text)
        self.assertEqual(self.client.get('/public/character/1').text,'<h1>Public</h1>')
        self.assertEqual(traffic.snapshot()['summary']['views'],0)
        with traffic.connection() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM security_ips').fetchone()[0],0)
        response=self.client.post(url,json={'choice':'accepted'},headers=headers)
        self.assertIn('HttpOnly',response.headers['set-cookie'])
        self.assertEqual(self.client.get('/public/character/1').text,'<h1>Public</h1>')
        for value in ('accepted.1.1.invalid',traffic.access_choice('accepted',time.time()-181*86400)):
            self.client.cookies.set(traffic.ACCESS_COOKIE,value)
            self.assertIn('siteNotice',self.client.get('/public/character/1').text)

    def test_security_ip_block_enforced_and_purge_preserves_only_active_blocks(self):
        self.client.get('/public/character/1')
        traffic.record('/public/character/1','blocked',429,0,client_ip='8.8.8.8',security_incident=True)
        url='/admin/public-traffic/ip-block';headers={'origin':'http://testserver'}
        self.assertEqual(self.client.post(url,json={'ip':'8.8.8.8','blocked':True,'reason':'abuse'},headers=headers).status_code,403)
        with patch.object(routes,'require_login',return_value=ADMIN):
            self.assertEqual(self.client.post(url,json={'ip':'8.8.8.8','blocked':True,'reason':''},headers=headers).status_code,400)
            self.assertEqual(self.client.post(url,json={'ip':'8.8.8.8','blocked':True,'reason':'abuse'},headers=headers).status_code,200)
        self.assertEqual(self.client.get('/public/character/1').status_code,403)
        with traffic.connection() as c:
            c.execute('UPDATE security_ips SET last_seen=?',(time.time()-40*86400,))
            c.execute('INSERT INTO security_ips(ip,first_seen,last_seen) VALUES (?,?,?)',('1.1.1.1',0,time.time()-40*86400))
        traffic.purge()
        with traffic.connection() as c:self.assertEqual([r[0] for r in c.execute('SELECT ip FROM security_ips')],['8.8.8.8'])
        with patch.object(routes,'require_login',return_value=ADMIN):
            self.assertEqual(self.client.post(url,json={'ip':'8.8.8.8','blocked':False},headers=headers).status_code,200)
        traffic.purge()
        with traffic.connection() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM security_ips').fetchone()[0],0)

    def test_abuse_detection_stores_and_blocks_only_after_threshold(self):
        self.client.cookies.set(traffic.ACCESS_COOKIE,traffic.access_choice('declined'))
        with patch.object(self.security,'RATE_LIMIT',2):
            self.assertEqual(self.client.get('/public/character/1').status_code,200)
            self.assertEqual(self.client.get('/public/character/1').status_code,200)
            with traffic.connection() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM security_ips').fetchone()[0],0)
            self.assertEqual(self.client.get('/public/character/1').status_code,429)
            with traffic.connection() as c:
                row=c.execute('SELECT ip,blocked,reason FROM security_ips').fetchone()
                self.assertEqual(row['ip'],'8.8.8.8');self.assertEqual(row['blocked'],1)
        self.assertEqual(self.client.get('/public/character/1').status_code,403)

    def test_legal_page_public_before_choice_after_refusal_and_when_blocked(self):
        self.client.cookies.clear()
        for path in ('/legal','/public/legal'):
            response=self.client.get(path)
            self.assertEqual(response.status_code,200)
            self.assertIn('Legal &amp; Privacy',response.text)
            self.assertIn('If we detect abusive activity',response.text)
            self.assertIn('Change analytics preferences',response.text)
            self.assertNotIn('<dialog',response.text)
            self.assertNotIn('set-cookie',response.headers)
        self.assertEqual(traffic.snapshot()['summary']['requests'],0)
        self.client.cookies.set(traffic.ACCESS_COOKIE,traffic.access_choice('declined'))
        self.assertEqual(self.client.get('/public/legal').status_code,200)
        traffic.record('/public','blocked',429,0,client_ip='8.8.8.8',security_incident=True)
        with traffic.connection() as conn:conn.execute('UPDATE security_ips SET blocked=1')
        self.assertEqual(self.client.get('/public/character/1').status_code,403)
        self.assertEqual(self.client.get('/public/legal').status_code,200)


if __name__ == '__main__':unittest.main()
