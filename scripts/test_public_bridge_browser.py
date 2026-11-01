"""Real-browser cookie regression for public data reads and computations, offline."""
import socket
from threading import Thread
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright, expect
import uvicorn
from scripts.test_public_pages import f

ROOT = Path(__file__).resolve().parents[1]


def main():
    app = FastAPI(); calls = []
    renderer = f.load('public_page_renderer')
    env = Environment(loader=FileSystemLoader(ROOT/'web/templates'))

    @app.get('/public/entities/{kind}')
    def page(kind: str):
        bridge = env.get_template('public_read_bridge.js').render(
            public_target={'key': kind, 'tab': 'index', 'read_scope_url': '/entities/'+kind},
            public_policy={}, public_site=True)
        content = renderer.content('entity_index.html', {'entity_index': {'entity_type': kind, 'title': kind.title(), 'description': ''}})
        return HTMLResponse('<script>'+bridge+'</script><main id="publishedContent">'+content+'</main>')

    @app.get('/public/read')
    async def read(request: Request):
        choice = request.cookies.get('eveosint_public_notice_v1')
        calls.append(('read', choice))
        if choice not in ('accepted', 'declined'):
            return Response('Please acknowledge the site notice.', status_code=403)
        return HTMLResponse('<span id="loadedResult">Entity list loaded</span>')

    @app.post('/public/compute')
    async def compute(request: Request):
        choice = request.cookies.get('eveosint_public_notice_v1')
        calls.append(('compute', choice))
        if choice not in ('accepted', 'declined'):
            return Response(status_code=403)
        return JSONResponse({'ok': True})

    sock = socket.socket(); sock.bind(('127.0.0.1', 0)); sock.listen()
    base = 'http://127.0.0.1:'+str(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False))
    thread = Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True); thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=['--no-sandbox'])
            for choice in ('accepted', 'declined'):
                context = browser.new_context()
                context.add_cookies([{'name': 'eveosint_public_notice_v1', 'value': choice, 'url': base, 'httpOnly': True, 'sameSite': 'Lax'}])
                page = context.new_page(); errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                for kind in ('character', 'corporation', 'alliance', 'ship'):
                    page.goto(base+'/public/entities/'+kind)
                    expect(page.locator('#loadedResult')).to_have_text('Entity list loaded')
                    assert not page.locator('.alert-danger').count()
                result = page.evaluate("""async () => {
                    const response=await fetch('/api/tool/read-only-compute', {method:'POST', body:'{}'});
                    return [response.status, await response.json()];
                }""")
                assert result == [200, {'ok': True}], result
                assert not errors, errors
                context.close()
            assert len(calls) == 10, calls
            assert all(choice in ('accepted', 'declined') for kind, choice in calls), calls
            # No acknowledgement still cannot access the data gateway.
            context = browser.new_context(); page = context.new_page()
            page.goto(base+'/public/entities/character')
            expect(page.locator('.alert-danger')).to_have_text('Erreur 500')
            assert calls[-1] == ('read', None)
            context.close(); browser.close()
        print('Chromium: 8 entity lists + public POST; accepted/refused choice retained, missing choice denied.')
    finally:
        server.should_exit = True; thread.join(5); sock.close()


if __name__ == '__main__':
    main()
