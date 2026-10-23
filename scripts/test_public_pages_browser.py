"""Offline browser checks: publication switches, private preview, revoked tabs."""
from urllib.parse import urlsplit
from unittest.mock import patch
from playwright.sync_api import sync_playwright, expect
from scripts.test_public_pages import PublicTests, ADMIN, routes, public, f


def main():
    fixture = PublicTests()
    fixture.setUp()
    renderer = f.load('public_page_renderer')
    errors, api_calls = [], []
    try:
        with patch.object(routes, 'require_login', side_effect=lambda request: ADMIN if request.url.path.startswith('/admin/') else None), \
             patch.object(renderer, 'render', return_value='<h1>ISK rankings</h1><a href="/character/42">Pilot</a><a class="nav-link" href="/character/42?tab=stats">Monthly history</a>'), \
             sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=['--no-sandbox'])
            page = browser.new_page(viewport={'width':1500,'height':1100})
            page.on('pageerror', lambda error: errors.append(str(error)))

            def handle(route):
                request = route.request
                parsed = urlsplit(request.url)
                if parsed.netloc != 'publication.test':
                    route.abort()
                    return
                if parsed.path.startswith('/api/'):
                    api_calls.append(parsed.path)
                response = fixture.client.request(request.method, parsed.path + ('?'+parsed.query if parsed.query else ''),
                    content=request.post_data, headers={'content-type':request.headers.get('content-type',''),
                        'origin':'http://publication.test'}, follow_redirects=True)
                headers = {k:v for k,v in response.headers.items() if k not in ('content-length','content-encoding')}
                route.fulfill(status=response.status_code, headers=headers, body=response.content)

            page.route('**/*', handle)
            page.goto('http://publication.test/admin/public-pages')
            expect(page.get_by_role('heading',name='Public pages',exact=True)).to_be_visible()
            expect(page.locator('input[value="leaderboard"]')).not_to_be_checked()
            expect(page.locator('input[value="modules"]')).to_be_enabled()
            expect(page.locator('input[type="checkbox"]:disabled')).to_have_count(0)
            expect(page.get_by_text('Unfinished entity pages.',exact=True)).to_have_count(3)
            expect(page.get_by_text('Heavy calculations: work starts when a visitor requests a result.',exact=True)).to_be_visible()
            expect(page.locator('input[value="character:top-kills"]')).to_be_enabled()
            expect(page.locator('input[value="character:history"]')).to_be_enabled()
            expect(page.locator('input[value="character:pilotable-ships"]')).to_be_enabled()
            expect(page.locator('input[value="corporation:population"]')).to_be_enabled()
            page.locator('#publicSource').fill('/leaderboard')
            page.get_by_role('button',name='Prepare snapshot',exact=True).click()
            expect(page.locator('#publicPrepareStatus')).to_contain_text('Snapshot ready')
            page.reload()
            expect(page.get_by_text('Private',exact=True)).to_be_visible()
            # Preparation cannot expose even a successfully cached page.
            assert fixture.client.get('/public/leaderboard').status_code == 404
            for value in ('leaderboard','leaderboard:character','character','character:overview','character:stats'):
                page.locator(f'input[value="{value}"]').check()
            page.get_by_role('button',name='Save publication settings').click()
            expect(page.get_by_text('Publication settings saved.')).to_be_visible()
            page.goto('http://publication.test/public/leaderboard')
            expect(page.get_by_role('heading',name='ISK rankings')).to_be_visible()
            expect(page.get_by_role('link',name='Pilot',exact=True)).to_have_attribute('href','/public/character/42')
            expect(page.get_by_role('link',name='Monthly history',exact=True)).to_be_visible()
            page.goto('http://publication.test/admin/public-pages')
            page.locator('input[value="character:stats"]').uncheck()
            page.get_by_role('button',name='Save publication settings').click()
            page.goto('http://publication.test/public/leaderboard')
            expect(page.get_by_role('link',name='Monthly history',exact=True)).to_have_count(0)
            expect(page.get_by_role('link',name='Pilot',exact=True)).to_be_visible()
            assert fixture.client.get('/public/character/42?tab=stats').status_code == 404
            page.goto('http://publication.test/admin/public-pages')
            page.screenshot(path='/tmp/eveosint-public-pages-admin.png',full_page=True)
            # The normal grouped menu search also works without an account.
            @fixture.client.app.get('/api/entity-search')
            def public_search_results():
                return {'results':[{'url':'/character/42','label':'Public Pilot','entity_type':'character',
                                    'section':'CHARACTERS','image_url':'https://images.evetech.net/characters/42/portrait?size=64'}]}
            page.goto('http://publication.test/admin/public-pages')
            expect(page.get_by_role('checkbox',name='Killboard',exact=True)).to_have_count(1)
            page.get_by_role('checkbox',name='Killboard',exact=True).check()
            page.get_by_role('button',name='Save publication settings').click()
            page.goto('http://publication.test/public')
            search=page.get_by_role('searchbox',name='Search entity',exact=True)
            expect(search).to_be_visible()
            search.fill('Public')
            result=page.locator('[data-entity-search-results] a').filter(has_text='Public Pilot')
            expect(result).to_have_attribute('href','/public/character/42?tab=killboard')
            expect(result.locator('img')).to_have_count(1)
            expect(page.locator('[data-entity-search-results]')).to_contain_text('CHARACTERS')
            search.press('Enter')
            expect(page).to_have_url('http://publication.test/public/character/42?tab=killboard')
            expect(page.get_by_role('link',name='Killboard',exact=True)).to_have_count(2)
            fixture.enable()
            page.goto('http://publication.test/public')
            expect(page.get_by_role('searchbox',name='Search entity',exact=True)).to_have_count(0)
            browser.close()
        assert not errors, errors
        # Authenticated chrome polls update status; anonymous menu searches use
        # only the public gateway, never a direct private API request.
        assert all(path == '/api/update-status' for path in api_calls), api_calls
        print('Chromium: prepare, publication, tab revocation and direct URL blocking passed.')
    finally:
        fixture.tearDown()





def navigation():
    from fastapi import Request
    from fastapi.responses import HTMLResponse
    fixture=PublicTests();fixture.setUp()
    renderer=f.load('public_page_renderer')
    requests=[];errors=[];calls=[]
    profile=dict(entity_type='character',entity_id=42,name='Test Pilot',subtitle='',ticker=None,
                 image_url='',url='/character/42',current_corporation=None,current_alliance=None)
    context=dict(profile=profile,profile_tab='killboard',user=None,location_previews={},
                 killmail_ship_options=[],killmail_ship_options_error=None,killmail_page=None,killmail_mode='api')
    html=renderer.content('entity_profile.html',context)
    rows=f.page_fixture(limit=1)
    @fixture.client.app.get('/api/character/42/killmails')
    def killmails(request:Request):
        params=dict(request.query_params);calls.append(params)
        number=int(params.get('page','1'))
        result=dict(rows,killmail_pagination=dict(page=number,per_page=1,has_prev=number>1,
            has_next=number<2,prev_page=max(1,number-1),next_page=min(2,number+1)),killmail_participation='both')
        return HTMLResponse(f.templates.env.get_template('entity_killmails_fragment.html').render(killmail_page=result,character_id=42))
    public.save_settings({'killboard','character','character:overview'})
    try:
      with patch.object(renderer,'render',return_value=html),sync_playwright() as pw:
        browser=pw.chromium.launch(args=['--no-sandbox']);page=browser.new_page()
        page.on('pageerror',lambda e:errors.append(str(e)))
        def handle(route):
            request=route.request;url=urlsplit(request.url)
            if url.netloc!='publication.test':route.abort();return
            requests.append(url.path)
            response=fixture.client.request(request.method,url.path+('?' +url.query if url.query else ''),
                content=request.post_data,headers={'content-type':request.headers.get('content-type','')},follow_redirects=True)
            route.fulfill(status=response.status_code,body=response.content,headers={k:v for k,v in response.headers.items() if k not in ('content-length','content-encoding')})
        page.route('**/*',handle)
        page.goto('http://publication.test/public/character/42?tab=killboard')
        page.wait_for_timeout(500)
        expect(page.locator('.killmail-table tbody tr.killmail-row-loss')).to_have_count(1)
        page.locator('[data-killmail-page="2"]').first.click()
        expect(page.locator('[data-killmail-page="1"]').first).to_be_visible()
        assert any(c.get('page')=='2' for c in calls),calls
        page.locator('[data-killmail-page="1"]').first.click()
        expect(page.locator('[data-killmail-page="2"]').first).to_be_visible()
        assert len(calls)==2,calls
        page.locator('[data-kb-toggle]').click()
        page.locator('[data-kb-zone-preset="highsec"]').click()
        page.locator('[data-kb-apply]').click()
        expect(page.locator('.killmail-table tbody tr.killmail-row-loss')).to_have_count(1)
        page.wait_for_timeout(250)
        assert any('security:highsec' in c.get('builder_zone_include','') for c in calls),calls
        assert not any(path.startswith('/api/') for path in requests),requests
        assert not errors,errors
        browser.close()
      print('Chromium: shared entity KB, page 2/back, filters and read-cache reuse passed.')
    finally:fixture.tearDown()





def browser_cache():
    import socket
    import threading
    import time
    import uvicorn
    from fastapi.responses import HTMLResponse
    fixture=PublicTests();fixture.setUp()
    app=fixture.client.app;network=[];queries=[]
    @app.middleware('http')
    async def count_reads(request,call_next):
        if request.url.path=='/public/read':network.append(1)
        return await call_next(request)
    @app.get('/')
    def entry():return HTMLResponse('<!doctype html><title>Cache test</title>Cache test')
    @app.get('/api/character/42/history')
    def history():queries.append(1);return {'rows':['Corporation history']}
    public.save_settings({'character','character:history'})
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
    thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
    try:
      deadline=time.monotonic()+10
      while not server.started and time.monotonic()<deadline:time.sleep(.02)
      assert server.started,'Fixture server did not start'
      with sync_playwright() as pw:
        browser=pw.chromium.launch(args=['--no-sandbox']);page=browser.new_page()
        # No request interception here: real Chromium HTTP cache is enabled.
        page.goto('http://127.0.0.1:'+str(port)+'/')
        result=page.evaluate("""async()=>{
          const url='/public/read?'+new URLSearchParams({source:'/character/42?tab=history',path:'/api/character/42/history'});
          const one=await fetch(url);await one.json();
          const two=await fetch(url);await two.json();
          return {cache:two.headers.get('cache-control'),etag:two.headers.get('etag')};
        }""")
        assert len(network)==1,network
        assert len(queries)==1,queries
        assert 'max-age=60' in result['cache'] and result['etag'],result
        page.evaluate("""async()=>{
          const url='/public/read?'+new URLSearchParams({source:'/character/42?tab=history',path:'/api/character/42/history'});
          const response=await fetch(url,{cache:'no-cache'});return response.json();
        }""")
        assert len(network)==2,network
        assert len(queries)==1,queries
        browser.close()
      print('Chromium native HTTP cache: second read stays in browser; revalidation reuses server result.')
    finally:
      server.should_exit=True;thread.join(5);sock.close();fixture.tearDown()


if __name__ == '__main__':
    main()
    navigation()
    browser_cache()
