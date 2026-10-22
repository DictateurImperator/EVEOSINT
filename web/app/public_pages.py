"""Publication policy and bounded disk snapshots. This module never queries the DB.

Only an authenticated administrator can prepare snapshots. Public requests read
existing HTML, then apply the *current* policy to every link (also on cache hits).
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from html import escape
from html.parser import HTMLParser
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import parse_qsl, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[2] / 'data' / 'public_pages'
MAX_ENTRIES = 256
MAX_BYTES = 64 * 1024 * 1024
MAX_ENTRY_BYTES = 2 * 1024 * 1024
MAX_AGE = 24 * 60 * 60


def tab(key, label, reason=''):
    return dict(key=key, label=label, reason=reason)


def family(key, label, example, tabs, reason=''):
    return dict(key=key, label=label, example=example, tabs=tabs, reason=reason)


_ENTITY_TABS = [tab('overview', 'Overview'), tab('killboard', 'Killboard'),
                tab('stats', 'Combat stats · monthly history'),
                tab('top-kills', 'Combat stats · top kills', 'Scans killmail history; not available to public visitors.'),
                tab('history', 'Affiliation history', 'Interactive historical queries: public adapter not yet reviewed.'),
                tab('pilotable-ships', 'Pilotable ships', 'Interactive capability queries: public adapter not yet reviewed.')]
CATALOG = [
    family('leaderboard', 'Leaderboard', '/leaderboard', [tab(k, label) for k, label in
           [('character', 'Characters'), ('corporation', 'Corporations'), ('alliance', 'Alliances')]]),
    family('superintel', 'SuperINTEL', '/superintel', [tab('dashboard', 'Dashboard'), tab('detailed', 'Detailed dashboard'),
           tab('monthly-analysis', 'Monthly analysis')]),
    *[family(k, label, f'/{k}/' + {'character':'94036773','corporation':'98502318','alliance':'1354830081'}[k], list(_ENTITY_TABS) +
             ([tab('population', 'Population', 'Interactive population scans are not published.')] if k != 'character' else []))
      for k, label in [('character', 'Characters'), ('corporation', 'Corporations'), ('alliance', 'Alliances')]],
    *[family(k, label, f'/{k}/1', [tab('overview', 'Overview'), tab('killboard', 'Killboard')])
      for k, label in [('system', 'Systems'), ('constellation', 'Constellations'), ('region', 'Regions'), ('ship', 'Ships')]],
    family('kill', 'Killmail details', '/kill/138779791', [tab('detail', 'Details and fitting')]),
    family('coalition', 'Coalitions', '/coalition/1', [], 'Historical group expansion needs a separate public adapter.'),
    family('killboard', 'Global killboard', '/killboard', [], 'Global scan and filtering need a separate public adapter.'),
    family('map', 'Universe map', '/map', [], 'Interactive map and heatmap endpoints are not yet reviewed for public use.'),
    family('modules', 'Modules / weapons', '/weapon/1', [], 'Unfinished entity pages.'),
    family('skills', 'Skills', '/skill/1', [], 'Unfinished entity pages.'),
    family('commodities', 'Commodities', '/commodity/1', [], 'Unfinished entity pages.'),
    family('tools', 'Tools', '/tools', [], 'Tools that launch calculations stay behind login.'),
]
FAMILIES = {row['key']: row for row in CATALOG}


@dataclass(frozen=True)
class Target:
    key: str
    tab: str
    path: str
    query: str
    entity_id: int | None = None

    @property
    def source(self):
        return self.path + ('?' + self.query if self.query else '')

    @property
    def public(self):
        return '/public' + self.source

    @property
    def digest(self):
        return sha256(self.source.encode()).hexdigest()


def resolve(value):
    """Strict local URL grammar. No arbitrary internal proxy or cache keys."""
    if not isinstance(value, str) or len(value) > 1000 or '\\' in value:
        raise ValueError('Choose a supported local page URL.')
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.fragment or '//' in parsed.path:
        raise ValueError('Use a local path, for example /leaderboard.')
    path = parsed.path.removeprefix('/public') if parsed.path.startswith('/public/') else parsed.path
    pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=12)
    params = dict(pairs)
    if len(params) != len(pairs):
        raise ValueError('Repeated parameters are not supported.')
    key = None
    entity_id = None
    allowed = {}
    if path == '/leaderboard':
        key = 'leaderboard'
        selected = params.get('kind', 'character')
        allowed = {'kind': ('character', 'corporation', 'alliance'), 'mode': ('classic', 'weighted'),
                   'sort': ('destroyed', 'lost', 'net', 'efficiency', 'kills', 'losses'),
                   'aggregation': ('cumulative', 'monthly'), 'through': r'20\d{2}-(?:0[1-9]|1[0-2])', 'page': r'(?:[1-9]|1\d|20)'}
        defaults = {'kind': 'character', 'mode': 'classic', 'sort': 'destroyed', 'aggregation': 'cumulative', 'page': '1'}
    elif path in ('/superintel', '/superintel/detailed', '/superintel/monthly-analysis'):
        key = 'superintel'
        selected = path.rsplit('/', 1)[-1] if path != '/superintel' else 'dashboard'
        if selected == 'monthly-analysis':
            allowed = {'view': ('pilot', 'corporation', 'alliance'), 'period': r'20\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])',
                       'page': r'(?:[1-9]|1\d|20)', 'page_size': ('50',)}
            defaults = {'view': 'pilot', 'page': '1', 'page_size': '50'}
        else:
            allowed = {'view': ('alliance', 'corporation', 'player'), 'more': ('1',)}
            defaults = {'view': 'alliance'}
    else:
        match = re.fullmatch(r'/(character|corporation|alliance|system|constellation|region|ship|kill)/([1-9]\d{0,11})', path)
        if match:
            key, raw_id = match.groups()
            entity_id = int(raw_id)
        if key == 'kill':
            selected = 'detail'
            defaults = {}
        elif key:
            selected = params.get('tab', 'overview')
            if params.get('stats_view') == 'top-kills':
                selected = 'top-kills'
            allowed = {'tab': tuple(t['key'] for t in FAMILIES[key]['tabs']), 'stats_view': ('history', 'top-kills'),
                       'page': r'(?:[1-9]|1\d|20)', 'mode': ('api', 'hidden', 'total')}
            defaults = {'tab': 'overview', 'stats_view': 'history', 'page': '1', 'mode': 'api'}
    if not key:
        raise ValueError('This page is not available for publication.')
    for name, value in params.items():
        rule = allowed.get(name)
        if rule is None or (value not in rule if isinstance(rule, tuple) else not re.fullmatch(rule, value)):
            raise ValueError('Unsupported public filter: ' + name)
    if not any(t['key'] == selected and not t['reason'] for t in FAMILIES[key]['tabs']):
        raise ValueError('This tab is not ready for public access.')
    if key not in ('character', 'corporation', 'alliance') and selected == 'stats':
        raise ValueError('Combat stats are unavailable for this entity.')
    params = {k: v for k, v in params.items() if defaults.get(k) != v}
    return Target(key, selected, path, urlencode(sorted(params.items())), entity_id)


@contextmanager
def locked():
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _atomic(path, value):
    with tempfile.NamedTemporaryFile('w', dir=ROOT, delete=False, encoding='utf-8') as output:
        temporary = Path(output.name)
        try:
            os.chmod(temporary, 0o600)
            json.dump(value, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def settings():
    try:
        data = json.loads((ROOT / 'settings.json').read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(enabled):
    result = {}
    for page in CATALOG:
        if page['reason']:
            continue
        result[page['key']] = {'enabled': page['key'] in enabled,
            'tabs': [t['key'] for t in page['tabs'] if not t['reason'] and page['key'] + ':' + t['key'] in enabled]}
    with locked():
        _atomic(ROOT / 'settings.json', result)
    return result


def published(target, policy):
    page = policy.get(target.key, {})
    return bool(page.get('enabled') and target.tab in page.get('tabs', []))


def entries():
    # Read only a small metadata index, never every cached HTML page on a GET.
    try:
        path = ROOT / 'index.json'
        if path.stat().st_size > 256 * 1024:
            return []
        rows = json.loads(path.read_text())
        if not isinstance(rows, list):
            return []
        result = []
        for row in rows[:MAX_ENTRIES]:
            target = resolve(row['source'])
            result.append(dict(source=target.source, public=target.public, key=target.key, tab=target.tab,
                               created=row['created'], bytes=row['bytes'],
                               fresh=time.time() - row['created'] < MAX_AGE))
        return sorted(result, key=lambda row: row['created'], reverse=True)
    except (OSError, ValueError, KeyError, TypeError):
        return []


def store(target, html):
    data = dict(source=target.source, created=time.time(), html=html)
    if len(json.dumps(data, ensure_ascii=False).encode()) > MAX_ENTRY_BYTES:
        raise ValueError('Snapshot exceeds the 2 MiB page limit. Choose a smaller view.')
    with locked():
        path = ROOT / ('snapshot-' + target.digest + '.json')
        _atomic(path, data)
        rows = [row for row in entries() if row['source'] != target.source and row['fresh']]
        rows.insert(0, dict(source=target.source, created=data['created'], bytes=path.stat().st_size))
        size = sum(row['bytes'] for row in rows)
        while len(rows) > MAX_ENTRIES or size > MAX_BYTES:
            row = rows.pop()
            size -= row['bytes']
        retained = {'snapshot-' + resolve(row['source']).digest + '.json' for row in rows}
        # Also clean orphan files left by an interrupted atomic index update.
        for old in ROOT.glob('snapshot-*.json'):
            if old.name not in retained:
                old.unlink(missing_ok=True)
        _atomic(ROOT / 'index.json', [dict(source=row['source'], created=row['created'], bytes=row['bytes']) for row in rows])


def load(target):
    try:
        path = ROOT / ('snapshot-' + target.digest + '.json')
        if path.stat().st_size > MAX_ENTRY_BYTES:
            return None
        data = json.loads(path.read_text())
        if data['source'] != target.source or time.time() - data['created'] >= MAX_AGE:
            return None
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def clear():
    with locked():
        for path in ROOT.glob('snapshot-*.json'):
            path.unlink(missing_ok=True)
        _atomic(ROOT / 'index.json', [])


def link_url(value, policy, current='/'):
    if not value:
        return None
    if value.startswith('#'):
        return value
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return value if parsed.scheme in ('http', 'https') and parsed.hostname and parsed.hostname not in ('eveosint.com', 'www.eveosint.com') else None
    if value.startswith('?'):
        value = current.split('?', 1)[0] + value
    try:
        target = resolve(value)
    except ValueError:
        return None
    return target.public if published(target, policy) else None


class PublicHTML(HTMLParser):
    """Keep shared markup/styles, remove executable/private controls and links.

    The input is trusted server-rendered Jinja HTML, never arbitrary remote HTML.
    Parsing on each response means revocation also applies to old snapshots.
    """
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
    DROP = {'script', 'form', 'button', 'iframe', 'object', 'embed', 'input', 'select', 'textarea'}

    def __init__(self, policy, current):
        super().__init__(convert_charrefs=False)
        self.policy, self.current = policy, current
        self.stack = []
        self.suppressed = 0
        self.out = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        drop = self.suppressed > 0 or tag in self.DROP
        output_tag = tag
        if tag == 'a' and not drop:
            if attrs.get('data-killmail-page'):
                parts = urlsplit(self.current)
                params = dict(parse_qsl(parts.query))
                params.update(tab='killboard', page=attrs['data-killmail-page'])
                attrs['href'] = parts.path + '?' + urlencode(params)
            href = link_url(attrs.get('href'), self.policy, self.current)
            if href:
                attrs['href'] = href
            else:
                # Suppress disabled navigation tabs entirely; preserve names/logos elsewhere.
                if 'nav-link' in attrs.get('class', '').split() or 'context-link' in attrs.get('class', '').split():
                    drop = True
                output_tag = 'span'
                attrs.pop('href', None)
        if not drop:
            clean = []
            for key, value in attrs.items():
                if key.startswith('on') or key.startswith('data-') or key in ('action', 'formaction', 'srcdoc', 'autofocus'):
                    continue
                if key in ('href', 'src', 'xlink:href'):
                    if key != 'href' and value and not (value.startswith(('https://images.evetech.net/', '/static/', '#', 'data:image/png;base64,'))):
                        continue
                    if value and value.lower().startswith(('javascript:', 'vbscript:')):
                        continue
                key = {'viewbox': 'viewBox', 'preserveaspectratio': 'preserveAspectRatio', 'gradientunits': 'gradientUnits'}.get(key, key)
                clean.append(' ' + key + ('="' + escape(value, quote=True) + '"' if value is not None else ''))
            self.out.append('<' + output_tag + ''.join(clean) + '>')
        if tag not in self.VOID:
            self.stack.append((tag, output_tag, drop))
            if drop:
                self.suppressed += 1

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1][0] != tag:
            return
        _, output_tag, drop = self.stack.pop()
        if drop:
            self.suppressed -= 1
        elif output_tag:
            self.out.append('</' + output_tag + '>')

    def handle_data(self, data):
        if not self.suppressed:
            self.out.append(data)

    def handle_entityref(self, name):
        self.handle_data('&' + name + ';')

    def handle_charref(self, name):
        self.handle_data('&#' + name + ';')


def sanitize(html, policy, current):
    parser = PublicHTML(policy, current)
    parser.feed(html)
    parser.close()
    return ''.join(parser.out)


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
