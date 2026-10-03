import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests
from psycopg2.extras import execute_values

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.db import db
    from app.dotlan_throttle import REQUEST_INTERVAL_SECONDS, wait_for_dotlan_slot
else:
    from .db import db
    from .dotlan_throttle import REQUEST_INTERVAL_SECONDS, wait_for_dotlan_slot


DOTLAN_BASE = "https://evemaps.dotlan.net"
ALLIANCE_RANKING_PATH = "/alliance/all/memberCount"
MAX_WINDOW_DAYS = 1829
REQUEST_TIMEOUT_SECONDS = 45
MAX_ATTEMPTS = 4
USER_AGENT = "EVEOSINT population collector/1.0"


class AllianceListParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.max_page = 1
        self.total_alliances = None
        self._in_tr = False
        self._in_td = False
        self._cells = []
        self._cell_text = []
        self._cell_hrefs = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self._in_tr = True
            self._cells = []
        elif tag == "td" and self._in_tr:
            self._in_td = True
            self._cell_text = []
            self._cell_hrefs = []
        elif tag == "a":
            href = attrs.get("href") or ""
            match = re.search(r"/alliance/all/memberCount/page/(\d+)", href)
            if match:
                self.max_page = max(self.max_page, int(match.group(1)))
            if self._in_td and href:
                self._cell_hrefs.append(href)

    def handle_endtag(self, tag):
        if tag == "td" and self._in_td:
            text = " ".join("".join(self._cell_text).split())
            self._cells.append({"text": text, "hrefs": list(self._cell_hrefs)})
            self._in_td = False
        elif tag == "tr" and self._in_tr:
            self._consume_row(self._cells)
            self._in_tr = False
            self._cells = []

    def handle_data(self, data):
        if self._in_td:
            self._cell_text.append(data)

        if self.total_alliances is None:
            match = re.search(r"Alliance Ranking.*\[(\d+)\]", data)
            if match:
                self.total_alliances = int(match.group(1))

    def _consume_row(self, cells):
        alliance_idx = None
        alliance_href = None

        for idx, cell in enumerate(cells):
            for href in cell["hrefs"]:
                if re.fullmatch(r"/alliance/[^/]+", href) and href not in {
                    "/alliance/all",
                    "/alliance/dead",
                }:
                    alliance_idx = idx
                    alliance_href = href
                    break
            if alliance_href:
                break

        if alliance_idx is None or alliance_idx + 3 >= len(cells):
            return

        try:
            systems = int(cells[alliance_idx + 1]["text"].replace(",", ""))
            members = int(cells[alliance_idx + 2]["text"].replace(",", ""))
            corporations = int(cells[alliance_idx + 3]["text"].replace(",", ""))
        except (TypeError, ValueError):
            return

        self.rows.append(
            {
                "name": cells[alliance_idx]["text"],
                "href": alliance_href,
                "slug": alliance_href[len("/alliance/") :],
                "systems": systems,
                "members": members,
                "corporations": corporations,
            }
        )


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

    def get_with_url(self, path):
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
                return response.text, response.url

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

    def get(self, path):
        html, _ = self.get_with_url(path)
        return html


def parse_alliance_page_id(html):
    match = re.search(r"link-16159-(\d+)", html)
    if not match:
        match = re.search(
            r"<td>\s*<b>AllianceID</b>\s*</td>\s*<td>\s*(\d+)\s*</td>",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
    if not match:
        raise ValueError("dotlan_alliance_id_missing")
    return int(match.group(1))


def resolve_alliance_href_by_id(client, alliance_id):
    """Resolve the exact DOTLAN alliance page from AllianceID only."""
    alliance_id = int(alliance_id)
    html, final_url = client.get_with_url(f"/alliance/{alliance_id}")
    parsed_id = parse_alliance_page_id(html)
    if parsed_id != alliance_id:
        raise ValueError(
            f"dotlan_alliance_id_mismatch:expected={alliance_id} got={parsed_id}"
        )

    stats_link = re.search(
        r"""href=["']([^"']*/alliance/[^"']+/stats)["']""",
        html,
        flags=re.IGNORECASE,
    )
    if stats_link:
        stats_url = urljoin(DOTLAN_BASE, stats_link.group(1))
        path = urlparse(stats_url).path
        href = path.rsplit("/stats", 1)[0]
    else:
        path = urlparse(final_url).path.rstrip("/")
        if not path.startswith("/alliance/"):
            raise ValueError("dotlan_alliance_canonical_url_missing")
        href = path

    slug = href[len("/alliance/"):] if href.startswith("/alliance/") else ""
    if not slug:
        raise ValueError("dotlan_alliance_canonical_slug_missing")
    return href, slug


def _parse_iso_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def _parse_json_array(raw):
    return json.loads(raw)


def _to_int(value):
    if value is None or value == "":
        return None
    return int(value)


def parse_stats_page(html):
    alliance_id = parse_alliance_page_id(html)

    # Do not parse the first StatUtil.init() argument (the DOTLAN path).
    # Alliance names can contain apostrophes/quotes and DOTLAN may serialize
    # that argument differently. We only need the four ISO dates + max_days.
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
        r"new\s+(dotLineChart|dotSovChart)\(\s*[\"']#chart_\d+[\"']\s*,\s*\{\s*"
        r"labels:\s*(\[[^\]]*\])\s*,\s*datasets:\s*(\[.*?\])\s*\}\s*\)\s*;",
        flags=re.DOTALL,
    )

    series = {}
    sov_series = []
    labels_seen = []

    for chart_type, labels_raw, datasets_raw in chart_pattern.findall(html):
        labels = _parse_json_array(labels_raw)
        datasets = _parse_json_array(datasets_raw)
        labels_seen.extend(labels)

        for dataset in datasets:
            label = str(dataset.get("label", ""))
            values = dataset.get("data") or []
            if len(values) != len(labels):
                raise ValueError(f"dotlan_series_length_mismatch:{label}")

            if label in {"Members", "Corporations"}:
                series[label] = dict(zip(labels, values))
            elif label.startswith("Sov "):
                sov_series.append(dict(zip(labels, values)))

    if "Members" not in series:
        raise ValueError("dotlan_members_series_missing")

    all_dates = sorted(set(labels_seen))
    rows = []
    for day_text in all_dates:
        member_value = series.get("Members", {}).get(day_text)
        corporation_value = series.get("Corporations", {}).get(day_text)

        sov_values = [_to_int(s.get(day_text)) for s in sov_series]
        sov_non_null = [value for value in sov_values if value is not None]
        sovereignty_count = sum(sov_non_null) if sov_non_null else 0

        rows.append(
            {
                "snapshot_date": _parse_iso_date(day_text),
                "member_count": _to_int(member_value),
                "corporation_count": _to_int(corporation_value),
                "sovereignty_count": sovereignty_count,
            }
        )

    return {
        "alliance_id": alliance_id,
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
            CREATE TABLE IF NOT EXISTS population.alliance_daily (
                alliance_id bigint NOT NULL,
                snapshot_date date NOT NULL,
                member_count integer,
                corporation_count integer,
                sovereignty_count integer,
                source text NOT NULL DEFAULT 'dotlan',
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (alliance_id, snapshot_date)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS population.alliance_sync (
                alliance_id bigint PRIMARY KEY,
                alliance_name text,
                dotlan_slug text NOT NULL,
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
            CREATE INDEX IF NOT EXISTS idx_population_alliance_sync_slug
            ON population.alliance_sync (dotlan_slug)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_population_alliance_daily_date
            ON population.alliance_daily (snapshot_date)
            """
        )
    conn.commit()


def load_sync_by_slug(conn, slug):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT alliance_id, first_available_date, oldest_synced_date,
                   last_synced_date, initialization_done
            FROM population.alliance_sync
            WHERE dotlan_slug = %s
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (slug,),
        )
        row = cur.fetchone()

    if not row:
        return None

    return {
        "alliance_id": row[0],
        "first_available_date": row[1],
        "oldest_synced_date": row[2],
        "last_synced_date": row[3],
        "initialization_done": row[4],
    }


def load_sync_by_id(conn, alliance_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT alliance_id, first_available_date, oldest_synced_date,
                   last_synced_date, initialization_done, dotlan_slug, alliance_name
            FROM population.alliance_sync
            WHERE alliance_id = %s
            LIMIT 1
            """,
            (int(alliance_id),),
        )
        row = cur.fetchone()

    if not row:
        return None

    return {
        "alliance_id": row[0],
        "first_available_date": row[1],
        "oldest_synced_date": row[2],
        "last_synced_date": row[3],
        "initialization_done": row[4],
        "dotlan_slug": row[5],
        "alliance_name": row[6],
    }


def alliance_has_population_rows(conn, alliance_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM population.alliance_daily
                WHERE alliance_id = %s
            )
            """,
            (int(alliance_id),),
        )
        return bool(cur.fetchone()[0])


def sync_is_complete_with_rows(conn, sync):
    return bool(
        sync
        and sync.get("initialization_done")
        and alliance_has_population_rows(conn, sync["alliance_id"])
    )


def get_alliance_history_initialization_state(alliance_id):
    """Return the persisted DOTLAN population-history initialization state."""
    conn = db()
    try:
        ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT initialization_done, first_available_date, oldest_synced_date,
                       last_synced_date, last_attempt_at, last_success_at, last_error
                FROM population.alliance_sync
                WHERE alliance_id = %s
                LIMIT 1
                """,
                (int(alliance_id),),
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

        has_rows = alliance_has_population_rows(conn, alliance_id)

        return {
            "known": True,
            "initialization_done": bool(row[0]) and has_rows,
            "first_available_date": iso(row[1]),
            "oldest_synced_date": iso(row[2]),
            "last_synced_date": iso(row[3]),
            "last_attempt_at": iso(row[4]),
            "last_success_at": iso(row[5]),
            "last_error": row[6],
        }
    finally:
        conn.close()


def alliance_history_initialization_needed(alliance_id):
    return not get_alliance_history_initialization_state(alliance_id)["initialization_done"]


def _ensure_sync_placeholder(conn, alliance_id, alliance_name, slug):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO population.alliance_sync (
                alliance_id, alliance_name, dotlan_slug, initialization_done,
                last_attempt_at, updated_at
            )
            VALUES (%s, %s, %s, false, now(), now())
            ON CONFLICT (alliance_id) DO UPDATE SET
                alliance_name = COALESCE(EXCLUDED.alliance_name, population.alliance_sync.alliance_name),
                dotlan_slug = EXCLUDED.dotlan_slug,
                last_attempt_at = now(),
                last_error = NULL,
                updated_at = now()
            """,
            (int(alliance_id), alliance_name, slug),
        )
    conn.commit()


def upsert_daily_rows(conn, alliance_id, rows):
    if not rows:
        return

    values = [
        (
            alliance_id,
            row["snapshot_date"],
            row["member_count"],
            row["corporation_count"],
            row["sovereignty_count"],
        )
        for row in rows
    ]

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO population.alliance_daily (
                alliance_id,
                snapshot_date,
                member_count,
                corporation_count,
                sovereignty_count
            ) VALUES %s
            ON CONFLICT (alliance_id, snapshot_date) DO UPDATE SET
                member_count = EXCLUDED.member_count,
                corporation_count = EXCLUDED.corporation_count,
                sovereignty_count = EXCLUDED.sovereignty_count,
                source = 'dotlan',
                updated_at = now()
            """,
            values,
            page_size=1000,
        )
    conn.commit()


def upsert_sync_success(
    conn,
    alliance_id,
    alliance_name,
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
            INSERT INTO population.alliance_sync (
                alliance_id,
                alliance_name,
                dotlan_slug,
                first_available_date,
                oldest_synced_date,
                last_synced_date,
                initialization_done,
                last_attempt_at,
                last_success_at,
                last_error,
                updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now(), NULL, now())
            ON CONFLICT (alliance_id) DO UPDATE SET
                alliance_name = EXCLUDED.alliance_name,
                dotlan_slug = EXCLUDED.dotlan_slug,
                first_available_date = COALESCE(
                    population.alliance_sync.first_available_date,
                    EXCLUDED.first_available_date
                ),
                oldest_synced_date = CASE
                    WHEN population.alliance_sync.oldest_synced_date IS NULL THEN EXCLUDED.oldest_synced_date
                    WHEN EXCLUDED.oldest_synced_date IS NULL THEN population.alliance_sync.oldest_synced_date
                    ELSE LEAST(population.alliance_sync.oldest_synced_date, EXCLUDED.oldest_synced_date)
                END,
                last_synced_date = CASE
                    WHEN population.alliance_sync.last_synced_date IS NULL THEN EXCLUDED.last_synced_date
                    WHEN EXCLUDED.last_synced_date IS NULL THEN population.alliance_sync.last_synced_date
                    ELSE GREATEST(population.alliance_sync.last_synced_date, EXCLUDED.last_synced_date)
                END,
                initialization_done = population.alliance_sync.initialization_done OR EXCLUDED.initialization_done,
                last_attempt_at = now(),
                last_success_at = now(),
                last_error = NULL,
                updated_at = now()
            """,
            (
                alliance_id,
                alliance_name,
                slug,
                first_available_date,
                oldest,
                latest,
                initialization_done,
            ),
        )
    conn.commit()


def mark_sync_error(conn, alliance_id, message):
    if alliance_id is None:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE population.alliance_sync
            SET last_attempt_at = now(),
                last_error = %s,
                updated_at = now()
            WHERE alliance_id = %s
            """,
            (message[:4000], alliance_id),
        )
    conn.commit()


def discover_alliances(client):
    first_html = client.get(ALLIANCE_RANKING_PATH)
    parser = AllianceListParser()
    parser.feed(first_html)

    # DOTLAN pagination is zero-based in the URL:
    #   displayed page 1 -> base URL (no /page/N)
    #   displayed page 2 -> /page/1
    #   ...
    #   displayed page 37 -> /page/36
    # parser.max_page therefore represents the highest URL page index, not the
    # number of displayed pages.
    max_page_index = parser.max_page
    if parser.total_alliances and parser.rows:
        rows_per_page = len(parser.rows)
        total_pages = max(1, (parser.total_alliances + rows_per_page - 1) // rows_per_page)
        max_page_index = max(max_page_index, total_pages - 1)

    displayed_pages = max_page_index + 1
    alliances = list(parser.rows)
    seen = {row["href"] for row in alliances}

    print(
        f"[DISCOVERY] page 1/{displayed_pages} alliances={len(parser.rows)} "
        f"total={parser.total_alliances or '?'}",
        flush=True,
    )

    for page_index in range(1, max_page_index + 1):
        html = client.get(f"{ALLIANCE_RANKING_PATH}/page/{page_index}")
        page_parser = AllianceListParser()
        page_parser.feed(html)
        added = 0
        for row in page_parser.rows:
            if row["href"] in seen:
                continue
            alliances.append(row)
            seen.add(row["href"])
            added += 1
        display_page = page_index + 1
        print(
            f"[DISCOVERY] page {display_page}/{displayed_pages} rows={len(page_parser.rows)} "
            f"new={added} unique={len(alliances)}",
            flush=True,
        )

    return alliances, displayed_pages, parser.total_alliances


def _apply_current_snapshot(rows, current, today):
    by_date = {row["snapshot_date"]: row for row in rows}
    row = by_date.get(today)
    if row is None:
        row = {
            "snapshot_date": today,
            "member_count": current["members"],
            "corporation_count": current["corporations"],
            "sovereignty_count": current["systems"],
        }
        rows.append(row)
        return

    row["member_count"] = current["members"]
    row["corporation_count"] = current["corporations"]
    row["sovereignty_count"] = current["systems"]


def initialize_alliance(
    conn,
    client,
    alliance,
    index,
    total,
    *,
    apply_current_snapshot=True,
    expected_alliance_id=None,
):
    if expected_alliance_id is not None:
        sync = load_sync_by_id(conn, expected_alliance_id)
    else:
        sync = load_sync_by_slug(conn, alliance["slug"])
    if sync_is_complete_with_rows(conn, sync):
        print(
            f"[{index}/{total}] SKIP {alliance['name']} already initialized "
            f"alliance_id={sync['alliance_id']}",
            flush=True,
        )
        return "skipped"

    today = date.today()
    alliance_id = sync["alliance_id"] if sync else None
    has_existing_rows = bool(alliance_id is not None and alliance_has_population_rows(conn, alliance_id))
    first_available_date = sync["first_available_date"] if sync and has_existing_rows else None

    if sync and has_existing_rows and sync["oldest_synced_date"]:
        window_end = sync["oldest_synced_date"] - timedelta(days=1)
    else:
        window_end = today

    if first_available_date and window_end < first_available_date:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE population.alliance_sync
                SET initialization_done = true,
                    last_attempt_at = now(),
                    last_success_at = now(),
                    last_error = NULL,
                    updated_at = now()
                WHERE alliance_id = %s
                """,
                (alliance_id,),
            )
        conn.commit()
        print(
            f"[{index}/{total}] DONE {alliance['name']} alliance_id={alliance_id} (resume complete)",
            flush=True,
        )
        return "completed"

    calls_for_alliance = 0

    while True:
        window_start = window_end - timedelta(days=MAX_WINDOW_DAYS)
        if first_available_date and window_start < first_available_date:
            window_start = first_available_date

        path = f"{alliance['href']}/stats/{window_start.isoformat()}:{window_end.isoformat()}"
        print(
            f"[{index}/{total}] FETCH {alliance['name']} {window_start} -> {window_end}",
            flush=True,
        )

        try:
            html = client.get(path)
            parsed = parse_stats_page(html)
        except Exception as exc:
            mark_sync_error(conn, alliance_id, str(exc))
            print(
                f"[{index}/{total}] ERROR {alliance['name']}: {exc}",
                flush=True,
            )
            return "error"

        calls_for_alliance += 1
        parsed_alliance_id = parsed["alliance_id"]
        if expected_alliance_id is not None and parsed_alliance_id != int(expected_alliance_id):
            message = (
                f"dotlan_alliance_id_mismatch:expected={int(expected_alliance_id)} "
                f"got={parsed_alliance_id} slug={alliance['slug']}"
            )
            mark_sync_error(conn, expected_alliance_id, message)
            print(f"[{index}/{total}] ERROR {alliance['name']}: {message}", flush=True)
            return "error"
        if alliance_id is not None and parsed_alliance_id != int(alliance_id):
            message = (
                f"dotlan_alliance_id_changed:expected={int(alliance_id)} "
                f"got={parsed_alliance_id} slug={alliance['slug']}"
            )
            mark_sync_error(conn, alliance_id, message)
            print(f"[{index}/{total}] ERROR {alliance['name']}: {message}", flush=True)
            return "error"

        alliance_id = parsed_alliance_id
        first_available_date = parsed["first_available_date"]
        rows = parsed["rows"]

        if (
            apply_current_snapshot
            and window_end == today
            and all(key in alliance for key in ("members", "corporations", "systems"))
        ):
            _apply_current_snapshot(rows, alliance, today)

        reached_beginning = window_start <= first_available_date

        if not rows:
            # Old alliances can contain real holes in DOTLAN history. An empty
            # requested window therefore does NOT mean the alliance has no older
            # history. Keep walking backwards until first_available_date is reached.
            upsert_sync_success(
                conn,
                alliance_id,
                alliance["name"],
                alliance["slug"],
                first_available_date,
                rows,
                reached_beginning,
            )
            if reached_beginning:
                print(
                    f"[{index}/{total}] DONE {alliance['name']} alliance_id={alliance_id} "
                    f"empty final window {window_start}->{window_end}",
                    flush=True,
                )
                return "completed"

            print(
                f"[{index}/{total}] GAP {alliance['name']} alliance_id={alliance_id} "
                f"no rows {window_start}->{window_end}; continuing backwards",
                flush=True,
            )
            window_end = window_start - timedelta(days=1)
            continue

        actual_oldest = min(row["snapshot_date"] for row in rows)

        upsert_daily_rows(conn, alliance_id, rows)
        upsert_sync_success(
            conn,
            alliance_id,
            alliance["name"],
            alliance["slug"],
            first_available_date,
            rows,
            reached_beginning,
        )

        print(
            f"[{index}/{total}] SAVED {alliance['name']} alliance_id={alliance_id} "
            f"rows={len(rows)} oldest={actual_oldest} first={first_available_date} "
            f"done={'yes' if reached_beginning else 'no'}",
            flush=True,
        )

        if reached_beginning:
            print(
                f"[{index}/{total}] DONE {alliance['name']} calls={calls_for_alliance}",
                flush=True,
            )
            return "completed"

        window_end = window_start - timedelta(days=1)


def initialize_alliance_on_demand(alliance_id, alliance_name):
    """Best-effort historical DOTLAN initialization for an alliance profile.

    Alliance identity is resolved strictly from the EVE AllianceID. The alliance
    name is display-only and is never used to select a DOTLAN page. Existing
    partial initializations are resumed. Historical holes are crossed by
    initialize_alliance() instead of being mistaken for end-of-history.
    """
    alliance_id = int(alliance_id)
    alliance_name = str(alliance_name or "").strip() or f"Alliance {alliance_id}"

    conn = db()
    locked = False
    try:
        ensure_tables(conn)
        sync = load_sync_by_id(conn, alliance_id)
        if sync_is_complete_with_rows(conn, sync):
            return "skipped"

        # Prevent two web requests/workers from backfilling the same alliance at once.
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (alliance_id,))
            locked = bool(cur.fetchone()[0])
        if not locked:
            return "already_running"

        # Re-check after obtaining the lock: another request may just have completed.
        sync = load_sync_by_id(conn, alliance_id)
        if sync_is_complete_with_rows(conn, sync):
            return "skipped"

        client = DotlanClient()

        # Resolve the canonical DOTLAN page from the numeric AllianceID itself.
        # The alliance name is never used for identity or URL construction.
        try:
            canonical_href, canonical_slug = resolve_alliance_href_by_id(client, alliance_id)
        except Exception as exc:
            _ensure_sync_placeholder(conn, alliance_id, alliance_name, str(alliance_id))
            mark_sync_error(conn, alliance_id, str(exc))
            return "error"

        _ensure_sync_placeholder(conn, alliance_id, alliance_name, canonical_slug)

        alliance = {
            "name": alliance_name,
            "slug": canonical_slug,
            "href": canonical_href,
        }
        return initialize_alliance(
            conn,
            client,
            alliance,
            1,
            1,
            apply_current_snapshot=False,
            expected_alliance_id=alliance_id,
        )
    finally:
        if locked:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (alliance_id,))
                conn.commit()
            except Exception:
                conn.rollback()
        conn.close()


def main():
    started = datetime.now(timezone.utc)
    print(
        f"=== Population alliance INIT started {started.isoformat()} ===",
        flush=True,
    )
    print(
        f"DOTLAN throttle: {1.0 / REQUEST_INTERVAL_SECONDS:.2f} requests/s; "
        f"historical window: {MAX_WINDOW_DAYS} days difference max",
        flush=True,
    )

    client = DotlanClient()

    conn = db()
    try:
        ensure_tables(conn)
        alliances, page_count, advertised_total = discover_alliances(client)

        print(
            f"[DISCOVERY] complete pages={page_count} unique_alliances={len(alliances)} "
            f"advertised_total={advertised_total or '?'}",
            flush=True,
        )

        stats = {"completed": 0, "skipped": 0, "error": 0}
        total = len(alliances)

        for index, alliance in enumerate(alliances, start=1):
            result = initialize_alliance(conn, client, alliance, index, total)
            stats[result] = stats.get(result, 0) + 1
            print(
                f"[PROGRESS] {index}/{total} completed={stats['completed']} "
                f"skipped={stats['skipped']} errors={stats['error']} "
                f"http_calls={client.request_count}",
                flush=True,
            )
    finally:
        conn.close()

    finished = datetime.now(timezone.utc)
    print(
        f"=== Population alliance INIT finished {finished.isoformat()} "
        f"duration={finished - started} http_calls={client.request_count} ===",
        flush=True,
    )


if __name__ == "__main__":
    main()
