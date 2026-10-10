"""Read-only regional MER series for the economic map."""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from .db import db
from .map_data import _topology

# These published amounts use the same unit. Detailed mining/moon datasets
# overlap regional totals and must not be added to them automatically.
METRICS = {
    'mining_isk': ('Mining', '#4ade80'),
    'production_isk': ('Production', '#60a5fa'),
    'destruction_isk': ('Destruction', '#fb7185'),
    'trade_isk': ('Trade', '#fbbf24'),
    'npc_bounties_isk': ('NPC bounties', '#c084fc'),
    'imports_isk': ('Imports', '#22d3ee'),
    'exports_isk': ('Exports', '#fb923c'),
}


class EconomyMapError(Exception):
    pass


def _check_table(cur):
    cur.execute("SELECT to_regclass('mer.region_economy_monthly')")
    if not cur.fetchone()[0]:
        raise EconomyMapError('Economic data has not been imported yet. Run Import economic data in Admin MER.')


def get_economy_options():
    with db() as conn, conn.cursor() as cur:
        _check_table(cur)
        cur.execute("""SELECT period_start,metric FROM mer.region_economy_monthly
                       WHERE dataset='regional' AND dimensions='{}'::jsonb AND unit='ISK'
                         AND metric=ANY(%s) GROUP BY period_start,metric ORDER BY period_start""", (list(METRICS),))
        rows = cur.fetchall()
    months = sorted({day.strftime('%Y-%m') for day, _metric in rows})
    present = {metric for _day, metric in rows}
    return {'months': months, 'metrics': [{'key': key, 'label': label, 'color': color}
                                         for key, (label, color) in METRICS.items() if key in present]}


def month(value):
    try:
        if len(value) != 7:
            raise ValueError()
        return date.fromisoformat(value + '-01')
    except (TypeError, ValueError) as exc:
        raise EconomyMapError('Choose a valid month (YYYY-MM).') from exc


def month_range(start, end):
    distance = (end.year-start.year)*12 + end.month-start.month
    if distance < 0 or distance >= 240:
        raise EconomyMapError('Choose an ordered range of at most 240 months.')
    return [date(start.year + (start.month-1+i)//12, (start.month-1+i)%12+1, 1) for i in range(distance+1)]


def measurement(key, label, value, count, required, production, production_count):
    reason = None
    if value is None:
        reason = f'Missing indicator data: {count}/{required} months'
    elif production is None:
        reason = f'Missing production data: {production_count}/{required} months'
    elif production <= 0:
        reason = 'Production is zero; relative intensity is undefined'
    ratio = float(value / production) if value is not None and production is not None and production > 0 else None
    return {'key': key, 'label': label, 'value': str(value) if value is not None else None,
            'ratio': ratio, 'observed_months': count, 'missing_reason': reason}


def build_frames(rows, regions, months, metrics, evolution=False):
    names = {region['name'].casefold(): region_id for region_id, region in regions.items()}
    values = defaultdict(dict)
    unplaced = set()
    for day, region_id, scope_kind, scope_name, metric, value in rows:
        if region_id not in regions:
            region_id = names.get(scope_name.casefold()) if scope_kind in {'region', 'region_name'} else None
        if region_id is None or region_id not in regions:
            unplaced.add(scope_name)
            continue
        # One regional total per metric: never multiply it by the system count.
        values[(day, region_id)][metric] = Decimal(value)
    slices = [[day] for day in months] if evolution else [months]
    frames = []
    for days in slices:
        entries = []
        for region_id in sorted({key[1] for key in values}):
            observed = [values.get((day, region_id), {}) for day in days]
            production_points = [v['production_isk'] for v in observed if 'production_isk' in v]
            production = sum(production_points, Decimal(0)) if len(production_points) == len(days) else None
            measurements = []
            for metric in metrics:
                points = [v[metric] for v in observed if metric in v]
                total = sum(points, Decimal(0)) if len(points) == len(days) else None
                measurements.append(measurement(metric, METRICS[metric][0], total, len(points), len(days), production, len(production_points)))
            complete = all(entry['value'] is not None for entry in measurements)
            combined_value = sum((Decimal(entry['value']) for entry in measurements), Decimal(0)) if complete else None
            combined_count = sum(all(metric in point for metric in metrics) for point in observed)
            combined = measurement('combined', 'Combined indicators', combined_value, combined_count, len(days), production, len(production_points))
            if not complete:
                combined['missing_reason'] = 'Missing selected indicator data: ' + ', '.join(entry['label'] for entry in measurements if entry['value'] is None)
            entries.append({'region_id': region_id, 'region_name': regions[region_id]['name'],
                            'production': str(production) if production is not None else None,
                            'required_months': len(days), 'production_months': len(production_points),
                            'metrics': measurements, 'combined': combined})
        frames.append({'month': days[0].strftime('%Y-%m') if evolution else None,
                       'regions': entries})
    return frames, sorted(unplaced)


def get_economy_series(from_month, to_month, metrics, evolution=False):
    start, end = month(from_month), month(to_month)
    months = month_range(start, end)
    metrics = list(dict.fromkeys(metrics or ['mining_isk']))
    if any(metric not in METRICS for metric in metrics):
        raise EconomyMapError('Unknown economic indicator.')
    with db() as conn, conn.cursor() as cur:
        _check_table(cur)
        cur.execute("""SELECT period_start,region_id,scope_kind,scope_name,metric,value
                       FROM mer.region_economy_monthly
                       WHERE dataset='regional' AND dimensions='{}'::jsonb AND unit='ISK'
                         AND period_start BETWEEN %s AND %s AND metric=ANY(%s)
                       ORDER BY period_start,scope_kind,scope_key,metric""",
                    (start, end, list(set(metrics) | {'production_isk'})))
        rows = cur.fetchall()
    frames, unplaced = build_frames(rows, _topology()['regions'], months, metrics, evolution)
    return {'from': from_month, 'to': to_month, 'frames': frames, 'unplaced_scopes': unplaced,
            'metrics': [{'key': key, 'label': METRICS[key][0], 'color': METRICS[key][1]} for key in metrics]}
