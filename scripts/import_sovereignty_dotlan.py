#!/usr/bin/env python3
"""Collect historical DOTLAN Sovereignty Changes by system, as source events.

Separate from the ESI job and deliberately NOT in the weekly pipeline:
a complete historical crawl can take many hours. Stores raw events with
source URL and event text; does not manufacture ownership intervals.
"""
import argparse
import hashlib
import json
import logging
import re
import sys
import time
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urljoin

import psycopg2
from psycopg2.extras import execute_values
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web"))
from app.dotlan_throttle import wait_for_dotlan_slot  # noqa: E402
from sovereignty_scope import load_claimable_sov_systems

CONFIG_PATH = ROOT / "config" / "db.json"
BASE_URL = "https://evemaps.dotlan.net"
USER_AGENT = "EVEOSINT-SovereigntyHistory/1.0 (+https://github.com/DictateurImperator/EVEOSINT)"
LOG = logging.getLogger("sov_dotlan")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")
IHUB_EFFECTIVE_FROM = date(2015, 7, 14)
SOVHUB_TRANSITION_FROM = date(2024, 6, 11)
SOVHUB_ONLY_FROM = date(2024, 10, 29)


class TableParser(HTMLParser):
    """Capture displayed table cells AND the hrefs missing from text extraction."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.depth = 0
        self.table = None
        self.row = None
        self.cell = None
        self.in_anchor = False
        self.anchor_href = None
        self.anchor_text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            if self.depth == 0:
                self.table = []
            self.depth += 1
        elif self.depth == 1 and tag == "tr":
            self.row = []
        elif self.depth == 1 and tag in ("td", "th") and self.row is not None:
            self.cell = {"text_parts": [], "links": [], "hints": []}
        elif self.cell is not None and tag == "a":
            self.in_anchor = True
            self.anchor_href = attrs.get("href")
            self.anchor_text = []
        elif self.cell is not None and tag == "img":
            hint = attrs.get("alt") or attrs.get("title")
            if hint:
                self.cell["hints"].append(hint)
                if normalized(hint) in ("->", "→", "➜"):
                    self.cell["text_parts"].append(" " + hint + " ")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["text_parts"].append(data)
        if self.in_anchor:
            self.anchor_text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.in_anchor:
            if self.cell is not None and self.anchor_href:
                self.cell["links"].append({
                    "href": self.anchor_href,
                    "text": " ".join("".join(self.anchor_text).split()),
                })
            self.in_anchor = False
            self.anchor_href = None
            self.anchor_text = []
        elif self.depth == 1 and tag in ("td", "th") and self.cell is not None:
            self.row.append({
                "text": " ".join("".join(self.cell["text_parts"]).split()),
                "links": self.cell["links"],
                "hints": self.cell["hints"],
            })
            self.cell = None
        elif self.depth == 1 and tag == "tr" and self.row is not None:
            if self.table is not None and self.row:
                self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.depth:
            self.depth -= 1
            if self.depth == 0 and self.table is not None:
                self.tables.append(self.table)
                self.table = None


def normalized(value):
    return " ".join(str(value or "").split())


def ownership_model(event_at):
    """Return the EVEOSINT territorial-control convention for a DOTLAN event."""
    event_day = event_at.date() if isinstance(event_at, datetime) else event_at
    if event_day >= SOVHUB_ONLY_FROM:
        return "sovhub"
    if event_day >= SOVHUB_TRANSITION_FROM:
        return "ihub_sovhub_transition_proxy"
    if event_day >= IHUB_EFFECTIVE_FROM:
        return "ihub_proxy"
    return "legacy_sov"


def classification(action):
    label = action.lower()
    if label == "gain":
        return "GAIN"
    if label == "lost":
        return "LOST"
    if label == "transfer":
        return "TRANSFER"
    if re.search(r"\b\d+\s*(?:-|→|to|>)\s*>?\s*\d+\b", label):
        return "LEVEL_CHANGE"
    return "OTHER"


def entity_cell(cells, prefix, action_pos):
    aliases = ("corp", "corporation") if prefix == "corp" else (prefix,)
    for cell in cells[action_pos + 1:]:
        for link in cell["links"]:
            href = link["href"]
            for alias in aliases:
                needle = "/" + alias + "/"
                if needle.lower() in href.lower():
                    candidate = unquote(href.split(needle, 1)[-1].split("?")[0])
                    name = (
                        normalized(link["text"]) or normalized(cell["text"])
                        or next((normalized(hint) for hint in cell["hints"] if normalized(hint)), "")
                        or candidate.replace("_", " ")
                    )
                    return name, urljoin(BASE_URL, href)

    # DOTLAN occasionally renders a plain label without an entity href.
    # Keep its original text instead of claiming to know an entity ID.
    position = action_pos + (4 if prefix == "corp" else 2)
    if position < len(cells):
        cell = cells[position]
        label = normalized(cell["text"]) or next(
            (normalized(hint) for hint in cell["hints"] if normalized(hint)), ""
        )
        if label and label not in ("-", "—"):
            return label, None
    return None, None


def parse_events(html, system_id, url):
    parser = TableParser()
    parser.feed(html)
    # An explicit DOTLAN sovereignty header is required: do not turn an error
    # page or an unrelated chart table into a successful empty import.
    if not re.search(r"Sovereignt?y\s+Changes", html, flags=re.I):
        raise ValueError("DOTLAN sovereignty change header missing")

    selected = None
    for table in parser.tables:
        for row in table[:6]:
            labels = {normalized(cell["text"]).lower() for cell in row}
            if {"date", "time", "action", "alliance"}.issubset(labels):
                selected = table
                break
        if selected is not None:
            break
    if selected is None:
        plain = re.sub(r"<[^>]*>", " ", html)
        plain = " ".join(plain.split())
        if re.search(r"Sovereignt?y Changes \[\s*0\s*\]", plain, flags=re.I):
            return []
        raise ValueError("DOTLAN sovereignty table/header missing")

    events = []
    for row_index, cells in enumerate(selected):
        values = [normalized(cell["text"]) for cell in cells]
        date_index = next((i for i, value in enumerate(values) if DATE_RE.fullmatch(value)), None)
        if date_index is None or date_index + 2 >= len(cells):
            continue
        raw_date = values[date_index]
        raw_time = values[date_index + 1]
        if not TIME_RE.fullmatch(raw_time):
            raise ValueError("Unrecognized DOTLAN sovereignty event time: " + raw_time)
        event_at = datetime.strptime(
            raw_date + " " + raw_time,
            "%Y-%m-%d %H:%M:%S" if raw_time.count(":") == 2 else "%Y-%m-%d %H:%M",
        )
        if event_at.date() > date.today():
            raise ValueError("Future DOTLAN sovereignty event date")
        action = values[date_index + 2]
        if not action:
            raise ValueError("DOTLAN sovereignty action missing")

        alliance_name, alliance_url = entity_cell(cells, "alliance", date_index + 2)
        corporation_name, corporation_url = entity_cell(cells, "corp", date_index + 2)
        raw_cells = [
            {"text": value, "links": cell["links"], "hints": cell["hints"]}
            for value, cell in zip(values, cells)
        ]

        # Same source event is idempotent across retries/reimports, regardless
        # of new events changing the row number on the page.
        identity = json.dumps(
            [system_id, event_at.isoformat(), action, alliance_url,
             corporation_url, alliance_name, corporation_name],
            ensure_ascii=False, separators=(",", ":"),
        )
        event_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        events.append({
            "event_hash": event_hash,
            "event_at": event_at,
            "action": classification(action),
            "action_raw": action,
            "ownership_model": ownership_model(event_at),
            "alliance_name": alliance_name,
            "alliance_url": alliance_url,
            "corporation_name": corporation_name,
            "corporation_url": corporation_url,
            "raw_cells": raw_cells,
            "row_position": row_index,
        })
    declared = re.search(r"Sovereignt?y\s+Changes\s*\[\s*(\d+)\s*\]", html, flags=re.I)
    if declared and int(declared.group(1)) != len(events):
        raise ValueError(
            "DOTLAN source event-count mismatch: expected %s parsed %s"
            % (declared.group(1), len(events))
        )
    return events


def connect():
    with CONFIG_PATH.open(encoding="utf-8") as fp:
        cfg = json.load(fp)
    return psycopg2.connect(
        dbname=cfg["db_name"], user=cfg["db_user"],
        password=cfg["db_password"], host=cfg["db_host"], port=cfg["db_port"],
    )


def ensure_tables(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS sovereignty")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sovereignty.dotlan_events (
                system_id BIGINT NOT NULL,
                event_hash CHAR(64) NOT NULL,
                event_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                action TEXT NOT NULL,
                action_raw TEXT NOT NULL,
                ownership_model TEXT,
                alliance_name TEXT,
                alliance_url TEXT,
                corporation_name TEXT,
                corporation_url TEXT,
                raw_cells JSONB NOT NULL,
                row_position INTEGER NOT NULL,
                source_url TEXT NOT NULL,
                fetched_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (system_id, event_hash)
            )
        """)
        cur.execute("""
            ALTER TABLE sovereignty.dotlan_events
            ADD COLUMN IF NOT EXISTS ownership_model TEXT
        """)
        cur.execute("""
            UPDATE sovereignty.dotlan_events
            SET ownership_model = CASE
                WHEN event_at::date >= DATE '2024-10-29' THEN 'sovhub'
                WHEN event_at::date >= DATE '2024-06-11' THEN 'ihub_sovhub_transition_proxy'
                WHEN event_at::date >= DATE '2015-07-14' THEN 'ihub_proxy'
                ELSE 'legacy_sov'
            END
            WHERE ownership_model IS NULL
        """)
        cur.execute("""
            ALTER TABLE sovereignty.dotlan_events
            ALTER COLUMN ownership_model SET NOT NULL
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS sov_dotlan_events_date_idx
            ON sovereignty.dotlan_events (event_at DESC)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sovereignty.dotlan_system_sync (
                system_id BIGINT PRIMARY KEY,
                system_name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                content_sha256 CHAR(64),
                event_count INTEGER NOT NULL DEFAULT 0,
                fetched_at TIMESTAMPTZ,
                last_status TEXT NOT NULL DEFAULT 'pending',
                last_error TEXT
            )
        """)
    conn.commit()


def system_names(conn, scope, selected):
    claimable = load_claimable_sov_systems(conn)
    if len(claimable) < 1000:
        raise RuntimeError(
            "SDE conquerable-nullsec scope looks incomplete (%d systems)" % len(claimable)
        )

    current_ids = set()
    if scope == "current":
        with conn.cursor() as cur:
            cur.execute("SELECT system_id FROM sovereignty.current_map")
            current_ids = {int(row[0]) for row in cur.fetchall()}
        if not current_ids and not selected:
            raise RuntimeError("Current SOV table empty: run sync_sovereignty_esi.py first")

    wanted = {str(value).casefold() for value in selected}
    systems = []
    for system_id, metadata in claimable.items():
        name = str(metadata["name"])
        if selected:
            if str(system_id).casefold() not in wanted and name.casefold() not in wanted:
                continue
        elif scope == "current" and system_id not in current_ids:
            continue
        systems.append((system_id, name))

    if selected and len(systems) != len(selected):
        found = {str(i).casefold() for i, _ in systems} | {name.casefold() for _, name in systems}
        missing = [value for value in selected if str(value).casefold() not in found]
        if missing:
            raise ValueError(
                "Unknown or non-conquerable-nullsec SDE system(s): " + ", ".join(missing)
            )
    return sorted(systems, key=lambda item: item[0])



def fetch_page(session, url):
    for attempt in range(1, 4):
        wait_for_dotlan_slot()  # same process/interprocess lock as population jobs
        try:
            response = session.get(url, timeout=(5, 45))
        except requests.RequestException as exc:
            if attempt == 3:
                raise RuntimeError("DOTLAN transport error: " + type(exc).__name__) from exc
            time.sleep(15 * attempt)
            continue

        if response.status_code == 200:
            if len(response.content) > 4_000_000:
                raise ValueError("DOTLAN response unusually large")
            return response.text
        if response.status_code in (403, 401):
            raise RuntimeError("DOTLAN access denied HTTP %s: stop crawl" % response.status_code)
        if response.status_code == 429:
            try:
                delay = max(60, int(response.headers.get("Retry-After", "60")))
            except ValueError:
                delay = 60
            if delay > 3600:
                raise RuntimeError("DOTLAN rate limited for %ss; stop crawl without early retry" % delay)
            if attempt < 3:
                LOG.warning("DOTLAN rate limited; waiting %ss", delay)
                time.sleep(delay)
                continue
        if response.status_code in (500, 502, 503, 504) and attempt < 3:
            time.sleep(20 * attempt)
            continue
        raise RuntimeError("DOTLAN HTTP %d" % response.status_code)
    raise RuntimeError("DOTLAN retries exhausted")


def save_system(conn, system_id, name, url, html, events):
    fetched = datetime.now(timezone.utc)
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
    # Per-system transaction: parser/network failure cannot destroy old events.
    with conn.cursor() as cur:
        cur.execute("DELETE FROM sovereignty.dotlan_events WHERE system_id = %s", (system_id,))
        if events:
            execute_values(cur, """
                INSERT INTO sovereignty.dotlan_events (
                    system_id, event_hash, event_at, action, action_raw, ownership_model,
                    alliance_name, alliance_url, corporation_name, corporation_url,
                    raw_cells, row_position, source_url, fetched_at
                ) VALUES %s
                ON CONFLICT (system_id, event_hash) DO NOTHING
            """, [
                (
                    system_id, item["event_hash"], item["event_at"],
                    item["action"], item["action_raw"], item["ownership_model"],
                    item["alliance_name"], item["alliance_url"],
                    item["corporation_name"], item["corporation_url"],
                    json.dumps(item["raw_cells"], ensure_ascii=False),
                    item["row_position"], url, fetched,
                )
                for item in events
            ], page_size=300)
        cur.execute("""
            INSERT INTO sovereignty.dotlan_system_sync (
                system_id, system_name, source_url, content_sha256, event_count,
                fetched_at, last_status, last_error
            ) VALUES (%s, %s, %s, %s, %s, %s, 'ok', NULL)
            ON CONFLICT (system_id) DO UPDATE SET
                system_name=EXCLUDED.system_name,
                source_url=EXCLUDED.source_url,
                content_sha256=EXCLUDED.content_sha256,
                event_count=EXCLUDED.event_count,
                fetched_at=EXCLUDED.fetched_at,
                last_status='ok',
                last_error=NULL
        """, (system_id, name, url, digest, len(events), fetched))
    conn.commit()


def save_error(conn, system_id, name, url, error):
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sovereignty.dotlan_system_sync (
                system_id, system_name, source_url, last_status, last_error
            ) VALUES (%s, %s, %s, 'failed', %s)
            ON CONFLICT (system_id) DO UPDATE SET
                last_status='failed', last_error=EXCLUDED.last_error
        """, (system_id, name, url, str(error)[:500]))
    conn.commit()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scope", choices=("current", "all-nullsec"), default="current",
                    help="Default currently claimed conquerable nullsec; all-nullsec = all conquerable nullsec, including currently unclaimed systems.")
    ap.add_argument("--system", action="append", default=[], help="One SDE system name or ID; repeat as needed.")
    ap.add_argument("--limit", type=int, default=25, help="Max systems per invocation; 0 = all pending.")
    ap.add_argument("--refresh", action="store_true", help="Also re-fetch systems previously imported successfully.")
    args = ap.parse_args()
    if args.limit < 0:
        ap.error("--limit must be >= 0")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(184624, 2)")
            if not cur.fetchone()[0]:
                LOG.error("Another DOTLAN SOV import is already active")
                return 1
        try:
            ensure_tables(conn)
            systems = system_names(conn, args.scope, args.system)
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT system_id FROM sovereignty.dotlan_system_sync
                    WHERE last_status = 'ok'
                """)
                done = {int(row[0]) for row in cur.fetchall()}
            pending = [
                (system_id, name) for system_id, name in systems
                if args.refresh or system_id not in done
            ]
            if args.limit:
                pending = pending[:args.limit]

            LOG.info("SOV_DOTLAN scope=claimable_nullsec systems_selected=%d pending=%d already_ok=%d",
                     len(systems), len(pending), len(done & {i for i, _ in systems}))
            ok = failed = total_events = 0
            with requests.Session() as session:
                session.headers.update({
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "en-US,en;q=0.8",
                })
                for system_id, name in pending:
                    path = "/system/" + quote(name.replace(" ", "_"), safe="-_")
                    url = urljoin(BASE_URL, path)
                    try:
                        html = fetch_page(session, url)
                        events = parse_events(html, system_id, url)
                        save_system(conn, system_id, name, url, html, events)
                        ok += 1
                        total_events += len(events)
                        LOG.info("SOV_DOTLAN system=%s id=%s events=%s status=ok",
                                 name, system_id, len(events))
                    except (KeyboardInterrupt, SystemExit):
                        raise
                    except Exception as exc:
                        save_error(conn, system_id, name, url, exc)
                        failed += 1
                        LOG.error("SOV_DOTLAN system=%s id=%s status=failed error=%s",
                                  name, system_id, exc)
                        if "access denied" in str(exc):
                            break
            LOG.info("SOV_DOTLAN completed=%d failed=%d events=%d remaining_estimate=%d",
                     ok, failed, total_events, max(0, len(systems) - len(done) - ok))
            return 1 if failed else 0
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(184624, 2)")
            conn.commit()


if __name__ == "__main__":
    sys.exit(main())
