#!/usr/bin/env python3
"""Manually import all daily zKill valuation prices, resumably, from 2003-01-01."""

import argparse
import hashlib
import json
import logging
import os
import shutil
import signal
import threading
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from pathlib import Path

import psycopg2
import requests

START = date(2003, 1, 1)
LOCK = 79130422
SCHEMA = Path(__file__).with_name("zkill_prices_schema.sql")
USER_AGENT = "EVEOSINT historical valuation import https://github.com/DictateurImperator/EVEOSINT"
LOG = logging.getLogger("zkill_prices")


class StopRequested(Exception):
    pass


class RateLimited(RuntimeError):
    pass


def normalize_prices(payload):
    """Preserve decimal precision and handle JSON numbers and numeric strings alike."""
    if payload == []:
        payload = {}  # PHP encodes an empty associative result as [].
    if not isinstance(payload, dict):
        raise ValueError("Expected an item-ID to ISK-price object")
    if "error" in payload:
        raise ValueError("zKill API error: " + str(payload["error"]))
    prices = {}
    for key, value in payload.items():
        if (
            not isinstance(key, str)
            or not key.isascii()
            or not key.isdigit()
            or int(key) <= 0
        ):
            raise ValueError("Invalid inventory type ID")
        if isinstance(value, (bool, type(None), dict, list)):
            raise ValueError("Invalid ISK price for type " + key)
        try:
            price = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("Invalid ISK price for type " + key) from exc
        if (
            not price.is_finite()
            or price < 0
            or price.adjusted() > 30
            or price.as_tuple().exponent < -30
        ):
            raise ValueError("Invalid ISK price for type " + key)
        canonical = str(int(key))
        if canonical in prices:
            raise ValueError("Duplicate inventory type ID")
        prices[canonical] = price
    return prices


def prices_json(prices):
    # Decimal must become JSON numbers, not strings or rounded Python floats.
    return (
        "{"
        + ",".join(
            json.dumps(k) + ":" + format(v, "f") for k, v in sorted(prices.items())
        )
        + "}"
    )


def retry_after(value, now=None):
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            return max(
                0.0,
                (
                    parsedate_to_datetime(value) - (now or datetime.now(UTC))
                ).total_seconds(),
            )
        except (TypeError, ValueError, OverflowError):
            return None


class Client:
    def __init__(self, session, stopped, interval=2, attempts=4, on_rate_limit=None):
        self.session = session
        self.stopped = stopped
        self.interval = interval
        self.attempts = attempts
        self.on_rate_limit = on_rate_limit
        self.next_request = 0.0
        self.requests = 0
        session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Encoding": "gzip",
                "Accept": "application/json",
            }
        )

    def wait(self, seconds):
        if self.stopped.wait(max(0, seconds)):
            raise StopRequested()

    def fetch(self, day):
        url = f"https://zkillboard.com/api/prices/date/{day.isoformat()}/"
        for attempt in range(self.attempts):
            self.wait(self.next_request - time.monotonic())
            self.requests += 1
            response = None
            try:
                response = self.session.get(url, timeout=(10, 60))
                self.next_request = time.monotonic() + self.interval
                if response.status_code in (420, 429):
                    delay = max(
                        60, retry_after(response.headers.get("Retry-After")) or 60
                    )
                    LOG.warning("API_RATE_LIMIT day=%s wait_seconds=%s", day, delay)
                    self.next_request = time.monotonic() + delay
                    if self.on_rate_limit:
                        self.on_rate_limit(delay, self.requests)
                    if attempt + 1 == self.attempts:
                        raise RateLimited(
                            "zKill rate limit persists; stopped. Resume later."
                        )
                    continue
                if response.status_code >= 500:
                    response.raise_for_status()
                response.raise_for_status()
                prices = normalize_prices(
                    json.loads(response.content, parse_float=Decimal, parse_int=Decimal)
                )
                return prices, url, hashlib.sha256(response.content).hexdigest()
            except requests.RequestException:
                self.next_request = max(
                    self.next_request, time.monotonic() + min(120, 5 * 2**attempt)
                )
                if response is not None and response.status_code < 500:
                    raise
                if attempt + 1 == self.attempts:
                    raise
            finally:
                if response is not None:
                    response.close()
        raise RuntimeError("Request attempts exhausted")


def import_day(conn, client, day):
    prices, url, checksum = client.fetch(day)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO market.zkill_daily_prices
                (price_date,prices,item_count,status,source_url,source_sha256)
                VALUES(%s,%s::jsonb,%s,%s,%s,%s)
                ON CONFLICT(price_date) DO UPDATE SET prices=excluded.prices,
                item_count=excluded.item_count,status=excluded.status,source_url=excluded.source_url,
                source_sha256=excluded.source_sha256,fetched_at=now()""",
                (
                    day,
                    prices_json(prices),
                    len(prices),
                    "imported" if prices else "empty",
                    url,
                    checksum,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(prices)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-config", type=Path, default=Path.home() / "eveosint/config/db.json"
    )
    parser.add_argument(
        "--progress",
        type=Path,
        default=Path.home() / "eveosint/data/market/zkill_prices_progress.json",
    )
    parser.add_argument("--from", dest="start", type=date.fromisoformat, default=START)
    parser.add_argument(
        "--through",
        dest="end",
        type=date.fromisoformat,
        default=datetime.now(UTC).date() - timedelta(days=1),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Minimum seconds between sequential requests (at least 1).",
    )
    parser.add_argument(
        "--refresh-days",
        type=int,
        default=7,
        help="Recheck the latest seven calendar days; older completed days are reused.",
    )
    parser.add_argument("--min-free-gb", type=float, default=2.0)
    parser.add_argument("--schema-only", action="store_true")
    args = parser.parse_args()
    if (
        args.start < START
        or args.end > datetime.now(UTC).date()
        or args.end < args.start
        or not 1 <= args.interval <= 60
        or not 0 <= args.refresh_days <= 30
        or not 0 <= args.min_free_gb <= 100
    ):
        parser.error(
            "Invalid dates, request interval, refresh window or disk threshold"
        )
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    conn = None
    owns_lock = False
    state = {
        "pid": os.getpid(),
        "from": args.start.isoformat(),
        "through": args.end.isoformat(),
        "total_days": (args.end - args.start).days + 1,
        "processed": 0,
        "downloaded": 0,
        "skipped": 0,
        "empty": 0,
        "items": 0,
        "errors": 0,
        "requests": 0,
    }

    def progress(phase, **values):
        state.update(phase=phase, updated_at=datetime.now(UTC).isoformat(), **values)
        args.progress.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.progress.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(args.progress)

    try:
        cfg = json.loads(args.db_config.read_text())
        conn = psycopg2.connect(
            dbname=cfg["db_name"],
            user=cfg["db_user"],
            password=cfg["db_password"],
            host=cfg["db_host"],
            port=cfg.get("db_port", 5432),
            application_name="zkill_prices_import",
        )
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (LOCK,))
            if not cur.fetchone()[0]:
                raise RuntimeError("Another zKill price import is running")
            owns_lock = True
            cur.execute("SET statement_timeout='60s'; SET lock_timeout='5s'")
            cur.execute(SCHEMA.read_text())
        conn.commit()
        if args.schema_only:
            progress("schema_ready")
            return 0
        with conn.cursor() as cur:
            cur.execute(
                "SELECT price_date FROM market.zkill_daily_prices WHERE price_date BETWEEN %s AND %s",
                (args.start, args.end),
            )
            completed = {row[0] for row in cur}
        conn.commit()
        recent = datetime.now(UTC).date() - timedelta(days=args.refresh_days)
        pending = [
            args.start + timedelta(days=i)
            for i in range(state["total_days"])
            if args.start + timedelta(days=i) not in completed
            or args.start + timedelta(days=i) >= recent
        ]
        state.update(
            to_download=len(pending),
            skipped=state["total_days"] - len(pending),
            processed=state["total_days"] - len(pending),
        )
        progress("running")
        with requests.Session() as session:
            client = Client(
                session,
                stopped,
                args.interval,
                on_rate_limit=lambda delay, requests: progress(
                    "rate_limited",
                    requests=requests,
                    message=f"zKill rate limit: retry in {delay:.0f} seconds.",
                ),
            )
            consecutive_errors = 0
            for day in pending:
                if stopped.is_set():
                    raise StopRequested()
                if (
                    shutil.disk_usage(args.progress.parent).free
                    < args.min_free_gb * 1024**3
                ):
                    raise RuntimeError(
                        "Disk reserve reached; import paused before the next day. Free space and resume."
                    )
                progress("running", current_day=day.isoformat(), message=None)
                try:
                    count = import_day(conn, client, day)
                    state["downloaded"] += 1
                    state["items"] += count
                    state["empty"] += int(count == 0)
                    consecutive_errors = 0
                    LOG.info("PRICE_DAY day=%s items=%s", day, count)
                except (StopRequested, RateLimited):
                    raise
                except (requests.RequestException, ValueError) as exc:
                    state["errors"] += 1
                    consecutive_errors += 1
                    LOG.error("PRICE_FAILED day=%s error=%s", day, exc)
                    state["last_error"] = f"{day}: {exc}"
                    if consecutive_errors >= 5:
                        raise RuntimeError(
                            "Five consecutive API failures; stopped. Resume later."
                        ) from exc
                state["processed"] += 1
                progress("running", requests=client.requests, message=None)
        if state["errors"]:
            raise RuntimeError(
                "Some days failed; see the log. Restart retries missing days."
            )
        progress("completed", current_day=None)
        LOG.info("DONE %s", json.dumps(state))
        return 0
    except StopRequested:
        progress(
            "paused", message="Stopped. Committed days are retained; resume skips them."
        )
        return 0
    except Exception as exc:
        LOG.exception("zKill price import failed")
        if owns_lock or conn is None:
            progress("failed", error=str(exc))
        return 1
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
