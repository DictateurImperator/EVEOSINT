"""Bounded audience pseudonyms and separate, incident-only security IP records."""
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import logging
import fcntl
import hmac
import ipaddress
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
from . import traffic_countries

ROOT = Path(__file__).resolve().parents[2] / 'data' / 'public_traffic'
COOKIE = 'eveosint_public_visitor'
ACCESS_COOKIE = 'eveosint_public_notice_v1'
RETENTION_DAYS = 30
LOG = logging.getLogger(__name__)
_last_error = False
_last_warning = 0


def googlebot_blocked():
    """Allow cache-only indexing by default; preserve explicit administrator blocks."""
    try:
        return json.loads((ROOT / 'crawler_policy.json').read_text()).get('block_googlebot') is not False
    except FileNotFoundError:
        return False
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
                country=traffic_countries.country(request.client.host if request.client else None),
                signal='Scanner-like path' if probe else 'Declared crawler' if '(declared)' in agent else
                'Automation client' if agent in ('curl','wget','Python requests','Python HTTPX','Headless Chrome') else 'Unclassified')


def visitor_id(value, day):
    if not re.fullmatch(r'[a-f0-9]{32}', value or ''):
        return None
    return sha256((day+value).encode()).digest()[:16]


def ip_visitor_id(address, day):
    """Daily keyed pseudonym; neither the IP nor an unkeyed IP hash is persisted."""
    try:
        ip = ipaddress.ip_address(address)
    except (ValueError,TypeError):
        return None
    ip = getattr(ip,'ipv4_mapped',None) or ip
    ROOT.mkdir(parents=True,exist_ok=True,mode=0o700)
    directory = ROOT/'visitor-keys'
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (directory/'lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        path=directory/(day+'.key')
        if not path.exists():
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as out:out.write(secrets.token_bytes(32))
        key=path.read_bytes()
        if len(key)!=32:
            raise OSError('Visitor key unavailable')
        for previous in directory.glob('????-??-??.key'):
            if previous.stem<day:previous.unlink(missing_ok=True)
    return hmac.new(key,(day+'\0'+str(ip)).encode(),'sha256').digest()


def access_choice(choice, issued=None):
    """Signed acknowledgement cookie, separate from the daily audience pseudonym."""
    ROOT.mkdir(parents=True,exist_ok=True,mode=0o700)
    directory=ROOT/'visitor-keys';directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (directory/'lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        path=directory/'access.key'
        if not path.exists():
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as out:out.write(secrets.token_bytes(32))
        key=path.read_bytes()
    body=f'{choice}.{int(time.time() if issued is None else issued)}.1'
    return body+'.'+hmac.new(key,body.encode(),'sha256').hexdigest()


def access_accepted(request):
    return access_decision(request)=='accepted'


def access_decision(request):
    value=request.cookies.get(ACCESS_COOKIE,'')
    parts=value.split('.')
    if len(parts)!=4 or parts[0] not in ('accepted','declined') or not parts[1].isdigit() or parts[2]!='1':return None
    if not 0<=time.time()-int(parts[1])<=180*86400:return None
    try:return parts[0] if hmac.compare_digest(value,access_choice(parts[0],int(parts[1]))) else None
    except OSError:return None


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
        CREATE TABLE IF NOT EXISTS country_requests (
            day TEXT, country TEXT, kind TEXT, status INTEGER, count INTEGER,
            PRIMARY KEY(day,country,kind,status)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS country_visitors (
            day TEXT, visitor BLOB, country TEXT, PRIMARY KEY(day,visitor,country)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS recent_countries (id INTEGER PRIMARY KEY, country TEXT);
        CREATE TABLE IF NOT EXISTS country_page_views (
            day TEXT, country TEXT, page TEXT, count INTEGER, elapsed REAL,
            PRIMARY KEY(day,country,page)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS country_page_visitors (
            day TEXT, country TEXT, page TEXT, visitor BLOB,
            PRIMARY KEY(day,country,page,visitor)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS ip_visitors (
            day TEXT, visitor BLOB, page TEXT, PRIMARY KEY(day,visitor,page)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS ip_presence (
            visitor BLOB, page TEXT, seen REAL, PRIMARY KEY(visitor,page)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS ip_country_visitors (
            day TEXT, visitor BLOB, country TEXT, PRIMARY KEY(day,visitor,country)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS security_ips (
            ip TEXT PRIMARY KEY, first_seen REAL, last_seen REAL,
            blocked INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS recent_ips (id INTEGER PRIMARY KEY, ip TEXT);
        CREATE INDEX IF NOT EXISTS visitors_page ON visitors(day,page);
        CREATE INDEX IF NOT EXISTS presence_seen ON presence(seen);
        CREATE INDEX IF NOT EXISTS country_visitors_country ON country_visitors(day,country);
        CREATE INDEX IF NOT EXISTS ip_visitors_page ON ip_visitors(day,page);
        CREATE INDEX IF NOT EXISTS ip_presence_seen ON ip_presence(seen);
        CREATE INDEX IF NOT EXISTS ip_country_visitors_country ON ip_country_visitors(day,country);
        CREATE INDEX IF NOT EXISTS security_ips_retention ON security_ips(blocked,last_seen);
        ''')
        yield conn
        conn.commit()
    finally:
        conn.close()


def record(page, kind, status, elapsed, cookie=None, now=None, presence=False, details=None, client_ip=None, security_incident=False):
    global _last_error, _last_warning
    now = time.time() if now is None else now
    day = datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%d')
    try:
        visitor = ip_visitor_id(client_ip,day)
        with connection() as conn:
            if visitor and security_incident:
                ip=str(ipaddress.ip_address(client_ip))
                conn.execute('''INSERT INTO security_ips(ip,first_seen,last_seen) VALUES (?,?,?)
                    ON CONFLICT(ip) DO UPDATE SET last_seen=excluded.last_seen''',(ip,now,now))
            if presence:
                if visitor:
                    conn.executemany('INSERT OR IGNORE INTO ip_visitors VALUES (?,?,?)', [(day,visitor,''),(day,visitor,page)])
                    conn.execute('INSERT INTO ip_presence VALUES (?,?,?) ON CONFLICT(visitor,page) DO UPDATE SET seen=excluded.seen', (visitor,page,now))
                conn.execute('DELETE FROM ip_presence WHERE seen < ?', (now-90,))
            else:
                conn.execute('''INSERT INTO requests VALUES (?,?,?,?,1,?,?)
                    ON CONFLICT(day,page,kind,status) DO UPDATE SET
                    count=count+1, elapsed=elapsed+excluded.elapsed, maximum=MAX(maximum,excluded.maximum)''',
                    (day,page,kind,status,elapsed,elapsed))
                inserted = conn.execute('INSERT INTO recent(at,page,kind,status,elapsed) VALUES (?,?,?,?,?)', (now,page,kind,status,elapsed))
                if visitor and security_incident:conn.execute('INSERT INTO recent_ips VALUES (?,?)',(inserted.lastrowid,ip))
                if details:
                    conn.execute('INSERT INTO request_details VALUES (?,?,?,?,?)',
                                 (inserted.lastrowid,details['target'],details['method'],details['client'],details['signal']))
                    country = details.get('country','ZZ')
                    if not isinstance(country,str) or not re.fullmatch('[A-Z]{2}',country):
                        country = 'ZZ'
                    conn.execute('INSERT INTO recent_countries VALUES (?,?)',(inserted.lastrowid,country))
                    conn.execute('''INSERT INTO country_requests VALUES (?,?,?,?,1)
                        ON CONFLICT(day,country,kind,status) DO UPDATE SET count=count+1''',(day,country,kind,status))
                    if visitor and kind == 'page' and 200 <= status < 300:
                        conn.execute('INSERT OR IGNORE INTO ip_country_visitors VALUES (?,?,?)',(day,visitor,country))
                    if kind == 'page' and 200 <= status < 300:
                        conn.execute('''INSERT INTO country_page_views VALUES (?,?,?,1,?)
                            ON CONFLICT(day,country,page) DO UPDATE SET
                            count=count+1,elapsed=elapsed+excluded.elapsed''',(day,country,page,elapsed))
                        if visitor:
                            conn.execute('INSERT OR IGNORE INTO country_page_visitors VALUES (?,?,?,?)',
                                         (day,country,page,visitor))
                        conn.execute("INSERT OR IGNORE INTO metadata VALUES ('country_pages_started',?)",(str(now),))
                conn.execute('DELETE FROM recent WHERE id <= (SELECT MAX(id)-1000 FROM recent)')
                conn.execute('DELETE FROM request_details WHERE id < (SELECT MIN(id) FROM recent)')
                conn.execute('DELETE FROM recent_countries WHERE id < (SELECT MIN(id) FROM recent)')
                conn.execute('DELETE FROM recent_ips WHERE id < (SELECT MIN(id) FROM recent)')
                if visitor and kind == 'page' and 200 <= status < 300:
                    conn.executemany('INSERT OR IGNORE INTO ip_visitors VALUES (?,?,?)', [(day,visitor,''),(day,visitor,page)])
            # Cleanup at most once per minute, shared by all web workers.
            cleaned = conn.execute("SELECT value FROM metadata WHERE key='cleaned'").fetchone()
            if not cleaned or now-float(cleaned[0]) >= 60:
                cutoff = datetime.fromtimestamp(now-RETENTION_DAYS*86400,timezone.utc).strftime('%Y-%m-%d')
                conn.execute('DELETE FROM requests WHERE day <= ?', (cutoff,))
                conn.execute('DELETE FROM visitors WHERE day <= ?', (cutoff,))
                conn.execute('DELETE FROM country_requests WHERE day <= ?', (cutoff,))
                conn.execute('DELETE FROM country_visitors WHERE day <= ?', (cutoff,))
                conn.execute('DELETE FROM country_page_views WHERE day <= ?', (cutoff,))
                conn.execute('DELETE FROM country_page_visitors WHERE day <= ?', (cutoff,))
                conn.execute('DELETE FROM ip_visitors WHERE day <= ?', (cutoff,))
                conn.execute('DELETE FROM ip_country_visitors WHERE day <= ?', (cutoff,))
                conn.execute('DELETE FROM ip_presence WHERE seen < ?', (now-90,))
                conn.execute('DELETE FROM security_ips WHERE blocked=0 AND last_seen<?',(now-RETENTION_DAYS*86400,))
                conn.execute('DELETE FROM recent_ips WHERE id IN (SELECT id FROM recent WHERE at<?)',(now-RETENTION_DAYS*86400,))
                conn.execute('DELETE FROM presence WHERE seen < ?', (now-90,))
                conn.execute("INSERT INTO metadata VALUES ('cleaned',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now),))
            conn.execute("INSERT OR IGNORE INTO metadata VALUES ('started',?)", (str(now),))
            conn.execute("INSERT OR IGNORE INTO metadata VALUES ('ip_uniques_started',?)", (str(now),))
        _last_error = False
    except (OSError, sqlite3.Error):
        _last_error = True
        if now-_last_warning >= 60:
            LOG.warning('Public traffic metrics unavailable; requests continue normally.')
            _last_warning = now


def purge():
    """Also runs without traffic; blocked addresses survive the 30-day purge."""
    now=time.time();cutoff=datetime.fromtimestamp(now-RETENTION_DAYS*86400,timezone.utc).strftime('%Y-%m-%d')
    with connection() as conn:
        for table in ('requests','visitors','country_requests','country_visitors','country_page_views',
                      'country_page_visitors','ip_visitors','ip_country_visitors'):
            conn.execute(f'DELETE FROM {table} WHERE day<=?',(cutoff,))
        conn.execute('DELETE FROM security_ips WHERE blocked=0 AND last_seen<?',(now-RETENTION_DAYS*86400,))
        conn.execute('DELETE FROM recent_ips WHERE id IN (SELECT id FROM recent WHERE at<?)',(now-RETENTION_DAYS*86400,))
        conn.execute('DELETE FROM recent WHERE at<?',(now-RETENTION_DAYS*86400,))
        for table in ('request_details','recent_countries','recent_ips'):
            conn.execute(f'DELETE FROM {table} WHERE id NOT IN (SELECT id FROM recent)')
        conn.execute('DELETE FROM ip_presence WHERE seen<?',(now-90,))
        conn.execute('DELETE FROM presence WHERE seen<?',(now-90,))
    directory=ROOT/'visitor-keys'
    if directory.exists():
        today=datetime.fromtimestamp(now,timezone.utc).strftime('%Y-%m-%d')
        with (directory/'lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            for key in directory.glob('????-??-??.key'):
                if key.stem<today:key.unlink(missing_ok=True)


def start_cleanup():
    import threading
    def loop():
        while True:
            try:purge()
            except (OSError,sqlite3.Error):LOG.warning('Public traffic cleanup unavailable.')
            threading.Event().wait(3600)
    threading.Thread(target=loop,name='public-traffic-cleanup',daemon=True).start()


def ip_blocked(address):
    try:
        ip=str(ipaddress.ip_address(address))
        with connection() as conn:
            row=conn.execute('SELECT blocked FROM security_ips WHERE ip=?',(ip,)).fetchone()
        return bool(row and row[0])
    except (OSError,sqlite3.Error,ValueError,TypeError):return False


def snapshot(day=None, country=''):
    # Empty selects all countries; never interpolate an untrusted country into SQL.
    country = country.upper() if isinstance(country,str) else ''
    if country and not re.fullmatch('[A-Z]{2}',country):
        raise ValueError('Choose a two-letter country code.')
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
            (SELECT COUNT(*) FROM ip_visitors v WHERE v.day=r.day AND v.page='') unique_visitors
            FROM requests r GROUP BY r.day ORDER BY r.day DESC LIMIT 30''')]
        summary = dict(conn.execute('''SELECT COALESCE(SUM(count),0) requests,
            COALESCE(SUM(CASE WHEN kind='page' AND status BETWEEN 200 AND 299 THEN count ELSE 0 END),0) views,
            COALESCE(SUM(CASE WHEN status>=400 THEN count ELSE 0 END),0) errors,
            COALESCE(SUM(elapsed)/NULLIF(SUM(count),0),0) average,
            COALESCE(MAX(maximum),0) maximum FROM requests WHERE day=?''',(day,)).fetchone())
        summary['unique_visitors'] = conn.execute("SELECT COUNT(*) FROM ip_visitors WHERE day=? AND page=''",(day,)).fetchone()[0]
        active = [dict(r) for r in conn.execute('SELECT page, COUNT(DISTINCT visitor) visitors FROM ip_presence WHERE seen>=? GROUP BY page ORDER BY visitors DESC LIMIT 100',(now-90,))]
        summary['active'] = conn.execute('SELECT COUNT(DISTINCT visitor) FROM ip_presence WHERE seen>=?',(now-90,)).fetchone()[0]
        if country:
            pages = [dict(r) for r in conn.execute('''SELECT r.page,r.count views,
                (SELECT COUNT(*) FROM country_page_visitors v
                 WHERE v.day=r.day AND v.country=r.country AND v.page=r.page) unique_visitors,
                r.elapsed/r.count average FROM country_page_views r
                WHERE r.day=? AND r.country=? ORDER BY views DESC,r.page LIMIT 100''',(day,country))]
        else:
            pages = [dict(r) for r in conn.execute('''SELECT r.page, SUM(r.count) views,
                (SELECT COUNT(*) FROM ip_visitors v WHERE v.day=? AND v.page=r.page) unique_visitors,
                SUM(r.elapsed)/SUM(r.count) average
                FROM requests r WHERE r.day=? AND r.kind='page' AND r.status BETWEEN 200 AND 299
                GROUP BY r.page ORDER BY views DESC,r.page LIMIT 100''',(day,day))]
        recent = [dict(r) for r in conn.execute('''SELECT r.at,r.page,r.kind,r.status,r.elapsed,
            d.target,d.method,d.client,d.signal,c.country,i.ip FROM recent r LEFT JOIN request_details d ON d.id=r.id
            LEFT JOIN recent_countries c ON c.id=r.id
            LEFT JOIN recent_ips i ON i.id=r.id
            ORDER BY r.id DESC LIMIT 100''')]
        diagnostics = [dict(r) for r in conn.execute('''SELECT d.target,d.method,d.client,d.signal,
            r.status,COUNT(*) requests,MIN(r.at) first_seen,MAX(r.at) last_seen
            FROM recent r JOIN request_details d ON d.id=r.id
            WHERE r.at>=? AND r.at<? AND (r.status>=400 OR r.page LIKE '/public/other%')
            GROUP BY d.target,d.method,d.client,d.signal,r.status
            ORDER BY requests DESC,last_seen DESC LIMIT 100''',
            (selected_start,selected_start+86400))]
        started = conn.execute("SELECT value FROM metadata WHERE key='started'").fetchone()
        ip_uniques_started = conn.execute("SELECT value FROM metadata WHERE key='ip_uniques_started'").fetchone()
        blocked_ips=[dict(r) for r in conn.execute('SELECT ip,reason,last_seen FROM security_ips WHERE blocked=1 ORDER BY last_seen DESC')]
        country_pages_started = conn.execute("SELECT value FROM metadata WHERE key='country_pages_started'").fetchone()
        countries = [dict(r) for r in conn.execute('''SELECT country,SUM(count) requests,
            SUM(CASE WHEN kind='page' AND status BETWEEN 200 AND 299 THEN count ELSE 0 END) views,
            SUM(CASE WHEN kind='blocked' THEN count ELSE 0 END) blocked,
            (SELECT COUNT(*) FROM ip_country_visitors v WHERE v.day=? AND v.country=r.country) unique_visitors
            FROM country_requests r WHERE day=? GROUP BY country
            ORDER BY unique_visitors DESC, views DESC, requests DESC''',(day,day))]
    from .public_security import GOOGLE_IPS
    blocked_ips = [r for r in blocked_ips if r['ip'] not in GOOGLE_IPS]
    blocked_ips += [dict(ip=ip, reason='GOOGLE BOT', enforced_by='Nginx', last_seen=None) for ip in GOOGLE_IPS]
    for r in recent:
        r['at'] = datetime.fromtimestamp(r['at'],timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    for r in diagnostics:
        for key in ('first_seen','last_seen'):
            r[key] = datetime.fromtimestamp(r[key],timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    return dict(day=day,today=today,days=days,summary=summary,pages=pages,active=active,recent=recent,diagnostics=diagnostics,
                started=datetime.fromtimestamp(float(started[0]),timezone.utc).isoformat() if started else None,
                recording_error=_last_error, block_googlebot=googlebot_blocked(),
                ip_uniques_started=datetime.fromtimestamp(float(ip_uniques_started[0]),timezone.utc).isoformat() if ip_uniques_started else None,
                countries=countries, country_database=traffic_countries.status(), country=country,blocked_ips=blocked_ips,
                country_pages_started=datetime.fromtimestamp(float(country_pages_started[0]),timezone.utc).isoformat() if country_pages_started else None)


async def traffic_middleware(request, call_next):
    path = request.url.path
    from .public_security import blocked_crawler
    if path != '/robots.txt' and blocked_crawler(request.client.host if request.client else None, request.headers.get('user-agent', '')):
        return PlainTextResponse('Access denied.', status_code=403, headers={'Cache-Control': 'no-store'})
    if not (path == '/public' or path.startswith('/public/')):
        return await call_next(request)
    if path in ('/public/access/choice','/public/legal'):return await call_next(request)
    start = time.monotonic()
    from .public_security import abusive_rate
    if abusive_rate(request.client.host if request.client else None):
        await run_in_threadpool(record,page_key(str(request.url)),'blocked',429,0,details=request_details(request),
                                client_ip=request.client.host if request.client else None,security_incident=True)
        with connection() as conn:
            conn.execute("UPDATE security_ips SET blocked=1,reason='Public request rate exceeded' WHERE ip=?",(request.client.host,))
        return PlainTextResponse('Request rate exceeded.',status_code=429,headers={'Cache-Control':'no-store','Retry-After':'60'})
    if await run_in_threadpool(ip_blocked,request.client.host if request.client else None):
        await run_in_threadpool(record,page_key(str(request.url)),'blocked',403,0,
                                details=request_details(request),client_ip=request.client.host if request.client else None,security_incident=True)
        return PlainTextResponse('Access denied.',status_code=403,headers={'Cache-Control':'no-store'})
    crawler = 'googlebot' in request.headers.get('user-agent', '').lower()
    if crawler:
        response = await run_in_threadpool(googlebot_access, request)
        if response is not None:
            await run_in_threadpool(record, page_key(str(request.url)), 'blocked', response.status_code,
                                    (time.monotonic()-start)*1000, details=request_details(request),client_ip=request.client.host if request.client else None)
            return response
    if not crawler and (path=='/public/access-required' or access_decision(request) is None):
        if request.method=='GET' and path not in ('/public/read','/public/compute','/public/traffic/presence'):
            from .main_objects import templates
            return templates.TemplateResponse(request=request,name='public_access_notice.html',context=dict(
                request=request,user=None,show_app_layout=False,public_site=True,public_search=False,public_notice=True,
                title='EVEOSINT · Privacy and security'),headers={'Cache-Control':'no-store'})
        return PlainTextResponse('Please acknowledge the site notice before continuing.',status_code=403,headers={'Cache-Control':'no-store'})
    if path == '/public/traffic/presence':
        return await call_next(request)
    response = await call_next(request)
    if not crawler and not access_accepted(request):
        return response  # Refusal keeps access and creates no audience record.
    is_page = path not in ('/public/read','/public/compute') and request.method == 'GET' and 'text/html' in response.headers.get('content-type','')
    cookie = None
    if request.cookies.get(COOKIE):response.delete_cookie(COOKIE,path='/public')
    # API URLs contain the page scope; retain its safe page identity only.
    source = request.query_params.get('source','') if path == '/public/read' else ''
    key = page_key(source) if source.startswith('/') else page_key(str(request.url))
    await run_in_threadpool(record,key,'page' if is_page else 'data',response.status_code,
                            (time.monotonic()-start)*1000,cookie,details=request_details(request),client_ip=request.client.host if request.client else None)
    return response


def googlebot_access(request):
    """No database, renderer, data endpoint or background task on a crawler miss."""
    def denied(status=403):
        headers = {'Cache-Control': 'no-store'}
        if status == 503:
            headers['Retry-After'] = '3600'
        return PlainTextResponse('Crawler access blocked.' if status == 403 else 'Cached page unavailable.',
                                 status_code=status, headers=headers)
    if googlebot_blocked():
        return denied()
    path = request.url.path
    if request.method != 'GET' or request.url.query:
        return denied()
    if path in ('/public', '/public/', '/public/entities'):
        return None
    from . import public_pages as public
    try:
        target = public.resolve(path)
    except ValueError:
        return denied()
    if (target.key in ('tools', 'map', 'coalition') or target.tab not in ('overview', 'detail', 'index', 'character', 'dashboard', 'killboard')
            or target.path == '/ship-category'):
        return denied()
    if not public.published(target, public.settings()):
        return PlainTextResponse('Not found.', status_code=404, headers={'Cache-Control':'no-store'})
    snapshot = public.load(target, allow_stale=True)
    if snapshot is None:
        return denied(503)
    # Pass the actual snapshot to the shared gateway so expiry/eviction cannot
    # cause a subsequent cache check to fall through to the expensive renderer.
    request.state.googlebot_snapshot = snapshot
    return None
