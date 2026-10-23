"""Publication policy and bounded disk snapshots. This module never queries the DB.

Public requests render allowed pages on demand and keep only recent results.
The current publication policy applies to every response, including cache hits.
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
MAX_AGE = 5 * 60
BROWSER_MAX_AGE = 60
BUILD_SLOTS = 2


def tab(key, label, reason=''):
    return dict(key=key, label=label, reason=reason)


def family(key, label, example, tabs, reason=''):
    return dict(key=key, label=label, example=example, tabs=tabs, reason=reason)


_ENTITY_TABS = [tab('overview', 'Overview'), tab('killboard', 'Killboard'),
                tab('stats', 'Combat stats · monthly history', 'Reads stored monthly scores; usually light.'),
                tab('top-kills', 'Combat stats · top kills', 'Scans killmail history on demand; can be expensive.'),
                tab('history', 'Affiliation history', 'Historical membership queries; cost depends on the selected period.'),
                tab('pilotable-ships', 'Pilotable ships', 'Capability queries can be expensive for large groups.')]
CATALOG = [
    family('leaderboard', 'Leaderboard', '/leaderboard', [tab(k, label) for k, label in
           [('character', 'Characters'), ('corporation', 'Corporations'), ('alliance', 'Alliances')]]),
    family('superintel', 'SuperINTEL', '/superintel', [tab('dashboard', 'Dashboard'), tab('detailed', 'Detailed dashboard'),
           tab('monthly-analysis', 'Monthly analysis')]),
    *[family(k, label, f'/{k}/' + {'character':'94036773','corporation':'98502318','alliance':'1354830081'}[k], list(_ENTITY_TABS) +
             ([tab('population', 'Population', 'Population scans can be expensive for large groups or long periods.')] if k != 'character' else []))
      for k, label in [('character', 'Characters'), ('corporation', 'Corporations'), ('alliance', 'Alliances')]],
    *[family(k, label, f'/{k}/1', [tab('overview', 'Overview'), tab('killboard', 'Killboard')])
      for k, label in [('system', 'Systems'), ('constellation', 'Constellations'), ('region', 'Regions'), ('ship', 'Ships')]],
    family('kill', 'Killmail details', '/kill/138779791', [tab('detail', 'Details and fitting')]),
    family('coalition', 'Coalitions', '/coalition/1', [tab('killboard','Killboard'),tab('population','Population'),tab('history','History'),tab('pilotable-ships','Pilotable ships')], 'Historical membership expansion can be expensive for large coalitions.'),
    family('killboard', 'Killboard', '/killboard', [tab('killboard', 'Killboard')]),
    family('map', 'Universe map', '/map', [tab('universe','Universe'),tab('2d','2D map'),tab('region','Region'),tab('constellation','Constellation'),tab('system','System')], 'Interactive maps and heatmaps can run expensive queries on demand.'),
    family('modules', 'Modules / weapons', '/weapon/1', [tab('overview','Page')], 'Unfinished entity pages.'),
    family('skills', 'Skills', '/skill/1', [tab('overview','Page')], 'Unfinished entity pages.'),
    family('commodities', 'Commodities', '/commodity/1', [tab('overview','Page')], 'Unfinished entity pages.'),
    family('tools', 'Tools', '/tools', [tab('overview','Tools menu'),tab('ship-analysis','Ship analysis'),tab('super-evolution','Super evolution'),tab('population','Population comparison'),tab('economics','Economics comparison')], 'Heavy calculations: work starts when a visitor requests a result.'),
]
FAMILIES = {row['key']: row for row in CATALOG}


ENTITY_INDEX_KEYS = {key:key for key in ('character','corporation','alliance','coalition','ship','system','constellation','region')}
ENTITY_INDEX_KEYS.update(weapon='modules',skill='skills',commodity='commodities')


EXTRA_PAGES = {
    '/tools':('tools','overview'), '/ship-analysis':('tools','ship-analysis'),
    '/superintel/evolution':('tools','super-evolution'),
    '/tools/population':('tools','population'), '/tools/economics':('tools','economics'),
    '/map':('map','universe'), '/map/eve-2d':('map','2d'),
}


def tab_path(key, selected, current):
    for path,(family_key,tab_key) in EXTRA_PAGES.items():
        if (family_key,tab_key)==(key,selected):return path
    if key=='map' and selected in ('region','constellation','system'):
        if current.startswith('/map/'+selected+'/'):return current
        return '/map/'+selected+'/'+{'region':'10000002','constellation':'20000020','system':'30000142'}[selected]
    return current


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
    def read_scope_url(self):
        # Scope is publication identity only. Avoid duplicating full filter/date
        # queries in every API URL/cache key (the API URL already contains them).
        if self.key == 'leaderboard':
            return '/leaderboard?' + urlencode({'kind':self.tab})
        if self.entity_id is not None and self.key != 'kill':
            return f"/{ {'modules':'weapon','skills':'skill','commodities':'commodity'}.get(self.key,self.key) }/{self.entity_id}" + ('?' + urlencode({'tab':self.tab}) if self.tab != 'overview' else '')
        return self.path

    @property
    def digest(self):
        return sha256(self.source.encode()).hexdigest()


def resolve(value):
    """Strict local URL grammar. No arbitrary internal proxy or cache keys."""
    if not isinstance(value, str) or len(value) > 8000 or '\\' in value:
        raise ValueError('Choose a supported local page URL.')
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.fragment or '//' in parsed.path:
        raise ValueError('Use a local path, for example /leaderboard.')
    path = parsed.path.removeprefix('/public') if parsed.path.startswith('/public/') else parsed.path
    pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=80)
    params = dict(pairs)
    repeated = {name for name, _ in pairs if sum(k == name for k, _ in pairs) > 1}
    list_names = {'include', 'exclude', 'type_ids', 'module_type_ids', 'affiliation_corporation_ids',
        'affiliation_alliance_ids', 'involved_corporation_ids', 'involved_alliance_ids',
        'builder_ship_include','builder_ship_exclude','builder_entity_include','builder_entity_exclude',
        'builder_zone_include','builder_zone_exclude'}
    if repeated - list_names and not (path in EXTRA_PAGES or path.startswith('/map/')):
        raise ValueError('Repeated scalar parameters are not supported.')
    key = selected = None
    entity_id = None
    allowed = {}
    if path.startswith('/entities/') and path.removeprefix('/entities/') in ENTITY_INDEX_KEYS:
        key,selected=ENTITY_INDEX_KEYS[path.removeprefix('/entities/')],'index'
        allowed={'q':r'.{0,100}','tier':r'.{1,40}','group':r'.{1,80}','size':r'.{1,40}','faction':r'.{1,80}'}
        defaults={}
    elif path in EXTRA_PAGES:
        key,selected=EXTRA_PAGES[path]
        allowed={name:r'.{0,200}' for name in params if re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,60}',name)}
        defaults={}
    elif re.fullmatch(r'/map/(region|constellation|system)/[1-9]\d{0,11}',path):
        key,selected='map',path.split('/')[2]
        allowed={name:r'.{0,200}' for name in params if re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,60}',name)}
        defaults={}
    elif path == '/leaderboard':
        key = 'leaderboard'
        selected = params.get('kind', 'character')
        allowed = {'kind': ('character', 'corporation', 'alliance'), 'mode': ('classic', 'weighted'),
                   'sort': ('destroyed', 'lost', 'net', 'efficiency', 'kills', 'losses'),
                   'aggregation': ('cumulative', 'monthly'), 'through': r'20\d{2}-(?:0[1-9]|1[0-2])', 'page': r'[1-9]\d{0,7}'}
        allowed.update(q=r'.{0,100}', target_id=r'[1-9]\d{0,11}', position=r'[1-9]\d{0,11}', search_by=('', 'name', 'rank'), view=('ranking',), include=r'(?:universe|coalition|alliance|corporation):\d{1,12}', exclude=r'(?:universe|coalition|alliance|corporation):\d{1,12}')
        defaults = {'kind': 'character', 'mode': 'classic', 'sort': 'destroyed', 'aggregation': 'cumulative', 'page': '1'}
    elif path == '/killboard':
        key = selected = 'killboard'
        defaults = {'mode':'api','page':'1','participation':'both'}
    elif path in ('/superintel', '/superintel/detailed', '/superintel/monthly-analysis'):
        key = 'superintel'
        selected = path.rsplit('/', 1)[-1] if path != '/superintel' else 'dashboard'
        if selected == 'monthly-analysis':
            allowed = {'view': ('pilot', 'corporation', 'alliance'), 'period': r'20\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])',
                       'page': r'[1-9]\d{0,7}', 'page_size': ('50',)}
            allowed.update(reason=r'[a-z_]{1,40}', sort=r'[a-z_]{1,60}', dir=('asc','desc'), q=r'.{0,100}')
            for name in params:
                if re.fullmatch(r'f_[a-z0-9_]{1,60}',name): allowed[name] = r'.{0,100}'
            defaults = {'view': 'pilot', 'page': '1', 'page_size': '50'}
        else:
            allowed = {'view': ('alliance', 'corporation', 'player'), 'more': ('1',), 'sort': ('score','titans','supers'), 'q': r'.{0,100}', 'page':r'[1-9]\d{0,7}', 'page_size':('25','50','100')}
            if selected == 'detailed':
                allowed.update(period=('1d','7d','30d'), analysis_view=r'[a-z_]{1,40}',analysis_movement=r'[a-z_]{1,40}',analysis_asset=r'[a-z_]{1,40}',analysis_q=r'.{0,100}',analysis_page=r'[1-9]\d{0,7}',analysis_page_size=('25','50','100'))
            defaults = {'view': 'alliance'}
    elif re.fullmatch(r'/kill/mer/20\d{2}-\d{2}-01/[1-9]\d{0,14}(?:/hidden)?',path):
        key,selected,defaults='kill','detail',{}
        allowed={'kill_datetime':r'[0-9TZ:+. -]{10,40}'}
    elif re.fullmatch(r'/(corporation|alliance|coalition)/[1-9]\d{0,11}/pilotable-ships/[1-9]\d{0,11}',path):
        key=path.split('/')[1]
        entity_id=int(path.split('/')[2])
        selected='pilotable-ships'
        allowed={'page':r'[1-9]\d{0,7}','q':r'.{0,100}'}
        defaults={'page':'1'}
    else:
        match = re.fullmatch(r'/(character|corporation|alliance|coalition|system|constellation|region|ship|kill|weapon|skill|commodity)/([1-9]\d{0,11})', path)
        if match:
            key, raw_id = match.groups()
            entity_id = int(raw_id)
            key = {'weapon':'modules','skill':'skills','commodity':'commodities'}.get(key,key)
        if key == 'kill':
            selected = 'detail'
            defaults = {}
        elif key:
            selected = params.get('tab', 'killboard' if key=='coalition' else 'overview')
            if params.get('stats_view') == 'top-kills':
                selected = 'top-kills'
            allowed = {'tab': tuple(t['key'] for t in FAMILIES[key]['tabs']), 'stats_view': ('history', 'top-kills'),
                       'page': r'[1-9]\d{0,7}', 'mode': ('api', 'hidden', 'total'), 'tier':r'.{1,40}', 'group':r'.{1,80}', 'size':r'.{1,40}', 'faction':r'.{1,80}' }
            defaults = {'tab': 'killboard' if key=='coalition' else 'overview', 'stats_view': 'history', 'page': '1', 'mode': 'api'}
    if key=='coalition' and selected=='history':
        allowed['history_view']=('members','coalitions')
    if selected == 'killboard':
        allowed.update(mode=('api','hidden','total'), page=r'[1-9]\d{0,7}', participation=('both','kills','losses'),
            date_from=r'20\d{2}-\d{2}-\d{2}', date_to=r'20\d{2}-\d{2}-\d{2}',
            datetime_from=r'[0-9TZ:+. -]{10,40}', datetime_to=r'[0-9TZ:+. -]{10,40}',
            scan_month=r'20\d{2}-\d{2}-01', scan_before=r'[0-9TZ:+. -]{10,40}', scan_row=r'\d{1,15}',
            type_role=('both','attacker','victim'),involved_role=('both','attacker','victim'))
        for name in list_names - {'include','exclude'}:
            allowed[name] = r'[^<>\x00-\x1f]{1,120}' if name.startswith('builder_') else r'\d{1,12}'
    if not key:
        raise ValueError('This page is not available for publication.')
    for name, value in pairs:
        rule = allowed.get(name)
        if rule is not None and value == '':
            continue
        if rule is None or (value not in rule if isinstance(rule, tuple) else not re.fullmatch(rule, value)):
            raise ValueError('Unsupported public filter: ' + name)
    if selected!='index' and not any(t['key'] == selected for t in FAMILIES[key]['tabs']):
        raise ValueError('This tab is not ready for public access.')
    if key not in ('character', 'corporation', 'alliance') and selected == 'stats':
        raise ValueError('Combat stats are unavailable for this entity.')
    clean = sorted(set((k,v) for k,v in pairs if v and defaults.get(k) != v))
    return Target(key, selected, path, urlencode(clean), entity_id)


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


def save_settings(enabled, max_entries=None):
    enabled=set(enabled)
    # One master switch owns all KB access; discard obsolete per-entity switches.
    enabled={value for value in enabled if not value.endswith(':killboard') and value not in ('kill','kill:detail')}
    if 'killboard' in enabled:enabled.add('killboard:killboard')
    result = {}
    if max_entries is not None:
        result['_cache'] = {'max_entries':max(8,min(4096,int(max_entries)))}
    elif settings().get('_cache'):
        result['_cache'] = settings()['_cache']
    for page in CATALOG:
        result[page['key']] = {'enabled': page['key'] in enabled,
            'tabs': [t['key'] for t in page['tabs'] if page['key'] + ':' + t['key'] in enabled]}
    with locked():
        _atomic(ROOT / 'settings.json', result)
        _prune([row for row in entries() if row['fresh']], result.get('_cache',{}).get('max_entries',MAX_ENTRIES))
    return result


KILLBOARD_ENTITIES = ('character','corporation','alliance','coalition','system','constellation','region','ship')


def killboards_enabled(policy):
    page=policy.get('killboard',{})
    return bool(page.get('enabled'))


def effective_policy(policy):
    # Legacy individual KB switches never bypass the single master control.
    result={key:dict(value,tabs=[tab for tab in value.get('tabs',[]) if tab!='killboard'])
            for key,value in policy.items() if key!='_cache'}
    result['kill']={'enabled':False,'tabs':[]}
    result['killboard']={'enabled':killboards_enabled(policy),'tabs':[]}
    if killboards_enabled(policy):
        for key,tab_key in [('killboard','killboard'),*((key,'killboard') for key in KILLBOARD_ENTITIES),('kill','detail')]:
            page=result.setdefault(key,{'enabled':False,'tabs':[]})
            if not page.get('enabled'):page['tabs']=[]
            page['enabled']=True
            if tab_key not in page['tabs']:page['tabs'].append(tab_key)
    tools=result.get('tools',{})
    if tools.get('enabled') and tools.get('tabs') and 'overview' not in tools['tabs']:tools['tabs'].append('overview')
    return result


def entity_navigation(policy):
    effective=effective_policy(policy)
    links=[]
    for kind,key in ENTITY_INDEX_KEYS.items():
        page=effective.get(key,{})
        if page.get('enabled') and page.get('tabs'):
            links.append(dict(label=FAMILIES[key]['label'],href='/public/entities/'+kind,key=key))
    page=effective.get('map',{})
    if page.get('enabled') and page.get('tabs'):
        links.append(dict(label='Map',href='/public'+tab_path('map',page['tabs'][0],'/map'),key='map'))
    return links


def entity_destination(target,policy):
    page=effective_policy(policy).get(target.key,{})
    if not page.get('enabled') or not page.get('tabs'):return None
    from dataclasses import replace
    selected=next((tab for tab in ('killboard','overview') if tab in page['tabs']),page['tabs'][0])
    return replace(target,tab=selected,query='' if selected=='overview' else urlencode({'tab':selected})).public


def admin_catalog():
    """One Killboard checkbox; other families expose only their non-KB pages."""
    master=dict(FAMILIES['killboard'],tabs=[],reason='Opens every killboard, kill details and menu search. Filtered historical scans can be expensive.')
    return [master,*[dict(page,tabs=[tab for tab in page['tabs'] if tab['key']!='killboard'])
                    for page in CATALOG if page['key'] not in ('killboard','kill')]]


def published(target, policy):
    if target.tab=='index':
        page=effective_policy(policy).get(target.key,{})
        return bool(page.get('enabled') and page.get('tabs'))
    if target.key=='tools' and target.tab=='overview':
        page=effective_policy(policy).get('tools',{})
        return bool(page.get('enabled') and page.get('tabs'))
    if target.key=='killboard' or target.tab=='killboard' or (target.key=='kill' and target.tab=='detail'):
        return killboards_enabled(policy)
    page = policy.get(target.key, {})
    return bool(page.get('enabled') and target.tab in page.get('tabs', []))


def entries():
    # Read only a small metadata index, never every cached HTML page on a GET.
    try:
        path = ROOT / 'index.json'
        if path.stat().st_size > 4 * 1024 * 1024:
            return []
        rows = json.loads(path.read_text())
        if not isinstance(rows, list):
            return []
        result = []
        for row in rows[:4096]:
            target = cache_target(row['source'])
            result.append(dict(source=target.source, public=target.public, key=target.key, tab=target.tab,
                               created=row['created'], bytes=row['bytes'], internal=target.path in ('/_read','/_compute'),
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
        _prune(rows, settings().get('_cache', {}).get('max_entries', MAX_ENTRIES))


def _prune(rows, limit):
    rows.sort(key=lambda row: (ROOT / ('snapshot-' + cache_target(row['source']).digest + '.json')).stat().st_mtime, reverse=True)
    def metadata_size(row):
        return len(json.dumps(dict(source=row['source'],created=row['created'],bytes=row['bytes']),ensure_ascii=False).encode()) + 2
    size = sum(row['bytes'] for row in rows)
    metadata = sum(metadata_size(row) for row in rows) + 2
    while len(rows) > limit or size + metadata > MAX_BYTES or metadata > 4 * 1024 * 1024:
        row = rows.pop()
        size -= row['bytes']
        metadata -= metadata_size(row)
    retained = {'snapshot-' + cache_target(row['source']).digest + '.json' for row in rows}
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
        os.utime(path, None)  # LRU access; no index rewrite/fsync on cache hits.
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
    if published(target,policy):return target.public
    if target.key in ENTITY_INDEX_KEYS.values() and (target.tab=='overview' or (target.key=='coalition' and target.tab=='killboard' and not parsed.query)):
        return entity_destination(target,policy)
    return None


class PublicHTML(HTMLParser):
    """Keep shared markup/styles, published links and permitted GET controls.

    The input is trusted server-rendered Jinja HTML, never arbitrary remote HTML.
    Interactive adapters retain their shared scripts; other views strip scripts.
    Parsing on each response means revocation also applies to old snapshots.
    """
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
    DROP = {'iframe', 'object', 'embed'}

    def __init__(self, policy, current, interactive=False):
        super().__init__(convert_charrefs=False)
        self.policy, self.current = policy, current
        self.interactive = interactive
        self.stack = []
        self.suppressed = 0
        self.out = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        drop = self.suppressed > 0 or tag in self.DROP or (tag == 'script' and not self.interactive)
        output_tag = tag
        if tag == 'form':
            drop = drop or attrs.get('method','get').lower() != 'get'
            attrs['method'] = 'get'
            attrs['action'] = '/public' + self.current.split('?',1)[0]
        if tag == 'button':
            switch = 'mode' if attrs.get('data-killmail-mode') else 'participation' if attrs.get('data-killmail-participation') else None
            if switch:
                parts = urlsplit(self.current)
                pairs = [(k,v) for k,v in parse_qsl(parts.query) if k not in (switch,'page','scan_before','scan_month','scan_row')]
                pairs.append((switch,attrs.get('data-killmail-'+switch)))
                attrs['href'] = parts.path + '?' + urlencode(pairs)
                output_tag = 'a'
            elif not self.interactive and not any(frame[0] == 'form' and not frame[2] for frame in self.stack):
                drop = True
        if output_tag == 'a' and not drop:
            if attrs.get('data-killmail-page') and not self.interactive:
                parts = urlsplit(self.current)
                params = dict(parse_qsl(parts.query))
                params.update(page=attrs['data-killmail-page'])
                if self.current.split('?',1)[0] != '/killboard':
                    params['tab'] = 'killboard'
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
                if key.startswith('on') or (key.startswith('data-') and not self.interactive and key not in ('data-rank-kind','data-year','data-default-through')) or key in ('formaction', 'srcdoc', 'autofocus'):
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


def sanitize(html, policy, current, interactive=False):
    parser = PublicHTML(policy, current, interactive)
    parser.feed(html)
    parser.close()
    return ''.join(parser.out)


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


class CacheBusy(Exception):
    pass


@contextmanager
def render_slot(target):
    """Bounded cross-worker work slots and striped single-flight locks."""
    ROOT.mkdir(parents=True, exist_ok=True)
    key_lock = (ROOT / ('key-%02d.lock' % (int(target.digest[:8],16) % 32))).open('a')
    slot = None
    try:
        try:
            fcntl.flock(key_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CacheBusy('This result is already loading. Please retry shortly.') from exc
        for number in range(BUILD_SLOTS):
            candidate = (ROOT / ('render-%d.lock' % number)).open('a')
            try:
                fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                slot = candidate
                break
            except BlockingIOError:
                candidate.close()
        if slot is None:
            raise CacheBusy('Other pages are loading. Please retry shortly.')
        yield
    finally:
        if slot:
            slot.close()
        key_lock.close()


def cached_result(target, builder):
    snapshot = load(target)
    if snapshot:
        return snapshot, True
    with render_slot(target):
        snapshot = load(target)
        if snapshot:
            return snapshot, True
        html = builder()
        try:
            store(target, html)
        except ValueError:
            # Cache is an optimization; an oversized result still displays.
            pass
        return dict(source=target.source, created=time.time(), html=html), False


def read_target(private_url, source):
    """Allow only GET data for this published page, never arbitrary proxying."""
    if len(private_url)>8000 or '\\' in private_url:
        raise ValueError('Invalid data URL.')
    url = urlsplit(private_url)
    if url.scheme or url.netloc or url.fragment:
        raise ValueError('Use a local data URL.')
    params = parse_qsl(url.query,max_num_fields=100)
    # Anonymous visitors cannot force restarts or execute management operations.
    params = [(k,v) for k,v in params if k != 'retry']
    path = url.path
    required = source.tab
    if source.tab=='index':
        kind=source.path.removeprefix('/entities/')
        if path not in ('/api/entity-search','/api/entities/'+kind+'/recent'):
            raise ValueError('Data endpoint unavailable.')
    elif source.key == 'map':
        if not re.fullmatch(r'/api/map(?:/[a-zA-Z0-9_-]+)*',path):
            raise ValueError('Data endpoint unavailable.')
    elif source.key == 'tools':
        allowed_paths={
            'ship-analysis': ('/api/ship-analysis/search','/api/ship-selection-ranking'),
            'super-evolution': (),
            'population': ('/api/tools/entity-search',),
            'economics': ('/api/tools/entity-search',),
            'overview': (),
        }
        comparison = source.tab in ('population','economics') and re.fullmatch(r'/api/(?:alliance|coalition)/[1-9]\d{0,11}/(?:population(?:-intelligence(?:/(?:group|series))?)?|population-economics(?:/(?:options|evolution|comparison|best-month))?)',path)
        if path not in allowed_paths.get(source.tab,()) and not comparison and path not in ('/api/entity-search','/killboard/search'):
            raise ValueError('Data endpoint unavailable.')
    elif source.key == 'leaderboard':
        if path != '/leaderboard/lookup':
            raise ValueError('Data endpoint unavailable.')
        kind = dict(params).get('kind','character')
        if kind not in ('character','corporation','alliance'):
            raise ValueError('Invalid ranking kind.')
        required = kind
    elif source.key == 'killboard':
        if path not in ('/killboard/data','/killboard/scan','/killboard/search','/api/entity-search'):
            raise ValueError('Data endpoint unavailable.')
        required = 'killboard'
    else:
        prefix = f'/api/{source.key}/{source.entity_id}/'
        if path.startswith(prefix):
            suffix = path[len(prefix):]
            if suffix in ('killmails','killmail-affiliations'):
                required = 'killboard'
            elif re.fullmatch(r'history(?:/kills)?',suffix):
                required = 'history'
            elif re.fullmatch(r'pilotable-ships(?:/[1-9]\d{0,11})?',suffix):
                required = 'pilotable-ships'
            elif suffix == 'combat-stats/top-kills':
                required = 'top-kills'
            elif re.fullmatch(r'population(?:/pilots(?:/subset)?)?|population-intelligence(?:/(?:group|series))?|population-flows/chunk|population-affiliations(?:/(?:pilots|series))?|population-history-check/status|population-economics(?:/(?:options|evolution|comparison|best-month))?',suffix):
                required = 'population'
            else:
                raise ValueError('Data endpoint unavailable.')
        elif source.key in ('system','constellation','region') and path == f'/api/location/{source.key}/{source.entity_id}/killmails':
            required = 'killboard'
        elif path not in ('/killboard/search','/api/entity-search','/api/killmail-filter/entity-search',
                          '/api/killmail-filter/module-search','/api/killmail-filter/ships'):
            raise ValueError('Data endpoint unavailable.')
    private_url = path + ('?' + urlencode(sorted(params)) if params else '')
    return Target(source.key, required, '/_read', urlencode({'source':source.source,'path':private_url}), source.entity_id)


COMPUTE_PATHS = {
    'ship-analysis':('/api/ship-analysis',),
    'super-evolution':('/api/superintel/evolution/data','/superintel/evolution/download'),
}


def compute_target(source, path, body_hash):
    if source.key!='tools' or path not in COMPUTE_PATHS.get(source.tab,()) or not re.fullmatch(r'[a-f0-9]{64}',body_hash):
        raise ValueError('This computation is unavailable.')
    return Target(source.key,source.tab,'/_compute',urlencode({'source':source.read_scope_url,'path':path,'body':body_hash}))


def cache_target(value):
    if value.startswith('/_compute?'):
        params=dict(parse_qsl(urlsplit(value).query))
        return compute_target(resolve(params['source']),params['path'],params['body'])
    if value.startswith('/_read?'):
        params = dict(parse_qsl(urlsplit(value).query))
        return read_target(params['path'],resolve(params['source']))
    return resolve(value)
