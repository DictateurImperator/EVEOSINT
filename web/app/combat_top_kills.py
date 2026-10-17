"""Exact per-entity top kills, with bounded memory and no new persistent facts."""
from collections import OrderedDict
from contextlib import closing
from datetime import UTC
from decimal import Decimal, localcontext
import threading
import time
import logging

from psycopg2 import sql
from psycopg2.extras import execute_values

from .db import db
from .killmail_rankings import KINDS

LIMIT = 20
BATCH = 64
_CACHE = OrderedDict()
_LOCK = threading.Lock()
_SLOTS = threading.BoundedSemaphore(2)
_RUNS = OrderedDict()
LOG = logging.getLogger(__name__)


class TopKillsBusy(Exception):
    pass


def score_key(row, field):
    return row[field], row['killmail_time'], row['killmail_id']


def _scan(conn, kind, entity_id, limit, report=None):
    """Walk monthly partitions using entity indexes and exact value bounds."""
    raw, weighted = [], []
    examined = 0
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout='60s'")
        cur.execute('SET LOCAL jit=off')
    from .entities import _rawkm_partition_tables
    attackers = dict((name, schema) for schema,name in _rawkm_partition_tables(conn,'killmail_attackers'))
    relations = []
    for schema,name in _rawkm_partition_tables(conn,'killmails'):
        attack_name = 'killmail_attackers'+name[len('killmails'):]
        attack_schema = attackers.get(attack_name)
        relations.append((schema,name,attack_schema or 'rawkm',attack_name if attack_schema else 'killmail_attackers'))
    for index,(kill_schema,kill_table,attack_schema,attack_table) in enumerate(relations):
        month = kill_table.removeprefix('killmails_').replace('_','-')
        if report:
            report(phase='scanning',checked=index,total=len(relations),current_month=month,kills_examined=examined)
        examined += _scan_tables(conn,kind,entity_id,limit,sql.Identifier(kill_schema,kill_table),
                                sql.Identifier(attack_schema,attack_table),raw,weighted)
        if report:
            result = dict(raw=raw,weighted=weighted)
            _enrich(conn,result)
            snapshot = {mode:[dict(row) for row in group] for mode,group in result.items()}
            report(phase='scanning',checked=index+1,total=len(relations),current_month=month,
                   kills_examined=examined,result=snapshot)
    if report:
        report(phase='completed',checked=len(relations),total=len(relations),kills_examined=examined)
    return dict(raw=raw, weighted=weighted)


def _scan_tables(conn,kind,entity_id,limit,kills,attackers,raw,weighted):
    """Raw ISK bounds true ISK. Skip only kills that cannot enter either exact top."""
    column = sql.Identifier(KINDS[kind][2])
    threshold = weighted[-1]['true_cents'] if len(weighted)==limit else None
    examined = 0
    with conn.cursor(name='combat_top_candidates') as stream:
        stream.execute(sql.SQL('''
            SELECT k.killmail_id,k.killmail_time,k.estimated_value_cents
            FROM (SELECT DISTINCT a.killmail_id,a.killmail_time
                  FROM {attackers} a WHERE a.{column}=%s
                 ) p
            CROSS JOIN LATERAL (
                SELECT k.killmail_id,k.killmail_time,k.estimated_value_cents
                FROM {kills} k
                WHERE k.killmail_id=p.killmail_id AND k.killmail_time=p.killmail_time
                  AND k.estimated_value_cents IS NOT NULL
                  AND (%s::numeric IS NULL OR k.estimated_value_cents >= %s)
                OFFSET 0
            ) k
            ORDER BY k.estimated_value_cents DESC,k.killmail_time DESC,k.killmail_id DESC
        ''').format(column=column,kills=kills,attackers=attackers), (entity_id,threshold,threshold))
        done = False
        while not done:
            batch = stream.fetchmany(BATCH)
            if not batch:
                break
            candidates = []
            for kill_id, at, cents in batch:
                row = dict(killmail_id=kill_id, killmail_time=at.astimezone(UTC),
                           raw_cents=Decimal(cents))
                if len(weighted) == limit and score_key(row, 'raw_cents') <= score_key(weighted[-1], 'true_cents'):
                    done = True
                    break
                candidates.append(row)
            if not candidates:
                break
            examined += len(candidates)
            # The denominator includes every attacker, including NPCs and objects.
            with conn.cursor() as cur:
                execute_values(cur, sql.SQL('''
                    SELECT v.id,v.at,d.total,d.damage FROM (VALUES %s) v(id,at)
                    CROSS JOIN LATERAL (
                        SELECT sum(GREATEST(COALESCE(a.damage_done,0),0)::numeric) AS total,
                               sum(GREATEST(COALESCE(a.damage_done,0),0)::numeric)
                                   FILTER (WHERE a.{column}={entity}) AS damage
                        FROM {attackers} a
                        WHERE a.killmail_id=v.id AND a.killmail_time=v.at
                    ) d''').format(column=column,entity=sql.Literal(entity_id),attackers=attackers),
                    [(r['killmail_id'],r['killmail_time']) for r in candidates])
                totals = {(id, at): (total,damage) for id, at, total,damage in cur}
            for row in candidates:
                total, damage = totals[(row['killmail_id'],row['killmail_time'])]
                total = total or Decimal(0)
                row['entity_damage'] = damage or Decimal(0)
                with localcontext() as ctx:
                    ctx.prec = 50
                    row['damage_percent'] = Decimal(100)*row['entity_damage']/total if total else None
                    row['true_cents'] = row['raw_cents']*row['entity_damage']/total if total else Decimal(0)
                raw.append(row)
                weighted.append(row)
            raw.sort(key=lambda r: score_key(r,'raw_cents'),reverse=True)
            del raw[limit:]
            weighted.sort(key=lambda r: score_key(r,'true_cents'),reverse=True)
            del weighted[limit:]
    return examined


def _enrich(conn, result):
    from .entities import _lookup_type_names, _lookup_system_names
    rows = {(r['killmail_id'],r['killmail_time']): r for group in result.values() for r in group if 'ship_name' not in r}
    if not rows:
        return
    with conn.cursor() as cur:
        execute_values(cur, '''
            SELECT k.killmail_id,k.killmail_time,k.victim_ship_type_id,k.solar_system_id,
                   k.victim_character_id,c.name,k.victim_corporation_id,corp.name
            FROM (VALUES %s) v(id,at)
            CROSS JOIN LATERAL (
                SELECT k.killmail_id,k.killmail_time,k.victim_ship_type_id,k.solar_system_id,
                       k.victim_character_id,k.victim_corporation_id
                FROM rawkm.killmails k
                WHERE k.killmail_id=v.id AND k.killmail_time=v.at OFFSET 0
            ) k
            LEFT JOIN entities.characters c ON c.character_id=k.victim_character_id
            LEFT JOIN entities.corporations corp ON corp.corporation_id=k.victim_corporation_id
        ''', list(rows))
        details = list(cur)
    ships = _lookup_type_names(conn,[r[2] for r in details])
    systems = _lookup_system_names(conn,[r[3] for r in details])
    for id,at,ship,system,pilot,pilot_name,corp,corp_name in details:
        # A kill can appear in both lists as distinct dict objects.
        for group in result.values():
            for row in group:
                if (row['killmail_id'],row['killmail_time']) == (id,at):
                    row.update(ship_id=ship,ship_name=ships.get(ship,f'Type {ship}' if ship else 'Unknown ship'),
                               system_id=system,system_name=systems.get(system,f'System {system}' if system else 'Unknown system'),
                               victim_id=pilot,victim_name=pilot_name or corp_name or 'Unknown victim',
                               victim_corporation_id=corp)


def top_kills(kind, entity_id, report=None):
    if kind not in KINDS or not 0 < entity_id < 2**63:
        raise ValueError('Invalid entity.')
    key = kind, entity_id
    with _LOCK:
        entry = _CACHE.get(key)
        if entry and time.monotonic()-entry[0] < 300:
            _CACHE.move_to_end(key)
            return entry[1]
    if not _SLOTS.acquire(blocking=False):
        raise TopKillsBusy('Two top-kill searches are running. Please retry in a moment.')
    try:
        with closing(db()) as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn:
                result = _scan(conn,kind,entity_id,LIMIT,report=report)
                _enrich(conn,result)
        with _LOCK:
            _CACHE[key] = time.monotonic(), result
            _CACHE.move_to_end(key)
            while len(_CACHE)>32:
                _CACHE.popitem(last=False)
        return result
    finally:
        _SLOTS.release()


def top_kills_state(kind, entity_id, retry=False):
    """Start once, then poll a bounded in-process run and its provisional tops."""
    if kind not in KINDS or not 0 < entity_id < 2**63:
        raise ValueError('Invalid entity.')
    key = kind,entity_id
    with _LOCK:
        cached = _CACHE.get(key)
        state = _RUNS.get(key)
        if cached and time.monotonic()-cached[0] < 300:
            return dict(status='completed',result=cached[1],progress=dict(state['progress']) if state else {})
        if state and (state['status']=='running' or (state['status']=='failed' and not retry)):
            return dict(state,progress=dict(state['progress']))
        if sum(s['status']=='running' for s in _RUNS.values())>=2:
            raise TopKillsBusy('Two top-kill searches are running. Please retry in a moment.')
        state = dict(status='running',progress=dict(phase='starting',checked=0,total=0,kills_examined=0),result=None)
        _RUNS[key] = state
        _RUNS.move_to_end(key)
        while len(_RUNS)>32:
            discard = next(k for k,s in _RUNS.items() if s['status']!='running')
            del _RUNS[discard]

        def report(**values):
            with _LOCK:
                if 'result' in values:
                    state['result'] = values.pop('result')
                state['progress'].update(values)

        def run():
            try:
                result = top_kills(kind,entity_id,report=report)
                with _LOCK:
                    state.update(status='completed',result=result)
            except Exception:
                LOG.exception('Top kills scan failed for %s %s',kind,entity_id)
                with _LOCK:
                    state.update(status='failed',error='Top kills could not be completed. Please retry.')

        threading.Thread(target=run,name=f'combat-top-{kind}-{entity_id}',daemon=True).start()
        return dict(state,progress=dict(state['progress']))
