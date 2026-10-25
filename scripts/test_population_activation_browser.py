"""Verify daily chart batching and CSV on the shared entity templates, offline."""
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    env = Environment(loader=FileSystemLoader(ROOT/'web/templates'))
    dates = [(date(2026, 9, 1) + timedelta(days=i)).isoformat() for i in range(33)]
    with sync_playwright() as p:
        browser = p.chromium.launch(args=['--no-sandbox'])
        for kind in ['alliance', 'corporation', 'coalition']:
            html = '<meta charset="utf-8">' + env.get_template('coalition_population.html' if kind == 'coalition' else 'entity_population.html').render(profile={'entity_type': kind, 'entity_id': 1, 'name': 'Example'})
            page = browser.new_page(); errors = []; calls = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.add_init_script('window.eveosintPublicError=e=>e.message || String(e)')
            def handle(route):
                url = urlsplit(route.request.url)
                if url.path.endswith('/population'):
                    route.fulfill(json={'initialization_done': True, 'rows': [{'date': d, 'member_count': 100, 'corporation_count': 2, 'sovereignty_count': 1} for d in dates]})
                elif url.path.endswith('/population-intelligence/group'):
                    query = parse_qs(url.query); requested = query['dates'][0].split(',')
                    if query['group'][0] == 'first_pvp': calls.append((query['mode'][0], requested))
                    route.fulfill(json={'rows': [{'date': d, 'median_activation_delay_days': 7.352025} for d in requested]})
                elif url.path.startswith('/api/'):
                    route.fulfill(json={'initialization_done': True, 'rows': [], 'dates': [], 'events': [], 'corporations': []})
                else:
                    route.fulfill(content_type='text/html', body=html)
            page.route('**/*', handle)
            page.goto(f'http://activation.test/{kind}/1?tab=population')
            expect(page.locator('#pi-activity-loading')).to_be_hidden()
            page.locator('[data-pi-mode="attacker"]').click()
            expect(page.locator('#pi-activity-loading')).to_be_hidden()
            calls.clear()
            page.locator('#pi-chart-metric').select_option('median_activation_delay_days')
            expect(page.locator('#pi-series-loading')).to_be_hidden()
            assert [len(batch) for mode, batch in calls] == [16, 16, 1], calls
            assert all(mode == 'attacker' for mode, batch in calls), calls
            assert [d for mode, batch in calls for d in batch] == dates, calls
            expect(page.locator('#pi-chart-context')).to_contain_text('Attacker only')
            expect(page.locator('#pi-series-error')).to_be_hidden()
            expect(page.locator('#pi-chart path')).to_have_count(1)
            page.locator('#pi-show-csv').click()
            csv = page.locator('#pi-csv').inner_text()
            for d in dates:
                assert d in csv, (kind, d, csv)
            assert not errors, errors
            print(kind, '33 daily points, 3 requests, attacker mode, CSV: OK')
            page.close()
        browser.close()


if __name__ == '__main__':
    main()
