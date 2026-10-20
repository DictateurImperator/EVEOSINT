"""Offline browser test of Debug tabs and single-killmail submit states."""
from urllib.parse import urlsplit
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright,expect
from scripts.test_killmail_esi_import import debug,routes,URL


def main():
 app=FastAPI();app.include_router(debug.router);app.include_router(routes.router);client=TestClient(app)
 user={'id':1,'username':'admin','permissions':{'admin.jobs.view','admin.jobs.run'}}
 calls=[];errors=[];mode={'value':'imported'}
 with sync_playwright() as p,patch.object(debug,'require_login',return_value=user):
  browser=p.chromium.launch(args=['--no-sandbox']);page=browser.new_page(viewport={'width':1280,'height':900})
  page.on('pageerror',lambda error:errors.append(str(error)))
  def handle(route):
   u=urlsplit(route.request.url)
   if u.netloc!='killmail.test':route.abort();return
   if u.path=='/admin/debug/killmail/import':
    calls.append(route.request.post_data)
    if mode['value']=='error':route.fulfill(status=422,json={'error':'Killmail introuvable.'});return
    route.fulfill(json={'status':mode['value'],'killmail_id':139240959,'killmail_time':'2026-10-17T23:34:43+00:00','attackers':1});return
   if u.path.endswith('/catalog'):route.fulfill(json={'first':None,'last':None,'baskets':[],'items':[]});return
   response=client.get(u.path+('?' + u.query if u.query else ''))
   route.fulfill(status=response.status_code,content_type='text/html',body=response.content)
  page.route('**/*',handle);page.goto('http://killmail.test/admin/debug?tab=killmail')
  expect(page.locator('#killmailImportForm')).to_be_visible();assert not calls
  page.locator('#killmailEsiUrl').fill(URL);page.locator('#killmailImportButton').click()
  expect(page.locator('#killmailImportStatus')).to_contain_text('importé');expect(page.locator('#killmailImportResult a')).to_have_attribute('href','/kill/139240959')
  mode['value']='existing';page.locator('#killmailImportButton').click();expect(page.locator('#killmailImportStatus')).to_contain_text('déjà présent')
  mode['value']='error';page.locator('#killmailImportButton').click();expect(page.locator('#killmailImportStatus')).to_contain_text('introuvable');expect(page.locator('#killmailImportButton')).to_be_enabled()
  page.screenshot(path='/tmp/eveosint-debug-killmail-import.png',full_page=True)
  page.get_by_role('link',name='Indices économiques',exact=True).click();expect(page.locator('#economicForm')).to_be_visible();assert len(calls)==3
  assert not errors,errors;browser.close()
 print('Debug tabs and import success/replay/error browser checks passed.')

if __name__=='__main__':main()
