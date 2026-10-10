"""Exercise actual alliance/coalition tab navigation and economic rendering offline."""
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import expect, sync_playwright

ROOT=Path(__file__).resolve().parents[1]


def main():
    env=Environment(loader=FileSystemLoader(ROOT/'web/templates'))
    payload={'available':True,'coverage':['2026-08'],'economic_days':31,'activity_days':31,
             'denominators':{'member':'150.5','active':5,'kill':10,'loss':0},
             'rows':[{'label':'Mining','total':'3100123456789.123456','monthly_average':'3100000000','daily_average':'100000000',
                      'ratios':{'member':'20.59','active':'620','kill':'3100000000','loss':None}}],
             'regions':[{'month':'2026-08','region':'Delve','owned_average':'8','total_average':'100','share':'.08'}],
             'activity_coverage':[{'from':'2026-08-01','through':'2026-08-31'}]}
    with sync_playwright() as p:
        browser=p.chromium.launch(args=['--no-sandbox'])
        for kind in ['alliance','coalition','corporation']:
            html='<meta charset="utf-8">'+env.get_template('coalition_population.html' if kind=='coalition' else 'entity_population.html').render(profile={'entity_type':kind,'entity_id':1,'name':'Example'})
            page=browser.new_page();errors=[];calls=[];available=True;fail=False;fail_chart=False
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.add_init_script("window.eveosintPublicError=e=>e.message || String(e)")
            def handle(route):
                path=urlsplit(route.request.url).path
                if path.endswith('/population-economics/options'):
                    route.fulfill(json={'available':available,'months':[f'{2025+i//12}-{i%12+1:02d}' for i in range(20)] if available else []})
                elif path.endswith('/population-economics/evolution'):
                    q=parse_qs(urlsplit(route.request.url).query);a=q['from'][0];b=q['to'][0]
                    start=int(a[:4])*12+int(a[5:])-1;end=int(b[:4])*12+int(b[5:])-1
                    offset=int(q['offset'][0]);
                    if fail_chart and offset>0:route.fulfill(status=500,json={'error':'Test chart error'});return
                    months=[f'{i//12}-{i%12+1:02d}' for i in range(start,end+1)]
                    points=[{'month':m,'denominator':'10','values':{key:None if m=='2026-05' else str(1000000000+i*100000000) for key in ['npc_bounties_isk','mining_isk','production_isk']}} for i,m in enumerate(months)]
                    size=1 if q['basis'][0] in ['active','loss','kill'] else 12
                    route.fulfill(json={'points':points[offset:offset+size],'next_offset':offset+size if offset+size<len(points) else None,'total_months':len(points)})
                elif path.endswith('/population-economics'):
                    calls.append(route.request.url)
                    if fail:route.fulfill(status=500,json={'error':'Test error'})
                    else:
                        q=parse_qs(urlsplit(route.request.url).query);a=q['from'][0];b=q['to'][0]
                        start=int(a[:4])*12+int(a[5:])-1;end=int(b[:4])*12+int(b[5:])-1
                        month_rows=[{'month':f'{i//12}-{i%12+1:02d}', 'rows':payload['rows'], 'denominators':payload['denominators'],'economic_days':31,'activity_days':31,'activity_coverage':payload['activity_coverage']} for i in range(start,end+1)]
                        route.fulfill(json={**payload,'months':month_rows,'next_offset':None})
                elif path.startswith('/api/'):
                    route.fulfill(json={'initialization_done':True,'rows':[],'dates':[],'events':[],'corporations':[]})
                else:route.fulfill(content_type='text/html',body=html)
            page.route('**/*',handle)
            page.goto('http://economics.test/'+kind+'/1?tab=population&population_view=economics')
            if kind=='corporation':
                expect(page.locator('[data-population-subtab="economics"]')).to_have_count(0)
                assert not calls
                page.close();continue
            button=page.locator('[data-population-subtab="economics"]')
            expect(button).to_be_visible();expect(page.locator('#population-economics')).to_be_visible()
            expect(page.locator('#population-global-metrics')).to_be_hidden()
            expect(page.locator('#pe-month-counts')).to_contain_text('150.5')
            expect(page.locator('#pe-rows')).to_contain_text('620')
            money=page.locator('#pe-rows td')
            expect(money.nth(2)).to_have_text('3.1 T')
            expect(money.nth(2)).to_have_attribute('title','3,100,123,456,789.123456 ISK')
            expect(money.nth(7)).to_have_text('3.1 B')
            expect(money.nth(3)).to_have_text('100 M')
            expect(page.locator('#pe-chart-status')).to_contain_text('12 months')
            chart=page.locator('#pe-chart');expect(chart.locator('path')).to_have_count(1)
            # Missing May must interrupt the line rather than interpolate across it.
            assert chart.locator('path').get_attribute('d').count('M')==2
            chart.scroll_into_view_if_needed();rect=chart.bounding_box();page.mouse.move(rect['x']+rect['width']*.3,rect['y']+rect['height']*.4)
            expect(page.locator('#pe-chart-tooltip')).to_be_visible()
            expect(page.locator('#pe-chart-tooltip')).to_contain_text('ISK')
            end_before=int(chart.get_attribute('data-view-end'));page.mouse.wheel(0,-200)
            expect(chart).not_to_have_attribute('data-view-end',str(end_before))
            start_before=int(chart.get_attribute('data-view-start'));page.mouse.move(rect['x']+rect['width']*.5,rect['y']+rect['height']*.5);page.mouse.down();page.mouse.move(rect['x']+rect['width']*.2,rect['y']+rect['height']*.5);page.mouse.up()
            assert int(chart.get_attribute('data-view-start'))>=start_before
            page.locator('#pe-chart-reset').click();expect(chart).to_have_attribute('data-view-start','0');expect(chart).to_have_attribute('data-view-end','11')
            page.locator('#pe-chart-metric').select_option('mining_isk');expect(chart.locator('path')).to_have_attribute('stroke','#4ade80')
            with page.expect_download() as download:page.locator('#pe-chart-csv').click()
            assert 'economics.csv' in download.value.suggested_filename
            page.locator('#pe-chart-from').select_option('2025-01');page.locator('#pe-chart-load').click();expect(page.locator('#pe-chart-status')).to_contain_text('20 months')
            fail_chart=True;page.locator('#pe-chart-load').click();expect(page.locator('#pe-chart-status')).to_contain_text('Loaded 12 / 20 months. Test chart error');expect(chart.locator('path')).to_have_count(1)
            fail_chart=False
            page.locator('#pe-chart-basis').select_option('active');expect(page.locator('#pe-chart-status')).to_contain_text('Per active PvP pilot')
            expect(page.locator('#pe-regions')).to_contain_text('8%')
            expect(page.locator('#pe-coverage')).to_contain_text('2026-08-31')
            page.locator('[data-population-subtab="flows"]').click()
            expect(page.locator('#population-economics')).to_be_hidden()
            button.click();expect(page.locator('#population-flows')).to_be_hidden()
            page.locator('#pe-from').select_option('2026-07');page.locator('#pe-window').fill('60');page.locator('#pe-apply').click()
            expect(page.locator('#pe-status')).to_contain_text('MER months')
            expect(page.locator('#pe-month-counts tr')).to_have_count(2)
            expect(page.locator('#pe-rows tr').first).to_contain_text('2026-07')
            expect(page.locator('#pe-rows tr').last).to_contain_text('2026-08')
            assert 'window=60' in calls[-1] and 'from=2026-07' in calls[-1]
            fail=True;page.locator('#pe-apply').click()
            expect(page.locator('#pe-status')).to_have_text('Test error')
            expect(page.locator('#pe-rows tr')).to_have_count(0)
            page.locator('[data-population-subtab="metrics"]').click()
            expect(page.locator('#population-economics')).to_be_hidden()
            available=False;page.reload();expect(button).to_be_hidden()
            expect(page.locator('#population-global-metrics')).to_be_visible()
            assert not errors,errors
            page.close()
        browser.close()
    print('Alliance/coalition Economics navigation, averages, filters, errors and availability: passed')


if __name__=='__main__':main()
