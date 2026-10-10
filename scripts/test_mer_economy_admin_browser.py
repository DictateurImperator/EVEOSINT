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
    errors = []
    with patch.object(routes, 'require_login', return_value=user), patch.object(routes, 'list_mer_catalog', return_value=[]), patch.object(routes, 'get_mer_catalog_summary', return_value={}), patch.object(routes, 'read_mer_economy_progress', side_effect=lambda: state), patch.object(routes, 'run_mer_economy_import_job') as launch, sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=['--no-sandbox'])
        page = browser.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        def handle(route):
            parts = urlsplit(route.request.url)
            if parts.netloc != 'mer.test':
                route.abort(); return
            response = client.get(parts.path)
            route.fulfill(status=response.status_code, content_type=response.headers.get('content-type', 'text/html'), body=response.content)
        page.route('**/*', handle)
        page.goto('http://mer.test/admin/mer')
        expect(page.locator('#economy-import-status')).to_contain_text('Archives: 3 / 127')
        expect(page.locator('#economy-import-button')).to_be_disabled()
        expect(page.locator('#economy-import-status')).to_contain_text('Current report: 2026-08')
        state['running'] = False
        state['progress']['phase'] = 'completed'
        expect(page.locator('#economy-import-status')).to_contain_text('Completed', timeout=6000)
        expect(page.locator('#economy-import-button')).to_be_enabled()
        launch.assert_not_called()
        assert not errors, errors
        browser.close()
    print('MER economic import browser checks passed.')


if __name__ == '__main__':
    main()
