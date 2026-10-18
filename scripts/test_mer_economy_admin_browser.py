"""Offline browser verification of the MER import panel."""
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from scripts.test_mer_economy_admin import routes


def main():
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)
    user = {'id': 1, 'username': 'admin', 'permissions': {'admin.mer.view'}}
    state = {'running': True, 'progress': {'phase': 'running', 'total': 127, 'processed': 3, 'skipped': 1, 'failed': 0, 'facts': 1000, 'month': '2026-08-01'}}
    operations={'operations':[{'key':'MER_ALL','label':'MER ALL','status':'not_started'}, {'key':'KILL_MATCH','label':'Match existing killmails','status':'running','total':2,'processed':1,'current':'killmails_2026_09','waiting_for_lock':True,'waiting_for':['Historical alliance IDs'],'metrics':{'unique':15,'unresolved':4}}], 'log':'<script>window.bad=true</script> database update'}
    def launch_all():
        operations['operations'][0].update(status='running',total=7,processed=4,current='Economic data import')
        return True
    errors = []
    with patch.object(routes, 'require_login', return_value=user), patch.object(routes, 'list_mer_catalog', return_value=[]), patch.object(routes, 'get_mer_catalog_summary', return_value={}), patch.object(routes, 'read_mer_economy_progress', side_effect=lambda: state), patch.object(routes, 'run_mer_economy_import_job') as launch, patch.object(routes.mer_progress,'snapshot',side_effect=lambda:operations), patch.object(routes.mer_progress,'log_tail',return_value='<script>window.bad=true</script> database update'), patch.object(routes.mer_pipeline,'start',side_effect=launch_all) as all_launch, patch.object(routes,'audit_log'), sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=['--no-sandbox'])
        page = browser.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        def handle(route):
            parts = urlsplit(route.request.url)
            if parts.netloc != 'mer.test':
                route.abort(); return
            response = client.request(route.request.method,parts.path+('?' + parts.query if parts.query else ''),follow_redirects=False)
            if response.status_code in (302,303):
                response=client.get(response.headers['location'])
            route.fulfill(status=response.status_code, content_type=response.headers.get('content-type', 'text/html'), body=response.content)
        page.route('**/*', handle)
        page.goto('http://mer.test/admin/mer')
        expect(page.locator('#economy-import-status')).to_contain_text('Archives: 3 / 127')
        expect(page.locator('#economy-import-button')).to_be_disabled()
        expect(page.locator('#economy-import-status')).to_contain_text('Current report: 2026-08')
        expect(page.locator('#merOperations')).to_contain_text('Waiting for database lock')
        expect(page.locator('#merOperations')).to_contain_text('Historical alliance IDs')
        expect(page.locator('#merOperations')).to_contain_text('Unique matches: 15')
        assert page.evaluate('window.bad') is None
        operations['operations'][1]['status']='completed';operations['operations'][1]['waiting_for_lock']=False
        state['running'] = False
        state['progress']['phase'] = 'completed'
        expect(page.locator('#economy-import-status')).to_contain_text('Completed', timeout=6000)
        expect(page.locator('#economy-import-button')).to_be_enabled()
        launch.assert_not_called();all_launch.assert_not_called()
        page.get_by_role('button',name='MER ALL',exact=True).click()
        expect(page.locator('[data-mer-operation=MER_ALL]')).to_contain_text('Steps completed: 4 / 7')
        expect(page.locator('#mer-all-button')).to_be_disabled()
        operations['operations'][0].update(status='completed',processed=7,finished_at='2026-10-18T20:00:00+00:00')
        expect(page.locator('[data-mer-operation=MER_ALL]')).to_contain_text('Completed',timeout=6000)
        expect(page.locator('#mer-all-button')).to_be_enabled();all_launch.assert_called_once()
        assert not errors, errors
        browser.close()
    print('MER economic import browser checks passed.')


if __name__ == '__main__':
    main()
