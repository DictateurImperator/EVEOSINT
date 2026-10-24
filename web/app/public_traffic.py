"""Bounded local traffic metrics; no authentication data, IPs or raw queries."""
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import logging
import json
import os
import re
import secrets
import sqlite3
import time
import tempfile
from urllib.parse import parse_qs, urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.responses import PlainTextResponse

ROOT = Path(__file__).resolve().parents[2] / 'data' / 'public_traffic'
COOKIE = 'eveosint_public_visitor'
RETENTION_DAYS = 30
LOG = logging.getLogger(__name__)
_last_error = False
_last_warning = 0


def googlebot_blocked():
    """Small local setting; default to the administrator's requested block."""
    try:
        return json.loads((ROOT / 'crawler_policy.json').read_text()).get('block_googlebot') is not False
    except (OSError, ValueError, AttributeError):
        return True


def set_googlebot_blocked(blocked):
    ROOT.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.crawler-policy-', dir=ROOT)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump({'block_googlebot': bool(blocked)}, stream)
        os.replace(name, ROOT / 'crawler_policy.json')
    finally:
        if os.path.exists(name):
            os.unlink(name)


def page_key(url):
    """Retain known page names/IDs, never free text, public tokens or searches."""
    parts = urlsplit(url)
    path = parts.path.removeprefix('/public') or '/'
    if path.startswith('/groups/'):
        segments = path.split('/')
        # A public group link is a capability: never put it into traffic records.
        path = '/groups/group-' + sha256(segments[2].encode()).hexdigest()[:12]
    elif path not in ('/', '/read', '/compute', '/search'):
        # Use the real public route resolver instead of a second, incomplete
        # route vocabulary. Resolving a path performs no DB query or rendering.
        from .public_pages import resolve
        try:
            resolve(path)
        except ValueError:
            path = '/other'
    # Keep only recognized page parameters; all search strings are discarded.
    params = parse_qs(parts.query[:4096])
    suffix = []
    for key, allowed in [('tab', {'overview','attributes','ranking','killboard','stats','top-kills','history','pilotable-ships','population','economics'}),
                         ('view', {'stats','killboard','ranking','ships','members','rules'}),
                         ('stats_view', {'history','top-kills'})]:
        value = params.get(key, [''])[0]
        if value in allowed:
            suffix.append(key+'='+value)
    return '/public'+path+('?'+'&'.join(suffix) if suffix else '')


def requested_path(url):
    """Readable requested path, excluding queries and obvious capability values."""
    path = url.split('?',1)[0].split('#',1)[0]
    if path.startswith('/public/groups/'):
        return page_key(path)
    parts = []
    hide_next = False
    for part in path.split('/')[:16]:
        if not part:
            parts.append('')
            continue
        sensitive_name = part.lower() in {'token','access_token','password','passwd','secret','api_key','apikey','session','authorization'}
        if (hide_next or len(part)>48 or re.search(r'[^a-zA-Z0-9_.~-]', part)
                or re.fullmatch(r'[a-fA-F0-9]{24,}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}', part)
                or (len(part)>=24 and re.search('[A-Z]',part) and re.search('[0-9]',part))):
            parts.append('[redacted-'+sha256(part.encode()).hexdigest()[:10]+']')
        else:
            parts.append(part)
        hide_next = sensitive_name
    return '/'.join(parts)[:300] or '/'


def client_type(agent):
    """Fixed labels only: UA strings are untrusted and can contain arbitrary data."""
    agent = (agent or '')[:1024].lower()
    for marker, label in [('googlebot','Googlebot (declared)'),('bingbot','Bingbot (declared)'),
                          ('duckduckbot','DuckDuckBot (declared)'),('yandexbot','YandexBot (declared)'),
                          ('bytespider','Bytespider (declared)'),('gptbot','GPTBot (declared)'),
                          ('claudebot','ClaudeBot (declared)'),('ahrefsbot','AhrefsBot (declared)'),
                          ('semrushbot','SemrushBot (declared)'),('curl/','curl'),('wget/','wget'),
                          ('python-requests','Python requests'),('python-httpx','Python HTTPX'),
                          ('headlesschrome','Headless Chrome'),('edg/','Edge'),('firefox/','Firefox'),
                          ('chrome/','Chrome'),('safari/','Safari')]:
        if marker in agent:
            return label
    return 'Other bot (declared)' if re.search(r'bot|crawler|spider',agent) else 'Unidentified client'


def request_details(request):
    target = requested_path(request.url.path)
    if request.url.path == '/public/read':
        endpoint = request.query_params.get('path','')
        if endpoint.startswith('/'):
            target += ' → '+requested_path(endpoint)
    agent = client_type(request.headers.get('user-agent'))
    probe = bool(re.search(r'(?i)(?:/\.env(?:[./]|$)|/\.git(?:/|$)|wp-admin|wp-login|xmlrpc\.php|phpmyadmin|/vendor/phpunit)', target))
    return dict(target=target,method=request.method[:12],client=agent,
                signal='Scanner-like path' if probe else 'Declared crawler' if '(declared)' in agent else
                'Automation client' if agent in ('curl','wget','Python requests','Python HTTPX','Headless Chrome') else 'Unclassified')


def visitor_id(value, day):
    if not re.fullmatch(r'[a-f0-9]{32}', value or ''):
        return None
    return sha256((day+value).encode()).digest()[:16]


@contextmanager
def connection():
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(ROOT / 'traffic.sqlite3', timeout=0.15)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA journal_size_limit=1048576')
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS requests (
            day TEXT, page TEXT, kind TEXT, status INTEGER, count INTEGER, elapsed REAL, maximum REAL,
            PRIMARY KEY(day,page,kind,status)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS visitors (
            day TEXT, visitor BLOB, page TEXT, PRIMARY KEY(day,visitor,page)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS presence (
            visitor BLOB, page TEXT, seen REAL, PRIMARY KEY(visitor,page)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS recent (
            id INTEGER PRIMARY KEY, at REAL, page TEXT, kind TEXT, status INTEGER, elapsed REAL);
        CREATE TABLE IF NOT EXISTS request_details (
            id INTEGER PRIMARY KEY, target TEXT, method TEXT, client TEXT, signal TEXT);
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
        CREATE INDEX IF NOT EXISTS visitors_page ON visitors(day,page);
        CREATE INDEX IF NOT EXISTS presence_seen ON presence(seen);
        ''')
        yield conn
        conn.commit()
    finally:
        conn.close()


def record(page, kind, status, elapsed, cookie=None, now=None, presence=False, details=None):
    global _last_error, _last_warning
    now = time.time() if now is None else now
    day = datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%d')
    visitor = visitor_id(cookie, day)
    try:
        with connection() as conn:
            if presence:
                if visitor:
                    conn.executemany('INSERT OR IGNORE INTO visitors VALUES (?,?,?)', [(day,visitor,''),(day,visitor,page)])
                    conn.execute('INSERT INTO presence VALUES (?,?,?) ON CONFLICT(visitor,page) DO UPDATE SET seen=excluded.seen', (visitor,page,now))
                conn.execute('DELETE FROM presence WHERE seen < ?', (now-90,))
            else:
                conn.execute('''INSERT INTO requests VALUES (?,?,?,?,1,?,?)
                    ON CONFLICT(day,page,kind,status) DO UPDATE SET
                    count=count+1, elapsed=elapsed+excluded.elapsed, maximum=MAX(maximum,excluded.maximum)''',
                    (day,page,kind,status,elapsed,elapsed))
                inserted = conn.execute('INSERT INTO recent(at,page,kind,status,elapsed) VALUES (?,?,?,?,?)', (now,page,kind,status,elapsed))
                if details:
                    conn.execute('INSERT INTO request_details VALUES (?,?,?,?,?)',
                                 (inserted.lastrowid,details['target'],details['method'],details['client'],details['signal']))
                conn.execute('DELETE FROM recent WHERE id <= (SELECT MAX(id)-1000 FROM recent)')
                conn.execute('DELETE FROM request_details WHERE id < (SELECT MIN(id) FROM recent)')
                if visitor and kind == 'page' and 200 <= status < 300:
                    conn.executemany('INSERT OR IGNORE INTO visitors VALUES (?,?,?)', [(day,visitor,''),(day,visitor,page)])
            # Cleanup at most once per minute, shared by all web workers.
            cleaned = conn.execute("SELECT value FROM metadata WHERE key='cleaned'").fetchone()
            if not cleaned or now-float(cleaned[0]) >= 60:
                cutoff = datetime.fromtimestamp(now-RETENTION_DAYS*86400,timezone.utc).strftime('%Y-%m-%d')
                conn.execute('DELETE FROM requests WHERE day <= ?', (cutoff,))
                conn.execute('DELETE FROM visitors WHERE day <= ?', (cutoff,))
                conn.execute('DELETE FROM presence WHERE seen < ?', (now-90,))
                conn.execute("INSERT INTO metadata VALUES ('cleaned',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now),))
            conn.execute("INSERT OR IGNORE INTO metadata VALUES ('started',?)", (str(now),))
        _last_error = False
    except (OSError, sqlite3.Error):
        _last_error = True
        if now-_last_warning >= 60:
            LOG.warning('Public traffic metrics unavailable; requests continue normally.')
            _last_warning = now


def snapshot(day=None):
    now = time.time()
    today = datetime.fromtimestamp(now,timezone.utc).strftime('%Y-%m-%d')
    day = day if re.fullmatch(r'\d{4}-\d{2}-\d{2}', day or '') else today
    try:
        selected_start = datetime.strptime(day,'%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        day = today
        selected_start = datetime.strptime(day,'%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp()
    with connection() as conn:
        days = [dict(r) for r in conn.execute('''SELECT r.day, SUM(r.count) requests,
            SUM(CASE WHEN kind='page' AND status BETWEEN 200 AND 299 THEN count ELSE 0 END) views,
            (SELECT COUNT(*) FROM visitors v WHERE v.day=r.day AND v.page='') unique_visitors
            FROM requests r GROUP BY r.day ORDER BY r.day DESC LIMIT 30''')]
        summary = dict(conn.execute('''SELECT COALESCE(SUM(count),0) requests,
            COALESCE(SUM(CASE WHEN kind='page' AND status BETWEEN 200 AND 299 THEN count ELSE 0 END),0) views,
            COALESCE(SUM(CASE WHEN status>=400 THEN count ELSE 0 END),0) errors,
            COALESCE(SUM(elapsed)/NULLIF(SUM(count),0),0) average,
            COALESCE(MAX(maximum),0) maximum FROM requests WHERE day=?''',(day,)).fetchone())
        summary['unique_visitors'] = conn.execute("SELECT COUNT(*) FROM visitors WHERE day=? AND page=''",(day,)).fetchone()[0]
        active = [dict(r) for r in conn.execute('SELECT page, COUNT(DISTINCT visitor) visitors FROM presence WHERE seen>=? GROUP BY page ORDER BY visitors DESC LIMIT 100',(now-90,))]
        summary['active'] = conn.execute('SELECT COUNT(DISTINCT visitor) FROM presence WHERE seen>=?',(now-90,)).fetchone()[0]
        pages = [dict(r) for r in conn.execute('''SELECT r.page, SUM(r.count) views,
            (SELECT COUNT(*) FROM visitors v WHERE v.day=? AND v.page=r.page) unique_visitors,
            SUM(r.elapsed)/SUM(r.count) average
            FROM requests r WHERE r.day=? AND r.kind='page' AND r.status BETWEEN 200 AND 299
            GROUP BY r.page ORDER BY views DESC LIMIT 100''',(day,day))]
        recent = [dict(r) for r in conn.execute('''SELECT r.at,r.page,r.kind,r.status,r.elapsed,
            d.target,d.method,d.client,d.signal FROM recent r LEFT JOIN request_details d ON d.id=r.id
            ORDER BY r.id DESC LIMIT 100''')]
        diagnostics = [dict(r) for r in conn.execute('''SELECT d.target,d.method,d.client,d.signal,
            r.status,COUNT(*) requests,MIN(r.at) first_seen,MAX(r.at) last_seen
            FROM recent r JOIN request_details d ON d.id=r.id
            WHERE r.at>=? AND r.at<? AND (r.status>=400 OR r.page LIKE '/public/other%')
            GROUP BY d.target,d.method,d.client,d.signal,r.status
            ORDER BY requests DESC,last_seen DESC LIMIT 100''',
            (selected_start,selected_start+86400))]
        started = conn.execute("SELECT value FROM metadata WHERE key='started'").fetchone()
    for r in recent:
        r['at'] = datetime.fromtimestamp(r['at'],timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    for r in diagnostics:
        for key in ('first_seen','last_seen'):
            r[key] = datetime.fromtimestamp(r[key],timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    return dict(day=day,today=today,days=days,summary=summary,pages=pages,active=active,recent=recent,diagnostics=diagnostics,
                started=datetime.fromtimestamp(float(started[0]),timezone.utc).isoformat() if started else None,
                recording_error=_last_error, block_googlebot=googlebot_blocked())


async def traffic_middleware(request, call_next):
    path = request.url.path
    if not (path == '/public' or path.startswith('/public/')):
        return await call_next(request)
    start = time.monotonic()
    if 'googlebot' in request.headers.get('user-agent', '').lower() and googlebot_blocked():
        await run_in_threadpool(record, page_key(str(request.url)), 'blocked', 403,
                                (time.monotonic()-start)*1000, details=request_details(request))
        return PlainTextResponse('Crawler access blocked.', status_code=403,
                                 headers={'Cache-Control': 'no-store'})
    if path == '/public/traffic/presence':
        return await call_next(request)
    response = await call_next(request)
    is_page = path not in ('/public/read','/public/compute') and request.method == 'GET' and 'text/html' in response.headers.get('content-type','')
    cookie = request.cookies.get(COOKIE)
    if is_page and 200 <= response.status_code < 300 and visitor_id(cookie,'check') is None:
        cookie = secrets.token_hex(16)
        response.set_cookie(COOKIE,cookie,max_age=30*86400,httponly=True,secure=request.url.scheme=='https',samesite='lax',path='/public')
    # API URLs contain the page scope; retain its safe page identity only.
    source = request.query_params.get('source','') if path == '/public/read' else ''
    key = page_key(source) if source.startswith('/') else page_key(str(request.url))
    await run_in_threadpool(record,key,'page' if is_page else 'data',response.status_code,
                            (time.monotonic()-start)*1000,cookie,details=request_details(request))
    return response
