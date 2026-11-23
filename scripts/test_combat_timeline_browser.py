"""Exercise the actual Debug timeline template and its API with an isolated store."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import socket
from threading import Thread
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from playwright.sync_api import sync_playwright, expect
import uvicorn
from scripts.test_combat_timeline import store, routes, ADMIN, event, body
from scripts import test_killmail_forensics as f
import tempfile


def main():
    with tempfile.TemporaryDirectory(prefix='combat-browser-') as directory, \
         patch.object(store,'ROOT',Path(directory)), \
         patch.object(routes,'require_login',return_value=ADMIN), \
         patch.object(routes,'require_permission_or_redirect',return_value=None):
        app=FastAPI();app.include_router(routes.router)
        rows=[event(1,'Pilot A'),event(2,'Pilot B','damage_out'),event(3,'Pilot A','loss_signal')]
        rows[0]['source']['name']='<img src=x onerror=alert(1)>'
        payload=body(rows);store.receive(1,payload,__import__('hashlib').sha256(payload).hexdigest())
        renderer=f.load('public_page_renderer')
        @app.get('/admin/debug')
        def page(tab: str='combat'):
            return HTMLResponse(renderer.content('admin_debug_index.html',{'debug_tab':tab,'can_import_killmail':True}))
        sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen();base='http://127.0.0.1:'+str(sock.getsockname()[1])
        server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False));thread=Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(args=['--no-sandbox']);page=browser.new_page(viewport={'width':1500,'height':1050});errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(base+'/admin/debug?tab=combat')
                expect(page.locator('#ctRows tr')).to_have_count(3)
                expect(page.locator('#ctTotal')).to_have_text('3 observations received')
                expect(page.get_by_role('link',name='Combat logs',exact=True)).to_have_class('nav-link active')
                expect(page.locator('#ctRows img')).to_have_count(0)
                page.locator('#ctObserver').select_option('Pilot A');expect(page.locator('#ctRows tr')).to_have_count(2)
                page.locator('#ctKind').select_option('loss_signal');expect(page.locator('#ctRows tr')).to_have_count(1)
                page.locator('#ctObserver').select_option('');page.locator('#ctKind').select_option('')
                expect(page.get_by_role('link',name='Download Windows reader')).to_have_count(0)
                expect(page.locator('#ctSecret')).to_have_count(0)
                expect(page.locator('#ctRevoke')).to_have_count(0)
                assert 'Windows reader' not in page.content()
                # Poll shows a newly committed observation without reloading the page.
                payload=body([event(4,'Pilot C')]);store.receive(1,payload,__import__('hashlib').sha256(payload).hexdigest())
                expect(page.locator('#ctRows tr')).to_have_count(4,timeout=8000)
                page.locator('#ctFrom').fill('2026-10-21T00:00');page.locator('#ctFrom').dispatch_event('change')
                expect(page.locator('#ctRows tr')).to_have_count(0)
                page.locator('#ctFrom').fill('');page.locator('#ctFrom').dispatch_event('change')
                expect(page.locator('#ctRows tr')).to_have_count(4)
                page.screenshot(path='/tmp/combat-timeline-debug-preview.png',full_page=True)
                assert not errors,errors
                browser.close()
            print('Browser: Debug tab, observer/type/time filters, live polling, no reader login/download controls, XSS escaping: OK')
        finally:
            server.should_exit=True;thread.join(5);sock.close()


if __name__=='__main__':main()
