#!/usr/bin/env python3
"""Diagnose NULL killmail values, then recover quotes from zKill item pages."""
import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path

import psycopg2
from psycopg2 import sql
from psycopg2.extras import register_default_jsonb
import requests

if __package__:
    from . import calculate_killmail_values as values
    from .import_zkill_prices import StopRequested, retry_after
else:
    import calculate_killmail_values as values
    from import_zkill_prices import StopRequested, retry_after

LOG = logging.getLogger('killmail_price_recovery')
SCHEMA = Path(__file__).with_name('killmail_price_recovery_schema.sql')
LOCK = 79130424

def positive_price(text):
    try:
        value = Decimal(text.replace(',', '').replace('\xa0', '').strip())
        return value if value.is_finite() and value > 0 else None
    except (InvalidOperation, AttributeError):
        return None


class ItemPage(HTMLParser):
    """Read the item page's dated market table and separately labelled current price."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.item_ids = set()
        self.history = {}
        self.current = None
        self.heading = None
        self.heading_text = ''
        self.market_section = False
        self.cells = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'link' and a.get('rel') == 'canonical':
            match = re.fullmatch(r'https://zkillboard.com/item/(\d+)/', a.get('href', ''))
            if match:
                self.item_ids.add(int(match[1]))
        if tag in ('h4', 'h5'):
            self.heading, self.heading_text = tag, ''
        if tag == 'tr' and self.market_section:
            self.cells = []
        if tag == 'td' and self.cells is not None:
            self.cell = ''

    def handle_data(self, text):
        if self.heading is not None:
            self.heading_text += text
        if self.cell is not None:
            self.cell += text

    def handle_endtag(self, tag):
        if tag == self.heading:
            text = self.heading_text.strip()
            if tag == 'h4':
                self.market_section = text == 'Market Prices - The Forge'
            if tag == 'h5' and self.market_section:
                match = re.fullmatch(r'zKillboard Price:\s*(.*?)\s+ISK', text)
                if match:
                    self.current = positive_price(match[1])
            self.heading = None
        if tag == 'td' and self.cell is not None:
            self.cells.append(self.cell.strip())
            self.cell = None
        if tag == 'tr' and self.cells is not None:
            if len(self.cells) == 2:
                raw_day, raw_price = self.cells
                if re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw_day):
                    try:
                        day = date.fromisoformat(raw_day)
                    except ValueError:
                        day = None
                    price = positive_price(raw_price)
                    if day is not None and price is not None:
                        if day in self.history and self.history[day] != price:
                            raise ValueError('Conflicting prices in item history')
                        self.history[day] = price
            self.cells = None

    def quote(self, day):
        earlier = [d for d in self.history if d <= day]
        source_day = max(earlier) if earlier else min(self.history, default=None)
        if source_day is not None:
            return self.history[source_day], 'item_history', source_day
        if self.current is not None:
            return self.current, 'item_current', None
        return values.ONE_CENT, 'fallback', None


class PageClient:
    def __init__(self, session, stopped, interval=2):
        self.session, self.stopped = session, stopped
        self.interval = max(2, interval)
        self.last = 0
        self.requests = 0
        self.cache = {}

    def fetch(self, type_id):
        if type_id in self.cache:
            return self.cache[type_id]
        for attempt in range(4):
            if self.stopped.wait(max(0, self.last + self.interval - time.monotonic())):
                raise StopRequested()
            self.last = time.monotonic()
            self.requests += 1
            r = self.session.get(f'https://zkillboard.com/item/{int(type_id)}/', timeout=(15, 60), allow_redirects=False)
            if r.status_code in (420, 429):
                delay = max(60, retry_after(r.headers.get('Retry-After')) or 60)
                if delay > 900 or attempt == 3:
                    raise RuntimeError('zKill rate limit: retry later')
                if self.stopped.wait(delay):
                    raise StopRequested()
                continue
            if r.status_code in (500, 502, 503, 504) and attempt < 3:
                if self.stopped.wait(10 * (attempt + 1)):
                    raise StopRequested()
                continue
            if r.status_code == 404:
                self.cache[type_id] = None
                return None
            r.raise_for_status()
            if r.status_code != 200 or len(r.content) > 4 * 1024 * 1024:
                raise ValueError('Unexpected zKill response')
            page = ItemPage()
            page.feed(r.text)
            if page.item_ids != {type_id}:
                raise ValueError('Item page identity mismatch or unsupported response')
            self.cache[type_id] = page
            # Bound memory; we retain parsed pages, not HTML archives.
            if len(self.cache) > 128:
                self.cache.pop(next(iter(self.cache)))
            return page
        raise RuntimeError('zKill request failed')


class DiagnosticPricing(values.Pricing):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.issues = {}

    def price(self, type_id, kill_day, market_day, date_only=False, stack=()):
        try:
            result = super().price(type_id, kill_day, market_day, date_only, stack)
        except values.HistoricalRecipeUnavailable as exc:
            self.issues[(kill_day, type_id)] = ('historical_recipe_unavailable', str(exc))
            raise
        if result[1]:
            if type_id in stack or len(stack) >= 32:
                reason, detail = 'recipe_cycle', 'Recipe recursion is cyclic or exceeds 32 levels.'
            elif self.nearest_market(type_id, kill_day) is None and type_id not in self.recipes:
                reason, detail = (('type_missing_from_sde', 'No positive quote on any imported date; this type is also absent from the current SDE.')
                                  if type_id not in self.metadata else
                                  ('no_price_or_recipe', 'No positive quote on any imported date and no recipe in the selected SDE.'))
            else:
                reason, detail = 'recipe_component_unpriced', 'A required manufacturing component has no usable price.'
            self.issues[(kill_day, type_id)] = (reason, detail)
        return result


def save_gap(conn, day, type_id, name, reason, detail):
    with conn.cursor() as cur:
        cur.execute('''INSERT INTO market.killmail_price_gaps(price_date,item_type_id,item_name,reason,detail)
          VALUES (%s,%s,%s,%s,%s) ON CONFLICT(price_date,item_type_id) DO UPDATE SET
          active=true,item_name=excluded.item_name,reason=excluded.reason,detail=excluded.detail,checked_at=now()''',
                    (day, type_id, name, reason, detail))


def save_quote(conn, day, type_id, price, kill_id=None, *, source_kind='kill_page', source_date=None):
    """Positive official daily snapshots win; each supplementary quote retains its provenance."""
    source_url = (f'https://zkillboard.com/kill/{kill_id}/' if kill_id is not None
                  else f'https://zkillboard.com/item/{type_id}/')
    with conn.cursor() as cur:
        cur.execute('''INSERT INTO market.zkill_recovered_prices
          (price_date,item_type_id,unit_price_isk,source_killmail_id,source_url,source_kind,source_price_date)
          SELECT %s,%s,%s,%s,%s,%s,%s WHERE NOT EXISTS (
            SELECT 1 FROM market.zkill_daily_prices WHERE price_date=%s
            AND COALESCE((prices->>%s)::numeric,0)>0)
          ON CONFLICT(price_date,item_type_id) DO NOTHING''',
          (day, type_id, price, kill_id, source_url, source_kind, source_date, day, str(type_id)))
        return cur.rowcount


def diagnose(conn, cfg, stopped, progress, connections=None):
    targets = values.target_months(conn, 'loss-day')
    with conn.cursor() as cur:
        cur.execute('SELECT max(price_date) FROM market.zkill_daily_prices')
        imported = cur.fetchone()[0]
    if imported is None:
        raise RuntimeError('Import daily prices first')
    through = min(imported, datetime.now(UTC).date() - timedelta(days=1))
    with conn.cursor() as cur:
        cur.execute('UPDATE market.killmail_price_gaps SET active=false')
    conn.commit()
    progress('loading_prices', months_total=len(targets))
    first = values.load_first_prices(conn, through)
    pricing = DiagnosticPricing(values.load_metadata(conn), {}, {}, valuation_mode='loss-day',
                               recipe_history=values.RecipeHistory(), first_prices=first)
    conn.commit()
    with psycopg2.connect(**cfg, application_name='killmail_price_gap_stream') as reader:
        reader.set_session(readonly=True)
        if connections is not None:
            connections.append(reader)
        register_default_jsonb(reader, loads=lambda s: json.loads(s, parse_float=Decimal, parse_int=Decimal))
        with reader.cursor(name='gap_price_stream') as stream:
            stream.itersize = 1
            stream.execute(values.price_stream_query(conn), (values.START, through))
            pending = stream.fetchone()
            for index, (month, _) in enumerate(targets, 1):
                if stopped.is_set():
                    raise StopRequested()
                progress('diagnosing', current_month=str(month), months_processed=index-1)
                with conn.cursor() as cur:
                    cur.execute(sql.SQL("SELECT DISTINCT (killmail_time AT TIME ZONE 'UTC')::date FROM rawkm.{} WHERE estimated_value_cents IS NULL AND victim_ship_type_id IS NULL").format(sql.Identifier(values.relation_name('killmails', month))))
                    missing_hull_days = [row[0] for row in cur]
                for missing_day in missing_hull_days:
                    save_gap(conn, missing_day, 0, 'Missing victim ship type', 'missing_victim_ship', 'The raw killmail has no victim ship type ID; adding an item price cannot fix this.')
                needed = values.needed_prices(conn, month, 'loss-day')
                conn.commit()
                for day, pairs in sorted(needed.items()):
                    if stopped.is_set():
                        raise StopRequested()
                    if day > through:
                        for _, type_id in pairs:
                            save_gap(conn, day, type_id, pricing.metadata.get(type_id, ('', '', ''))[2],
                                     'price_day_not_imported', 'UTC day is unfinished or daily prices have not been imported yet.')
                    else:
                        while pending is not None and pending[0] <= day:
                            if stopped.is_set():
                                raise StopRequested()
                            pricing.add_day(*pending)
                            pending = stream.fetchone()
                        pricing.issues.clear()
                        for kill_day, type_id in pairs:
                            try:
                                pricing.price(type_id, kill_day, day)
                            except values.HistoricalRecipeUnavailable:
                                pass
                        for (gap_day, type_id), (reason, detail) in pricing.issues.items():
                            save_gap(conn, gap_day, type_id, pricing.metadata.get(type_id, ('', '', ''))[2], reason, detail)
                    conn.commit()
                    progress('diagnosing', current_day=str(day))
                counts = gap_counts(conn)
                conn.commit()
                progress('diagnosing', months_processed=index, **counts)


def gap_counts(conn):
    """Sum of missing days per item, never distinct items * global date range."""
    with conn.cursor() as cur:
        cur.execute("""SELECT count(DISTINCT item_type_id),count(*),count(DISTINCT price_date)
                       FROM market.killmail_price_gaps
                       WHERE active AND status NOT IN ('recovered','fallback')
                         AND reason NOT IN ('price_day_not_imported','missing_victim_ship')""")
        items, item_days, days = cur.fetchone()
    return dict(missing_items=items, missing_item_days=item_days, missing_dates=days,
                estimated_calls_max=items)


def recover(conn, stopped, client, progress):
    with conn.cursor() as cur:
        cur.execute("""SELECT price_date,item_type_id FROM market.killmail_price_gaps
          WHERE active AND status NOT IN ('recovered','fallback') AND reason NOT IN ('price_day_not_imported','missing_victim_ship')
          ORDER BY item_type_id,price_date""")
        gaps = cur.fetchall()
    counts = gap_counts(conn)
    conn.commit()
    total_items = counts['missing_items']
    progress('recovering_items', **counts, gaps_total=len(gaps), gaps_processed=0,
             items_processed=0, recovered=0, fallback_pairs=0, unresolved=0, requests=0,
             planned_calls_max=total_items, distinct_candidate_pages=total_items)
    recovered = fallbacks = items_processed = 0
    last_type = None
    page = None
    for index, (day, type_id) in enumerate(gaps, 1):
        if stopped.is_set():
            raise StopRequested()
        if type_id != last_type:
            progress('recovering_items', current_day=str(day), current_type_id=type_id, requests=client.requests)
            page = client.fetch(type_id)
            last_type = type_id
            items_processed += 1
        price, kind, source_day = page.quote(day) if page is not None else (values.ONE_CENT, 'fallback', None)
        with conn.cursor() as cur:
            save_quote(conn, day, type_id, price, source_kind=kind, source_date=source_day)
            cur.execute("""UPDATE market.killmail_price_gaps SET status=%s,unit_price_isk=%s,
                source_killmail_id=NULL,source_kind=%s,source_price_date=%s,
                last_error=%s,checked_at=now() WHERE price_date=%s AND item_type_id=%s""",
                ('fallback' if kind == 'fallback' else 'recovered', price, kind, source_day,
                 'No price found on item page; user-requested 0.01 ISK fallback.' if kind == 'fallback' else None,
                 day, type_id))
        conn.commit()
        recovered += kind != 'fallback'
        fallbacks += kind == 'fallback'
        progress('recovering_items', current_day=str(day), current_type_id=type_id,
                 gaps_processed=index, items_processed=items_processed, recovered=recovered,
                 fallback_pairs=fallbacks, unresolved=0, requests=client.requests)


def recalculate(conn, args, stopped, progress):
    """Use the regular NULL-only calculator, with its own locks and month commits."""
    with conn.cursor() as cur:
        cur.execute('SELECT pg_advisory_unlock(%s)', (values.LOCK,))
    conn.commit()
    child_progress = args.progress.with_name('killmail_price_recovery_valuation_progress.json')
    command = [sys.executable, str(Path(values.__file__).resolve()), '--db-config', str(args.config),
               '--progress', str(child_progress)]
    if stopped.is_set():
        raise StopRequested()
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL)
    progress('recalculating', valuation_pid=process.pid)
    try:
        while process.poll() is None:
            if stopped.wait(1):
                process.terminate()
                raise StopRequested()
            try:
                child = json.loads(child_progress.read_text())
            except (OSError, ValueError):
                continue
            if child.get('pid') == process.pid:
                progress('recalculating', valuation_phase=child.get('phase'),
                         valuation_month=child.get('current_month'),
                         valuation_months_processed=child.get('months_processed', 0),
                         valuation_months_total=child.get('months_total', 0),
                         killmails_valued=child.get('killmails_valued', 0),
                         killmails_unvalued=child.get('killmails_unvalued', 0))
        if stopped.is_set():
            raise StopRequested()
        if process.returncode:
            raise RuntimeError(f'NULL killmail recalculation failed (exit {process.returncode}); see job log')
        # Also capture a very short final pass, which can finish between polls.
        if child_progress.exists():
            child = json.loads(child_progress.read_text())
            if child.get('pid') == process.pid:
                progress('recalculated', killmails_valued=child.get('killmails_valued', 0),
                         killmails_unvalued=child.get('killmails_unvalued', 0))
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def main():
    root = Path.home() / 'eveosint'
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=root / 'config' / 'db.json')
    parser.add_argument('--diagnose-only', action='store_true')
    parser.add_argument('--progress', type=Path, default=root / 'data/market/killmail_price_recovery_progress.json')
    args = parser.parse_args()
    log_path = root / 'data/logs/killmail_price_recovery.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[logging.StreamHandler(), logging.FileHandler(log_path)])
    stopped = threading.Event()
    state = {'pid': os.getpid(), 'started_at': datetime.now(UTC).isoformat()}
    def progress(phase, **kw):
        state.update(phase=phase, updated_at=datetime.now(UTC).isoformat(), **kw)
        args.progress.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.progress.with_suffix('.tmp')
        tmp.write_text(json.dumps(state))
        tmp.replace(args.progress)
        LOG.info('PRICE_RECOVERY %s', json.dumps(state))
    connections = []
    def stop(*_):
        stopped.set()
        for connection in connections:
            if not connection.closed:
                connection.cancel()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    conn = None
    locked = False
    try:
        cfg = values.load_config(args.config)
        conn = psycopg2.connect(**cfg, application_name='killmail_price_recovery')
        connections.append(conn)
        with conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
            cur.execute('SELECT pg_try_advisory_lock(%s)', (LOCK,))
            if not cur.fetchone()[0]:
                raise RuntimeError('Price recovery is already running')
            locked = True
            cur.execute('SELECT pg_try_advisory_lock(%s)', (values.LOCK,))
            if not cur.fetchone()[0]:
                raise RuntimeError('Stop or finish Calculate ISK values before diagnosing missing prices')
            cur.execute(SCHEMA.read_text())
        conn.commit()
        progress('starting')
        diagnose(conn, cfg, stopped, progress, connections)
        progress('diagnosed', **gap_counts(conn))
        conn.commit()
        if not args.diagnose_only:
            with requests.Session() as session:
                session.headers.update({'User-Agent': 'EVEOSINT missing item price recovery https://github.com/DictateurImperator/EVEOSINT', 'Accept-Encoding': 'gzip'})
                recover(conn, stopped, PageClient(session, stopped), progress)
            recalculate(conn, args, stopped, progress)
        progress('completed', message='Diagnosis complete.' if args.diagnose_only else 'Recovered prices saved; NULL killmail recalculation complete.')
        return 0
    except (StopRequested, psycopg2.errors.QueryCanceled):
        if conn:
            conn.rollback()
        if stopped.is_set():
            progress('stopped')
            return 0
        progress('failed', error='Database statement cancelled externally')
        return 1
    except Exception as exc:
        if conn:
            conn.rollback()
        LOG.exception('Price recovery failed')
        if locked:
            progress('failed', error=str(exc))
        return 1
    finally:
        if conn:
            conn.close()


if __name__ == '__main__':
    raise SystemExit(main())
