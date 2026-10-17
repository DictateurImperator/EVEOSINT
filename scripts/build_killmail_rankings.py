#!/usr/bin/env python3
"""Build sparse monthly ISK rankings. One transaction per month; source KM are read only."""
if __package__:
    from .pipeline_metrics import MetricEmitter
else:
    from pipeline_metrics import MetricEmitter

import argparse
import json
import logging
import os
import re
import shutil
import signal
import threading
from datetime import date, datetime, UTC
from pathlib import Path

import psycopg2
from psycopg2 import sql

LOCK = 79130425
LOG = logging.getLogger('killmail_rankings')
SCHEMA = Path(__file__).with_name('killmail_rankings_schema.sql')


class ProgressReporter:
    """Publish a heartbeat during SQL work; only committed months count as saved."""
    def __init__(self, path, state):
        self.emit_metrics = MetricEmitter("killmail_rankings")
        self.path, self.state = path, state
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)

    def __call__(self, **values):
        with self.lock:
            now = datetime.now(UTC).isoformat()
            if values.get('phase', self.state.get('phase')) != self.state.get('phase'):
                self.state['phase_started_at'] = now
                LOG.info('PHASE %s month=%s', values['phase'], values.get('current_month', self.state.get('current_month', '')))
            self.state.update(values, updated_at=now)
            temp = self.path.with_suffix('.tmp')
            temp.write_text(json.dumps(self.state))
            temp.replace(self.path)
            self.emit_metrics(self.state)

    def _heartbeat(self):
        while not self.done.wait(2):
            try:
                self()
            except OSError:
                LOG.exception('Unable to publish ranking heartbeat')

    def start(self):
        self()
        self.thread.start()

    def close(self):
        self.done.set()
        if self.thread.is_alive():
            self.thread.join()


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


METRICS=('destroyed_cents','weighted_destroyed_cents','lost_cents','kills','losses','unvalued_kills','unvalued_losses')


def ensure_current(conn,report=lambda **kwargs:None):
    """One-time backfill from existing monthly facts; source killmails are not read."""
    with conn.cursor() as cur:
        cur.execute('SELECT ready FROM analytics.killmail_ranking_current_state WHERE singleton')
        if cur.fetchone()[0]:
            return
        cur.execute("SET LOCAL work_mem='32MB'")
        cur.execute('SET LOCAL max_parallel_workers_per_gather=2')
        report(phase='initializing_cumulative_snapshots')
        columns=sql.SQL(',').join(sql.SQL('sum({c}) OVER w AS {alias}').format(
            c=sql.Identifier(name),alias=sql.Identifier('cumulative_'+name)) for name in METRICS)
        cur.execute(sql.SQL("""CREATE TEMP TABLE ranking_cumulative_stage ON COMMIT DROP AS
            SELECT month,entity_kind,entity_id,{columns} FROM analytics.killmail_rankings_monthly
            WINDOW w AS (PARTITION BY entity_kind,entity_id ORDER BY month ROWS UNBOUNDED PRECEDING)""").format(columns=columns))
        assignments=sql.SQL(',').join(sql.SQL('{c}=s.{c}').format(c=sql.Identifier('cumulative_'+name)) for name in METRICS)
        cur.execute(sql.SQL("""UPDATE analytics.killmail_rankings_monthly m SET {assignments}
            FROM ranking_cumulative_stage s
            WHERE m.month=s.month AND m.entity_kind=s.entity_kind AND m.entity_id=s.entity_id""").format(assignments=assignments))
        cur.execute('DELETE FROM analytics.killmail_rankings_current')
        fields=sql.SQL(',').join(sql.Identifier('cumulative_'+name) for name in METRICS)
        cur.execute(sql.SQL("""INSERT INTO analytics.killmail_rankings_current
            SELECT DISTINCT ON (entity_kind,entity_id) entity_kind,entity_id,{fields},month
            FROM analytics.killmail_rankings_monthly ORDER BY entity_kind,entity_id,month DESC""").format(fields=fields))
        cur.execute('UPDATE analytics.killmail_ranking_current_state SET ready=true WHERE singleton')
        cur.execute('ANALYZE analytics.killmail_rankings_current')


def update_current(cur,month):
    # Existing months contribute only (new - old), including corrected old killmails.
    cur.execute("""CREATE TEMP TABLE ranking_delta ON COMMIT DROP AS
        SELECT kind AS entity_kind,id AS entity_id,sum(destroyed) AS destroyed_cents,
               sum(weighted) AS weighted_destroyed_cents,sum(lost) AS lost_cents,
               sum(kills)::bigint AS kills,sum(losses)::bigint AS losses,
               sum(unvalued_kills)::bigint AS unvalued_kills,sum(unvalued_losses)::bigint AS unvalued_losses
        FROM (SELECT * FROM ranking_stage
              UNION ALL SELECT entity_kind,entity_id,-destroyed_cents,-weighted_destroyed_cents,-lost_cents,
                       -kills,-losses,-unvalued_kills,-unvalued_losses
              FROM analytics.killmail_rankings_monthly WHERE month=%s) d
        GROUP BY kind,id""",(month,))
    cur.execute('ANALYZE ranking_delta')
    assignments=sql.SQL(',').join(sql.SQL('{c}=c.{c}+d.{c}').format(c=sql.Identifier(name)) for name in METRICS)
    changed=sql.SQL(' OR ').join(sql.SQL('d.{}<>0').format(sql.Identifier(name)) for name in METRICS)
    cur.execute(sql.SQL("""UPDATE analytics.killmail_rankings_current c SET {assignments}
        FROM ranking_delta d WHERE c.entity_kind=d.entity_kind AND c.entity_id=d.entity_id AND ({changed})""").format(
        assignments=assignments,changed=changed))
    cur.execute("""INSERT INTO analytics.killmail_rankings_current
        SELECT d.*,%s FROM ranking_delta d WHERE NOT EXISTS
          (SELECT 1 FROM analytics.killmail_rankings_current c
           WHERE c.entity_kind=d.entity_kind AND c.entity_id=d.entity_id) AND (d.kills>0 OR d.losses>0)""",(month,))
    cur.execute("""DELETE FROM analytics.killmail_rankings_current c USING ranking_delta d
                   WHERE c.entity_kind=d.entity_kind AND c.entity_id=d.entity_id AND c.kills=0 AND c.losses=0""")


def write_month_snapshots(cur,month):
    # Propagate corrected old activity to later ACTIVE snapshots only.
    assignments=sql.SQL(',').join(sql.SQL('{c}=m.{c}+d.{delta}').format(
        c=sql.Identifier('cumulative_'+name),delta=sql.Identifier(name)) for name in METRICS)
    changed=sql.SQL(' OR ').join(sql.SQL('d.{}<>0').format(sql.Identifier(name)) for name in METRICS)
    cur.execute(sql.SQL("""UPDATE analytics.killmail_rankings_monthly m SET {assignments}
        FROM ranking_delta d WHERE m.entity_kind=d.entity_kind AND m.entity_id=d.entity_id
          AND m.month>%s AND ({changed})""").format(assignments=assignments,changed=changed),(month,))
    # Insert this month's snapshot from the last preceding active month.
    aliases=dict(zip(METRICS,('destroyed','weighted','lost','kills','losses','unvalued_kills','unvalued_losses')))
    columns=sql.SQL(',').join(sql.Identifier('cumulative_'+name) for name in METRICS)
    values=sql.SQL(',').join(sql.SQL('COALESCE(p.{c},0)+s.{v}').format(
        c=sql.Identifier('cumulative_'+name),v=sql.Identifier(aliases[name])) for name in METRICS)
    cur.execute(sql.SQL("""INSERT INTO analytics.killmail_rankings_monthly
        (month,entity_kind,entity_id,destroyed_cents,weighted_destroyed_cents,lost_cents,
         kills,losses,unvalued_kills,unvalued_losses,{columns})
        SELECT %s,s.kind,s.id,s.destroyed,s.weighted,s.lost,s.kills,s.losses,
               s.unvalued_kills,s.unvalued_losses,{values}
        FROM ranking_stage s LEFT JOIN LATERAL (
            SELECT {columns} FROM analytics.killmail_rankings_monthly m
            WHERE m.entity_kind=s.kind AND m.entity_id=s.id AND m.month<%s
            ORDER BY month DESC LIMIT 1
        ) p ON true""").format(columns=columns,values=values),(month,month))
    rows=cur.rowcount
    cur.execute("""UPDATE analytics.killmail_rankings_current c SET last_month=m.month
        FROM ranking_delta d CROSS JOIN LATERAL (
            SELECT max(month) AS month FROM analytics.killmail_rankings_monthly p
            WHERE p.entity_kind=d.entity_kind AND p.entity_id=d.entity_id
        ) m WHERE c.entity_kind=d.entity_kind AND c.entity_id=d.entity_id
            AND c.last_month IS DISTINCT FROM m.month""")
    return rows


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
        report(phase='reading_killmails', current_month=month.strftime('%Y-%m'), killmails=None, valued=None)
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
            ) SELECT kind,id,sum(destroyed) AS destroyed,sum(weighted)::numeric(38,8) AS weighted,sum(lost) AS lost,
                     sum(kills)::bigint AS kills,sum(losses)::bigint AS losses,
                     sum(unvalued_kills)::bigint AS unvalued_kills,sum(unvalued_losses)::bigint AS unvalued_losses
              FROM facts GROUP BY kind,id''')
        update_current(cur,month)
        cur.execute('DELETE FROM analytics.killmail_rankings_monthly WHERE month=%s', (month,))
        rows = write_month_snapshots(cur,month)
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
    now = datetime.now(UTC).isoformat()
    state = dict(pid=os.getpid(), phase='starting', processed=0, months=0, entity_rows=0, started_at=now, phase_started_at=now)
    args.progress.parent.mkdir(parents=True, exist_ok=True)
    report = ProgressReporter(args.progress, state)
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
            cur.execute("SET LOCAL lock_timeout='10s'")
            cur.execute(SCHEMA.read_text())
        conn.commit()
        if shutil.disk_usage(args.progress.parent).free < args.min_free_gb*1024**3:
            raise RuntimeError('Disk reserve reached; initialization not started')
        conn.set_session(isolation_level='REPEATABLE READ')
        report.start()
        ensure_current(conn,report)
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
        if not stopped:
            report(phase='analyzing_totals')
            with conn.cursor() as cur:
                cur.execute('ANALYZE analytics.killmail_rankings_current')
                cur.execute('ANALYZE analytics.killmail_rankings_monthly')
            conn.commit()
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
        report.close()
        if conn is not None:
            conn.close()


if __name__ == '__main__':
    main()
