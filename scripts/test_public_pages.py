"""Offline publication policy, revocation, cache and routes. Never opens a DB."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from scripts import test_killmail_forensics as f

public = f.load('public_pages')
routes = f.load('routes_public_pages')
ADMIN = dict(id=1, username='Admin', permissions={'admin.system.view'})


class PublicTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = patch.object(public, 'ROOT', Path(self.temp.name))
        self.root.start()
        app = FastAPI()
        # Independent group routes must win over the generic public gateway.
        @app.get('/public/groups/{token}')
        def group(token):
            return {'group': token}
        app.include_router(routes.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.root.stop()
        self.temp.cleanup()

    def enable(self, *values):
        return public.save_settings(set(values))

    def test_default_closed_private_and_direct_tab_urls(self):
        for url in ('/public/leaderboard', '/public/character/1?tab=stats', '/public/admin',
                    '/public/account', '/public/profiles', '/public/api/entity-search', '/public/tools'):
            self.assertEqual(self.client.get(url).status_code, 404, url)
        self.assertEqual(self.client.get('/public/groups/existing').json(), {'group': 'existing'})
        self.enable('character', 'character:overview')
        self.assertEqual(self.client.get('/public/character/1').status_code, 200)
        self.assertEqual(self.client.get('/public/character/1?tab=stats').status_code, 404)
        self.enable('character:overview')  # tab alone cannot publish its parent
        self.assertEqual(self.client.get('/public/character/1').status_code, 404)

    def test_no_query_or_work_on_public_cache_miss(self):
        self.enable('leaderboard', 'leaderboard:character')
        with patch.object(routes, '_prepare', side_effect=AssertionError('No public work')):
            response = self.client.get('/public/leaderboard')
        self.assertEqual(response.status_code, 200)
        self.assertIn('No calculation has been started', response.text)
        self.assertNotIn('<script', response.text)
        self.assertNotIn('/api/update-status', response.text)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertIn("connect-src 'none'", response.headers['content-security-policy'])

    def test_cached_links_revoke_immediately_without_mutating_source(self):
        self.enable('character', 'character:overview', 'character:stats', 'leaderboard', 'leaderboard:character')
        target = public.resolve('/leaderboard')
        html = '<h1>Rankings</h1><a href="/character/42">Pilot</a><a class="nav-link" href="/character/42?tab=stats">Stats</a>'
        public.store(target, html)
        first = self.client.get('/public/leaderboard').text
        self.assertIn('href="/public/character/42"', first)
        self.assertIn('href="/public/character/42?tab=stats"', first)
        self.enable('character', 'character:overview', 'leaderboard', 'leaderboard:character')
        second = self.client.get('/public/leaderboard').text
        self.assertNotIn('?tab=stats', second)
        self.assertNotIn('>Stats<', second)
        self.assertIn('href="/public/character/42"', second)
        self.enable('leaderboard', 'leaderboard:character')
        third = self.client.get('/public/leaderboard').text
        self.assertNotIn('href="/public/character/42"', third)
        self.assertIn('>Pilot</span>', third)
        self.assertEqual(public.load(target)['html'], html)

    def test_canonical_parameters_and_rejected_proxy_or_heavy_routes(self):
        self.assertEqual(public.resolve('/public/leaderboard?kind=character&page=1').source, '/leaderboard')
        self.assertEqual(public.resolve('/character/42?tab=overview').source, '/character/42')
        for value in ('https://evil/leaderboard', '//evil/leaderboard', '/leaderboard?kind=character&kind=alliance',
                      '/leaderboard?include=alliance:1', '/leaderboard?page=500000', '/leaderboard?through=2026-99',
                      '/character/42?tab=top-kills', '/character/42?tab=stats&stats_view=top-kills',
                      '/character/42?tab=population', '/weapon/1', '/public/groups/token', '/admin/jobs',
                      '/leaderboard?retry=1', '/character/42?tab=stats&secret=x'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                public.resolve(value)

    def test_unfinished_tabs_cannot_be_enabled_by_crafted_admin_form(self):
        policy = self.enable('modules', 'tools', 'character', 'character:top-kills', 'character:history', 'character:stats')
        self.assertNotIn('modules', policy)
        self.assertNotIn('tools', policy)
        self.assertEqual(policy['character']['tabs'], ['stats'])

    def test_remove_scripts_forms_events_private_links_preserve_css_svg_and_names(self):
        policy = self.enable('character', 'character:overview')
        html = '''<style>.x{color:red}</style><script>secret()</script><form action="/admin/jobs"><input name="token" value="secret"><button>Run</button></form>
        <a href="/admin/jobs">Job</a><a href="javascript:evil()">Bad</a><a href="//evil/x">Remote</a>
        <img src="https://images.evetech.net/types/1/icon" onerror="secret()" data-url="/api/private">
        <svg viewBox="0 0 100 100"><circle cx="50"/></svg><a href="/character/1">Pilot</a>'''
        result = public.sanitize(html, policy, '/character/42')
        for text in ('secret', 'script', '<form', '<input', '<button', 'onerror', 'data-url', '/admin/jobs', 'javascript:', '//evil'):
            self.assertNotIn(text, result)
        self.assertIn('<style>', result)
        self.assertIn('viewBox="0 0 100 100"', result)
        self.assertIn('href="/public/character/1"', result)
        self.assertIn('Job</span>', result)

    def test_pagination_is_a_real_policy_checked_public_get_link(self):
        policy = self.enable('character', 'character:killboard')
        html = '<a href="#" data-killmail-page="2">Next</a>'
        self.assertIn('href="/public/character/42?page=2&amp;tab=killboard"',
                      public.sanitize(html, policy, '/character/42?tab=killboard'))

    def test_expiry_and_storage_limits_and_clear_do_not_touch_other_files(self):
        first, second = public.resolve('/character/1'), public.resolve('/character/2')
        with patch.object(public, 'MAX_ENTRIES', 1):
            public.store(first, '<p>first</p>')
            public.store(second, '<p>second</p>')
        self.assertIsNone(public.load(first))
        self.assertIsNotNone(public.load(second))
        with patch.object(public.time, 'time', return_value=time.time()+public.MAX_AGE+1):
            self.assertIsNone(public.load(second))
        with patch.object(public, 'MAX_ENTRY_BYTES', 32), self.assertRaises(ValueError):
            public.store(first, 'x'*100)
        (public.ROOT/'unrelated.json').write_text('{}')
        public.clear()
        self.assertTrue((public.ROOT/'unrelated.json').exists())
        self.assertFalse(public.entries())

    def test_total_byte_eviction(self):
        with patch.object(public, 'MAX_BYTES', 550):
            for i in range(1, 5):
                public.store(public.resolve('/character/'+str(i)), 'x'*180)
        self.assertLessEqual(sum(row['bytes'] for row in public.entries()), 550)

    def test_admin_endpoints_require_system_permission_without_writes(self):
        for user in (None, dict(id=2, username='Reader', permissions={'entities.view'})):
            with patch.object(routes, 'require_login', return_value=user):
                for method, url in [('get','/admin/public-pages'), ('get','/admin/public-pages/status'),
                                    ('post','/admin/public-pages/settings'), ('post','/admin/public-pages/prepare'),
                                    ('post','/admin/public-pages/clear')]:
                    response = getattr(self.client, method)(url, follow_redirects=False)
                    self.assertEqual(response.status_code, 302)
        self.assertFalse((public.ROOT/'settings.json').exists())

    def test_admin_settings_and_template(self):
        with patch.object(routes, 'require_login', return_value=ADMIN):
            response = self.client.get('/admin/public-pages')
            self.assertEqual(response.status_code, 200)
            self.assertIn('character:stats', response.text)
            self.assertIn('character:killboard', response.text)
            response = self.client.post('/admin/public-pages/settings',
                data={'enabled':['character','character:stats']}, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
        self.assertEqual(public.settings()['character']['tabs'], ['stats'])

    def test_selected_tabs_and_published_variants_only(self):
        self.enable('leaderboard', 'leaderboard:alliance')
        public.store(public.resolve('/leaderboard?kind=alliance'), '<p>Alliance scores</p>')
        public.store(public.resolve('/character/1'), '<p>private</p>')
        response = self.client.get('/public/leaderboard?kind=alliance')
        self.assertIn('Alliance scores', response.text)
        self.assertNotIn('>Characters</a>', response.text)
        self.assertNotIn('/public/character/1', response.text)
        self.assertEqual(self.client.get('/public/leaderboard').status_code, 404)

    def test_preview_requires_admin_and_does_not_publish(self):
        public.store(public.resolve('/leaderboard'), '<h1>Private preview content</h1>')
        self.assertEqual(self.client.get('/public/leaderboard').status_code,404)
        with patch.object(routes,'require_login',return_value=None):
            self.assertEqual(self.client.get('/admin/public-pages/preview?source=/leaderboard',follow_redirects=False).status_code,302)
        with patch.object(routes,'require_login',return_value=ADMIN):
            response=self.client.get('/admin/public-pages/preview?source=/leaderboard')
        self.assertEqual(response.status_code,200)
        self.assertIn('Private preview content',response.text)
        self.assertEqual(self.client.get('/public/leaderboard').status_code,404)

    def test_preparation_lock_excludes_a_second_job_and_reports_running(self):
        import fcntl
        with (public.ROOT/'prepare.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            public._atomic(public.ROOT/'prepare-status.json',dict(state='running',updated=time.time(),source='/leaderboard'))
            with patch.object(routes,'require_login',return_value=ADMIN):
                response=self.client.post('/admin/public-pages/prepare',json={'source':'/leaderboard'})
                self.assertEqual(response.status_code,409)
                self.assertEqual(self.client.get('/admin/public-pages/status').json()['state'],'running')
        self.assertEqual(routes._status()['state'],'interrupted')

    def test_all_templates_compile(self):
        for name in f.templates.env.list_templates():
            if name.endswith('.html'):
                f.templates.env.get_template(name)

class RendererTests(unittest.TestCase):
    def test_entity_header_and_shared_stats_render_without_population_or_scan(self):
        import sys
        from types import ModuleType
        from decimal import Decimal
        from datetime import date
        renderer = f.load('public_page_renderer')
        stubs = {}
        for name in ('routes_entities','routes_superintel','routes_admin_rankings','killmail_rankings'):
            mod = ModuleType(f.PACKAGE+'.'+name)
            stubs[mod.__name__] = mod
        row = dict(month=date(2026,10,1),kills=2,losses=1,destroyed_cents=10000,
                   weighted_destroyed_cents=2500,lost_cents=3000,ratio=Decimal(25),dirty=False,
                   unvalued_kills=0,unvalued_losses=0)
        ranking = stubs[f.PACKAGE+'.killmail_rankings']
        ranking.combat_history = lambda kind,id:dict(ready=True, rows=[row], totals=row)
        ranking.compact_isk = lambda value:str(value)
        ranking.full_isk = lambda value:str(value)+' ISK'
        profile = dict(entity_type='character',entity_id=42,name='Public Pilot',image_url='',subtitle='',url='/character/42',
                       current_corporation=None,current_alliance=None)
        target = public.resolve('/character/42?tab=stats')
        request = f.Request({'type':'http','method':'GET','path':'/admin/public-pages/prepare',
                             'headers':[], 'query_string':b'', 'scheme':'http','server':('testserver',80)})
        with patch.dict(sys.modules, stubs),patch.object(f.entities, 'get_entity_profile',return_value=profile):
            html = renderer.render(request,target)
        result = public.sanitize(html, {'character':dict(enabled=True,tabs=['stats'])},target.source)
        self.assertIn('Public Pilot',result)
        self.assertIn('Combat stats',result)
        self.assertIn('2500',result)
        self.assertNotIn('Loading',result)
        self.assertNotIn('<script',result)
        self.assertNotIn('top-kills',result)
        self.assertNotIn('Admin',result)


class LandingTests(unittest.TestCase):
    def test_anonymous_root_and_home_land_on_public_home_and_login_remains_available(self):
        login = f.load('routes_login')
        home = f.load('routes_home')
        app = FastAPI()
        app.include_router(login.router)
        app.include_router(home.router)
        app.include_router(routes.router)
        with tempfile.TemporaryDirectory() as directory, patch.object(public,'ROOT',Path(directory)), \
             patch.object(login,'get_current_user',return_value=None), patch.object(home,'require_login',return_value=None):
            client = TestClient(app)
            for url in ('/', '/home'):
                response = client.get(url,follow_redirects=False)
                self.assertEqual(response.status_code,302)
                self.assertEqual(response.headers['location'],'/public')
            response = client.get('/')
            self.assertEqual(response.status_code,200)
            self.assertIn('Home</h1>',response.text)
            self.assertIn('Changelog',response.text)
            self.assertIn('href="/login">Log in</a>',response.text)
            self.assertNotIn('Active session',response.text)
            self.assertNotIn('<script',response.text)
            self.assertEqual(client.get('/login').status_code,200)

    def test_logged_in_public_home_keeps_normal_account_menu(self):
        app=FastAPI();app.include_router(routes.router)
        with tempfile.TemporaryDirectory() as directory,patch.object(public,'ROOT',Path(directory)), \
             patch.object(routes,'require_login',return_value=ADMIN):
            response=TestClient(app).get('/public')
        self.assertIn('My account',response.text)
        self.assertNotIn('href="/login">Log in</a>',response.text)
        self.assertNotIn('Content-Security-Policy',response.headers)


if __name__ == '__main__':
    unittest.main()
