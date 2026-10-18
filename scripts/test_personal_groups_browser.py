"""Offline browser workflow; run with python scripts/test_personal_groups_browser.py.
Requires Playwright and its Chromium browser. No production DB or server is used.
"""
import json,sys,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.test_personal_groups import RouteTests,routes,store
from playwright.sync_api import sync_playwright,expect
case=RouteTests();case.setUp()
group=case.group;group['name']='Imperial test group';group['public_tabs']=['stats','ships'];case.result['totals']['ships_ready']=True
periods=[dict(period='all'),dict(period='2026'),dict(period='2026-01')]
patches=[patch.object(store,'owned',return_value=group),patch.object(store,'listed',return_value=[group]),patch.object(store,'public',return_value=group),patch.object(store,'period_result',return_value=case.result),patch.object(store,'periods',return_value=periods),patch.object(store,'save',return_value=9)]
for p in patches:p.start()

class Bridge(BaseHTTPRequestHandler):
 def do_GET(self):self.respond()
 def do_POST(self):self.respond()
 def log_message(self,*args):pass
 def respond(self):
  response=case.client.request(self.command,self.path,content=self.rfile.read(int(self.headers.get('content-length',0))),headers=dict(self.headers),follow_redirects=False)
  self.send_response(response.status_code)
  for k,v in response.headers.items():self.send_header(k,v)
  self.end_headers();self.wfile.write(response.content)
server=ThreadingHTTPServer(('127.0.0.1',0),Bridge);threading.Thread(target=server.serve_forever,daemon=True).start()
origin=f'http://127.0.0.1:{server.server_port}'
try:
 with sync_playwright() as p:
  browser=p.chromium.launch(args=['--no-sandbox']);page=browser.new_page(viewport={'width':1440,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
  def serve(route):
   req=route.request;url=req.url
   if 'cdn.jsdelivr.net' in url:
    route.fulfill(status=200,content_type='text/css' if '.css' in url else 'application/javascript',body='');return
   if not url.startswith(origin+'/'):
    route.abort();return
   path=url.removeprefix(origin)
   if '/lookup?' in path:
    data={'results':[dict(kind='character',id=94036773,label='Dictateur Imperator',image_url=None)]};route.fulfill(json=data);return
   if path.endswith('/status'):
    route.fulfill(json={'progress':dict(state='running',phase='Scanning killmails',checked=3,total=10,current_period='2026-01',kills_examined=1234),'periods':['2026-01'],'ready':False});return
   if '/data?' in path:
    html=routes.templates.get_template('personal_group_ships.html').render(ships=[dict(name='Raven',group_name='Battleship',pilot_count=2,image_url=None)],result=case.result)
    route.fulfill(json={'html':html});return
   if path.startswith('/account/groups') or path.startswith('/public/groups'):
    response=case.client.request(req.method,path,content=req.post_data_buffer,headers={k:v for k,v in req.headers.items() if k in ('content-type','origin','referer')},follow_redirects=False)
    route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.content);return
   route.fulfill(json={})
  page.route('**/*',serve)
  page.goto(origin+'/account/groups/new')
  page.locator('.account-menu summary').click();expect(page.get_by_role('link',name='My groups',exact=True).first).to_be_visible();page.locator('.account-menu summary').click()
  page.locator('[name=name]').fill('Imperial test group');page.locator('#memberSearch').fill('Dictateur');page.locator('#memberSearch').press('ArrowDown')
  page.get_by_role('button',name='Dictateur Imperator (character)').click();expect(page.locator('#memberRules tr')).to_have_count(1)
  page.locator('#groupFrom').fill('2026-01-01');page.locator('#groupThrough').fill('2026-01-31');page.locator('#addPreset').click();expect(page.locator('#zoneRules tr')).to_have_count(1)
  with page.expect_navigation():page.get_by_role('button',name='Save group',exact=True).click()
  expect(page).to_have_url(origin+'/account/groups/9')
  page.locator('summary',has_text='Group controls').click()
  expect(page.locator('#groupProgress')).to_contain_text('Months 3 / 10')
  expect(page.locator('#groupProgress')).to_contain_text('1,234')
  expect(page.locator('[name=tabs]')).to_have_count(3)
  expect(page.locator('[name=tabs][value=killboard]')).not_to_be_checked()
  page.goto(origin+'/public/groups/'+'a'*32+'?view=stats&period=2026-01')
  expect(page.locator('h1')).to_have_text('Imperial test group');expect(page.locator('#shareGroup')).to_have_count(0);expect(page.locator('.account-menu')).to_have_count(0)
  expect(page.get_by_role('link',name='Killboard',exact=True)).to_have_count(0)
  page.get_by_role('link',name='Pilotable ships',exact=True).click();expect(page.get_by_role('cell',name='Raven',exact=True)).to_be_visible()
  assert not errors,errors
  browser.close()
 print('Browser PASS: username menu, editor autocomplete/dates/area/save, owner progress/page switches, anonymous public restrictions, ships display; no JavaScript errors.')
finally:
 server.shutdown();server.server_close()
 for p in reversed(patches):p.stop()
 case.doCleanups()
