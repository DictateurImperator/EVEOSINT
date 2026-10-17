#!/usr/bin/env python3
"""Build sparse monthly ISK rankings. One transaction per month; source KM are read only."""
import argparse
import json
import logging
import os
import re
import shutil
import signal
from datetime import date, datetime, UTC
from pathlib import Path

import psycopg2
from psycopg2 import sql

LOCK = 79130425
LOG = logging.getLogger('killmail_rankings')
SCHEMA = Path(__file__).with_name('killmail_rankings_schema.sql')


def parse_month(value):
    if not re.fullmatch(r'\d{4}-\d{2}', value):
        raise argparse.ArgumentTypeError('Use YYYY-MM')
    try:
        result = date.fromisoformat(value + '-01')
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if result.year < 2003:
        raise argparse.ArgumentTypeError('Earliest month: 2003-01')
    return result


def source_months(conn):
    with conn.cursor() as cur:
        cur.execute('''SELECT c.relname FROM pg_inherits i
                       JOIN pg_class c ON c.oid=i.inhrelid
                       WHERE i.inhparent='rawkm.killmails'::regclass''')
        return sorted(date(int(m[1]), int(m[2]), 1) for (name,) in cur
                      if (m := re.fullmatch(r'killmails_(\d{4})_(\d{2})', name)))


def build_month(conn, month, report=lambda **kwargs: None):
    """Caller starts REPEATABLE READ, commits only after this returns successfully."""
    suffix = month.strftime('%Y_%m')
    with conn.cursor() as cur:
        cur.execute("SET LOCAL work_mem='32MB'")
        cur.execute('SET LOCAL max_parallel_workers_per_gather=2')
        cur.execute("SET LOCAL lock_timeout='10s'")
        cur.execute('SELECT revision FROM analytics.killmail_ranking_versions WHERE month=%s', (month,))
        row = cur.fetchone()
        revision = row[0] if row else 0
        report(phase='reading_killmails', current_month=month.strftime('%Y-%m'))
        cur.execute(sql.SQL('''CREATE TEMP TABLE ranking_kills ON COMMIT DROP AS
            SELECT killmail_id,killmail_time,estimated_value_cents AS cents,
                   victim_character_id,victim_corporation_id,victim_alliance_id
            FROM rawkm.{}''').format(sql.Identifier('killmails_' + suffix)))
        cur.execute('ANALYZE ranking_kills')
        cur.execute('SELECT count(*),count(cents) FROM ranking_kills')
        total, valued = cur.fetchone()
        report(phase='aggregating_attackers', killmails=total, valued=valued)
        # The window denominator is computed BEFORE selecting character/corp/alliance IDs.
        # NULL-character NPC/structure damage therefore remains in the denominator.
        cur.execute(sql.SQL('''CREATE TEMP TABLE ranking_attacks ON COMMIT DROP AS
            SELECT k.killmail_id,k.killmail_time,k.cents,
                   a.character_id,a.corporation_id,a.alliance_id,
                   GREATEST(COALESCE(a.damage_done,0),0)::numeric AS damage,
                   sum(GREATEST(COALESCE(a.damage_done,0),0)::numeric)
                     OVER (PARTITION BY k.killmail_id,k.killmail_time) AS total_damage
            FROM ranking_kills k JOIN rawkm.{} a
              ON a.killmail_id=k.killmail_id AND a.killmail_time=k.killmail_time
            ''').format(sql.Identifier('killmail_attackers_' + suffix)))
        cur.execute('ANALYZE ranking_attacks')
        cur.execute('''SELECT count(*) FROM (SELECT killmail_id,killmail_time
            FROM ranking_attacks WHERE total_damage>0 GROUP BY killmail_id,killmail_time) a''')
        zero_damage = total - cur.fetchone()[0]
        report(phase='writing_rankings', zero_damage=zero_damage)
        cur.execute('''CREATE TEMP TABLE ranking_stage ON COMMIT DROP AS
            WITH per_kill AS (
                SELECT a.killmail_id,a.killmail_time,e.kind,e.id,max(a.cents) AS cents,
                       sum(a.damage) AS damage,max(a.total_damage) AS total_damage
                FROM ranking_attacks a
                CROSS JOIN LATERAL (VALUES (1,a.character_id),(2,a.corporation_id),(3,a.alliance_id)) e(kind,id)
                WHERE e.id>0
                GROUP BY a.killmail_id,a.killmail_time,e.kind,e.id
            ), facts AS (
                SELECT kind,id,sum(COALESCE(cents,0)) AS destroyed,
                       sum(CASE WHEN total_damage>0 THEN COALESCE(cents,0)::numeric*damage/total_damage ELSE 0 END) AS weighted,
                       0::numeric AS lost,count(*) AS kills,0::bigint AS losses,
                       count(*) FILTER(WHERE cents IS NULL) AS unvalued_kills,0::bigint AS unvalued_losses
                FROM per_kill GROUP BY kind,id
                UNION ALL
                SELECT e.kind,e.id,0,0,sum(COALESCE(k.cents,0)),0,count(*),0,
                       count(*) FILTER(WHERE k.cents IS NULL)
                FROM ranking_kills k
                CROSS JOIN LATERAL (VALUES (1,k.victim_character_id),(2,k.victim_corporation_id),(3,k.victim_alliance_id)) e(kind,id)
                WHERE e.id>0 GROUP BY e.kind,e.id
            ) SELECT kind,id,sum(destroyed) AS destroyed,sum(weighted) AS weighted,sum(lost) AS lost,
                     sum(kills)::bigint AS kills,sum(losses)::bigint AS losses,
                     sum(unvalued_kills)::bigint AS unvalued_kills,sum(unvalued_losses)::bigint AS unvalued_losses
              FROM facts GROUP BY kind,id''')
        cur.execute('DELETE FROM analytics.killmail_rankings_monthly WHERE month=%s', (month,))
        cur.execute('''INSERT INTO analytics.killmail_rankings_monthly
            SELECT %s,kind,id,destroyed,weighted,lost,kills,losses,unvalued_kills,unvalued_losses FROM ranking_stage''', (month,))
        rows = cur.rowcount
        cur.execute('''INSERT INTO analytics.killmail_ranking_months
            (month,built_revision,built_at,killmails,valued,zero_damage,entity_rows)
            VALUES(%s,%s,clock_timestamp(),%s,%s,%s,%s) ON CONFLICT(month) DO UPDATE SET
            built_revision=excluded.built_revision,built_at=excluded.built_at,
            killmails=excluded.killmails,valued=excluded.valued,zero_damage=excluded.zero_damage,entity_rows=excluded.entity_rows''',
            (month, revision, total, valued, zero_damage, rows))
    return dict(killmails=total, valued=valued, zero_damage=zero_damage, entity_rows=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db-config', type=Path, default=Path.home()/'eveosint/config/db.json')
    parser.add_argument('--progress', type=Path, default=Path.home()/'eveosint/data/market/killmail_rankings_progress.json')
    parser.add_argument('--from-month', type=parse_month)
    parser.add_argument('--through-month', type=parse_month)
    parser.add_argument('--force', action='store_true', help='Rebuild selected months, including unchanged months')
    parser.add_argument('--incremental-only', action='store_true', help='Daily: wait for first manual initialization')
    parser.add_argument('--min-free-gb', type=float, default=20)
    args = parser.parse_args()
    if args.from_month and args.through_month and args.from_month>args.through_month:
        parser.error('From must precede through')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    state = dict(pid=os.getpid(), phase='starting', processed=0, months=0, entity_rows=0)
    args.progress.parent.mkdir(parents=True, exist_ok=True)
    def report(**values):
        state.update(values, updated_at=datetime.now(UTC).isoformat())
        temp = args.progress.with_suffix('.tmp')
        temp.write_text(json.dumps(state))
        temp.replace(args.progress)
    conn = None
    stopped = False
    def stop(*_args):
        nonlocal stopped
        stopped = True
        if conn is not None:
            conn.cancel()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        cfg = json.loads(args.db_config.read_text())
        conn = psycopg2.connect(dbname=cfg['db_name'], user=cfg['db_user'], password=cfg['db_password'],
                                host=cfg['db_host'], port=cfg.get('db_port',5432), application_name='killmail_rankings')
        with conn.cursor() as cur:
            cur.execute('SELECT pg_try_advisory_lock(%s)', (LOCK,))
            if not cur.fetchone()[0]:
                # Do not overwrite the progress of the process that holds the lock.
                LOG.info('Another rankings builder is already running')
                return
            if args.incremental_only:
                cur.execute("SELECT to_regclass('analytics.killmail_ranking_months')")
                if cur.fetchone()[0] is None:
                    LOG.info('Rankings await manual initialization')
                    return
                cur.execute('SELECT EXISTS(SELECT 1 FROM analytics.killmail_ranking_months)')
                if not cur.fetchone()[0]:
                    LOG.info('Rankings await manual initialization')
                    return
            cur.execute(SCHEMA.read_text())
        conn.commit()
        months = source_months(conn)
        with conn.cursor() as cur:
            cur.execute('''SELECT m.month FROM analytics.killmail_ranking_months m
                           LEFT JOIN analytics.killmail_ranking_versions v USING(month)
                           WHERE m.built_revision=COALESCE(v.revision,0)''')
            clean = {row[0] for row in cur}
        conn.commit()
        months = [m for m in months if (args.force or m not in clean)
                  and (not args.from_month or m>=args.from_month)
                  and (not args.through_month or m<=args.through_month)]
        conn.set_session(isolation_level='REPEATABLE READ')
        report(phase='queued', months=len(months))
        for month in months:
            if stopped:
                break
            if shutil.disk_usage(args.progress.parent).free < args.min_free_gb*1024**3:
                raise RuntimeError('Disk reserve reached; completed months are saved')
            result = build_month(conn,month,report)
            if stopped:
                conn.rollback()
                break
            conn.commit()
            LOG.info('MONTH_DONE month=%s %s', month, json.dumps(result))
            report(processed=state['processed']+1,entity_rows=state['entity_rows']+result['entity_rows'])
        report(phase='stopped' if stopped else 'completed')
    except Exception:
        if conn is not None:
            conn.rollback()
        if stopped:
            report(phase='stopped')
        else:
            report(phase='failed',error='Build failed; see job log. Completed months are saved.')
            LOG.exception('Rankings failed')
            raise
    finally:
        if conn is not None:
            conn.close()


if __name__ == '__main__':
    main()
