"""Small, authenticated and idempotent repair report intake. No game log ingestion."""
import hashlib
import hmac
import json
import re
import secrets
from contextlib import closing
from datetime import datetime, timezone

from .db import db

MAX_BODY = 65536
HEX = re.compile(r'^[0-9a-f]{64}$')


def validate_report(body):
    try:
        p = json.loads(body)
    except (ValueError, UnicodeError) as exc:
        raise ValueError('Invalid JSON') from exc
    if not isinstance(p, dict) or set(p) - {'version', 'victim', 'loss_time', 'window_seconds', 'partial', 'logi', 'status'}:
        raise ValueError('Unexpected fields')
    if type(p.get('version')) is not int or p['version'] != 1 or type(p.get('window_seconds')) is not int or p['window_seconds'] != 1800:
        raise ValueError('Invalid version or window')
    if type(p.get('partial')) is not bool or p.get('status', 'manual') not in ('manual', 'pending_killmail'):
        raise ValueError('Invalid status')
    def name(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 128 or any(ord(c) < 32 for c in value):
            raise ValueError('Invalid name')
        return value
    victim = p.get('victim')
    if not isinstance(victim, dict) or set(victim) - {'pilot', 'ship'}:
        raise ValueError('Invalid victim')
    name(victim.get('pilot'))
    if victim.get('ship') is not None:
        name(victim['ship'])
    try:
        raw_time = p['loss_time']
        if not isinstance(raw_time, str) or not raw_time.endswith('Z'):
            raise ValueError('UTC required')
        at = datetime.strptime(raw_time, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        if at > datetime.now(timezone.utc):
            raise ValueError('Future loss')
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError('Invalid loss time') from exc
    logi = p.get('logi')
    if not isinstance(logi, list) or len(logi) > 500:
        raise ValueError('Invalid contributors')
    seen = set()
    for row in logi:
        if not isinstance(row, dict) or set(row) - {'pilot', 'ship', 'shield', 'armor', 'hull'}:
            raise ValueError('Invalid contribution')
        key = (name(row.get('pilot')), name(row.get('ship')))
        if key in seen:
            raise ValueError('Duplicate contribution')
        seen.add(key)
        if not any(row.get(k, 0) for k in ('shield', 'armor', 'hull')):
            raise ValueError('Empty contribution')
        for key in ('shield', 'armor', 'hull'):
            if type(row.get(key, 0)) is not int or not 0 <= row.get(key, 0) <= 10**12:
                raise ValueError('Invalid repair amount')
    return p, at


def rotate_token(user_id):
    token = f'{user_id}.{secrets.token_urlsafe(32)}'
    digest = hashlib.sha256(token.encode()).hexdigest()
    with closing(db()) as conn:
        with conn.cursor() as cur:
            cur.execute('INSERT INTO "KillmailGenerator".collector_tokens(user_id,token_hash) VALUES(%s,%s) '
                        'ON CONFLICT(user_id) DO UPDATE SET token_hash=excluded.token_hash,created_at=now()', (user_id, digest))
        conn.commit()
    return token


def revoke_token(user_id):
    with closing(db()) as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM "KillmailGenerator".collector_tokens WHERE user_id=%s', (user_id,))
        conn.commit()


def authenticate(token):
    prefix, sep, _ = token.partition('.')
    if not sep or not prefix.isascii() or not prefix.isdigit() or len(prefix) > 18 or len(token) > 160:
        return None
    with closing(db()) as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT t.token_hash FROM "KillmailGenerator".collector_tokens t '
                        'JOIN web.users u ON u.id=t.user_id WHERE t.user_id=%s AND u.is_active=TRUE', (int(prefix),))
            row = cur.fetchone()
    if row and hmac.compare_digest(row[0], hashlib.sha256(token.encode()).hexdigest()):
        return int(prefix)
    return None


def store_report(owner, report_id, body):
    if len(body) > MAX_BODY or not HEX.fullmatch(report_id) or hashlib.sha256(body).hexdigest() != report_id:
        raise ValueError('Invalid body or report id')
    p, at = validate_report(body)
    with closing(db()) as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL synchronous_commit=on; SET LOCAL statement_timeout='5s'; SET LOCAL lock_timeout='3s'")
            cur.execute('INSERT INTO "KillmailGenerator".repair_reports '
                        '(owner_user_id,report_id,victim_pilot,victim_ship,loss_time,window_seconds,partial,logi,payload) '
                        'VALUES(%s,%s,%s,%s,%s,1800,%s,%s::jsonb,%s::jsonb) '
                        'ON CONFLICT(owner_user_id,report_id) DO NOTHING',
                        (owner, report_id, p['victim']['pilot'], p['victim'].get('ship'), at,
                         p['partial'], json.dumps(p['logi']), json.dumps(p)))
        # The HTTP acknowledgement is constructed only AFTER this returns.
        conn.commit()
    return {'report_id': report_id, 'sha256': report_id, 'status': 'stored'}
