"""Trusted, narrowly scoped identity for published read-only adapters.

Never selected by an HTTP parameter/header. Only the publication gateway enters
this scope after checking the page/tab and an explicit data/calculation endpoint allowlist.
It grants no account, admin, job or management permission.
"""
from contextlib import contextmanager
from contextvars import ContextVar

_READER = ContextVar('published_reader', default=None)
PUBLIC_READER = dict(id=None, username='', roles=[], permissions={'entities.view','superintel.view'}, is_public=True)


def current_reader():
    return _READER.get()


@contextmanager
def read_scope():
    token = _READER.set(PUBLIC_READER)
    try:
        yield
    finally:
        _READER.reset(token)


async def read_get(app, url, accept='*/*', *, method='GET', request_body=b''):
    """Run a validated local data request through ASGI, without network dependencies."""
    import asyncio
    from types import SimpleNamespace
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    scope = dict(type='http', asgi={'version':'3.0','spec_version':'2.4'}, http_version='1.1',
                 method=method, scheme='http', path=parts.path, raw_path=parts.path.encode(),
                 query_string=parts.query.encode(), root_path='', client=None,
                 server=('public-read.internal',80),
                 headers=[(b'host',b'public-read.internal'),(b'accept',accept.encode()),
                          (b'content-type',b'application/json'),(b'content-length',str(len(request_body)).encode())])
    status=500
    headers={}
    body=bytearray()
    received=False
    complete=asyncio.Event()

    async def receive():
        nonlocal received
        if not received:
            received=True
            return {'type':'http.request','body':request_body,'more_body':False}
        await complete.wait()
        return {'type':'http.disconnect'}

    async def send(message):
        nonlocal status,headers
        if message['type']=='http.response.start':
            status=message['status']
            headers={k.decode().lower():v.decode() for k,v in message.get('headers',[])}
        elif message['type']=='http.response.body':
            body.extend(message.get('body',b''))
            if len(body)>8*1024*1024:
                raise ValueError('Public data response exceeds 8 MiB.')
            if not message.get('more_body',False):complete.set()

    await app(scope,receive,send)
    return SimpleNamespace(status_code=status,headers=headers,content=bytes(body),text=body.decode('utf-8',errors='replace'))
