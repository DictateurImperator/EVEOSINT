"""Offline economy controls, SVG/canvas, missing data and animation regression."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader
from playwright.sync_api import expect, sync_playwright

from scripts.test_map_economy import FEB, JAN, REGIONS, module, row

ROOT = Path(__file__).resolve().parents[1]


def main():
    env = Environment(loader=ChoiceLoader([DictLoader({'base.html': '<html><head><meta charset="utf-8"></head><body>{% block content %}{% endblock %}</body></html>'}), FileSystemLoader(ROOT / 'web/templates')]))
    payload = {'breadcrumbs': [], 'subtitle': 'Offline economy', 'nodes': [
        {'id': 30000001, 'name': 'One', 'x': 0, 'y': 0, 'region_id': 10000001, 'region_name': 'Derelik', 'security': .5},
        {'id': 30000002, 'name': 'Two', 'x': 100, 'y': 100, 'region_id': 10000002, 'region_name': 'The Forge', 'security': .5}], 'edges': []}
    rows = [row(JAN,'production_isk','100'),row(JAN,'mining_isk','50'),row(JAN,'trade_isk','20'),
            row(FEB,'production_isk','300'),row(FEB,'mining_isk','30'),row(FEB,'trade_isk','60'),
            row(JAN,'production_isk','0',10000002),row(JAN,'mining_isk','5',10000002),row(JAN,'trade_isk','0',10000002),
            row(FEB,'production_isk','0',10000002),row(FEB,'mining_isk','10',10000002),row(FEB,'trade_isk','0',10000002),
            row(JAN,'mining_isk','100',None,'space_group','Wormhole')]
    html = env.get_template('map_eve_2d.html').render(map_payload=payload,map_data_json=json.dumps(payload))
    errors, calls = [], []
    bad = False
    metrics = [{'key':k,'label':module.METRICS[k][0],'color':module.METRICS[k][1]} for k in ['mining_isk','trade_isk']]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=['--no-sandbox'])
        page = browser.new_page(viewport={'width':1600,'height':1000})
        page.on('pageerror',lambda error:errors.append(str(error)))
        def handle(route):
            parts = urlsplit(route.request.url)
            if parts.netloc != 'economy.test': route.abort(); return
            if parts.path.endswith('/economy/options'):
                route.fulfill(json={'months':['2026-01','2026-02'],'metrics':metrics}); return
            if parts.path.endswith('/economy'):
                calls.append(parts.query)
                if bad: route.fulfill(status=502,content_type='text/html',body='Bad gateway'); return
                q=parse_qs(parts.query); start,end=module.month(q['from'][0]),module.month(q['to'][0])
                selected=q.get('metric',['mining_isk']); months=module.month_range(start,end)
                frames, unplaced=module.build_frames([r for r in rows if start<=r[0]<=end], REGIONS, months, selected, q['evolution'][0]=='true')
                route.fulfill(json={'from':q['from'][0],'to':q['to'][0],'frames':frames,'unplaced_scopes':unplaced,'metrics':[m for m in metrics if m['key'] in selected]}); return
            route.fulfill(content_type='text/html',body=html)
        page.route('**/*',handle)
        page.goto('http://economy.test/map/eve-2d')
        page.locator('[data-eve2d-mode="economy"]').click()
        expect(page.locator('#eve2dEconomyStatus')).to_contain_text('2026-02')
        lights=page.locator('[data-economic-region]')
        expect(lights).to_have_count(2)
        first=page.locator('[data-economic-region="10000001"]')
        second=page.locator('[data-economic-region="10000002"]')
        expect(page.locator('#eve2dEconomyBasis')).to_have_value('absolute')
        expect(page.locator('#eve2dRankingPanel')).to_be_visible()
        expect(page.locator('#eve2dRankingTitle')).to_have_text('Economic ranking')
        ranks = page.locator('#eve2dRankingList .eve2d-ranking-row')
        expect(ranks).to_have_count(2)
        expect(ranks.first.locator('a')).to_have_text('Derelik')
        assert ranks.first.get_attribute('data-ranking-value') == '30'
        assert ranks.first.locator('a').get_attribute('href') == '/map/region/10000001'
        assert first.get_attribute('data-economic-value') == '30'
        assert second.get_attribute('data-economic-value') == '10'
        page.locator('#eve2dEconomyBasis').select_option('relative')
        expect(first.locator('title')).to_contain_text('10% of production')
        expect(ranks).to_have_count(1)
        expect(page.locator('#eve2dRankingMeta')).to_contain_text('% of regional production')
        expect(second.locator('title')).to_contain_text('Production is zero')
        assert first.get_attribute('href')=='/map/region/10000001'
        assert second.get_attribute('data-economic-value') is None
        page.locator('#eve2dEconomyFrom').fill('2026-01')
        page.locator('#eve2dEconomyFrom').dispatch_event('change')
        expect(first.locator('title')).to_contain_text('20% of production')
        expect(page.locator('#eve2dEconomyStatus')).to_contain_text('Wormhole')
        page.locator('#eve2dEconomyMetrics input[value="trade_isk"]').check()
        expect(lights).to_have_count(4)
        expect(page.locator('#eve2dRankingMetric')).to_be_visible()
        page.locator('#eve2dRankingMetric').select_option('trade_isk')
        expect(page.locator('#eve2dRankingMeta')).to_contain_text('Trade')
        page.locator('#eve2dEconomyCombined').check()
        expect(lights).to_have_count(2)
        expect(first.locator('title')).to_contain_text('40% of production')
        expect(page.locator('#eve2dRankingMetric')).to_be_hidden()
        assert ranks.first.get_attribute('data-ranking-value') == '0.4'
        page.locator('#eve2dEconomyMode').select_option('animated')
        expect(page.locator('#eve2dEconomyStatus')).to_contain_text('2026-01')
        expect(first.locator('title')).to_contain_text('70% of production')
        assert ranks.first.get_attribute('data-ranking-value') == '0.7'
        assert float(first.get_attribute('data-economic-intensity'))==1
        page.evaluate("document.querySelector('#eve2dStaticHost svg').testIdentity = true")
        page.locator('#eve2dEconomyFrame').evaluate("el => el.value = '1'")
        page.locator('#eve2dEconomyFrame').dispatch_event('input')
        expect(page.locator('#eve2dEconomyStatus')).to_contain_text('2026-02')
        assert abs(float(first.get_attribute('data-economic-intensity'))-3/7)<1e-9
        assert ranks.first.get_attribute('data-ranking-value') == '0.3'
        expect(page.locator('#eve2dRankingMeta')).to_contain_text('2026-02')
        assert page.evaluate("document.querySelector('#eve2dStaticHost svg').testIdentity")
        expect(page.locator('#eve2dEconomyLegend')).to_contain_text('70% of production')
        page.locator('#eve2dEconomyBasis').select_option('absolute')
        assert second.get_attribute('data-economic-value')=='10'
        page.locator('#eve2dInteractive').check()
        expect(page.locator('#eve2dCanvas')).to_be_visible()
        point = page.locator('#eve2dCanvas').evaluate('''el => {
          const r = el.getBoundingClientRect();
          const scale = Math.max(.05, Math.min((r.width-96)/3960, (r.height-96)/1800));
          return {x:r.left+(r.width-3960*scale)/2+1800*scale, y:r.top+(r.height-1800*scale)/2};
        }''')
        page.mouse.move(point['x'],point['y'])
        expect(page.locator('#eve2dTooltip')).to_contain_text('The Forge')
        expect(page.locator('#eve2dTooltip')).to_contain_text('Regional production: 0 ISK')
        page.locator('#eve2dEconomyPlay').click()
        expect(page.locator('#eve2dEconomyPlay')).to_have_text('Pause')
        page.locator('[data-eve2d-mode="systems"]').click()
        expect(page.locator('#eve2dEconomyPlay')).to_have_text('Play')
        page.locator('#eve2dInteractive').uncheck()
        expect(lights).to_have_count(0)
        expect(page.locator('#eve2dRankingPanel')).to_be_hidden()
        page.locator('[data-eve2d-mode="economy"]').click()
        expect(lights).to_have_count(2)
        bad=True
        page.locator('#eve2dEconomyApply').click()
        expect(page.locator('#eve2dEconomyStatus')).to_contain_text('Economic data unavailable')
        expect(ranks).to_have_count(0)
        expect(lights).to_have_count(0)
        bad=False
        page.locator('#eve2dEconomyApply').click()
        expect(lights).to_have_count(2)
        assert not errors,errors
        assert calls,calls
        browser.close()
    print('Economic map browser checks passed.')


if __name__=='__main__': main()
