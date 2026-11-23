from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from psycopg2.errors import UndefinedTable, InvalidSchemaName
from starlette.concurrency import run_in_threadpool

from . import killmail_generator as store

router = APIRouter()


@router.post('/api/combat-repairs')
async def receive_report(request: Request):
    auth = request.headers.get('authorization', '')
    if not auth.startswith('Bearer '):
        raise HTTPException(401)
    try:
        owner = await run_in_threadpool(store.authenticate, auth[7:])
    except (UndefinedTable, InvalidSchemaName):
        raise HTTPException(503, 'Collector setup required')
    if owner is None:
        raise HTTPException(401)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > store.MAX_BODY:
            raise HTTPException(413)
    try:
        ack = await run_in_threadpool(store.store_report, owner, request.headers.get('idempotency-key', ''), bytes(body))
    except ValueError:
        raise HTTPException(422, 'Invalid report')
    except (UndefinedTable, InvalidSchemaName):
        raise HTTPException(503, 'Collector setup required')
    return JSONResponse(ack, headers={'Cache-Control': 'no-store'})
