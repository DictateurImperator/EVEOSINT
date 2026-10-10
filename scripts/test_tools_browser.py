"""Offline browser checks for multi-entity analysis tools; production APIs are mocked."""
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
from jinja2 import Environment,FileSystemLoader
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[1]
env=Environment(loader=FileSystemLoader(ROOT/'web/templates'))
with sync_playwright() as p:
 browser=p.chromium.launch(args=['--no-sandbox'])
 for kind in ['economics','population']:
  html=env.get_template('tools_comparison.html').render(comparison_kind=kind)
  page=browser.new_page();errors=[];calls=[];fail_second=False
  page.on('pageerror',lambda e:errors.append(str(e)))
  def route(r):
   path=urlsplit(r.request.url).path;q=parse_qs(urlsplit(r.request.url).query);calls.append((path,q))
   if fail_second and '/coalition/' in path and (path.endswith('/evolution') or path.endswith('/series')):
    r.fulfill(status=504,json={'error':'Simulated timeout'});return
   if path=='/api/tools/entity-search':r.fulfill(json={'results':[{'entity_type':'alliance','entity_id':1,'name':'Example A'},{'entity_type':'coalition','entity_id':2,'name':'Example B'}]})
   elif path.endswith('/options'):r.fulfill(json={'available':True,'months':['2026-07','2026-08'],'purchasing_power_months':['2026-07','2026-08']})
   elif path.endswith('/evolution'):
    factor=1 if '/alliance/' in path else 2
    r.fulfill(json={'points':[{'month':'2026-07','values':{'npc_bounties_isk':str(factor*1000000000000),'mining_ppa_isk':str(factor*3)}},{'month':'2026-08','values':{'npc_bounties_isk':str(factor*2000000000000),'mining_ppa_isk':None}}],'next_offset':None})
   elif path.endswith('/series'):
    r.fulfill(json={'rows':[{'date':q['date_from'][0],'value':10 if '/alliance/' in path else 20},{'date':q['date_to'][0],'value':15 if '/alliance/' in path else 25}]})
   elif path=='/':r.fulfill(body=html,content_type='text/html')
   else:r.fulfill(body='',content_type='text/plain')
  page.route('**/*',route);page.goto('http://eveosint.test/')
  for label in ['Example A','Example B']:
   page.locator('#tc-search').fill('Example');page.get_by_role('button',name=label+' · '+('alliance' if label.endswith('A') else 'coalition'),exact=True).click()
  expect(page.locator('#tc-selected button')).to_have_count(2)
  if kind=='economics':expect(page.locator('#tc-from')).to_have_value('2026-07')
  else:page.locator('#tc-from').fill('2026-07-01');page.locator('#tc-to').fill('2026-07-10')
  page.locator('#tc-run').click();expect(page.locator('#tc-status')).to_contain_text('Loaded 2 entities')
  expect(page.locator('#tc-summary tr')).to_have_count(2)
  assert page.locator('#tc-chart svg path').count()==2
  with page.expect_download() as d:page.locator('#tc-csv').click()
  assert d.value.suggested_filename==kind+'-comparison.csv'
  page.locator('#tc-chart').hover();expect(page.locator('#tc-tip')).to_be_visible()
  if kind=='economics':
   evolution_calls=[q for path,q in calls if path.endswith('/evolution')]
   assert all(q['reference']==['2026-08'] for q in evolution_calls)
  fail_second=True
  page.locator('#tc-run').click();expect(page.locator('#tc-status')).to_contain_text('1 failed')
  assert page.locator('#tc-chart svg path').count()==1
  expect(page.locator('#tc-csv')).to_be_enabled()
  expect(page.locator('#tc-summary')).to_contain_text('No available data')
  page.locator('#tc-selected button').first.click();expect(page.locator('#tc-status')).to_contain_text('Selection changed')
  expect(page.locator('#tc-csv')).to_be_disabled()
  assert not errors,errors
  print(kind,'two entity comparison, graph, tooltip, CSV and invalidation PASS')
  page.close()
 browser.close()
