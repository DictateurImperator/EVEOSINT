import json
import re
import time
from datetime import date, datetime, timedelta
from urllib.parse import quote, urljoin

import requests
from psycopg2.extras import execute_values

from .db import db
from .dotlan_throttle import REQUEST_INTERVAL_SECONDS, wait_for_dotlan_slot


DOTLAN_BASE = "https://evemaps.dotlan.net"
MAX_WINDOW_DAYS = 1097
REQUEST_TIMEOUT_SECONDS = 45
MAX_ATTEMPTS = 4
USER_AGENT = "EVEOSINT population collector/1.0"


class DotlanClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.8",
            }
        )
        self.request_count = 0

    def _wait_for_slot(self):
        wait_for_dotlan_slot()

    def get(self, path):
        url = urljoin(DOTLAN_BASE, path)
        last_error = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._wait_for_slot()
            self.request_count += 1
            try:
                response = self.session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            except requests.RequestException as exc:
                last_error = f"request_error:{exc}"
                if attempt < MAX_ATTEMPTS:
                    time.sleep(min(15 * attempt, 60))
                    continue
                raise RuntimeError(last_error) from exc

            if response.status_code == 200:
                return response.text
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = max(30, int(retry_after)) if retry_after else 60
                except ValueError:
                    delay = 60
                last_error = f"http_429:{url}"
                if attempt < MAX_ATTEMPTS:
                    time.sleep(delay)
                    continue
                raise RuntimeError(last_error)
            if 500 <= response.status_code <= 599:
                last_error = f"http_{response.status_code}:{url}"
                if attempt < MAX_ATTEMPTS:
                    time.sleep(min(15 * attempt, 60))
                    continue
                raise RuntimeError(last_error)
            raise RuntimeError(f"http_{response.status_code}:{url}")
        raise RuntimeError(last_error or f"request_failed:{url}")


def _parse_iso_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def _to_int(value):
    if value is None or value == "":
        return None
    return int(value)


def parse_corporation_stats_page(html):
    corporation_id_match = re.search(
        r"<td>\s*<b>\s*corporationID\s*</b>\s*</td>\s*<td>\s*(\d+)\s*</td>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not corporation_id_match:
        corporation_id_match = re.search(r"corporationID[^0-9]{0,80}(\d+)", html, flags=re.IGNORECASE | re.DOTALL)
    if not corporation_id_match:
        raise ValueError("dotlan_corporation_id_missing")
    corporation_id = int(corporation_id_match.group(1))

    stat_match = re.search(
        r"StatUtil\.init\(.*?,\s*[\"'](\d{4}-\d{2}-\d{2})[\"']\s*,\s*"
        r"[\"'](\d{4}-\d{2}-\d{2})[\"']\s*,\s*"
        r"[\"'](\d{4}-\d{2}-\d{2})[\"']\s*,\s*"
        r"[\"'](\d{4}-\d{2}-\d{2})[\"']\s*,\s*(\d+)\s*\)",
        html,
        flags=re.DOTALL,
    )
    if not stat_match:
        raise ValueError("dotlan_statutil_missing")

    effective_start = _parse_iso_date(stat_match.group(1))
    effective_end = _parse_iso_date(stat_match.group(2))
    first_available_date = _parse_iso_date(stat_match.group(3))
    max_available_date = _parse_iso_date(stat_match.group(4))
    max_days = int(stat_match.group(5))

    chart_pattern = re.compile(
        r"new\s+dotLineChart\(\s*[\"']#chart_\d+[\"']\s*,\s*\{\s*"
        r"labels:\s*(\[[^\]]*\])\s*,\s*datasets:\s*(\[.*?\])\s*\}\s*\)\s*;",
        flags=re.DOTALL,
    )

    member_series = None
    labels_for_members = None
    for labels_raw, datasets_raw in chart_pattern.findall(html):
        labels = json.loads(labels_raw)
        datasets = json.loads(datasets_raw)
        for dataset in datasets:
            if str(dataset.get("label", "")) != "Members":
                continue
            values = dataset.get("data") or []
            if len(values) != len(labels):
                raise ValueError("dotlan_series_length_mismatch:Members")
            labels_for_members = labels
            member_series = values
            break
        if member_series is not None:
            break

    if member_series is None or labels_for_members is None:
        raise ValueError("dotlan_members_series_missing")

    rows = []
    for day_text, member_value in zip(labels_for_members, member_series):
        rows.append(
            {
                "snapshot_date": _parse_iso_date(day_text),
                "member_count": _to_int(member_value),
            }
        )

    return {
        "corporation_id": corporation_id,
        "effective_start": effective_start,
        "effective_end": effective_end,
        "first_available_date": first_available_date,
        "max_available_date": max_available_date,
        "max_days": max_days,
        "rows": rows,
    }


def ensure_tables(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS population")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS population.corporation_daily (
                corporation_id bigint NOT NULL,
                snapshot_date date NOT NULL,
                member_count integer,
                source text NOT NULL DEFAULT 'dotlan',
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (corporation_id, snapshot_date)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS population.corporation_sync (
                corporation_id bigint PRIMARY KEY,
                corporation_name text,
                dotlan_slug text,
                first_available_date date,
                oldest_synced_date date,
                last_synced_date date,
                initialization_done boolean NOT NULL DEFAULT false,
                last_attempt_at timestamptz,
                last_success_at timestamptz,
                last_error text,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_population_corporation_daily_date
            ON population.corporation_daily (snapshot_date)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_population_corporation_sync_slug
            ON population.corporation_sync (dotlan_slug)
            """
        )
    conn.commit()


def load_sync_by_id(conn, corporation_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT corporation_id, first_available_date, oldest_synced_date,
                   last_synced_date, initialization_done, dotlan_slug, corporation_name,
                   last_attempt_at, last_success_at
            FROM population.corporation_sync
            WHERE corporation_id = %s
            LIMIT 1
            """,
            (int(corporation_id),),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "corporation_id": int(row[0]),
        "first_available_date": row[1],
        "oldest_synced_date": row[2],
        "last_synced_date": row[3],
        "initialization_done": bool(row[4]),
        "dotlan_slug": row[5],
        "corporation_name": row[6],
        "last_attempt_at": row[7],
        "last_success_at": row[8],
    }


def _corporation_population_refresh_needed(sync, today=None):
    if not sync or not sync.get("initialization_done"):
        return True

    today = today or date.today()
    last_synced_date = sync.get("last_synced_date")
    if last_synced_date is not None and last_synced_date >= today:
        return False

    # If DOTLAN was already checked successfully today, do not hit it again
    # just because its newest available point is still yesterday.
    last_success_at = sync.get("last_success_at")
    if last_success_at is not None and last_success_at.date() >= today:
        return False

    return True


def get_corporation_history_initialization_state(corporation_id):
    conn = db()
    try:
        ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT initialization_done, first_available_date, oldest_synced_date,
                       last_synced_date, last_attempt_at, last_success_at, last_error
                FROM population.corporation_sync
                WHERE corporation_id = %s
                LIMIT 1
                """,
                (int(corporation_id),),
            )
            row = cur.fetchone()
        if not row:
            return {
                "known": False,
                "initialization_done": False,
                "first_available_date": None,
                "oldest_synced_date": None,
                "last_synced_date": None,
                "last_attempt_at": None,
                "last_success_at": None,
                "last_error": None,
            }

        def iso(value):
            return value.isoformat() if value is not None else None

        return {
            "known": True,
            "initialization_done": bool(row[0]),
            "first_available_date": iso(row[1]),
            "oldest_synced_date": iso(row[2]),
            "last_synced_date": iso(row[3]),
            "last_attempt_at": iso(row[4]),
            "last_success_at": iso(row[5]),
            "last_error": row[6],
        }
    finally:
        conn.close()


def corporation_history_initialization_needed(corporation_id):
    return not get_corporation_history_initialization_state(corporation_id)["initialization_done"]


def dotlan_slug_from_name(corporation_name):
    normalized = "_".join(str(corporation_name or "").strip().split())
    return quote(normalized, safe="._-")


def _ensure_sync_placeholder(conn, corporation_id, corporation_name, slug):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO population.corporation_sync (
                corporation_id, corporation_name, dotlan_slug, initialization_done,
                last_attempt_at, updated_at
            )
            VALUES (%s, %s, %s, false, now(), now())
            ON CONFLICT (corporation_id) DO UPDATE SET
                corporation_name = COALESCE(EXCLUDED.corporation_name, population.corporation_sync.corporation_name),
                dotlan_slug = CASE
                    WHEN population.corporation_sync.dotlan_slug IS NULL
                      OR population.corporation_sync.dotlan_slug = ''
                    THEN EXCLUDED.dotlan_slug
                    ELSE population.corporation_sync.dotlan_slug
                END,
                last_attempt_at = now(),
                last_error = NULL,
                updated_at = now()
            """,
            (int(corporation_id), corporation_name, slug),
        )
    conn.commit()


def _mark_sync_error(conn, corporation_id, message):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE population.corporation_sync
            SET last_attempt_at = now(), last_error = %s, updated_at = now()
            WHERE corporation_id = %s
            """,
            (str(message)[:4000], int(corporation_id)),
        )
    conn.commit()


def _upsert_daily_rows(conn, corporation_id, rows):
    if not rows:
        return
    values = [
        (int(corporation_id), row["snapshot_date"], row["member_count"])
        for row in rows
    ]
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO population.corporation_daily (
                corporation_id, snapshot_date, member_count
            ) VALUES %s
            ON CONFLICT (corporation_id, snapshot_date) DO UPDATE SET
                member_count = EXCLUDED.member_count,
                source = 'dotlan',
                updated_at = now()
            """,
            values,
            page_size=1000,
        )
    conn.commit()


def _upsert_sync_success(
    conn,
    corporation_id,
    corporation_name,
    slug,
    first_available_date,
    rows,
    initialization_done,
):
    dates = [row["snapshot_date"] for row in rows]
    oldest = min(dates) if dates else None
    latest = max(dates) if dates else None
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO population.corporation_sync (
                corporation_id, corporation_name, dotlan_slug,
                first_available_date, oldest_synced_date, last_synced_date,
                initialization_done, last_attempt_at, last_success_at,
                last_error, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now(), NULL, now())
            ON CONFLICT (corporation_id) DO UPDATE SET
                corporation_name = EXCLUDED.corporation_name,
                dotlan_slug = EXCLUDED.dotlan_slug,
                first_available_date = COALESCE(
                    population.corporation_sync.first_available_date,
                    EXCLUDED.first_available_date
                ),
                oldest_synced_date = CASE
                    WHEN population.corporation_sync.oldest_synced_date IS NULL THEN EXCLUDED.oldest_synced_date
                    WHEN EXCLUDED.oldest_synced_date IS NULL THEN population.corporation_sync.oldest_synced_date
                    ELSE LEAST(population.corporation_sync.oldest_synced_date, EXCLUDED.oldest_synced_date)
                END,
                last_synced_date = CASE
                    WHEN population.corporation_sync.last_synced_date IS NULL THEN EXCLUDED.last_synced_date
                    WHEN EXCLUDED.last_synced_date IS NULL THEN population.corporation_sync.last_synced_date
                    ELSE GREATEST(population.corporation_sync.last_synced_date, EXCLUDED.last_synced_date)
                END,
                initialization_done = population.corporation_sync.initialization_done OR EXCLUDED.initialization_done,
                last_attempt_at = now(),
                last_success_at = now(),
                last_error = NULL,
                updated_at = now()
            """,
            (
                int(corporation_id), corporation_name, slug, first_available_date,
                oldest, latest, bool(initialization_done),
            ),
        )
    conn.commit()


def initialize_corporation(conn, client, corporation_id, corporation_name, slug):
    sync = load_sync_by_id(conn, corporation_id)
    if sync and sync["initialization_done"]:
        return "skipped"

    first_available_date = sync["first_available_date"] if sync else None
    today = date.today()
    window_end = (sync["oldest_synced_date"] - timedelta(days=1)) if sync and sync["oldest_synced_date"] else today

    if first_available_date and window_end < first_available_date:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE population.corporation_sync
                SET initialization_done = true,
                    last_attempt_at = now(), last_success_at = now(),
                    last_error = NULL, updated_at = now()
                WHERE corporation_id = %s
                """,
                (int(corporation_id),),
            )
        conn.commit()
        return "completed"

    calls = 0
    while True:
        window_start = window_end - timedelta(days=MAX_WINDOW_DAYS)
        if first_available_date and window_start < first_available_date:
            window_start = first_available_date

        path = f"/corp/{slug}/stats/{window_start.isoformat()}:{window_end.isoformat()}"
        print(
            f"[CORP POP] FETCH {corporation_name} ({corporation_id}) {window_start} -> {window_end}",
            flush=True,
        )
        try:
            parsed = parse_corporation_stats_page(client.get(path))
        except Exception as exc:
            _mark_sync_error(conn, corporation_id, exc)
            print(f"[CORP POP] ERROR {corporation_name}: {exc}", flush=True)
            return "error"

        calls += 1
        parsed_id = int(parsed["corporation_id"])
        if parsed_id != int(corporation_id):
            message = f"dotlan_corporation_id_mismatch:expected={int(corporation_id)} got={parsed_id} slug={slug}"
            _mark_sync_error(conn, corporation_id, message)
            print(f"[CORP POP] ERROR {corporation_name}: {message}", flush=True)
            return "error"

        first_available_date = parsed["first_available_date"]
        rows = parsed["rows"]
        reached_beginning = window_start <= first_available_date

        # Important: an empty old window can be a real DOTLAN hole. Never use it
        # as an implicit end-of-history marker; keep walking to first_available_date.
        if not rows:
            _upsert_sync_success(
                conn, corporation_id, corporation_name, slug,
                first_available_date, rows, reached_beginning,
            )
            if reached_beginning:
                print(f"[CORP POP] DONE {corporation_name} empty final window calls={calls}", flush=True)
                return "completed"
            print(
                f"[CORP POP] GAP {corporation_name} no rows {window_start}->{window_end}; continuing backwards",
                flush=True,
            )
            window_end = window_start - timedelta(days=1)
            continue

        actual_oldest = min(row["snapshot_date"] for row in rows)
        _upsert_daily_rows(conn, corporation_id, rows)
        _upsert_sync_success(
            conn, corporation_id, corporation_name, slug,
            first_available_date, rows, reached_beginning,
        )
        print(
            f"[CORP POP] SAVED {corporation_name} rows={len(rows)} oldest={actual_oldest} "
            f"first={first_available_date} done={'yes' if reached_beginning else 'no'}",
            flush=True,
        )
        if reached_beginning:
            print(f"[CORP POP] DONE {corporation_name} calls={calls}", flush=True)
            return "completed"
        window_end = window_start - timedelta(days=1)


def refresh_corporation_current(conn, client, corporation_id, corporation_name, slug):
    sync = load_sync_by_id(conn, corporation_id)
    if not sync or not sync["initialization_done"]:
        return initialize_corporation(conn, client, corporation_id, corporation_name, slug)

    today = date.today()
    if not _corporation_population_refresh_needed(sync, today=today):
        return "skipped"

    last_synced_date = sync.get("last_synced_date")
    window_start = (last_synced_date + timedelta(days=1)) if last_synced_date else today

    while window_start <= today:
        window_end = min(window_start + timedelta(days=MAX_WINDOW_DAYS), today)
        path = f"/corp/{slug}/stats/{window_start.isoformat()}:{window_end.isoformat()}"
        print(
            f"[CORP POP] REFRESH {corporation_name} ({corporation_id}) {window_start} -> {window_end}",
            flush=True,
        )
        try:
            parsed = parse_corporation_stats_page(client.get(path))
        except Exception as exc:
            _mark_sync_error(conn, corporation_id, exc)
            print(f"[CORP POP] REFRESH ERROR {corporation_name}: {exc}", flush=True)
            return "error"

        parsed_id = int(parsed["corporation_id"])
        if parsed_id != int(corporation_id):
            message = (
                f"dotlan_corporation_id_mismatch:expected={int(corporation_id)} "
                f"got={parsed_id} slug={slug}"
            )
            _mark_sync_error(conn, corporation_id, message)
            print(f"[CORP POP] REFRESH ERROR {corporation_name}: {message}", flush=True)
            return "error"

        rows = parsed["rows"]
        _upsert_daily_rows(conn, corporation_id, rows)
        _upsert_sync_success(
            conn, corporation_id, corporation_name, slug,
            parsed["first_available_date"], rows, True,
        )
        print(
            f"[CORP POP] REFRESH SAVED {corporation_name} rows={len(rows)} "
            f"requested={window_start}->{window_end}",
            flush=True,
        )
        window_start = window_end + timedelta(days=1)

    return "refreshed"



def initialize_corporation_on_demand(corporation_id, corporation_name):
    corporation_id = int(corporation_id)
    corporation_name = str(corporation_name or "").strip()
    if not corporation_name:
        return "missing_name"

    conn = db()
    locked = False
    lock_key = -abs(corporation_id)
    try:
        ensure_tables(conn)
        sync = load_sync_by_id(conn, corporation_id)
        if sync and not _corporation_population_refresh_needed(sync):
            return "skipped"

        slug = (sync or {}).get("dotlan_slug") or dotlan_slug_from_name(corporation_name)
        if not slug:
            return "missing_slug"

        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,))
            locked = bool(cur.fetchone()[0])
        if not locked:
            return "already_running"

        sync = load_sync_by_id(conn, corporation_id)
        if sync and not _corporation_population_refresh_needed(sync):
            return "skipped"

        _ensure_sync_placeholder(conn, corporation_id, corporation_name, slug)
        sync = load_sync_by_id(conn, corporation_id)

        if sync and sync["initialization_done"]:
            return refresh_corporation_current(
                conn, DotlanClient(), corporation_id, corporation_name, slug
            )

        return initialize_corporation(
            conn, DotlanClient(), corporation_id, corporation_name, slug
        )
    finally:
        if locked:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
                conn.commit()
            except Exception:
                conn.rollback()
        conn.close()
