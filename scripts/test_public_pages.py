"""Offline publication policy, revocation, cache and routes. Never opens a DB."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.responses import HTMLResponse
from scripts import test_killmail_forensics as f

public = f.load('public_pages')
routes = f.load('routes_public_pages')
ADMIN = dict(id=1, username='Admin', permissions={'admin.system.view'})


def private_menu_fixture(module,bucket):
    if module=='tools':
        return {'title':'Tools','items':[dict(label=label,href=path,icon='') for label,path in
            [('All tools','/tools'),('Ship Analysis','/ship-analysis'),('Economics comparison','/tools/economics')]]}
    return {'title':'EntitiesINTEL','items':[
        dict(label=public.FAMILIES[key]['label'],href='/entities/'+kind,icon='◉')
        for kind,key in public.ENTITY_INDEX_KEYS.items() if kind not in ('system','constellation','region')]+[
        dict(label='MAP',href='/map',icon='▧')]}


class PublicTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = patch.object(public, 'ROOT', Path(self.temp.name))
        self.root.start()
        self.menu_patch=patch.object(routes,'shared_context_menu',side_effect=private_menu_fixture)
        self.menu_patch.start()
        renderer=f.load('public_page_renderer')
        self.render_patch=patch.object(renderer,'render',return_value='<h1>On demand</h1>')
        self.renderer=self.render_patch.start()
        app = FastAPI()
        # Independent group routes must win over the generic public gateway.
        @app.get('/public/groups/{token}')
        def group(token):
            return {'group': token}
        app.include_router(routes.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.menu_patch.stop()
        self.render_patch.stop()
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

    def test_global_killboard_opens_every_entity_kb_without_other_tabs(self):
        policy=self.enable('killboard')
        self.assertIn('data-entity-search-input',self.client.get('/public').text)
        for kind in public.KILLBOARD_ENTITIES:
            with self.subTest(kind=kind):
                target=public.resolve('/'+kind+'/42?tab=killboard')
                self.assertTrue(public.published(target,policy))
                response=self.client.get(target.public)
                self.assertEqual(response.status_code,200,response.text)
                self.assertIn('href="/public/killboard"',response.text)
                self.assertEqual(public.link_url('/'+kind+'/42',policy),target.public)
                if kind!='coalition':
                    bare=self.client.get('/public/'+kind+'/42',follow_redirects=False)
                    self.assertEqual(bare.status_code,302)
                    self.assertEqual(bare.headers['location'],target.public)
        self.assertEqual(self.client.get('/public/kill/42').status_code,200)
        for tab in ('stats','history','pilotable-ships','top-kills'):
            self.assertEqual(self.client.get('/public/character/42?tab='+tab).status_code,404)
        # Derived inheritance must not turn unchecked settings on permanently.
        self.assertFalse(public.settings()['character']['enabled'])
        self.enable()
        self.assertNotIn('data-entity-search-input',self.client.get('/public').text)
        self.assertEqual(self.client.get('/public/character/42?tab=killboard').status_code,404)
        self.assertEqual(self.client.get('/public/kill/42').status_code,404)

    def test_one_admin_checkbox_saves_all_kb_access_and_legacy_switches_cannot_bypass_it(self):
        with patch.object(routes,'require_login',return_value=ADMIN):
            page=self.client.get('/admin/public-pages').text
            import re
            values=re.findall(r'type="checkbox" name="enabled" value="([^"]+)"',page)
            self.assertEqual([value for value in values if value=='killboard' or value.endswith(':killboard') or value in ('kill','kill:detail')],['killboard'])
            response=self.client.post('/admin/public-pages/settings',data={'enabled':['killboard']},follow_redirects=False)
            self.assertEqual(response.status_code,303)
        for kind in public.KILLBOARD_ENTITIES:
            self.assertEqual(self.client.get('/public/'+kind+'/42?tab=killboard').status_code,200)
        self.assertEqual(self.client.get('/public/kill/42').status_code,200)
        self.assertTrue(public.killboards_enabled({'killboard':{'enabled':True,'tabs':[]}}))
        legacy={'character':{'enabled':True,'tabs':['killboard']},'kill':{'enabled':True,'tabs':['detail']}}
        self.assertFalse(public.published(public.resolve('/character/42?tab=killboard'),legacy))
        self.assertFalse(public.published(public.resolve('/kill/42'),legacy))
        self.enable('character','character:killboard','kill','kill:detail')
        self.assertEqual(self.client.get('/public/character/42?tab=killboard').status_code,404)
        self.assertEqual(self.client.get('/public/kill/42').status_code,404)

    def test_private_parent_menus_do_not_appear_without_an_enabled_child(self):
        for family in ('system','constellation','region'):
            with self.subTest(family=family):
                self.enable(family,family+':overview')
                self.assertNotIn('href="/public/entities"',self.client.get('/public').text)
        self.enable('tools','tools:overview')
        self.assertNotIn('href="/public/tools"',self.client.get('/public').text)
        self.assertEqual(self.client.get('/public/tools').status_code,404)
        self.enable('map','map:2d')
        self.assertIn('href="/public/entities"',self.client.get('/public').text)

    def test_profile_uses_private_sidebar_and_no_public_tab_bar(self):
        from html.parser import HTMLParser
        class Sidebar(HTMLParser):
            def __init__(self):super().__init__();self.links=[]
            def handle_starttag(self,tag,attrs):
                attrs=dict(attrs)
                if tag=='a' and 'context-link' in attrs.get('class',''):self.links.append(attrs['href'])
        self.enable('character','character:stats','character:history','killboard')
        response=self.client.get('/public/character/42?tab=stats')
        parser=Sidebar();parser.feed(response.text)
        self.assertEqual(parser.links,['/public/character/42?tab=killboard','/public/character/42?tab=stats','/public/character/42?tab=history'])
        self.assertIn('<div class="context-title">Profile</div>',response.text)
        self.assertNotIn('aria-label="Public page tabs"',response.text)
        self.assertNotIn('Combat stats · monthly history</a>',response.text)
        self.assertEqual(response.headers['cache-control'],'no-store')
        self.enable('character','character:top-kills')
        response=self.client.get('/public/character/42?tab=stats&stats_view=top-kills')
        parser=Sidebar();parser.feed(response.text)
        self.assertEqual(parser.links,['/public/character/42?stats_view=top-kills&tab=stats'])

    def test_shared_ship_template_renders_each_ship_once_without_megabytes_of_whitespace(self):
        ships=[dict(entity_type='ship',entity_id=i+1,name='Ship '+str(i),url='/ship/'+str(i+1),image_url='',
                    faction_name='Faction '+str(i%4),faction_logo_url='',ship_size='Frigate',ship_display_size='S',
                    ship_tree_row='combat',ship_tier='T1',group_name='Frigate') for i in range(823)]
        html=f.templates.env.get_template('entity_recent_fragment.html').render(entity_index=dict(entity_type='ship'),recent_entities=ships)
        self.assertEqual(html.count('class="ship-tree-link"'),823)
        self.assertLess(len(html.encode()),2*1024*1024)
        for ship in ships:self.assertEqual(html.count('href="'+ship['url']+'"'),1)

    def test_large_public_response_is_displayed_without_a_size_ceiling(self):
        size=9*1024*1024
        @self.client.app.get('/api/entities/ship/recent')
        def large():return HTMLResponse('x'*size)
        self.enable('ship','ship:overview')
        response=self.client.get('/public/read',params={'source':'/entities/ship','path':'/api/entities/ship/recent'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(len(response.content),size)
        self.assertFalse(public.entries()) # Oversized responses display without filling the result cache.

    def test_public_menus_use_private_shell_and_hide_disabled_items_even_when_logged_in(self):
        for user in (None,ADMIN):
            with self.subTest(user=user),patch.object(routes,'require_login',return_value=user):
                self.enable()
                html=self.client.get('/public').text
                self.assertNotIn('href="/public/entities"',html)
                self.assertNotIn('href="/public/tools"',html)
                self.assertNotIn('>Ships</span>',html)
                self.assertNotIn('href="/public/killboard"',html)
                self.assertEqual(self.client.get('/public/entities').status_code,404)
                self.enable('killboard')
                self.assertNotIn('href="/public/entities"',self.client.get('/public').text)
                self.assertEqual(self.client.get('/public/entities/ship').status_code,404)
                self.enable('ship','ship:overview')
                html=self.client.get('/public/entities/ship').text
                self.assertIn('class="app-layout"',html)
                self.assertIn('id="contextSidebar" class="context-sidebar"',html)
                self.assertIn('class="context-nav"',html)
                self.assertIn('>Ships</span>',html)
                self.assertNotIn('>Characters</span>',html)
                self.assertNotIn('>MAP</span>',html)
                self.assertNotIn('aria-label="Public page tabs"',html)
                self.assertNotIn('Recently viewed pages',html)
                self.assertNotIn('<p class="muted-line small">Updated',html)
        self.enable()
        with patch.object(routes,'require_login',return_value=ADMIN):
            self.assertEqual(self.client.get('/public/entities/ship').status_code,404)

    def test_enabled_children_open_parent_menus_and_only_their_categories(self):
        self.enable('ship','ship:overview')
        home=self.client.get('/public').text
        self.assertIn('href="/public/entities"',home)
        self.assertNotIn('href="/public/tools"',home)
        redirect=self.client.get('/public/entities',follow_redirects=False)
        self.assertEqual(redirect.headers['location'],'/public/entities/ship')
        self.assertEqual(self.client.get('/public/entities/ship').status_code,200)
        self.assertEqual(self.client.get('/public/entities/character').status_code,404)
        self.assertEqual(self.client.get('/public/entities/ship/manage').status_code,404)
        self.enable('tools','tools:ship-analysis')
        self.assertIn('href="/public/tools"',self.client.get('/public').text)
        self.assertEqual(self.client.get('/public/tools').status_code,200)
        self.assertEqual(self.client.get('/public/ship-analysis').status_code,200)
        self.assertEqual(self.client.get('/public/tools/economics').status_code,404)
        self.enable('coalition','coalition:history')
        self.assertEqual(public.link_url('/coalition/42',public.settings()),'/public/coalition/42?tab=history')
        self.assertEqual(self.client.get('/public/coalition/42',follow_redirects=False).headers['location'],'/public/coalition/42?tab=history')
        self.enable('map','map:2d')
        self.assertEqual(self.client.get('/public/entities',follow_redirects=False).headers['location'],'/public/map/eve-2d')
        self.enable()
        self.assertEqual(self.client.get('/public/entities').status_code,404)
        self.assertEqual(self.client.get('/public/entities/ship').status_code,404)
        self.assertEqual(self.client.get('/public/tools').status_code,404)

    def test_entity_list_data_uses_cache_and_current_publication_links(self):
        @self.client.app.get('/api/entities/ship/recent')
        def recent():
            return HTMLResponse('<a href="/ship/638">Raven</a><a href="/ship-category?tier=T2">T2</a><a href="/admin/jobs">Admin</a>')
        @self.client.app.get('/api/entity-search')
        def search():return {'results':[{'url':'/ship/638','label':'Raven'},{'url':'/character/42','label':'Hidden'}]}
        self.enable('ship','ship:overview')
        params={'source':'/entities/ship','path':'/api/entities/ship/recent'}
        response=self.client.get('/public/read',params=params)
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('href="/public/ship/638"',response.text)
        self.assertIn('href="/public/ship-category?tier=T2"',response.text)
        self.assertNotIn('href="/admin',response.text)
        data=self.client.get('/public/read',params={'source':'/entities/ship','path':'/api/entity-search?q=ra'}).json()
        self.assertEqual([item['url'] for item in data['results']],['/public/ship/638'])
        self.assertEqual(self.client.get('/public/read',params={'source':'/entities/ship','path':'/api/entities/character/recent'}).status_code,404)
        self.enable()
        self.assertEqual(self.client.get('/public/read',params=params).status_code,404)

    def test_public_menu_search_uses_shared_results_and_only_kb_links(self):
        calls=[]
        @self.client.app.get('/api/entity-search')
        def search():
            calls.append(True)
            return {'results':[{'url':'/character/42','label':'Pilot','image_url':'https://images.evetech.net/characters/42/portrait'},
                               {'url':'/ship/638','label':'Raven'},{'url':'/admin/jobs','label':'Private'}]}
        params={'source':'/killboard','path':'/api/entity-search?q=pi&limit=6&scope=global'}
        self.assertEqual(self.client.get('/public/read',params=params).status_code,404)
        self.enable('killboard')
        for _ in range(2):
            response=self.client.get('/public/read',params=params)
            self.assertEqual(response.status_code,200,response.text)
            results=response.json()['results']
            self.assertEqual([item['url'] for item in results],['/public/character/42?tab=killboard','/public/ship/638?tab=killboard'])
            self.assertEqual(results[0]['label'],'Pilot')
            self.assertIn('images.evetech.net',results[0]['image_url'])
        self.assertEqual(len(calls),1)
        self.enable()
        self.assertEqual(self.client.get('/public/read',params=params).status_code,404)

    def test_cache_miss_calculates_then_hits_cache_and_browser_revalidates(self):
        self.enable('leaderboard','leaderboard:character')
        first=self.client.get('/public/leaderboard')
        self.assertEqual(first.status_code,200)
        self.assertIn('On demand',first.text)
        self.assertEqual(self.renderer.call_count,1)
        second=self.client.get('/public/leaderboard')
        self.assertEqual(self.renderer.call_count,1)
        self.assertEqual(second.headers['cache-control'],'no-store')
        third=self.client.get('/public/leaderboard',headers={'If-None-Match':'"old-page"'})
        self.assertEqual(third.status_code,200)
        self.assertEqual(self.renderer.call_count,1)
        self.enable()
        self.assertEqual(self.client.get('/public/leaderboard').status_code,404)

    def test_previous_generic_public_page_cache_is_not_reused_after_renderer_change(self):
        target=public.resolve('/ship/24692')
        public.store(target,'Old generic ship page')
        path=public.ROOT/('snapshot-'+target.digest+'.json')
        data=json.loads(path.read_text());data.pop('cache_version');path.write_text(json.dumps(data))
        self.assertIsNone(public.load(target))
        public.store(target,'Shared private ship page')
        self.assertEqual(public.load(target)['html'],'Shared private ship page')

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
                      '/leaderboard?through=2026-99',
                      '/character/42?tab=population', '/public/groups/token', '/admin/jobs',
                      '/leaderboard?retry=1', '/character/42?tab=stats&secret=x'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                public.resolve(value)

    def test_all_pages_selectable_despite_informational_notes(self):
        policy=self.enable('modules','tools','character','character:top-kills','character:history',
                           'character:pilotable-ships','character:stats','corporation','corporation:population')
        self.assertTrue(policy['modules']['enabled'])
        self.assertTrue(policy['tools']['enabled'])
        values=[value for page in public.CATALOG for value in [page['key'],*[page['key']+':'+tab['key'] for tab in page['tabs']]]]
        all_policy=self.enable(*values)
        for page in public.CATALOG:
            with self.subTest(page=page['key']):
                self.assertTrue(public.published(public.resolve(page['example']),all_policy))
                self.assertEqual(self.client.get('/public'+page['example']).status_code,200)
        self.enable('character','character:top-kills','character:history','character:pilotable-ships','character:stats','corporation','corporation:population')
        self.assertEqual(set(policy['character']['tabs']),{'top-kills','history','pilotable-ships','stats'})
        for tab in ('top-kills','history','pilotable-ships'):
            self.assertEqual(self.client.get('/public/character/42?tab='+tab).status_code,200)
        self.assertEqual(self.client.get('/public/corporation/42?tab=population').status_code,200)

    def test_published_tool_computation_cached_and_revoked(self):
        from fastapi import Request
        calls=[]
        @self.client.app.post('/api/ship-analysis')
        async def calculate(request: Request):
            calls.append(await request.json())
            return {'answer':len(calls)}
        envelope={'source':'/ship-analysis','path':'/api/ship-analysis','payload':{'ships':[1]}}
        self.assertEqual(self.client.post('/public/compute',json=envelope).status_code,404)
        self.enable('tools','tools:ship-analysis')
        for _ in range(2):
            response=self.client.post('/public/compute',json=envelope)
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json(),{'answer':1})
        self.assertEqual(calls,[{'ships':[1]}])
        self.assertEqual(self.client.post('/public/compute',json=envelope,headers={'origin':'https://foreign.example'}).status_code,403)
        for path in ('/admin/jobs','/api/superintel/evolution/data','https://foreign.example'):
            self.assertEqual(self.client.post('/public/compute',json={**envelope,'path':path}).status_code,404)
        self.enable('tools')
        self.assertEqual(self.client.post('/public/compute',json=envelope).status_code,404)

    def test_remove_scripts_forms_events_private_links_preserve_css_svg_and_names(self):
        policy = self.enable('character', 'character:overview')
        html = '''<style>.x{color:red}</style><script>secret()</script><form method="post" action="/admin/jobs"><input name="token" value="secret"><button>Run</button></form>
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

    def test_coalition_logos_survive_public_filter_without_enabling_coalition_pages(self):
        policy = self.enable('killboard')
        for interactive in (False, True):
            with self.subTest(interactive=interactive):
                html = '<img src="/coalition/58/logo?v=123456789" alt="Coalition"><img src="/coalition/58/logo">'
                result = public.sanitize(html, policy, '/killboard', interactive=interactive)
                self.assertIn('src="/coalition/58/logo?v=123456789"', result)
                self.assertIn('src="/coalition/58/logo"', result)
                self.assertIsNone(public.link_url('/coalition/58?tab=history', policy))
                for source in ('/coalition/58/manage', '/coalition/58/logo/../manage',
                               '//foreign.example/coalition/58/logo', '/coalition/58/logo?v=1&other=2'):
                    result = public.sanitize('<img src="'+source+'">', policy, '/killboard', interactive=interactive)
                    self.assertNotIn('src=', result)

    def test_pagination_is_a_real_policy_checked_public_get_link(self):
        policy = self.enable('killboard')
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
            self.assertNotIn('value="character:killboard"', response.text)
            self.assertNotIn('value="killboard:killboard"',response.text)
            self.assertNotIn('value="kill"',response.text)
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
        stubs[f.PACKAGE+'.routes_entities'].entity_profile=lambda request,kind,id: f.templates.TemplateResponse(
            request=request,name='entity_profile.html',context=dict(request=request,profile=profile,profile_tab='stats',
            combat_stats=ranking.combat_history(kind,id),combat_stats_view='history',location_previews={},
            compact_isk=ranking.compact_isk,full_isk=ranking.full_isk,user=None))
        with patch.dict(sys.modules, stubs):
            html = renderer.render(request,target)
        result = public.sanitize(html, {'character':dict(enabled=True,tabs=['stats'])},target.source)
        self.assertIn('Public Pilot',result)
        self.assertIn('Combat stats',result)
        self.assertIn('2500',result)
        self.assertNotIn('Loading',result)
        self.assertNotIn('<script',result)
        self.assertNotIn('top-kills',result)
        self.assertNotIn('Admin',result)


class SharedPrivateShipTests(unittest.TestCase):
    def test_public_ship_renders_the_private_handler_template_and_filters_only_access(self):
        import sys
        from types import ModuleType
        from unittest.mock import Mock
        renderer=f.load('public_page_renderer')
        selection=dict(title='Abaddon',subtitle='Battleship',image_url='',ships_count=1,selection_type='ship',
                       active_filter={'type_id':24692},ship_ids=[24692],ships=[],sde_details={})
        request=f.Request({'type':'http','method':'GET','path':'/public/ship/24692','headers':[],
                           'query_string':b'','scheme':'http','server':('testserver',80)})
        private=ModuleType(f.PACKAGE+'.routes_entities')
        private.ship_profile=Mock(return_value=f.templates.TemplateResponse(request=request,name='ship_selection_profile.html',context={'selection':selection,'request':request}))
        stubs={f.PACKAGE+'.'+name:ModuleType(f.PACKAGE+'.'+name) for name in ('routes_admin_rankings','routes_superintel','routes_home')}
        stubs[private.__name__]=private
        target=public.resolve('/ship/24692')
        with patch.dict(sys.modules,stubs):html=renderer.render(request,target)
        self.assertEqual(private.ship_profile.call_count,1)
        self.assertIn('ship-selection-header',html)
        self.assertIn('>Abaddon</h1>',html)
        self.assertNotIn('EVE Entity Profile',html)
        policy={'ship':{'enabled':True,'tabs':['overview']},'killboard':{'enabled':True}}
        visible=public.sanitize(html,policy,target.source,interactive=True)
        for name in ('attributes','ranking','killboard'):self.assertIn('data-ship-tab="'+name+'"',visible)
        self.assertNotIn('data-ship-tab="analysis"',visible)
        policy={'killboard':{'enabled':True}}
        visible=public.sanitize(html,policy,'/ship/24692?tab=killboard',interactive=True)
        self.assertIn('data-ship-tab="killboard"',visible)
        for name in ('attributes','ranking','analysis'):self.assertNotIn('data-ship-tab="'+name+'"',visible)

    def test_private_root_macros_are_initialized_for_public_rendering(self):
        renderer=f.load('public_page_renderer')
        from jinja2 import DictLoader
        env=f.templates.env.overlay(loader=DictLoader({'base.html':'{% block content %}{% endblock %}',
            'macro.html':"{% extends 'base.html' %}{% macro entry(name) %}<a>{{ name }}</a>{% endmacro %}{% block content %}{{ entry('Coalition') }}{% endblock %}"}))
        with patch.object(renderer,'content_environment',return_value=env):
            self.assertEqual(renderer.content('macro.html',{}),'<a>Coalition</a>')

    def test_ship_api_scope_matches_selection_and_publication(self):
        source=public.resolve('/ship/24692')
        self.assertEqual(public.read_target('/api/ship-selection-ranking?type_id=24692',source).tab,'overview')
        self.assertEqual(public.read_target('/api/ship/24692/killmails',source).tab,'killboard')
        for path in ('/api/ship-selection-ranking?type_id=638','/api/ship/638/killmails','/api/ship-selection/killmails'):
            with self.subTest(path=path),self.assertRaises(ValueError):public.read_target(path,source)
        source=public.resolve('/ship-category?group=Battleship')
        self.assertEqual(public.read_target('/api/ship-selection-ranking?group=Battleship',source).tab,'overview')
        with self.assertRaises(ValueError):public.read_target('/api/ship-selection-ranking?group=Frigate',source)


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
            self.assertIn('Welcome to EVE OSINT</h1>',response.text)
            self.assertIn('OSINT website for EVE Online — in development.',response.text)
            self.assertNotIn('Changelog',response.text)
            self.assertIn('href="/login">Log in</a>',response.text)
            self.assertNotIn('Active session',response.text)
            self.assertNotIn('/api/update-status',response.text)
            self.assertEqual(client.get('/login').status_code,200)

    def test_logged_in_public_home_keeps_normal_account_menu(self):
        app=FastAPI();app.include_router(routes.router)
        with tempfile.TemporaryDirectory() as directory,patch.object(public,'ROOT',Path(directory)), \
             patch.object(routes,'require_login',return_value=ADMIN):
            response=TestClient(app).get('/public')
        self.assertIn('My account',response.text)
        self.assertNotIn('href="/login">Log in</a>',response.text)
        self.assertNotIn('Content-Security-Policy',response.headers)


class PublicDataTests(unittest.TestCase):
    setUp=PublicTests.setUp
    tearDown=PublicTests.tearDown
    enable=PublicTests.enable
    def test_parallel_private_data_requests_queue_instead_of_returning_busy_errors(self):
        import asyncio
        from concurrent.futures import ThreadPoolExecutor
        @self.client.app.get('/api/character/42/history')
        async def history():
            await asyncio.sleep(.04)
            return {'ready':True}
        self.enable('character','character:history')
        def fetch(index):
            return self.client.get('/public/read',params={'source':'/character/42?tab=history','path':'/api/character/42/history?part='+str(index)}).status_code
        with patch.object(public,'BUILD_SLOTS',1),ThreadPoolExecutor(max_workers=4) as workers:
            self.assertEqual(list(workers.map(fetch,range(4))),[200]*4)

    def test_data_gate_uses_read_identity_caches_and_revokes_per_tab(self):
        from fastapi import Request
        calls=[]
        @self.client.app.get('/api/character/42/history')
        def history(request:Request):
            user=f.auth.require_login(request)
            self.assertTrue(user['is_public'])
            self.assertNotIn('admin.system.view',user['permissions'])
            calls.append(1)
            return {'history':[{'name':'Old corp'}]}
        self.enable('character','character:history')
        params={'source':'/character/42?tab=history','path':'/api/character/42/history'}
        first=self.client.get('/public/read',params=params)
        self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(first.json()['history'][0]['name'],'Old corp')
        self.assertEqual(self.client.get('/public/read',params=params).status_code,200)
        self.assertEqual(len(calls),1)
        self.assertEqual(self.client.get('/public/read',params=params,headers={'If-None-Match':first.headers['etag']}).status_code,304)
        self.assertTrue(any(row['internal'] for row in public.entries()))
        self.assertNotIn('/_read',self.client.get('/public').text)
        self.enable('character','character:overview')
        self.assertEqual(self.client.get('/public/read',params=params).status_code,404)
        from importlib import import_module
        self.assertIsNone(import_module(f.PACKAGE+'.public_read').current_reader())

    def test_direct_private_data_url_requires_login_and_scope_does_not_leak(self):
        from fastapi import Request
        calls=[]
        @self.client.app.get('/api/character/42/history')
        def history(request:Request):
            f.auth.require_login(request)
            calls.append(1)
            return {'history':[]}
        self.assertEqual(self.client.get('/api/character/42/history').status_code,401)
        self.assertEqual(calls,[])
        self.enable('character','character:history')
        response=self.client.get('/public/read',params={'source':'/character/42?tab=history','path':'/api/character/42/history'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.client.get('/api/character/42/history').status_code,401)
        self.assertEqual(calls,[1])

    def test_read_proxy_never_calls_other_entities_admin_or_post_actions(self):
        self.enable('character','character:history','character:population','character:top-kills')
        source='/character/42?tab=history'
        for path in ('/admin/jobs','/account','https://evil/api/character/42/history',
                     '/api/character/43/history','/api/character/42/population-history-check',
                     '/api/character/42/delete','/api/character/42/history/../../admin'):
            self.assertEqual(self.client.get('/public/read',params={'source':source,'path':path}).status_code,404,path)
        self.assertEqual(self.client.post('/public/read',json={'source':source,'path':'/api/character/42/history'}).status_code,405)

    def test_running_scan_responses_are_not_frozen_in_cache(self):
        calls=[]
        @self.client.app.get('/api/character/42/combat-stats/top-kills')
        def progress():
            calls.append(1)
            return {'status':'running','progress':{'checked':len(calls)}}
        self.enable('character','character:top-kills')
        params={'source':'/character/42?tab=stats&stats_view=top-kills','path':'/api/character/42/combat-stats/top-kills?retry=1'}
        for count in (1,2):
            response=self.client.get('/public/read',params=params)
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['progress']['checked'],count)
            self.assertEqual(response.headers['cache-control'],'no-store')
        self.assertFalse(public.entries())

    def test_recently_accessed_results_survive_lru_eviction(self):
        one,two,three=[public.resolve('/character/'+str(id)) for id in (1,2,3)]
        with patch.object(public,'MAX_ENTRIES',2):
            public.store(one,'one');public.store(two,'two')
            public.load(one)
            # Make the access order deterministic on coarse filesystem clocks.
            import os
            os.utime(public.ROOT/('snapshot-'+one.digest+'.json'),(time.time()+1,time.time()+1))
            public.store(three,'three')
        self.assertIsNotNone(public.load(one))
        self.assertIsNone(public.load(two))
        self.assertIsNotNone(public.load(three))

    def test_get_forms_filters_and_pagination_have_no_20_page_ceiling(self):
        target=public.resolve('/leaderboard?page=21&q=&target_id=&position=&kind=character')
        self.assertEqual(target.source,'/leaderboard?page=21')
        target=public.resolve('/character/42?tab=killboard&page=51&builder_entity_include=both:alliance:1&builder_entity_include=victim:corporation:2')
        self.assertEqual(target.tab,'killboard')
        policy=self.enable('killboard')
        html='<form><select name="participation"><option value="kills">Kills</option></select><button type="submit">Show</button></form><button data-killmail-mode="hidden">Hidden</button>'
        result=public.sanitize(html,policy,target.source,interactive=True)
        self.assertIn('action="/public/character/42"',result)
        self.assertIn('name="participation"',result)
        self.assertIn('mode=hidden',result)
        self.assertIn('>Hidden</a>',result)


if __name__ == '__main__':
    unittest.main()
