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
            expect(page.locator('input[value="modules"]')).to_be_disabled()
            expect(page.locator('input[value="character:top-kills"]')).to_be_disabled()
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
            browser.close()
        assert not errors, errors
        # Authenticated page chrome can poll update status; public pages have no
        # scripts at all (also covered by the HTTP tests).
        assert all(path == '/api/update-status' for path in api_calls), api_calls
        print('Chromium: prepare, publication, tab revocation and direct URL blocking passed.')
    finally:
        fixture.tearDown()


if __name__ == '__main__':
    main()
