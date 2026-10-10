"""Exercise actual alliance/coalition tab navigation and economic rendering offline."""
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import expect, sync_playwright

ROOT=Path(__file__).resolve().parents[1]


def main():
    env=Environment(loader=FileSystemLoader(ROOT/'web/templates'))
    payload={'available':True,'coverage':['2026-08'],'economic_days':31,'activity_days':31,
             'denominators':{'member':'150.5','active':5,'kill':10,'loss':0},
             'rows':[{'label':'Mining','total':'3100','monthly_average':'3100','daily_average':'100',
                      'ratios':{'member':'20.59','active':'620','kill':'310','loss':None}}],
             'regions':[{'month':'2026-08','region':'Delve','owned_average':'8','total_average':'100','share':'.08'}],
             'activity_coverage':[{'from':'2026-08-01','through':'2026-08-31'}]}
    with sync_playwright() as p:
        browser=p.chromium.launch(args=['--no-sandbox'])
        for kind in ['alliance','coalition','corporation']:
            html=env.get_template('coalition_population.html' if kind=='coalition' else 'entity_population.html').render(profile={'entity_type':kind,'entity_id':1,'name':'Example'})
            page=browser.new_page();errors=[];calls=[];available=True;fail=False
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.add_init_script("window.eveosintPublicError=e=>e.message || String(e)")
            def handle(route):
                path=urlsplit(route.request.url).path
                if path.endswith('/population-economics/options'):
                    route.fulfill(json={'available':available,'months':['2026-07','2026-08'] if available else []})
                elif path.endswith('/population-economics'):
                    calls.append(route.request.url)
                    if fail:route.fulfill(status=500,json={'error':'Test error'})
                    else:route.fulfill(json=payload)
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
            expect(page.locator('#pe-counts')).to_contain_text('Average population: 150.5')
            expect(page.locator('#pe-rows')).to_contain_text('620')
            expect(page.locator('#pe-regions')).to_contain_text('8%')
            expect(page.locator('#pe-coverage')).to_contain_text('2026-08-31')
            page.locator('[data-population-subtab="flows"]').click()
            expect(page.locator('#population-economics')).to_be_hidden()
            button.click();expect(page.locator('#population-flows')).to_be_hidden()
            page.locator('#pe-from').select_option('2026-07');page.locator('#pe-window').fill('60');page.locator('#pe-apply').click()
            expect(page.locator('#pe-status')).to_contain_text('Covered MER months')
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
