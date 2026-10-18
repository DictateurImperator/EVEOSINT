"""Offline browser workflow; run with python scripts/test_personal_groups_browser.py.
Requires Playwright and its Chromium browser. No production DB or server is used.
"""
import json,sys,threading
from urllib.parse import urlparse,parse_qs
from decimal import Decimal
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.test_personal_groups import RouteTests,routes,store,base,JAN,FEB,sample_killmail
from playwright.sync_api import sync_playwright,expect
case=RouteTests();case.setUp()
group=case.group;group['name']='Imperial test group';group['public_tabs']=['stats','ships'];case.result['totals']['ships_ready']=True
case.client.app.include_router(base.routes.router)
periods=[dict(period='all'),dict(period='2026'),dict(period='2026-01')]
patches=[patch.object(store,'accessible',return_value=group),patch.object(store,'listed',return_value=[group]),patch.object(store,'delegated',return_value=[]),patch.object(store,'public',return_value=group),patch.object(store,'period_result',return_value=case.result),patch.object(store,'periods',return_value=periods),patch.object(store,'save',return_value=9),patch.object(store,'managers',return_value=[]),patch.object(store,'set_manager'),patch.object(base.routes,'require_login',return_value=case.user),patch.object(base.rankings,'catalog',return_value=[dict(month=m,built_at='v',killmails=1,valued=1,zero_damage=0,entity_rows=1,dirty=False) for m in (JAN,FEB)]),patch.object(base.rankings,'ranking_rows',return_value=([],True))]
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
  browser=p.chromium.launch(args=['--no-sandbox']);page=browser.new_page(permissions=['clipboard-read','clipboard-write'],viewport={'width':1440,'height':1000});errors=[];requests=[];page.on('pageerror',lambda e:errors.append(str(e)))
  def serve(route):
   req=route.request;url=req.url
   if 'cdn.jsdelivr.net' in url:
    route.fulfill(status=200,content_type='text/css' if '.css' in url else 'application/javascript',body='');return
   if not url.startswith(origin+'/'):
    route.abort();return
   path=url.removeprefix(origin);requests.append(path)
   if '/managers/lookup?' in path:
    route.fulfill(json={'results':[dict(id=2,username='Trusted manager')]});return
   if '/lookup?' in path and 'kind=ship' in path:
    route.fulfill(json={'results':[dict(kind='ship_group',id='Destroyer',label='Destroyer',subtitle='Ship group',image_url=None)]});return
   choices=[dict(kind='corporation',id=10,label='Example corporation',image_url=None,subtitle='Corporation',section='CORPORATIONS'),*[dict(kind='character',id=94036773+i,label='Dictateur Imperator' if i==0 else f'Example pilot {i}',image_url=None,subtitle='Pilot',section='CHARACTERS') for i in range(6)]]
   if path.startswith('/api/entity-search?'):
    route.fulfill(json={'results':[dict(item,entity_type=item['kind'],url=f"/{item['kind']}/{item['id']}") for item in choices]});return
   if '/lookup?' in path:
    data={'results':choices};route.fulfill(json=data);return
   if '/data?' in path and 'view=ranking' in path:
    html=routes.templates.get_template('personal_group_ranking.html').render(rows=[dict(rank=1,entity_id=1,name='Local pilot',destroyed=10000,raw=10000,weighted=2000,lost=0,kills=1,losses=0,net=10000,efficiency=100,unvalued_kills=0,unvalued_losses=0)],public_group='/public/' in path,kind='character',compact_isk=base.rankings.compact_isk,full_isk=base.rankings.full_isk,previous=None,next_page=None)
    route.fulfill(json={'html':html,'status':'completed','progress':{'checked':1,'total':1}});return
   if '/data?' in path and 'view=killboard' in path:
    html=routes.templates.get_template('personal_group_killboard.html').render(killmail_page=dict(killmail_groups=[{'date':'2026-01-02','items':[sample_killmail()]}],killmail_pagination={},global_context=True),killmail_public=True,killmail_mode='api',error=None)
    route.fulfill(json={'html':html,'scanned':1,'total':1});return
   if '/data?' in path and 'stats_view=top-kills' in path:
    row=dict(killmail_id=100,killmail_time=sample_killmail()['killmail_time'],ship_id=638,ship_name='Raven',victim_name='Victim',system_name='Jita',raw_cents=10000,true_cents=5000,damage_percent=50)
    html=routes.templates.get_template('combat_top_kills_fragment.html').render(top_kills={'raw':[row],'weighted':[row]},public_group=True,compact_isk=base.rankings.compact_isk,full_isk=base.rankings.full_isk)
    route.fulfill(json={'html':html,'status':'completed','progress':dict(checked=1,total=1)});return
   if '/data?' in path and 'view=stats' in path:
    html=routes.templates.get_template('personal_group_stats.html').render(combat_stats=dict(rows=[dict(case.result['totals'],month=JAN,ratio=30)],totals=dict(case.result['totals'],ratio=30)),compact_isk=base.rankings.compact_isk,full_isk=base.rankings.full_isk)
    route.fulfill(json={'html':html,'status':'completed','progress':dict(checked=1,total=1)});return
   if '/data?' in path:
    html=routes.templates.get_template('personal_group_ships.html').render(recent_entities=[dict(name='Raven',faction_name='Caldari',ship_tree_row='combat',ship_size='Battleship',ship_display_size='L',ship_tier='T1',group_name='Battleship',pilot_count=2,image_url=None)],result=case.result,entity_index={'entity_type':'ship'},pilotable_page={'entity_type':'group','ready':True},public_group=True,error=None)
    route.fulfill(json={'html':html,'status':'completed','progress':dict(checked=1,total=1)});return
   if path.startswith('/account/groups') or path.startswith('/public/groups') or path.startswith('/leaderboard'):
    response=case.client.request(req.method,path,content=req.post_data_buffer,headers=dict({k:v for k,v in req.headers.items() if k in ('content-type','origin','referer')},host=urlparse(origin).netloc),follow_redirects=False)
    route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.content);return
   route.fulfill(json={})
  page.route('**/*',serve)
  page.goto(origin+'/account/groups/new')
  page.locator('.account-menu summary').click();expect(page.get_by_role('link',name='My groups',exact=True).first).to_be_visible();page.locator('.account-menu summary').click()
  top=page.locator('[data-entity-search-input]');top.fill('Example');expect(page.locator('[data-entity-search-results] .topbar-entity-search-grid')).to_have_count(2)
  assert page.locator('[data-entity-search-results] .topbar-entity-search-grid').first.evaluate("el=>getComputedStyle(el).gridTemplateColumns.split(' ').length")==3
  top.press('Escape')
  page.locator('[name=name]').fill('Imperial test group');page.locator('#memberSearch').fill('Dictateur');page.locator('#memberSearch').press('ArrowDown')
  expect(page.locator('#memberResults .topbar-entity-search-section-title')).to_have_text(['CORPORATIONS','CHARACTERS'])
  assert page.locator('#memberResults .topbar-entity-search-grid').last.evaluate("el=>getComputedStyle(el).gridTemplateColumns.split(' ').length")==3
  cells=page.locator('#memberResults .topbar-entity-search-grid').last.locator('button')
  a,b,c,d=[cells.nth(i).bounding_box() for i in range(4)]
  assert a['y']==b['y']==c['y'] and a['x']<b['x']<c['x'] and d['y']>a['y']
  page.get_by_role('button',name='Example corporation Corporation',exact=True).focus();page.keyboard.press('ArrowDown');expect(page.get_by_role('button',name='Dictateur Imperator Pilot',exact=True)).to_be_focused()
  page.get_by_role('button',name='Dictateur Imperator Pilot').click();expect(page.locator('#memberRules tr')).to_have_count(1)
  expect(page.locator('#memberRules img')).to_have_attribute('src','https://images.evetech.net/characters/94036773/portrait?size=64')
  page.get_by_label('Member from: Dictateur Imperator',exact=True).fill('2026-01-05');page.get_by_label('Member through: Dictateur Imperator',exact=True).fill('2026-01-20')
  page.locator('#shipSearch').fill('Destroyer');page.get_by_role('button',name='Destroyer Ship group',exact=True).click();expect(page.locator('#shipRules tr')).to_have_count(1)
  page.locator('#shipRules select').last.select_option('victim')
  page.locator('#groupFrom').fill('2026-01-01');page.locator('#groupThrough').fill('2026-01-31');page.locator('#addPreset').click();expect(page.locator('#zoneRules tr')).to_have_count(1)
  with page.expect_navigation():page.get_by_role('button',name='Save group',exact=True).click()
  expect(page).to_have_url(origin+'/account/groups/9')
  payload=store.save.call_args.args[3]
  assert payload['members'][0]['from']=='2026-01-05' and payload['members'][0]['through']=='2026-01-20'
  assert payload['ships'][0]['role']=='victim' and payload['ships'][0]['id']=='Destroyer'

  expect(page.locator('#groupResult')).to_have_count(0)
  assert not any('/data?' in path for path in requests)
  expect(page.locator('#prepareGroup')).to_have_count(0)
  expect(page.locator('#stopGroup')).to_have_count(0)
  expect(page.locator('[name=tabs]')).to_have_count(4)
  expect(page.locator('[name=tabs][value=killboard]')).not_to_be_checked()
  expect(page.locator('#managerForm')).to_be_visible()
  page.locator('#managerSearch').fill('Trusted');page.get_by_role('button',name='Trusted manager',exact=True).click()
  with page.expect_navigation():page.get_by_role('button',name='Add manager',exact=True).click()
  store.set_manager.assert_called_with(9,1,2,remove=False)
  group['public_tabs'].append('ranking')
  page.goto(origin+'/public/groups/'+'a'*32)
  expect(page.locator('#groupResult')).to_have_count(0)
  assert not any('/data?' in path for path in requests)
  page.get_by_role('link',name='Combat stats',exact=True).click()
  expect(page.locator('#groupResultProgress')).to_contain_text('Complete')
  expect(page.locator('h1')).to_have_text('Imperial test group');expect(page.locator('#shareGroup')).to_have_count(0);expect(page.locator('.account-menu')).to_have_count(0)
  expect(page.get_by_role('link',name='Killboard',exact=True)).to_have_count(0)
  page.get_by_role('link',name='Pilotable ships',exact=True).click();expect(page.get_by_text('Raven',exact=False).last).to_be_visible()
  page.get_by_role('link',name='Local ranking',exact=True).click();expect(page.locator('#groupResultProgress')).to_contain_text('Complete');expect(page.get_by_text('Local pilot',exact=True)).to_be_visible()
  page.get_by_role('link',name='Combat stats',exact=True).click();expect(page.locator('.combat-history')).to_be_visible()
  page.get_by_role('link',name='Top kills',exact=True).click();expect(page.get_by_role('button',name='Highest true ISK')).to_be_visible()
  page.get_by_role('button',name='Highest true ISK').click();expect(page.locator('[data-combat-top-panel=weighted]')).to_be_visible();expect(page.locator('[data-combat-top-panel=raw]')).to_be_hidden()
  group['public_tabs'].append('killboard');page.reload();page.get_by_role('link',name='Killboard',exact=True).click()
  expect(page.locator('.killmail-table')).to_be_visible();expect(page.locator('.killmail-row-kill')).to_have_count(1)
  page.get_by_role('link',name='VICTIM',exact=True).click();expect(page).to_have_url(origin+'/public/groups/'+'a'*32+'?view=killboard&period=all&participation=losses')
  assert not any('/account/groups/9/data' in path for path in requests)
  case.user['id']=2;store.listed.return_value=[];store.delegated.return_value=[dict(group,owner_name='Owner')]
  page.goto(origin+'/account/groups');expect(page.get_by_text('Groups you manage',exact=True)).to_be_visible()
  expect(page.get_by_role('link',name='Public page',exact=True)).to_have_attribute('href','/public/groups/'+'a'*32)
  expect(page.get_by_label('Public link')).to_have_value(origin+'/public/groups/'+'a'*32)
  page.get_by_role('button',name='Copy',exact=True).click();expect(page.locator('[data-copy-status]')).to_have_text('Copied')
  case.user['id']=1
  page.goto(origin+'/leaderboard');page.locator('[name=aggregation]').select_option('monthly');page.locator('#rankMonth').select_option('2026-02')
  with page.expect_navigation():page.get_by_role('button',name='Show rankings',exact=True).click()
  assert parse_qs(urlparse(page.url).query)['aggregation']==['monthly']
  assert base.rankings.ranking_rows.call_args.args[3:5]==(FEB,FEB)
  expect(page.get_by_text('Month only · 2026-02',exact=True)).to_be_visible()
  assert not errors,errors
  browser.close()
 print('Browser PASS: username menu, editor autocomplete/dates/area/save, member portraits/individual dates/ship filters, on-request results without preparation, delegated-group public links, manager delegation, public local ranking, standard monthly mode; no JavaScript errors.')
finally:
 server.shutdown();server.server_close()
 for p in reversed(patches):p.stop()
 case.doCleanups()
