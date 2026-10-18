"""Offline browser check: custom basket, rebasing, chart, missing data and local save."""
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI,Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright,expect
from scripts.test_economic_indices import e,f,DAY,END
from decimal import Decimal


def main():
    app=FastAPI()
    @app.get('/admin/debug',response_class=HTMLResponse)
    def page(request:Request):
        return f.templates.TemplateResponse(request=request,name='admin_debug_index.html',context=dict(request=request,username='Admin',show_app_layout=True,top_menu=[],context_menu=[],context_title='Debug'))
    client=TestClient(app);requests=[];errors=[]
    data={'first':'2026-01-01','last':'2026-02-28','baskets':[{'key':'capital','label':'Capital','count':1}],
          'items':[{'id':1,'name':'Raven','group':'Battleship'},{'id':2,'name':'Tritanium','group':'Mineral'},{'id':3,'name':'Future hull','group':'Cruiser'}]}
    metadata={i['id']:i for i in data['items']}
    result=e.build_result([{'name':'My fleet <test>','quantities':{1:Decimal(2),2:Decimal(1),3:Decimal(1)}}],
         {DAY:{1:(Decimal(100),DAY),2:(Decimal(10),DAY)},END:{1:(Decimal(140),END),2:(Decimal(20),END),3:(Decimal(9),END)}},DAY,END,metadata)
    with sync_playwright() as p:
        browser=p.chromium.launch(args=['--no-sandbox']);page=browser.new_page(viewport={'width':1440,'height':1100})
        page.on('pageerror',lambda error:errors.append(str(error)))
        def handle(route):
            parts=urlsplit(route.request.url)
            if parts.netloc!='economics.test':route.abort();return
            if parts.path.endswith('/catalog'):route.fulfill(json=data);return
            if parts.path.endswith('/calculate'):
                requests.append(route.request.post_data_json);route.fulfill(status=202,json={'id':'run'});return
            if '/status/' in parts.path:route.fulfill(json={'status':'completed','progress':{'checked':2,'total':2},'result':result});return
            if '/api/' in parts.path:route.fulfill(json={});return
            response=client.get(parts.path);route.fulfill(status=response.status_code,content_type='text/html',body=response.content)
        page.route('**/*',handle);page.goto('http://economics.test/admin/debug')
        expect(page.locator('#ecoCalculate')).to_be_enabled();assert not requests
        page.locator('#ecoBase').fill('2026-01-15');page.locator('#ecoStart').fill('2026-01-01')
        page.locator('#ecoPresets input').uncheck()
        page.locator('#basketName').fill('My fleet <test>')
        for name in ('Raven','Tritanium','Future hull'):
            page.locator('#itemSearch').fill(name);page.locator('#itemResults button').first.click()
        page.get_by_label('Quantity of Raven',exact=True).fill('2')
        page.locator('#saveBasket').click();expect(page.locator('#ecoStatus')).to_contain_text('saved in this browser')
        page.locator('#ecoCalculate').click()
        expect(page.locator('#ecoResults')).to_be_visible();expect(page.locator('#ecoSummary')).to_contain_text('142.86%')
        assert requests[0]['items'][0]=={'id':1,'quantity':'2'}
        expect(page.locator('#ecoDetails')).to_contain_text('Future hull')
        expect(page.locator('#ecoDetails')).to_contain_text('1 objects excluded')
        assert page.locator('#ecoChart svg path').count()==1
        page.screenshot(path='/tmp/eveosint-economic-indices-preview.png',full_page=True)
        page.reload();expect(page.locator('#ecoCalculate')).to_be_enabled()
        page.locator('#savedBaskets').select_option(label='My fleet <test>')
        expect(page.get_by_label('Quantity of Raven',exact=True)).to_have_value('2')
        assert len(requests)==1 # Reloading and loading a saved basket never starts a calculation.
        assert not errors,errors
        browser.close()
    print('Economics browser checks passed; preview /tmp/eveosint-economic-indices-preview.png')


if __name__=='__main__':main()
