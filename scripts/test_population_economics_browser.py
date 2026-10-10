"""Exercise actual alliance/coalition tab navigation and economic rendering offline."""
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import expect, sync_playwright

from scripts.test_population_economics import module

ROOT=Path(__file__).resolve().parents[1]


def main():
    env=Environment(loader=FileSystemLoader(ROOT/'web/templates'))
    payload={'available':True,'coverage':['2026-08'],'economic_days':31,'activity_days':31,
             'denominators':{'member':'150.5','active':5,'kill':10,'loss':0},
             'rows':[{'label':'Mining','total':'3100123456789.123456','monthly_average':'3100000000','daily_average':'100000000',
                      'ratios':{'member':'20.59','active':'620000000','kill':'3100000000','loss':None}}],
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
                    route.fulfill(json={'available':available,'months':[f'{2025+i//12}-{i%12+1:02d}' for i in range(20)] if available else [],'purchasing_power_months':[f'{2025+i//12}-{i%12+1:02d}' for i in range(20)]})
                elif path.endswith('/population-economics/evolution'):
                    q=parse_qs(urlsplit(route.request.url).query);a=q['from'][0];b=q['to'][0]
                    start=int(a[:4])*12+int(a[5:])-1;end=int(b[:4])*12+int(b[5:])-1
                    offset=int(q['offset'][0]);
                    if fail_chart and offset>0:route.fulfill(status=500,json={'error':'Test chart error'});return
                    months=[f'{i//12}-{i%12+1:02d}' for i in range(start,end+1)]
                    points=[{'month':m,'denominator':'10','values':{key:None if m=='2026-05' else str(1000000000+i*100000000) for key in ['npc_bounties_isk','mining_isk','production_isk','npc_bounties_ppa_isk','mining_ppa_isk','production_ppa_isk','isk_purchasing_power_index','consumer_price_index_relative_index']}} for i,m in enumerate(months)]
                    size=1 if q['basis'][0] in ['active','loss','kill'] else 12
                    route.fulfill(json={'points':points[offset:offset+size],'next_offset':offset+size if offset+size<len(points) else None,'total_months':len(points)})
                elif path.endswith('/population-economics/best-month'):
                    q=parse_qs(urlsplit(route.request.url).query);offset=int(q['offset'][0]);size=1 if q['basis'][0]=='active' else 12
                    records=[{'month':f'{2025+i//12}-{i%12+1:02d}','value':f'10000000000000000.{i:02d}'} for i in range(offset,min(offset+size,20))]
                    ranked=list(reversed(records))
                    route.fulfill(json={'ranked_months':ranked,'best':ranked[0] if ranked else None,'checked':len(records),
                                        'available':len(records),'total_months':20,'next_offset':offset+len(records) if offset+len(records)<20 else None})
                elif path.endswith('/population-economics/comparison'):
                    calls.append(route.request.url)
                    if fail:route.fulfill(status=500,json={'error':'Test error'})
                    else:
                        q=parse_qs(urlsplit(route.request.url).query)
                        values={'total':module.change('3100123456789.123456','3000000000000'),
                                'member':module.change('20.59','30'), 'active':module.change('620000000','600000000'),
                                'loss':module.change(None,'1'),'kill':module.change('3100000000','3100000000')}
                        denoms={key:module.change(value,'100') for key,value in payload['denominators'].items()}
                        def measurement(m):return {'month':m,'activity_days':31,'activity_coverage':payload['activity_coverage']}
                        route.fulfill(json={'base_month':q['base'][0],'observed_month':q['observed'][0],
                                            'rows':[{'label':'Mining','values':values},{'label':'Mining · PPA','values':values}], 'denominators':{k:v for k,v in denoms.items() if k in ['member','active']},
                                            'purchasing_power':{'reference':q['reference'][0],'reference_cpi':'110','price_index':module.change('110','100'),'isk_power':module.change('90','100')},
                                            'base':measurement(q['base'][0]),'observed':measurement(q['observed'][0]),'regions':payload['regions']})
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
            expect(page.locator('#pe-from')).to_have_value('2026-07')
            expect(page.locator('#pe-to')).to_have_value('2026-08')
            expect(money.nth(1).locator('span').first).to_have_text('3.1 T')
            expect(money.nth(1).locator('span').first).to_have_attribute('title','3,100,123,456,789.123456 ISK')
            expect(money.nth(1).locator('.pe-change')).to_contain_text('(+100.12 B')
            expect(money.nth(1).locator('.increase')).to_have_count(1)
            expect(money.nth(2).locator('.decrease')).to_have_count(1)
            expect(money.nth(3).locator('span').first).to_have_text('620 M')
            expect(page.locator('#pe-chart-basis option[value="daily"]')).to_have_count(0)
            expect(page.locator('#pe-chart-basis option[value="loss"]')).to_have_count(0)
            expect(page.locator('#pe-chart-basis option[value="kill"]')).to_have_count(0)
            expect(page.locator('#pe-reference')).to_have_value('2026-08')
            expect(page.locator('#pe-ppa-rows')).to_contain_text('ISK purchasing power')
            expect(page.locator('#pe-rows')).to_contain_text('Mining · PPA')
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
            page.locator('#pe-chart-metric').select_option('mining_ppa_isk');expect(chart.locator('path')).to_have_attribute('stroke','#86efac')
            page.locator('#pe-reference').select_option('2026-01');expect(page.locator('#pe-ppa-status')).to_contain_text('PPA reference: 2026-01')
            assert 'reference=2026-01' in calls[-1]
            page.locator('#pe-chart-metric').select_option('isk_purchasing_power_index');expect(page.locator('#pe-chart-basis')).to_be_disabled();expect(chart.locator('path')).to_have_attribute('stroke','#fbbf24');expect(page.locator('#pe-chart-status')).to_contain_text('Index (reference 2026-01 = 100)')
            page.locator('#pe-chart-metric').select_option('mining_ppa_isk');expect(page.locator('#pe-chart-basis')).to_be_enabled()
            page.locator('#pe-best-metric').select_option('mining_ppa_isk');page.locator('#pe-best-find').click()
            expect(page.locator('#pe-best-status')).to_contain_text('20 months checked; 20 usable')
            expect(page.locator('#pe-best-ranking tr')).to_have_count(10)
            expect(page.locator('#pe-best-ranking tr').first).to_contain_text('2026-08')
            expect(page.locator('#pe-best-ranking tr').first.locator('td').last).to_have_attribute('title','10,000,000,000,000,000.19 PPA ISK (reference 2026-01)')
            page.locator('#pe-best-limit').select_option('20');expect(page.locator('#pe-best-ranking tr')).to_have_count(20)
            page.locator('#pe-best-ranking tr').nth(1).locator('button').click();expect(page.locator('#pe-to')).to_have_value('2026-07')
            expect(page.locator('#pe-status')).to_contain_text('Observed month: 2026-07')
            page.locator('#pe-to').select_option('2026-08')
            page.locator('#pe-best-basis').select_option('member');expect(page.locator('#pe-best-ranking tr')).to_have_count(0)
            expect(page.locator('#pe-regions')).to_contain_text('8%')
            expect(page.locator('#pe-coverage')).to_contain_text('2026-08-31')
            page.locator('[data-population-subtab="flows"]').click()
            expect(page.locator('#population-economics')).to_be_hidden()
            button.click();expect(page.locator('#population-flows')).to_be_hidden()
            page.locator('#pe-from').select_option('2026-06');page.locator('#pe-window').fill('60');page.locator('#pe-apply').click()
            expect(page.locator('#pe-status')).to_contain_text('Base month: 2026-06')
            expect(page.locator('#pe-month-counts tr')).to_have_count(2)
            expect(page.locator('#pe-rows tr')).to_have_count(2)
            assert 'window=60' in calls[-1] and 'base=2026-06' in calls[-1] and 'observed=2026-08' in calls[-1]
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
