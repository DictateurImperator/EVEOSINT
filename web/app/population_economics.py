"""Regional MER estimates apportioned by average daily sovereignty (read-only)."""
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from threading import Lock
from time import monotonic

from .db import db
from .map_data import _topology
from .map_economy import month, month_range
from .population_intelligence import (
    _coalition_scope_at,
    _load_coalition_official_rows,
    _load_coalition_rules,
    _load_official_rows,
    _official_at_or_before,
)

METRICS = {'npc_bounties_isk': 'NPC bounties', 'mining_isk': 'Mining', 'production_isk': 'Production'}
_CACHE = {}
_ACTIVITY_CACHE = {}
_LOCK = Lock()


def next_month(day):
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def ts(day):
    return datetime.combine(day, time.min, tzinfo=UTC)


def scope_at(kind, entity_id, rules, day):
    if kind == 'alliance':
        return {('alliance', entity_id)}
    return _coalition_scope_at(rules, entity_id, day)


def owns(scope, alliance, corporation):
    return ('alliance', alliance) in scope or ('corporation', corporation) in scope


def daily_shares(initial, events, months, systems, get_scope):
    """Ratio of mean owned systems to mean total claimed systems, UTC day-end samples."""
    owners = {}
    counts = defaultdict(Counter)

    def update(system, action, alliance, corporation):
        region = systems.get(system, {}).get('region_id')
        previous = owners.pop(system, None)
        if previous and region:
            counts[region][previous] -= 1
            if counts[region][previous] == 0:
                del counts[region][previous]
        owner = (alliance, corporation)
        if action == 'GAIN' and (alliance or corporation):
            owners[system] = owner
            if region:
                counts[region][owner] += 1

    for row in initial:
        update(*row)
    index = 0
    result = {}
    for start in months:
        end = next_month(start)
        owned_days, total_days = Counter(), Counter()
        day = start
        while day < end:
            cutoff = ts(day + timedelta(days=1))
            while index < len(events) and events[index][0] < cutoff:
                _when, system, action, alliance, corporation = events[index]
                update(system, action, alliance, corporation)
                index += 1
            scope = get_scope(day)
            for region, owner_counts in counts.items():
                total = sum(owner_counts.values())
                owned = sum(count for (alliance, corporation), count in owner_counts.items()
                            if count > 0 and owns(scope, alliance, corporation))
                total_days[region] += total
                owned_days[region] += owned
            day += timedelta(days=1)
        days = (end - start).days
        result[start] = {
            region: {'share': Decimal(owned) / Decimal(total_days[region]),
                     'owned_average': Decimal(owned) / days,
                     'total_average': Decimal(total_days[region]) / days}
            for region, owned in owned_days.items() if owned > 0 and total_days[region] > 0
        }
    return result


def _catalog(kind, entity_id):
    if kind not in {'alliance', 'coalition'} or entity_id <= 0:
        raise ValueError('Invalid entity.')
    key = (kind, entity_id)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and monotonic() - cached[0] < 300:
            return cached[1]
    topology = _topology()
    with db() as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '30000ms'")
        cur.execute("SELECT to_regclass('mer.region_economy_monthly'), to_regclass('sovereignty.reconciled_map')")
        if not all(cur.fetchone()):
            return {'months': [], 'facts': {}, 'shares': {}, 'rules': {}}
        cur.execute("SELECT MIN(event_at) FROM sovereignty.reconciled_map")
        earliest = cur.fetchone()[0]
        if earliest is None:
            return {'months': [], 'facts': {}, 'shares': {}, 'rules': {}}
        rules = _load_coalition_rules(conn, entity_id) if kind == 'coalition' else {}
        cur.execute("""SELECT period_start, region_id, scope_name, metric, value
            FROM mer.region_economy_monthly WHERE dataset='regional' AND dimensions='{}'::jsonb
            AND unit='ISK' AND metric=ANY(%s) AND scope_kind IN ('region','region_name')
            AND period_start >= %s ORDER BY period_start""", (list(METRICS), earliest.date()))
        names = {r['name'].casefold(): rid for rid, r in topology['regions'].items()}
        facts = defaultdict(lambda: defaultdict(dict))
        for day, region, name, metric, value in cur.fetchall():
            region = region or names.get(name.casefold())
            if region:
                facts[day][region][metric] = value
        months = sorted(facts)
        if not months:
            return {'months': [], 'facts': {}, 'shares': {}, 'rules': rules}
        cur.execute("""SELECT DISTINCT ON (system_id) system_id, action, alliance_id, corporation_id
            FROM sovereignty.reconciled_map WHERE event_at < %s
            ORDER BY system_id, event_at DESC, event_id DESC""", (ts(months[0]),))
        initial = cur.fetchall()
        cur.execute("""SELECT event_at, system_id, action, alliance_id, corporation_id
            FROM sovereignty.reconciled_map WHERE event_at >= %s AND event_at < %s
            ORDER BY event_at, event_id""", (ts(months[0]), ts(next_month(months[-1]))))
        events = cur.fetchall()
    # Include gaps when applying events: facts in a later month must see intervening changes.
    all_months = month_range(months[0], months[-1])
    shares = daily_shares(initial, events, all_months, topology['systems'],
                          lambda day: scope_at(kind, entity_id, rules, day))
    available = [day for day in months if any(region in facts[day] for region in shares[day])]
    result = {'months': months if available else [], 'facts': facts, 'shares': shares, 'rules': rules}
    with _LOCK:
        if len(_CACHE) >= 8:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = (monotonic(), result)
    return result


def get_options(kind, entity_id):
    catalog = _catalog(kind, entity_id)
    return {'available': bool(catalog['months']), 'months': [d.strftime('%Y-%m') for d in catalog['months']]}


def estimates(catalog, months, regions):
    totals = {key: Decimal(0) for key in METRICS}
    missing = set()
    details = []
    for day in months:
        for region, share in catalog['shares'][day].items():
            values = catalog['facts'][day].get(region, {})
            details.append({'month': day.strftime('%Y-%m'), 'region': regions.get(region, {}).get('name', str(region)),
                            'share': str(share['share']), 'owned_average': str(share['owned_average']),
                            'total_average': str(share['total_average'])})
            for key in METRICS:
                if key not in values:
                    missing.add(key)
                else:
                    totals[key] += values[key] * share['share']
    return {key: None if key in missing else value for key, value in totals.items()}, details


def activity_intervals(months, window):
    anchor = next_month(months[-1])
    start = anchor - timedelta(days=window)
    return [(max(day, start), next_month(day)) for day in months if next_month(day) > start]


def average_population(official, dates, months):
    total = Decimal(0)
    count = 0
    for start in months:
        day = start
        while day < next_month(start):
            value = (_official_at_or_before(official, dates, day) or {}).get('member_count')
            if value is None:
                return None
            total += value
            count += 1
            day += timedelta(days=1)
    return total / count if count else None


def _activity(conn, kind, entity_id, rules, intervals):
    # Disjoint time segments let us add distinct kill counts without transferring kill ID arrays.
    segments = []
    for start, end in intervals:
        day = start
        while day < end:
            scope = scope_at(kind, entity_id, rules, day)
            if segments and segments[-1][1] == day and segments[-1][2] == scope:
                segments[-1] = (segments[-1][0], day+timedelta(days=1), scope)
            else:
                segments.append((day, day+timedelta(days=1), scope))
            day += timedelta(days=1)
    key = (kind, entity_id, tuple((a,b,tuple(sorted(scope))) for a,b,scope in segments))
    with _LOCK:
        cached = _ACTIVITY_CACHE.get(key)
        if cached and monotonic()-cached[0] < 300:
            return cached[1]
    active, kills, losses = set(), 0, 0
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '12000ms'")
        for first, last, scope in segments:
            aids = [eid for typ,eid in scope if typ == 'alliance']
            cids = [eid for typ,eid in scope if typ == 'corporation']
            if not (aids or cids):
                continue
            # Filtering attackers and victims separately avoids an OR across both sides of
            # the join, which made PostgreSQL scan unrelated regional/global participation.
            cur.execute("""SELECT ARRAY_AGG(DISTINCT ka.character_id), COUNT(DISTINCT km.killmail_id)
                FROM rawkm.killmail_attackers ka JOIN rawkm.killmails km
                  ON km.killmail_id=ka.killmail_id AND km.killmail_time=ka.killmail_time
                WHERE ka.killmail_time >= %s AND ka.killmail_time < %s
                  AND km.killmail_time >= %s AND km.killmail_time < %s
                  AND ka.character_id > 0 AND km.victim_character_id > 0
                  AND (ka.alliance_id=ANY(%s) OR ka.corporation_id=ANY(%s))""",
                (ts(first), ts(last), ts(first), ts(last), aids, cids))
            pilot_ids, kill_count = cur.fetchone()
            active.update(pilot_ids or [])
            kills += kill_count or 0
            cur.execute("""SELECT COUNT(DISTINCT km.killmail_id) FROM rawkm.killmails km
                WHERE km.killmail_time >= %s AND km.killmail_time < %s
                  AND km.victim_character_id > 0
                  AND (km.victim_alliance_id=ANY(%s) OR km.victim_corporation_id=ANY(%s))
                  AND EXISTS (SELECT 1 FROM rawkm.killmail_attackers ka
                      WHERE ka.killmail_id=km.killmail_id AND ka.killmail_time=km.killmail_time
                        AND ka.killmail_time >= %s AND ka.killmail_time < %s
                        AND ka.character_id > 0)""",
                (ts(first), ts(last), aids, cids, ts(first), ts(last)))
            losses += cur.fetchone()[0] or 0
    result = (len(active), kills, losses)
    with _LOCK:
        if len(_ACTIVITY_CACHE) >= 256:
            _ACTIVITY_CACHE.pop(next(iter(_ACTIVITY_CACHE)))
        _ACTIVITY_CACHE[key] = (monotonic(), result)
    return result


def get_series(kind, entity_id, from_month, to_month, window=90, offset=0):
    start, end = month(from_month), month(to_month)
    if start > end or not 1 <= window <= 3650 or offset < 0:
        raise ValueError('Choose an ordered month range and an activity window from 1 to 3650 days.')
    catalog = _catalog(kind, entity_id)
    months = [day for day in catalog['months'] if start <= day <= end]
    if not months:
        return {'available': False, 'months': [], 'coverage': [], 'next_offset': None}
    measurements, details = [], []
    with db() as conn:
        official_loader = _load_official_rows if kind == 'alliance' else _load_coalition_official_rows
        official, dates = official_loader(conn, entity_id)
        for index in range(offset, min(offset+6, len(months))):
            day = months[index]
            totals, allocated = estimates(catalog, [day], _topology()['regions'])
            details.extend(allocated)
            intervals = activity_intervals([d for d in catalog['months'] if d <= day], window)
            population = average_population(official, dates, [day])
            active, kills, losses = _activity(conn, kind, entity_id, catalog['rules'], intervals)
            denominators = {'member': population, 'active': active, 'loss': losses, 'kill': kills}
            days = (next_month(day)-day).days
            rows = []
            for key, label in METRICS.items():
                value = totals[key]
                rows.append({'metric': key, 'label': label, 'total': str(value) if value is not None else None,
                             'daily_average': str(value/days) if value is not None else None,
                             'ratios': {name: str(value/count) if value is not None and count else None
                                        for name, count in denominators.items()}})
            measurements.append({'month': day.strftime('%Y-%m'), 'rows': rows,
                                 'denominators': {key: str(v) if isinstance(v, Decimal) else v for key, v in denominators.items()},
                                 'economic_days': days, 'activity_days': sum((b-a).days for a,b in intervals),
                                 'activity_coverage': [{'from': a.isoformat(), 'through': (b-timedelta(days=1)).isoformat()}
                                                       for a,b in intervals]})
    return {'available': True, 'months': measurements, 'regions': details,
            'coverage': [d.strftime('%Y-%m') for d in months],
            'next_offset': offset+len(measurements) if offset+len(measurements) < len(months) else None}


def get_evolution(kind, entity_id, from_month, to_month, basis='total', window=90, offset=0):
    """Monthly measurements; expensive PvP ratios are requested explicitly, in small batches."""
    if basis not in {'total', 'daily', 'member', 'active', 'loss', 'kill'} or not 1 <= window <= 3650 or offset < 0:
        raise ValueError('Choose a valid chart basis, activity window and offset.')
    months = month_range(month(from_month), month(to_month))
    catalog = _catalog(kind, entity_id)
    covered = set(catalog['months'])
    batch_size = 1 if basis in {'active', 'loss', 'kill'} else 12
    batch = months[offset:offset+batch_size]
    points = []
    with db() as conn:
        official, dates = ([], [])
        if basis == 'member':
            loader = _load_official_rows if kind == 'alliance' else _load_coalition_official_rows
            official, dates = loader(conn, entity_id)
        for day in batch:
            divisor = None
            amounts = {key: None for key in METRICS}
            if day in covered:
                totals, _details = estimates(catalog, [day], {})
                divisor = 1
                if basis == 'daily':
                    divisor = (next_month(day)-day).days
                elif basis == 'member':
                    divisor = average_population(official, dates, [day])
                elif basis in {'active', 'loss', 'kill'}:
                    history = [d for d in catalog['months'] if d <= day]
                    intervals = activity_intervals(history, window)
                    active, kills, losses = _activity(conn, kind, entity_id, catalog['rules'], intervals)
                    divisor = {'active': active, 'loss': losses, 'kill': kills}[basis]
                amounts = {key: str(value/divisor) if value is not None and divisor else None
                           for key, value in totals.items()}
            points.append({'month': day.strftime('%Y-%m'), 'values': amounts,
                           'denominator': str(divisor) if divisor is not None else None})
    return {'points': points, 'basis': basis, 'next_offset': offset+len(batch) if offset+len(batch) < len(months) else None,
            'total_months': len(months)}
