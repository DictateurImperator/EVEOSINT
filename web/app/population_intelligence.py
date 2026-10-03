from bisect import bisect_left, bisect_right
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from collections import OrderedDict
from math import ceil
from statistics import median
from threading import Lock
from time import monotonic

from psycopg2.errors import QueryCanceled

from .db import db


UTC = timezone.utc
MAX_SNAPSHOT_DATES = 16
DEFAULT_ACTIVITY_WINDOW_DAYS = 90
DEFAULT_CORE_MONTHS = 6


PILOT_LIST_METRICS = {
    "active_count": "PvP population",
    "victim_only_count": "Victim-only characters",
    "retained_pvp_count": "Retained PvP",
    "churn_count": "PvP churn",
    "newly_active_pvp": "Entered PvP population",
    "pvp_arrivals": "PvP-active joiners",
    "pvp_departures": "PvP-active leavers",
    "core_pvp_count": "Core PvP population",
}

_PILOT_SET_CACHE = OrderedDict()
_PILOT_SET_CACHE_LOCK = Lock()
_PILOT_SET_CACHE_TTL_SECONDS = 300
_PILOT_SET_CACHE_MAX = 64


ACTIVITY_METRICS = {
    "active_count",
    "active_rate_pct",
    "victim_only_count",
    "victim_only_rate_pct",
    "retained_pvp_count",
    "retention_pct",
    "churn_count",
    "churn_pct",
    "newly_active_pvp",
    "core_pvp_count",
    "core_pvp_rate_pct",
    "median_character_age_years",
    "median_alliance_tenure_days",
    "pvp_arrivals",
    "pvp_departures",
    "pvp_turnover_count",
    "pvp_turnover_rate_pct",
    "median_activation_delay_days",
    "top10_activity_concentration_pct",
}

OFFICIAL_METRICS = {"member_count", "corporation_count", "sovereignty_count"}
COALITION_OFFICIAL_METRICS = OFFICIAL_METRICS | {"alliance_count"}
ALL_METRICS = OFFICIAL_METRICS | ACTIVITY_METRICS


INDICATOR_GROUPS = {
    "activity": {
        "active_count",
        "active_rate_pct",
        "victim_only_count",
        "victim_only_rate_pct",
        "top10_activity_concentration_pct",
        "median_character_age_years",
        "median_alliance_tenure_days",
    },
    "retention": {
        "retained_pvp_count",
        "retention_pct",
        "churn_count",
        "churn_pct",
        "newly_active_pvp",
    },
    "movement": {
        "pvp_arrivals",
        "pvp_departures",
        "pvp_turnover_count",
        "pvp_turnover_rate_pct",
    },
    "core": {
        "core_pvp_count",
        "core_pvp_rate_pct",
    },
    "first_pvp": {
        "median_activation_delay_days",
    },
}



def _table_exists(conn, schema_name, table_name):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = %s
                  AND table_name = %s
            )
            """,
            (schema_name, table_name),
        )
        row = cur.fetchone()
    return bool(row and row[0])


def _column_exists(conn, schema_name, table_name, column_name):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = %s
                  AND table_name = %s
                  AND column_name = %s
            )
            """,
            (schema_name, table_name, column_name),
        )
        row = cur.fetchone()
    return bool(row and row[0])


def _format_count(value):
    if value is None:
        return None
    return f"{int(value):,}"


def _coerce_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _at_end_of_day(day):
    return datetime.combine(day, time.max, tzinfo=UTC)


def _month_floor(day):
    return date(day.year, day.month, 1)


def _shift_months(day, months):
    month_index = day.year * 12 + (day.month - 1) + months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def _safe_pct(numerator, denominator):
    if numerator is None or denominator in (None, 0):
        return None
    return (float(numerator) / float(denominator)) * 100.0


def _growth_pct(current, previous):
    if current is None or previous in (None, 0):
        return None
    return ((float(current) - float(previous)) / float(previous)) * 100.0


def _serialize_number(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value:  # NaN guard
            return None
        return round(value, 6)
    return value


def get_alliance_population_summary(alliance_id):
    alliance_id = int(alliance_id)

    with db() as conn:
        if not _table_exists(conn, "population", "alliance_daily"):
            return None

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    snapshot_date,
                    member_count,
                    corporation_count,
                    sovereignty_count
                FROM population.alliance_daily
                WHERE alliance_id = %s
                ORDER BY snapshot_date DESC
                LIMIT 1
                """,
                (alliance_id,),
            )
            row = cur.fetchone()

    if not row:
        return None

    snapshot_date, member_count, corporation_count, sovereignty_count = row
    parts = []
    if member_count is not None:
        parts.append(f"{_format_count(member_count)} members")
    if corporation_count is not None:
        parts.append(f"{_format_count(corporation_count)} corporations")
    if sovereignty_count is not None:
        parts.append(f"{_format_count(sovereignty_count)} systems")

    return {
        "snapshot_date": snapshot_date.isoformat() if snapshot_date else None,
        "member_count": int(member_count) if member_count is not None else None,
        "corporation_count": int(corporation_count) if corporation_count is not None else None,
        "sovereignty_count": int(sovereignty_count) if sovereignty_count is not None else None,
        "display": " · ".join(parts),
    }


def get_alliance_population_history(alliance_id):
    alliance_id = int(alliance_id)

    with db() as conn:
        if not _table_exists(conn, "population", "alliance_daily"):
            return {
                "alliance_id": alliance_id,
                "available": False,
                "source": "dotlan",
                "initialization_done": False,
                "first_available_date": None,
                "oldest_synced_date": None,
                "last_synced_date": None,
                "rows": [],
            }

        sync_row = None
        has_history_unavailable = False
        if _table_exists(conn, "population", "alliance_sync"):
            has_history_unavailable = _column_exists(
                conn,
                "population",
                "alliance_sync",
                "history_unavailable",
            )
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        first_available_date,
                        oldest_synced_date,
                        last_synced_date,
                        initialization_done,
                        last_error
                        {history_column}
                    FROM population.alliance_sync
                    WHERE alliance_id = %s
                    LIMIT 1
                    """.format(
                        history_column=", history_unavailable" if has_history_unavailable else ""
                    ),
                    (alliance_id,),
                )
                sync_row = cur.fetchone()

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    snapshot_date,
                    member_count,
                    corporation_count,
                    sovereignty_count
                FROM population.alliance_daily
                WHERE alliance_id = %s
                ORDER BY snapshot_date ASC
                """,
                (alliance_id,),
            )
            data_rows = cur.fetchall()

    rows = [
        {
            "date": row[0].isoformat(),
            "member_count": int(row[1]) if row[1] is not None else None,
            "corporation_count": int(row[2]) if row[2] is not None else None,
            "sovereignty_count": int(row[3]) if row[3] is not None else None,
        }
        for row in data_rows
    ]

    first_available_date = sync_row[0].isoformat() if sync_row and sync_row[0] else (rows[0]["date"] if rows else None)
    oldest_synced_date = sync_row[1].isoformat() if sync_row and sync_row[1] else (rows[0]["date"] if rows else None)
    last_synced_date = sync_row[2].isoformat() if sync_row and sync_row[2] else (rows[-1]["date"] if rows else None)

    history_unavailable = bool(sync_row[5]) if sync_row and has_history_unavailable else False

    return {
        "alliance_id": alliance_id,
        "available": bool(rows),
        "history_available": False if history_unavailable else (True if rows else None),
        "source": "dotlan",
        "initialization_done": bool(sync_row[3]) if sync_row else False,
        "first_available_date": first_available_date,
        "oldest_synced_date": oldest_synced_date,
        "last_synced_date": last_synced_date,
        "last_error": sync_row[4] if sync_row else None,
        "rows": rows,
    }



def get_corporation_population_summary(corporation_id):
    corporation_id = int(corporation_id)
    with db() as conn:
        if not _table_exists(conn, "population", "corporation_daily"):
            return None
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT snapshot_date, member_count
                FROM population.corporation_daily
                WHERE corporation_id = %s
                ORDER BY snapshot_date DESC
                LIMIT 1
                """,
                (corporation_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    snapshot_date, member_count = row
    return {
        "snapshot_date": snapshot_date.isoformat() if snapshot_date else None,
        "member_count": int(member_count) if member_count is not None else None,
        "display": f"{_format_count(member_count)} members" if member_count is not None else "",
    }


def get_corporation_population_history(corporation_id):
    corporation_id = int(corporation_id)
    with db() as conn:
        if not _table_exists(conn, "population", "corporation_daily"):
            return {
                "corporation_id": corporation_id,
                "available": False,
                "source": "dotlan",
                "initialization_done": False,
                "first_available_date": None,
                "oldest_synced_date": None,
                "last_synced_date": None,
                "rows": [],
            }

        sync_row = None
        if _table_exists(conn, "population", "corporation_sync"):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT first_available_date, oldest_synced_date, last_synced_date,
                           initialization_done, last_error
                    FROM population.corporation_sync
                    WHERE corporation_id = %s
                    LIMIT 1
                    """,
                    (corporation_id,),
                )
                sync_row = cur.fetchone()

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT snapshot_date, member_count
                FROM population.corporation_daily
                WHERE corporation_id = %s
                ORDER BY snapshot_date ASC
                """,
                (corporation_id,),
            )
            data_rows = cur.fetchall()

    rows = [
        {
            "date": row[0].isoformat(),
            "member_count": int(row[1]) if row[1] is not None else None,
            "corporation_count": None,
            "sovereignty_count": None,
        }
        for row in data_rows
    ]
    first_available_date = sync_row[0].isoformat() if sync_row and sync_row[0] else (rows[0]["date"] if rows else None)
    oldest_synced_date = sync_row[1].isoformat() if sync_row and sync_row[1] else (rows[0]["date"] if rows else None)
    last_synced_date = sync_row[2].isoformat() if sync_row and sync_row[2] else (rows[-1]["date"] if rows else None)
    return {
        "corporation_id": corporation_id,
        "available": bool(rows),
        "source": "dotlan",
        "initialization_done": bool(sync_row[3]) if sync_row else False,
        "first_available_date": first_available_date,
        "oldest_synced_date": oldest_synced_date,
        "last_synced_date": last_synced_date,
        "last_error": sync_row[4] if sync_row else None,
        "rows": rows,
    }


def _load_corporation_official_rows(conn, corporation_id):
    if not _table_exists(conn, "population", "corporation_daily"):
        return [], []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT snapshot_date, member_count
            FROM population.corporation_daily
            WHERE corporation_id = %s
            ORDER BY snapshot_date ASC
            """,
            (int(corporation_id),),
        )
        rows = cur.fetchall()
    result = [
        {
            "date": row[0],
            "member_count": int(row[1]) if row[1] is not None else None,
            "corporation_count": None,
            "sovereignty_count": None,
        }
        for row in rows
    ]
    return result, [row["date"] for row in result]

def _load_official_rows(conn, alliance_id):
    if not _table_exists(conn, "population", "alliance_daily"):
        return [], []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT snapshot_date, member_count, corporation_count, sovereignty_count
            FROM population.alliance_daily
            WHERE alliance_id = %s
            ORDER BY snapshot_date ASC
            """,
            (alliance_id,),
        )
        rows = cur.fetchall()
    result = [
        {
            "date": row[0],
            "member_count": int(row[1]) if row[1] is not None else None,
            "corporation_count": int(row[2]) if row[2] is not None else None,
            "sovereignty_count": int(row[3]) if row[3] is not None else None,
        }
        for row in rows
    ]
    return result, [row["date"] for row in result]


def _official_at_or_before(rows, dates, target):
    if not rows:
        return None
    idx = bisect_right(dates, target) - 1
    if idx < 0:
        return None
    return rows[idx]


def _load_alliance_membership_spells(conn, alliance_id, max_day):
    """Return merged character->alliance membership spells.

    This is read-only. A character spell is the temporal intersection between the
    character's corporation history and that corporation's alliance history.
    Adjacent/overlapping pieces are merged so an internal corporation transfer
    inside the same alliance does not manufacture a leave/join event.
    """
    max_ts = datetime.combine(max_day + timedelta(days=1), time.min, tzinfo=UTC)
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '120000ms'")
        cur.execute(
            """
            SELECT
                cch.character_id,
                GREATEST(cch.start_date, cah.start_date) AS start_at,
                LEAST(
                    COALESCE(cch.end_date, 'infinity'::timestamptz),
                    COALESCE(cah.end_date, 'infinity'::timestamptz)
                ) AS end_at
            FROM entities.corporation_alliance_history cah
            JOIN entities.character_corporation_history cch
              ON cch.corporation_id = cah.corporation_id
            WHERE cah.alliance_id = %s
              AND COALESCE(cah.is_deleted, FALSE) = FALSE
              AND COALESCE(cch.is_deleted, FALSE) = FALSE
              AND cch.start_date < COALESCE(cah.end_date, 'infinity'::timestamptz)
              AND cah.start_date < COALESCE(cch.end_date, 'infinity'::timestamptz)
              AND GREATEST(cch.start_date, cah.start_date) < %s
            ORDER BY cch.character_id, start_at, end_at
            """,
            (alliance_id, max_ts),
        )
        rows = cur.fetchall()

    spells_by_character = {}
    all_spells = []
    current_character = None
    merged = []

    def flush():
        nonlocal merged, current_character
        if current_character is None:
            return
        spells_by_character[current_character] = merged
        all_spells.extend((current_character, start_at, end_at) for start_at, end_at in merged)

    for character_id, start_at, end_at in rows:
        character_id = int(character_id)
        if current_character != character_id:
            flush()
            current_character = character_id
            merged = []

        if getattr(end_at, "tzinfo", None) is None and end_at.year < 9999:
            end_at = end_at.replace(tzinfo=UTC)
        if getattr(start_at, "tzinfo", None) is None:
            start_at = start_at.replace(tzinfo=UTC)

        if not merged:
            merged.append([start_at, end_at])
            continue

        previous = merged[-1]
        previous_end = previous[1]
        # PostgreSQL infinity is returned as datetime.max by psycopg2.
        if previous_end.year >= 9999 or start_at <= previous_end + timedelta(seconds=1):
            if previous_end.year >= 9999 or end_at <= previous_end:
                continue
            previous[1] = end_at
        else:
            merged.append([start_at, end_at])

    flush()

    # Normalize lists to tuples so callers cannot accidentally mutate the cache-like structure.
    normalized = {
        character_id: [(spell[0], spell[1]) for spell in spells]
        for character_id, spells in spells_by_character.items()
    }
    normalized_all = [(cid, start_at, end_at) for cid, start_at, end_at in all_spells]
    return normalized, normalized_all



def _load_corporation_membership_spells(conn, corporation_id, max_day):
    """Return merged character membership spells for one corporation."""
    max_ts = datetime.combine(max_day + timedelta(days=1), time.min, tzinfo=UTC)
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '120000ms'")
        cur.execute(
            """
            SELECT character_id, start_date, COALESCE(end_date, 'infinity'::timestamptz)
            FROM entities.character_corporation_history
            WHERE corporation_id = %s
              AND COALESCE(is_deleted, FALSE) = FALSE
              AND start_date < %s
            ORDER BY character_id, start_date, end_date NULLS LAST
            """,
            (int(corporation_id), max_ts),
        )
        rows = cur.fetchall()

    spells_by_character = {}
    all_spells = []
    current_character = None
    merged = []

    def flush():
        nonlocal merged, current_character
        if current_character is None:
            return
        spells_by_character[current_character] = [(row[0], row[1]) for row in merged]
        all_spells.extend((current_character, row[0], row[1]) for row in merged)

    for character_id, start_at, end_at in rows:
        character_id = int(character_id)
        if getattr(start_at, "tzinfo", None) is None:
            start_at = start_at.replace(tzinfo=UTC)
        if getattr(end_at, "tzinfo", None) is None and end_at.year < 9999:
            end_at = end_at.replace(tzinfo=UTC)

        if current_character != character_id:
            flush()
            current_character = character_id
            merged = []

        if not merged:
            merged.append([start_at, end_at])
            continue
        previous = merged[-1]
        previous_end = previous[1]
        if previous_end.year >= 9999 or start_at <= previous_end + timedelta(seconds=1):
            if previous_end.year < 9999 and end_at > previous_end:
                previous[1] = end_at
        else:
            merged.append([start_at, end_at])

    flush()
    return spells_by_character, all_spells

def _load_birthdays(conn, character_ids):
    ids = sorted({int(value) for value in character_ids if value is not None})
    if not ids:
        return {}
    result = {}
    chunk_size = 10000
    with conn.cursor() as cur:
        for offset in range(0, len(ids), chunk_size):
            chunk = ids[offset:offset + chunk_size]
            cur.execute(
                """
                SELECT character_id, birthday
                FROM entities.characters
                WHERE character_id = ANY(%s)
                  AND birthday IS NOT NULL
                  AND COALESCE(is_deleted, FALSE) = FALSE
                """,
                (chunk,),
            )
            for character_id, birthday in cur.fetchall():
                result[int(character_id)] = birthday
    return result


def _load_alliance_activity_days(conn, alliance_id, start_day, end_day):
    """Return one row per character/day with any/attacker killmail counts.

    The alliance scope intentionally mirrors the existing alliance killboard:
    direct alliance_id when present, corporation-alliance history fallback when
    old killmails have a NULL alliance_id.
    """
    start_ts = datetime.combine(start_day, time.min, tzinfo=UTC)
    end_ts = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=UTC)

    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '120000ms'")
        cur.execute(
            """
            WITH scoped_events AS (
                SELECT
                    km.victim_character_id::bigint AS character_id,
                    km.killmail_id::bigint AS killmail_id,
                    km.killmail_time::date AS activity_date,
                    FALSE AS is_attacker
                FROM rawkm.killmails km
                WHERE km.killmail_time >= %s
                  AND km.killmail_time < %s
                  AND km.victim_character_id IS NOT NULL
                  AND (
                        km.victim_alliance_id = %s
                        OR (
                            km.victim_alliance_id IS NULL
                            AND km.victim_corporation_id IS NOT NULL
                            AND EXISTS (
                                SELECT 1
                                FROM entities.corporation_alliance_history cah
                                WHERE cah.corporation_id = km.victim_corporation_id
                                  AND cah.alliance_id = %s
                                  AND cah.is_deleted = FALSE
                                  AND cah.start_date <= km.killmail_time
                                  AND (cah.end_date IS NULL OR km.killmail_time < cah.end_date)
                            )
                        )
                  )

                UNION ALL

                SELECT
                    ka.character_id::bigint AS character_id,
                    ka.killmail_id::bigint AS killmail_id,
                    ka.killmail_time::date AS activity_date,
                    TRUE AS is_attacker
                FROM rawkm.killmail_attackers ka
                WHERE ka.killmail_time >= %s
                  AND ka.killmail_time < %s
                  AND ka.character_id IS NOT NULL
                  AND (
                        ka.alliance_id = %s
                        OR (
                            ka.alliance_id IS NULL
                            AND ka.corporation_id IS NOT NULL
                            AND EXISTS (
                                SELECT 1
                                FROM entities.corporation_alliance_history cah
                                WHERE cah.corporation_id = ka.corporation_id
                                  AND cah.alliance_id = %s
                                  AND cah.is_deleted = FALSE
                                  AND cah.start_date <= ka.killmail_time
                                  AND (cah.end_date IS NULL OR ka.killmail_time < cah.end_date)
                            )
                        )
                  )
            ), deduped AS (
                SELECT DISTINCT character_id, killmail_id, activity_date, is_attacker
                FROM scoped_events
            )
            SELECT
                character_id,
                activity_date,
                COUNT(DISTINCT killmail_id)::integer AS any_killmails,
                COUNT(DISTINCT killmail_id) FILTER (WHERE is_attacker)::integer AS attacker_killmails
            FROM deduped
            GROUP BY character_id, activity_date
            ORDER BY character_id, activity_date
            """,
            (
                start_ts, end_ts, alliance_id, alliance_id,
                start_ts, end_ts, alliance_id, alliance_id,
            ),
        )
        rows = cur.fetchall()

    activity = {}
    for character_id, activity_date, any_count, attacker_count in rows:
        cid = int(character_id)
        bucket = activity.setdefault(cid, {"dates": [], "any": [], "attacker": []})
        bucket["dates"].append(activity_date)
        bucket["any"].append(int(any_count or 0))
        bucket["attacker"].append(int(attacker_count or 0))
    return activity


def _load_member_activity_days(conn, character_ids, start_day, end_day):
    """Return daily PvP observations for known historical members only.

    Membership is resolved separately from the affiliation histories. This query
    therefore only asks the killmail tables whether those characters were seen
    during the requested time slice; _compute_snapshot intersects the result
    with the exact alliance-membership spell for each reference date.
    """
    ids = sorted({int(value) for value in character_ids if value is not None})
    if not ids or start_day > end_day:
        return {}

    start_ts = datetime.combine(start_day, time.min, tzinfo=UTC)
    end_ts = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=UTC)

    with conn.cursor() as cur:
        # Return before the reverse proxy timeout instead of hanging the whole page.
        cur.execute("SET LOCAL statement_timeout = '45000ms'")
        cur.execute(
            """
            WITH scoped_events AS (
                SELECT
                    km.victim_character_id::bigint AS character_id,
                    km.killmail_id::bigint AS killmail_id,
                    km.killmail_time::date AS activity_date,
                    FALSE AS is_attacker
                FROM rawkm.killmails km
                WHERE km.killmail_time >= %s
                  AND km.killmail_time < %s
                  AND km.victim_character_id = ANY(%s)

                UNION ALL

                SELECT
                    ka.character_id::bigint AS character_id,
                    ka.killmail_id::bigint AS killmail_id,
                    ka.killmail_time::date AS activity_date,
                    TRUE AS is_attacker
                FROM rawkm.killmail_attackers ka
                WHERE ka.killmail_time >= %s
                  AND ka.killmail_time < %s
                  AND ka.character_id = ANY(%s)
            ), deduped AS (
                SELECT DISTINCT character_id, killmail_id, activity_date, is_attacker
                FROM scoped_events
            )
            SELECT
                character_id,
                activity_date,
                COUNT(DISTINCT killmail_id)::integer AS any_killmails,
                COUNT(DISTINCT killmail_id) FILTER (WHERE is_attacker)::integer AS attacker_killmails
            FROM deduped
            GROUP BY character_id, activity_date
            ORDER BY character_id, activity_date
            """,
            (start_ts, end_ts, ids, start_ts, end_ts, ids),
        )
        rows = cur.fetchall()

    activity = {}
    for character_id, activity_date, any_count, attacker_count in rows:
        cid = int(character_id)
        bucket = activity.setdefault(cid, {"dates": [], "any": [], "attacker": []})
        bucket["dates"].append(activity_date)
        bucket["any"].append(int(any_count or 0))
        bucket["attacker"].append(int(attacker_count or 0))
    return activity


def _load_first_activity_for_spells(conn, candidates, max_day, mode):
    """Return the first qualifying PvP date inside each supplied alliance spell.

    The lookup is deliberately candidate-driven and processed in small chunks.
    This lets PostgreSQL use the character/time indexes on the existing killmail
    tables instead of planning a broad scan across rawkm. Nothing is persisted.
    """
    max_end = datetime.combine(max_day + timedelta(days=1), time.min, tzinfo=UTC)
    normalized = []
    seen = set()
    for character_id, spell_start, spell_end in candidates:
        cid = int(character_id)
        bounded_end = max_end if spell_end.year >= 9999 or spell_end > max_end else spell_end
        key = (cid, spell_start)
        if key in seen or spell_start >= bounded_end:
            continue
        seen.add(key)
        normalized.append((cid, spell_start, bounded_end))
    if not normalized:
        return {}

    result = {}
    chunk_size = 500
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '45000ms'")
        for offset in range(0, len(normalized), chunk_size):
            chunk = normalized[offset:offset + chunk_size]
            ids = [row[0] for row in chunk]
            starts = [row[1] for row in chunk]
            ends = [row[2] for row in chunk]

            if mode == "attacker":
                first_sql = """
                    SELECT MIN(ka.killmail_time) AS first_at
                    FROM rawkm.killmail_attackers ka
                    WHERE ka.character_id = c.character_id
                      AND ka.killmail_time >= c.spell_start
                      AND ka.killmail_time < c.spell_end
                """
            elif mode == "loss":
                # Loss only means victim activity with NO attacker activity in
                # the same considered spell window. Merely having a loss is not
                # enough if the character also appears as an attacker.
                first_sql = """
                    SELECT MIN(km.killmail_time) AS first_at
                    FROM rawkm.killmails km
                    WHERE km.victim_character_id = c.character_id
                      AND km.killmail_time >= c.spell_start
                      AND km.killmail_time < c.spell_end
                      AND NOT EXISTS (
                          SELECT 1
                          FROM rawkm.killmail_attackers ka
                          WHERE ka.character_id = c.character_id
                            AND ka.killmail_time >= c.spell_start
                            AND ka.killmail_time < c.spell_end
                      )
                """
            else:
                first_sql = """
                    SELECT MIN(event_time) AS first_at
                    FROM (
                        SELECT MIN(km.killmail_time) AS event_time
                        FROM rawkm.killmails km
                        WHERE km.victim_character_id = c.character_id
                          AND km.killmail_time >= c.spell_start
                          AND km.killmail_time < c.spell_end

                        UNION ALL

                        SELECT MIN(ka.killmail_time) AS event_time
                        FROM rawkm.killmail_attackers ka
                        WHERE ka.character_id = c.character_id
                          AND ka.killmail_time >= c.spell_start
                          AND ka.killmail_time < c.spell_end
                    ) events
                """

            cur.execute(
                """
                WITH candidates AS (
                    SELECT *
                    FROM unnest(%s::bigint[], %s::timestamptz[], %s::timestamptz[])
                         AS t(character_id, spell_start, spell_end)
                )
                SELECT c.character_id, c.spell_start, first_hit.first_at
                FROM candidates c
                JOIN LATERAL (
                """ + first_sql + """
                ) first_hit ON first_hit.first_at IS NOT NULL
                """,
                (ids, starts, ends),
            )
            for character_id, spell_start, first_at in cur.fetchall():
                result[(int(character_id), spell_start)] = (
                    first_at.date() if isinstance(first_at, datetime) else first_at
                )

    return result


def _load_first_activity_for_anchor_candidates(conn, candidates, mode):
    """Return first qualifying PvP for anchor-bounded membership windows.

    candidates contains (candidate_key, character_id, spell_start, spell_end).
    spell_end is already capped at the analysed anchor, so Loss only can safely
    enforce victims-minus-attackers for each analysed date independently.
    """
    normalized = []
    for candidate_key, character_id, spell_start, spell_end in candidates:
        if spell_start is None or spell_end is None or spell_start >= spell_end:
            continue
        normalized.append((str(candidate_key), int(character_id), spell_start, spell_end))
    if not normalized:
        return {}

    result = {}
    chunk_size = 500
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '45000ms'")
        for offset in range(0, len(normalized), chunk_size):
            chunk = normalized[offset:offset + chunk_size]
            keys = [row[0] for row in chunk]
            ids = [row[1] for row in chunk]
            starts = [row[2] for row in chunk]
            ends = [row[3] for row in chunk]

            if mode == "attacker":
                predicate_sql = """
                    SELECT MIN(ka.killmail_time) AS first_at
                    FROM rawkm.killmail_attackers ka
                    WHERE ka.character_id = c.character_id
                      AND ka.killmail_time >= c.spell_start
                      AND ka.killmail_time < c.spell_end
                """
            elif mode == "loss":
                predicate_sql = """
                    SELECT MIN(km.killmail_time) AS first_at
                    FROM rawkm.killmails km
                    WHERE km.victim_character_id = c.character_id
                      AND km.killmail_time >= c.spell_start
                      AND km.killmail_time < c.spell_end
                      AND NOT EXISTS (
                          SELECT 1
                          FROM rawkm.killmail_attackers ka
                          WHERE ka.character_id = c.character_id
                            AND ka.killmail_time >= c.spell_start
                            AND ka.killmail_time < c.spell_end
                      )
                """
            else:
                predicate_sql = """
                    SELECT MIN(event_time) AS first_at
                    FROM (
                        SELECT MIN(km.killmail_time) AS event_time
                        FROM rawkm.killmails km
                        WHERE km.victim_character_id = c.character_id
                          AND km.killmail_time >= c.spell_start
                          AND km.killmail_time < c.spell_end
                        UNION ALL
                        SELECT MIN(ka.killmail_time) AS event_time
                        FROM rawkm.killmail_attackers ka
                        WHERE ka.character_id = c.character_id
                          AND ka.killmail_time >= c.spell_start
                          AND ka.killmail_time < c.spell_end
                    ) events
                """

            cur.execute(
                """
                WITH candidates AS (
                    SELECT *
                    FROM unnest(
                        %s::text[], %s::bigint[], %s::timestamptz[], %s::timestamptz[]
                    ) AS t(candidate_key, character_id, spell_start, spell_end)
                )
                SELECT c.candidate_key, first_hit.first_at
                FROM candidates c
                JOIN LATERAL (
                """ + predicate_sql + """
                ) first_hit ON first_hit.first_at IS NOT NULL
                """,
                (keys, ids, starts, ends),
            )
            for candidate_key, first_at in cur.fetchall():
                result[str(candidate_key)] = first_at.date() if isinstance(first_at, datetime) else first_at
    return result


def _spell_at(spells, when):
    # Usually a character has only a handful of spells; a linear walk is faster
    # than building another index and keeps the semantics obvious.
    for start_at, end_at in spells:
        if start_at <= when < end_at:
            return start_at, end_at
    return None


def _activity_counts_between(bucket, start_day, end_day):
    if not bucket or start_day > end_day:
        return 0, 0
    dates = bucket["dates"]
    left = bisect_left(dates, start_day)
    right = bisect_right(dates, end_day)
    return sum(bucket["any"][left:right]), sum(bucket["attacker"][left:right])


def _activity_between(bucket, start_day, end_day, mode="any"):
    any_count, attacker_count = _activity_counts_between(bucket, start_day, end_day)
    if mode == "attacker":
        return attacker_count
    if mode == "loss":
        # Strict Loss only = Any involvement - Attacker only over THIS exact
        # time window. One attacker appearance excludes the character even if
        # they also have one or more losses.
        return any_count if any_count > 0 and attacker_count == 0 else 0
    return any_count


def _first_activity_between(bucket, start_day, end_day, mode="any"):
    if not bucket or start_day > end_day:
        return None
    dates = bucket["dates"]
    left = bisect_left(dates, start_day)
    right = bisect_right(dates, end_day)
    if mode == "loss":
        # If the character attacked anywhere in the considered interval they
        # are not Loss only, so there is no qualifying first activity.
        if sum(bucket["attacker"][left:right]) > 0:
            return None
        values = bucket["any"]
    else:
        values = bucket["attacker"] if mode == "attacker" else bucket["any"]
    for idx in range(left, right):
        if values[idx] > 0:
            return dates[idx]
    return None


def _member_state(spells_by_character, anchor_day):
    anchor_ts = _at_end_of_day(anchor_day)
    result = {}
    for character_id, spells in spells_by_character.items():
        spell = _spell_at(spells, anchor_ts)
        if spell:
            result[character_id] = spell
    return result


def _rolling_month_activity_core(character_id, spell, bucket, anchor_day, core_months, mode):
    spell_start = spell[0].date()
    spell_end = spell[1].date() if spell[1].year < 9999 else None

    # Rolling months are anchored on the analysed day, not calendar months.
    # Example for 2026-09-13:
    #   month 1 -> 2026-08-13 .. 2026-09-13
    #   month 2 -> 2026-07-13 .. 2026-08-12
    #   month 3 -> 2026-06-13 .. 2026-07-12
    for back in range(core_months):
        period_start = _shift_months(anchor_day, -(back + 1))
        period_end = anchor_day if back == 0 else _shift_months(anchor_day, -back) - timedelta(days=1)
        start_day = max(period_start, spell_start)
        if spell_end is not None:
            period_end = min(period_end, spell_end)
        if start_day > period_end or _activity_between(bucket, start_day, period_end, mode) <= 0:
            return False
    return True


def _top10_concentration(counts):
    counts = [int(value) for value in counts if value and value > 0]
    if not counts:
        return None
    counts.sort(reverse=True)
    top_n = max(1, ceil(len(counts) * 0.10))
    total = sum(counts)
    return _safe_pct(sum(counts[:top_n]), total)


def _compute_snapshot(
    anchor_day,
    window_days,
    mode,
    core_months,
    spells_by_character,
    all_spells,
    activity,
    birthdays,
    official_rows,
    official_dates,
    include_pilot_sets=False,
):
    window_start = anchor_day - timedelta(days=window_days - 1)
    previous_anchor = anchor_day - timedelta(days=window_days)
    previous_start = previous_anchor - timedelta(days=window_days - 1)

    current_members = _member_state(spells_by_character, anchor_day)
    previous_members = _member_state(spells_by_character, previous_anchor)

    current_any = set()
    current_attacker = set()
    selected_counts = {}

    for character_id, spell in current_members.items():
        bucket = activity.get(character_id)
        start_day = max(window_start, spell[0].date())
        any_count = _activity_between(bucket, start_day, anchor_day, "any")
        attacker_count = _activity_between(bucket, start_day, anchor_day, "attacker")
        if any_count > 0:
            current_any.add(character_id)
        if attacker_count > 0:
            current_attacker.add(character_id)
        if mode == "attacker":
            selected_count = attacker_count
        elif mode == "loss":
            selected_count = any_count if any_count > 0 and attacker_count == 0 else 0
        else:
            selected_count = any_count
        if selected_count > 0:
            selected_counts[character_id] = selected_count

    if mode == "attacker":
        selected_active = current_attacker
    elif mode == "loss":
        selected_active = current_any - current_attacker
    else:
        selected_active = current_any
    victim_only = current_any - current_attacker

    # Retention/churn compare two consecutive non-overlapping activity windows.
    # A character is retained only when both observations belong to the same
    # continuous alliance-membership spell; leave/rejoin is never continuity.
    current_active_keys = {
        (character_id, current_members[character_id][0])
        for character_id in selected_active
    }
    previous_active_keys = set()
    for character_id, spell in previous_members.items():
        bucket = activity.get(character_id)
        start_day = max(previous_start, spell[0].date())
        if _activity_between(bucket, start_day, previous_anchor, mode) > 0:
            previous_active_keys.add((character_id, spell[0]))

    retained_keys = current_active_keys & previous_active_keys
    churned_keys = previous_active_keys - retained_keys
    newly_active_keys = current_active_keys - previous_active_keys

    core = set()
    if core_months > 0:
        for character_id in selected_active:
            spell = current_members[character_id]
            if _rolling_month_activity_core(
                character_id,
                spell,
                activity.get(character_id),
                anchor_day,
                core_months,
                mode,
            ):
                core.add(character_id)

    character_ages = []
    tenures = []
    for character_id in selected_active:
        birthday = birthdays.get(character_id)
        if birthday:
            character_ages.append(max(0.0, (anchor_day - birthday.date()).days / 365.2425))
        spell = current_members.get(character_id)
        if spell:
            tenures.append(max(0, (anchor_day - spell[0].date()).days))

    # PvP movement metrics intentionally use only identifiable PvP characters.
    # They never claim to describe every member of the alliance. A movement is
    # counted only when that membership spell starts/ends inside the selected
    # period AND the character has qualifying PvP activity while in that spell
    # during the same selected period.
    pvp_arrival_ids = set()
    pvp_departure_ids = set()
    window_start_ts = datetime.combine(window_start, time.min, tzinfo=UTC)
    window_end_ts = datetime.combine(anchor_day + timedelta(days=1), time.min, tzinfo=UTC)

    for character_id, spell_start, spell_end in all_spells:
        bucket = activity.get(character_id)
        if not bucket:
            continue

        finite_end = spell_end.year < 9999
        last_spell_day = (spell_end - timedelta(microseconds=1)).date() if finite_end else anchor_day
        overlap_start = max(window_start, spell_start.date())
        overlap_end = min(anchor_day, last_spell_day)
        has_selected_pvp = (
            overlap_start <= overlap_end
            and _activity_between(bucket, overlap_start, overlap_end, mode) > 0
        )
        if not has_selected_pvp:
            continue

        character_id = int(character_id)
        if window_start_ts <= spell_start < window_end_ts:
            pvp_arrival_ids.add(character_id)
        if finite_end and window_start_ts <= spell_end < window_end_ts:
            pvp_departure_ids.add(character_id)

    official = _official_at_or_before(official_rows, official_dates, anchor_day)
    official_members = official["member_count"] if official else None

    active_count = len(selected_active)
    previous_active_count = len(previous_active_keys)
    turnover_count = len(pvp_arrival_ids) + len(pvp_departure_ids)

    result = {
        "date": anchor_day.isoformat(),
        "activity_window_days": window_days,
        "activity_mode": mode,
        "core_months": core_months,
        "member_count": official["member_count"] if official else None,
        "alliance_count": official.get("alliance_count") if official else None,
        "corporation_count": official["corporation_count"] if official else None,
        "sovereignty_count": official["sovereignty_count"] if official else None,
        "active_count": active_count,
        "active_rate_pct": _safe_pct(active_count, official_members),
        "victim_only_count": len(victim_only),
        "victim_only_rate_pct": _safe_pct(len(victim_only), official_members),
        "retained_pvp_count": len(retained_keys),
        "retention_pct": _safe_pct(len(retained_keys), previous_active_count),
        "churn_count": len(churned_keys),
        "churn_pct": _safe_pct(len(churned_keys), previous_active_count),
        "newly_active_pvp": len(newly_active_keys),
        "core_pvp_count": len(core),
        "core_pvp_rate_pct": _safe_pct(len(core), active_count),
        "median_character_age_years": median(character_ages) if character_ages else None,
        "median_alliance_tenure_days": median(tenures) if tenures else None,
        "pvp_arrivals": len(pvp_arrival_ids),
        "pvp_departures": len(pvp_departure_ids),
        "pvp_turnover_count": turnover_count,
        "pvp_turnover_rate_pct": _safe_pct(turnover_count, active_count),
        "median_activation_delay_days": None,
        "top10_activity_concentration_pct": _top10_concentration(selected_counts.values()),
    }

    serialized = {key: _serialize_number(value) for key, value in result.items()}
    if not include_pilot_sets:
        return serialized

    pilot_sets = {
        "active_count": set(selected_active),
        "victim_only_count": set(victim_only),
        "retained_pvp_count": {character_id for character_id, _spell_start in retained_keys},
        "churn_count": {character_id for character_id, _spell_start in churned_keys},
        "newly_active_pvp": {character_id for character_id, _spell_start in newly_active_keys},
        "pvp_arrivals": set(pvp_arrival_ids),
        "pvp_departures": set(pvp_departure_ids),
        "core_pvp_count": set(core),
    }
    return serialized, pilot_sets


def _normalize_activity_request(dates, activity_window_days, activity_mode, core_months):
    normalized_dates = sorted({_coerce_date(value) for value in dates})
    if not normalized_dates:
        raise ValueError("dates_required")
    if len(normalized_dates) > MAX_SNAPSHOT_DATES:
        raise ValueError(f"too_many_dates_max_{MAX_SNAPSHOT_DATES}")

    window_days = int(activity_window_days or DEFAULT_ACTIVITY_WINDOW_DAYS)
    if window_days < 1 or window_days > 730:
        raise ValueError("activity_window_days_must_be_1_to_730")

    mode = str(activity_mode or "any").strip().lower()
    if mode not in {"any", "attacker", "loss"}:
        raise ValueError("activity_mode_must_be_any_attacker_or_loss")

    months = int(core_months or DEFAULT_CORE_MONTHS)
    if months < 1 or months > 24:
        raise ValueError("core_months_must_be_1_to_24")

    return normalized_dates, window_days, mode, months


def _candidate_ids_for_range(spells_by_character, start_day, end_day):
    result = set()
    start_ts = datetime.combine(start_day, time.min, tzinfo=UTC)
    end_ts = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=UTC)
    for character_id, spells in spells_by_character.items():
        for spell_start, spell_end in spells:
            if spell_start < end_ts and (spell_end.year >= 9999 or spell_end > start_ts):
                result.add(character_id)
                break
    return result


def _calculate_intelligence(
    conn,
    alliance_id,
    normalized_dates,
    window_days,
    mode,
    core_months,
    scope="full",
    cache_pilot_sets=False,
):
    official_rows, official_dates = _load_official_rows(conn, alliance_id)
    max_day = max(normalized_dates)

    required = [
        ("entities", "character_corporation_history"),
        ("entities", "corporation_alliance_history"),
    ]
    if scope != "membership":
        required.extend([("rawkm", "killmails"), ("rawkm", "killmail_attackers")])
    missing = [f"{schema}.{table}" for schema, table in required if not _table_exists(conn, schema, table)]
    if missing:
        raise RuntimeError("missing_tables:" + ",".join(missing))

    spells_by_character, all_spells = _load_alliance_membership_spells(conn, alliance_id, max_day)

    activity = {}
    birthdays = {}
    needs_daily_activity = scope in {
        "activity", "retention", "movement", "core", "full"
    }
    if needs_daily_activity:
        min_anchor = min(normalized_dates)
        if scope in {"activity", "movement"}:
            event_start = min_anchor - timedelta(days=window_days - 1)
        elif scope == "retention":
            event_start = min_anchor - timedelta(days=(window_days * 2) - 1)
        elif scope == "core":
            event_start = _shift_months(min_anchor, -core_months)
        else:
            retention_start = min_anchor - timedelta(days=(window_days * 2) - 1)
            core_start = _shift_months(min_anchor, -core_months)
            event_start = min(retention_start, core_start)

        candidate_ids = _candidate_ids_for_range(spells_by_character, event_start, max_day)
        activity = _load_member_activity_days(conn, candidate_ids, event_start, max_day)

        if scope in {"activity", "full"}:
            birthdays = _load_birthdays(conn, candidate_ids)

    snapshots = []
    for anchor_day in normalized_dates:
        if cache_pilot_sets:
            snapshot, pilot_sets = _compute_snapshot(
                anchor_day,
                window_days,
                mode,
                core_months,
                spells_by_character,
                all_spells,
                activity,
                birthdays,
                official_rows,
                official_dates,
                include_pilot_sets=True,
            )
            cache_metric_keys = set(PILOT_LIST_METRICS) if scope == "full" else set(INDICATOR_GROUPS.get(scope, set()))
            for metric, pilot_ids in pilot_sets.items():
                if metric in PILOT_LIST_METRICS and metric in cache_metric_keys:
                    _pilot_set_cache_put(
                        _pilot_set_cache_key(
                            alliance_id, metric, anchor_day, window_days, mode, core_months
                        ),
                        pilot_ids,
                    )
        else:
            snapshot = _compute_snapshot(
                anchor_day,
                window_days,
                mode,
                core_months,
                spells_by_character,
                all_spells,
                activity,
                birthdays,
                official_rows,
                official_dates,
            )
        snapshots.append(snapshot)

    # First-PvP metrics use the true first qualifying PvP event after the
    # start of the current alliance spell. Candidates are bounded per analysed
    # date so Loss only is evaluated on that date's exact interval; a later
    # attacker appearance must not rewrite an older historical result.
    if scope in {"first_pvp", "full"}:
        candidates = []
        current_by_date = {}
        candidate_keys = {}
        for anchor_day in normalized_dates:
            window_start = anchor_day - timedelta(days=window_days - 1)
            anchor_end = datetime.combine(anchor_day + timedelta(days=1), time.min, tzinfo=UTC)
            current_members = _member_state(spells_by_character, anchor_day)
            current_by_date[anchor_day] = current_members
            for character_id, spell in current_members.items():
                if not (window_start <= spell[0].date() <= anchor_day):
                    continue
                bounded_end = anchor_end if spell[1].year >= 9999 or spell[1] > anchor_end else spell[1]
                candidate_key = f"{anchor_day.isoformat()}:{int(character_id)}:{spell[0].isoformat()}"
                candidate_keys[(anchor_day, int(character_id), spell[0])] = candidate_key
                candidates.append((candidate_key, character_id, spell[0], bounded_end))

        first_dates = _load_first_activity_for_anchor_candidates(conn, candidates, mode)
        by_date = {row["date"]: row for row in snapshots}
        for anchor_day in normalized_dates:
            window_start = anchor_day - timedelta(days=window_days - 1)
            current_members = current_by_date[anchor_day]
            delays = []
            for character_id, spell in current_members.items():
                if not (window_start <= spell[0].date() <= anchor_day):
                    continue
                candidate_key = candidate_keys.get((anchor_day, int(character_id), spell[0]))
                first_day = first_dates.get(candidate_key) if candidate_key else None
                if first_day is None or first_day > anchor_day or first_day < window_start:
                    continue
                delays.append(max(0, (first_day - spell[0].date()).days))

            target = by_date[anchor_day.isoformat()]
            target["median_activation_delay_days"] = _serialize_number(
                median(delays) if delays else None
            )

    return snapshots



def _pilot_set_cache_key(alliance_id, metric, anchor_day, window_days, mode, core_months):
    return (
        int(alliance_id), str(metric), anchor_day.isoformat(), int(window_days), str(mode), int(core_months)
    )


def _pilot_set_cache_get(key):
    now = monotonic()
    with _PILOT_SET_CACHE_LOCK:
        cached = _PILOT_SET_CACHE.get(key)
        if not cached:
            return None
        created_at, pilot_ids = cached
        if now - created_at > _PILOT_SET_CACHE_TTL_SECONDS:
            _PILOT_SET_CACHE.pop(key, None)
            return None
        _PILOT_SET_CACHE.move_to_end(key)
        return set(pilot_ids)


def _pilot_set_cache_put(key, pilot_ids):
    with _PILOT_SET_CACHE_LOCK:
        _PILOT_SET_CACHE[key] = (monotonic(), tuple(sorted(int(value) for value in pilot_ids)))
        _PILOT_SET_CACHE.move_to_end(key)
        while len(_PILOT_SET_CACHE) > _PILOT_SET_CACHE_MAX:
            _PILOT_SET_CACHE.popitem(last=False)


def _pilot_metric_scope(metric):
    for group, metric_keys in INDICATOR_GROUPS.items():
        if metric in metric_keys:
            return group
    return "activity"


def _load_indicator_pilot_ids(conn, alliance_id, metric, anchor_day, window_days, mode, core_months):
    cache_key = _pilot_set_cache_key(
        alliance_id, metric, anchor_day, window_days, mode, core_months
    )
    cached = _pilot_set_cache_get(cache_key)
    if cached is not None:
        return cached

    official_rows, official_dates = _load_official_rows(conn, alliance_id)
    spells_by_character, all_spells = _load_alliance_membership_spells(conn, alliance_id, anchor_day)
    scope = _pilot_metric_scope(metric)

    if scope in {"activity", "movement"}:
        event_start = anchor_day - timedelta(days=window_days - 1)
    elif scope == "retention":
        event_start = anchor_day - timedelta(days=(window_days * 2) - 1)
    elif scope == "core":
        event_start = _shift_months(anchor_day, -core_months)
    else:
        event_start = anchor_day - timedelta(days=window_days - 1)

    candidate_ids = _candidate_ids_for_range(spells_by_character, event_start, anchor_day)
    activity = _load_member_activity_days(conn, candidate_ids, event_start, anchor_day)
    snapshot, pilot_sets = _compute_snapshot(
        anchor_day,
        window_days,
        mode,
        core_months,
        spells_by_character,
        all_spells,
        activity,
        {},
        official_rows,
        official_dates,
        include_pilot_sets=True,
    )
    pilot_ids = set(pilot_sets.get(metric) or set())

    expected = snapshot.get(metric)
    if expected is not None and int(expected) != len(pilot_ids):
        raise RuntimeError(
            f"population_indicator_pilot_count_mismatch:{metric}:{int(expected)}:{len(pilot_ids)}"
        )

    _pilot_set_cache_put(cache_key, pilot_ids)
    return pilot_ids


def _pilot_affiliation_base_sql():
    return """
        WITH selected AS (
            SELECT unnest(%s::bigint[]) AS character_id
        ), resolved AS (
            SELECT
                s.character_id,
                COALESCE(NULLIF(ch.name, ''), 'Unknown') AS character_name,
                aff.corporation_id,
                COALESCE(NULLIF(corp.name, ''), 'Unknown') AS corporation_name,
                corp.ticker AS corporation_ticker,
                corp_alliance.alliance_id,
                COALESCE(NULLIF(alli.name, ''), 'Unknown') AS alliance_name,
                alli.ticker AS alliance_ticker
            FROM selected s
            LEFT JOIN entities.characters ch
              ON ch.character_id = s.character_id
            LEFT JOIN LATERAL (
                SELECT cch.corporation_id
                FROM entities.character_corporation_history cch
                WHERE cch.character_id = s.character_id
                  AND COALESCE(cch.is_deleted, FALSE) = FALSE
                  AND cch.start_date <= %s
                  AND (cch.end_date IS NULL OR %s < cch.end_date)
                ORDER BY cch.start_date DESC
                LIMIT 1
            ) aff ON TRUE
            LEFT JOIN entities.corporations corp
              ON corp.corporation_id = aff.corporation_id
            LEFT JOIN LATERAL (
                SELECT cah.alliance_id
                FROM entities.corporation_alliance_history cah
                WHERE cah.corporation_id = aff.corporation_id
                  AND COALESCE(cah.is_deleted, FALSE) = FALSE
                  AND cah.start_date <= %s
                  AND (cah.end_date IS NULL OR %s < cah.end_date)
                ORDER BY cah.start_date DESC
                LIMIT 1
            ) corp_alliance ON TRUE
            LEFT JOIN entities.alliances alli
              ON alli.alliance_id = corp_alliance.alliance_id
        )
    """


def _pilot_corporation_summary_rows(conn, pilot_ids, alliance_id, anchor_day, query, sort_key, direction):
    ids = sorted({int(value) for value in pilot_ids if value is not None})
    if not ids:
        return [], 0

    anchor_ts = _at_end_of_day(anchor_day)
    base_sql = _pilot_affiliation_base_sql()

    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '45000ms'")
        cur.execute(
            base_sql + """
            SELECT
                CASE WHEN alliance_id = %s AND corporation_id IS NOT NULL THEN corporation_id ELSE NULL END AS grouped_corporation_id,
                CASE WHEN alliance_id = %s AND corporation_id IS NOT NULL THEN corporation_name ELSE NULL END AS grouped_corporation_name,
                CASE WHEN alliance_id = %s AND corporation_id IS NOT NULL THEN corporation_ticker ELSE NULL END AS grouped_corporation_ticker,
                CASE WHEN alliance_id = %s AND corporation_id IS NOT NULL THEN TRUE ELSE FALSE END AS in_alliance,
                COUNT(*)::bigint AS pilot_count
            FROM resolved
            GROUP BY 1, 2, 3, 4
            """,
            (
                ids,
                anchor_ts,
                anchor_ts,
                anchor_ts,
                anchor_ts,
                int(alliance_id),
                int(alliance_id),
                int(alliance_id),
                int(alliance_id),
            ),
        )
        rows = cur.fetchall()

    corporations = []
    outside_count = 0
    for corporation_id, corporation_name, corporation_ticker, in_alliance, pilot_count in rows:
        count = int(pilot_count or 0)
        if not in_alliance or corporation_id is None:
            outside_count += count
            continue
        corporations.append({
            "corporation_id": int(corporation_id),
            "name": corporation_name or "Unknown",
            "ticker": corporation_ticker,
            "pilot_count": count,
        })

    query = str(query or "").strip().lower()
    if query:
        corporations = [
            row for row in corporations
            if query in (row.get("name") or "").lower()
            or query in (row.get("ticker") or "").lower()
        ]

    direction = "desc" if str(direction or "").lower() == "desc" else "asc"
    sort_key = sort_key if sort_key in {"corporation", "pilots"} else "pilots"
    reverse = direction == "desc"
    if sort_key == "corporation":
        corporations.sort(
            key=lambda row: ((row.get("name") or "").lower(), int(row.get("corporation_id") or 0)),
            reverse=reverse,
        )
    else:
        corporations.sort(
            key=lambda row: (int(row.get("pilot_count") or 0), (row.get("name") or "").lower()),
            reverse=reverse,
        )

    return corporations, outside_count


def _pilot_affiliation_rows(
    conn,
    pilot_ids,
    anchor_day,
    query,
    sort_key,
    direction,
    page,
    per_page,
    target_alliance_id=None,
    corporation_id=None,
    outside_alliance=False,
):
    ids = sorted({int(value) for value in pilot_ids if value is not None})
    if not ids:
        return [], 0, 1

    query = str(query or "").strip()
    direction = "desc" if str(direction or "").lower() == "desc" else "asc"
    order_map = {
        "name": "lower(COALESCE(character_name, ''))",
        "corporation": "lower(COALESCE(corporation_name, ''))",
        "alliance": "lower(COALESCE(alliance_name, ''))",
    }
    sort_key = sort_key if sort_key in order_map else "name"
    order_sql = order_map[sort_key]
    reverse_sql = "DESC" if direction == "desc" else "ASC"
    anchor_ts = _at_end_of_day(anchor_day)
    needle = f"%{query}%"
    base_sql = _pilot_affiliation_base_sql()

    scope_sql = ""
    scope_params = []
    if corporation_id is not None:
        if target_alliance_id is None:
            raise ValueError("pilot_list_target_alliance_required")
        scope_sql = " AND corporation_id = %s AND alliance_id = %s"
        scope_params.extend([int(corporation_id), int(target_alliance_id)])
    elif outside_alliance:
        if target_alliance_id is None:
            raise ValueError("pilot_list_target_alliance_required")
        scope_sql = " AND alliance_id IS DISTINCT FROM %s"
        scope_params.append(int(target_alliance_id))

    common_params = [ids, anchor_ts, anchor_ts, anchor_ts, anchor_ts, query, needle, *scope_params]

    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '45000ms'")
        cur.execute(
            base_sql + f"""
            SELECT COUNT(*)
            FROM resolved
            WHERE (%s = '' OR character_name ILIKE %s)
            {scope_sql}
            """,
            common_params,
        )
        total_rows = int(cur.fetchone()[0] or 0)
        total_pages = max(1, ceil(total_rows / per_page)) if total_rows else 1
        effective_page = min(page, total_pages)
        offset = (effective_page - 1) * per_page

        cur.execute(
            base_sql + f"""
            SELECT
                character_id,
                character_name,
                corporation_id,
                corporation_name,
                corporation_ticker,
                alliance_id,
                alliance_name,
                alliance_ticker
            FROM resolved
            WHERE (%s = '' OR character_name ILIKE %s)
            {scope_sql}
            ORDER BY {order_sql} {reverse_sql} NULLS LAST, lower(character_name) ASC, character_id ASC
            LIMIT %s OFFSET %s
            """,
            [*common_params, per_page, offset],
        )
        rows = cur.fetchall()

    result = []
    for row in rows:
        result.append({
            "character_id": int(row[0]),
            "name": row[1] or "Unknown",
            "corporation": ({
                "corporation_id": int(row[2]),
                "name": row[3] or "Unknown",
                "ticker": row[4],
            } if row[2] is not None else None),
            "alliance": ({
                "alliance_id": int(row[5]),
                "name": row[6] or "Unknown",
                "ticker": row[7],
            } if row[5] is not None else None),
        })
    return result, total_rows, effective_page


def get_alliance_population_indicator_pilots(
    alliance_id,
    metric,
    anchor_date,
    activity_window_days=DEFAULT_ACTIVITY_WINDOW_DAYS,
    activity_mode="any",
    core_months=DEFAULT_CORE_MONTHS,
    page=1,
    per_page=50,
    query="",
    sort="",
    direction="",
    view="corporations",
):
    alliance_id = int(alliance_id)
    metric = str(metric or "").strip()
    if metric not in PILOT_LIST_METRICS:
        raise ValueError("pilot_list_metric_invalid")

    normalized_dates, window_days, mode, months = _normalize_activity_request(
        [anchor_date], activity_window_days, activity_mode, core_months
    )
    anchor_day = normalized_dates[0]
    page = max(1, int(page or 1))
    per_page = max(1, min(50, int(per_page or 50)))
    query = str(query or "").strip()[:120]
    view = str(view or "corporations").strip().lower()
    if view not in {"corporations", "pilots"}:
        view = "corporations"

    with db() as conn:
        pilot_ids = _load_indicator_pilot_ids(
            conn, alliance_id, metric, anchor_day, window_days, mode, months
        )

        if view == "corporations":
            sort = str(sort or "pilots").strip().lower()
            if sort not in {"corporation", "pilots"}:
                sort = "pilots"
            direction = str(direction or ("desc" if sort == "pilots" else "asc")).strip().lower()
            direction = "desc" if direction == "desc" else "asc"
            corporations, outside_count = _pilot_corporation_summary_rows(
                conn,
                pilot_ids,
                alliance_id,
                anchor_day,
                query,
                sort,
                direction,
            )
            pilots = []
            total_rows = len(pilot_ids)
            page = 1
        else:
            sort = str(sort or "name").strip().lower()
            if sort not in {"name", "corporation", "alliance"}:
                sort = "name"
            direction = "desc" if str(direction or "asc").strip().lower() == "desc" else "asc"
            pilots, total_rows, page = _pilot_affiliation_rows(
                conn,
                pilot_ids,
                anchor_day,
                query,
                sort,
                direction,
                page,
                per_page,
            )
            corporations = []
            outside_count = 0

    total_pages = max(1, ceil(total_rows / per_page)) if total_rows else 1

    return {
        "entity_type": "alliance",
        "entity_id": alliance_id,
        "alliance_id": alliance_id,
        "metric": metric,
        "metric_label": PILOT_LIST_METRICS[metric],
        "date": anchor_day.isoformat(),
        "activity_window_days": window_days,
        "activity_mode": mode,
        "core_months": months,
        "view": view,
        "query": query,
        "sort": sort,
        "direction": direction,
        "total_pilots": len(pilot_ids),
        "corporations": corporations,
        "outside_count": outside_count,
        "pilots": pilots,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total_rows": total_rows,
            "total_pages": total_pages,
            "has_prev": view == "pilots" and page > 1,
            "has_next": view == "pilots" and page < total_pages,
            "prev_page": page - 1 if view == "pilots" and page > 1 else None,
            "next_page": page + 1 if view == "pilots" and page < total_pages else None,
        },
    }


def get_alliance_population_indicator_pilot_subset(
    alliance_id,
    metric,
    anchor_date,
    activity_window_days=DEFAULT_ACTIVITY_WINDOW_DAYS,
    activity_mode="any",
    core_months=DEFAULT_CORE_MONTHS,
    corporation_id=None,
    outside_alliance=False,
    page=1,
    per_page=50,
):
    alliance_id = int(alliance_id)
    metric = str(metric or "").strip()
    if metric not in PILOT_LIST_METRICS:
        raise ValueError("pilot_list_metric_invalid")
    if corporation_id is None and not outside_alliance:
        raise ValueError("pilot_list_subset_missing")
    if corporation_id is not None and outside_alliance:
        raise ValueError("pilot_list_subset_ambiguous")

    normalized_dates, window_days, mode, months = _normalize_activity_request(
        [anchor_date], activity_window_days, activity_mode, core_months
    )
    anchor_day = normalized_dates[0]
    page = max(1, int(page or 1))
    per_page = max(1, min(50, int(per_page or 50)))

    with db() as conn:
        pilot_ids = _load_indicator_pilot_ids(
            conn, alliance_id, metric, anchor_day, window_days, mode, months
        )
        pilots, total_rows, page = _pilot_affiliation_rows(
            conn,
            pilot_ids,
            anchor_day,
            "",
            "name",
            "asc",
            page,
            per_page,
            target_alliance_id=alliance_id,
            corporation_id=corporation_id,
            outside_alliance=outside_alliance,
        )

    total_pages = max(1, ceil(total_rows / per_page)) if total_rows else 1
    return {
        "alliance_id": alliance_id,
        "metric": metric,
        "date": anchor_day.isoformat(),
        "activity_window_days": window_days,
        "activity_mode": mode,
        "core_months": months,
        "corporation_id": int(corporation_id) if corporation_id is not None else None,
        "outside_alliance": bool(outside_alliance),
        "pilots": pilots,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total_rows": total_rows,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
            "prev_page": page - 1 if page > 1 else None,
            "next_page": page + 1 if page < total_pages else None,
        },
    }


def get_alliance_population_intelligence_group(
    alliance_id,
    group,
    dates,
    activity_window_days=DEFAULT_ACTIVITY_WINDOW_DAYS,
    activity_mode="any",
    core_months=DEFAULT_CORE_MONTHS,
):
    alliance_id = int(alliance_id)
    group = str(group or "").strip().lower()
    if group not in INDICATOR_GROUPS:
        raise ValueError("indicator_group_invalid")

    normalized_dates, window_days, mode, months = _normalize_activity_request(
        dates, activity_window_days, activity_mode, core_months
    )

    with db() as conn:
        snapshots = _calculate_intelligence(
            conn, alliance_id, normalized_dates, window_days, mode, months,
            scope=group, cache_pilot_sets=True
        )

    metric_keys = INDICATOR_GROUPS[group]
    rows = []
    for snapshot in snapshots:
        row = {"date": snapshot["date"]}
        for key in metric_keys:
            row[key] = snapshot.get(key)
        rows.append(row)

    return {
        "alliance_id": alliance_id,
        "group": group,
        "activity_window_days": window_days,
        "activity_mode": mode,
        "core_months": months,
        "rows": rows,
    }


def get_alliance_population_intelligence(
    alliance_id,
    dates,
    activity_window_days=DEFAULT_ACTIVITY_WINDOW_DAYS,
    activity_mode="any",
    core_months=DEFAULT_CORE_MONTHS,
):
    alliance_id = int(alliance_id)
    normalized_dates, window_days, mode, months = _normalize_activity_request(
        dates, activity_window_days, activity_mode, core_months
    )

    with db() as conn:
        snapshots = _calculate_intelligence(
            conn, alliance_id, normalized_dates, window_days, mode, months,
            cache_pilot_sets=True
        )

    return {
        "alliance_id": alliance_id,
        "activity_window_days": window_days,
        "activity_mode": mode,
        "core_months": months,
        "rows": snapshots,
        "definitions": {
            "membership": "A character is counted only while they belong to this alliance at that date.",
            "activity": "PvP activity before joining or after leaving the alliance is not counted.",
            "total_population": "Total population is the alliance population stored for that date.",
        },
    }


def _series_dates(start_day, end_day, max_points=None):
    if start_day > end_day:
        raise ValueError("date_from_after_date_to")
    # Never sample Population Intelligence history. Long requests are chunked
    # by the caller/API, but every calendar day is preserved.
    span_days = (end_day - start_day).days
    return [start_day + timedelta(days=offset) for offset in range(span_days + 1)], 1


def get_alliance_population_intelligence_series(
    alliance_id,
    metric,
    date_from,
    date_to,
    activity_window_days=DEFAULT_ACTIVITY_WINDOW_DAYS,
    activity_mode="any",
    core_months=DEFAULT_CORE_MONTHS,
    max_points=None,
):
    alliance_id = int(alliance_id)
    metric = str(metric or "").strip()
    if metric not in ALL_METRICS:
        raise ValueError("metric_invalid")

    start_day = _coerce_date(date_from)
    end_day = _coerce_date(date_to)

    # Official metrics are cheap and should stay daily; they do not need the PvP
    # history engine at all.
    if metric in OFFICIAL_METRICS:
        with db() as conn:
            official_rows, _official_dates = _load_official_rows(conn, alliance_id)
        rows = [
            {"date": row["date"].isoformat(), "value": row[metric]}
            for row in official_rows
            if start_day <= row["date"] <= end_day
        ]
        return {
            "alliance_id": alliance_id,
            "metric": metric,
            "activity_window_days": None,
            "activity_mode": None,
            "core_months": None,
            "step_days": 1,
            "rows": rows,
        }

    normalized_dates, window_days, mode, months = _normalize_activity_request(
        [start_day], activity_window_days, activity_mode, core_months
    )
    del normalized_dates
    anchors, step_days = _series_dates(start_day, end_day, max_points)

    # Compute only the metric's indicator group. This keeps evolution requests
    # bounded to the history they actually need instead of loading every metric.
    metric_scope = next(
        (group for group, metric_keys in INDICATOR_GROUPS.items() if metric in metric_keys),
        "activity",
    )
    with db() as conn:
        snapshots = _calculate_intelligence(
            conn, alliance_id, anchors, window_days, mode, months, scope=metric_scope
        )

    rows = [{"date": row["date"], "value": row.get(metric)} for row in snapshots]
    return {
        "alliance_id": alliance_id,
        "metric": metric,
        "activity_window_days": window_days,
        "activity_mode": mode,
        "core_months": months,
        "step_days": step_days,
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# Corporation Population Intelligence
# ---------------------------------------------------------------------------


def _calculate_corporation_intelligence(
    conn,
    corporation_id,
    normalized_dates,
    window_days,
    mode,
    core_months,
    scope="full",
    cache_pilot_sets=False,
):
    corporation_id = int(corporation_id)
    official_rows, official_dates = _load_corporation_official_rows(conn, corporation_id)
    max_day = max(normalized_dates)

    required = [("entities", "character_corporation_history")]
    if scope != "membership":
        required.extend([("rawkm", "killmails"), ("rawkm", "killmail_attackers")])
    missing = [f"{schema}.{table}" for schema, table in required if not _table_exists(conn, schema, table)]
    if missing:
        raise RuntimeError("missing_tables:" + ",".join(missing))

    spells_by_character, all_spells = _load_corporation_membership_spells(conn, corporation_id, max_day)

    activity = {}
    birthdays = {}
    needs_daily_activity = scope in {"activity", "retention", "movement", "core", "full"}
    if needs_daily_activity:
        min_anchor = min(normalized_dates)
        if scope in {"activity", "movement"}:
            event_start = min_anchor - timedelta(days=window_days - 1)
        elif scope == "retention":
            event_start = min_anchor - timedelta(days=(window_days * 2) - 1)
        elif scope == "core":
            event_start = _shift_months(min_anchor, -core_months)
        else:
            retention_start = min_anchor - timedelta(days=(window_days * 2) - 1)
            core_start = _shift_months(min_anchor, -core_months)
            event_start = min(retention_start, core_start)

        candidate_ids = _candidate_ids_for_range(spells_by_character, event_start, max_day)
        activity = _load_member_activity_days(conn, candidate_ids, event_start, max_day)
        if scope in {"activity", "full"}:
            birthdays = _load_birthdays(conn, candidate_ids)

    snapshots = []
    for anchor_day in normalized_dates:
        if cache_pilot_sets:
            snapshot, pilot_sets = _compute_snapshot(
                anchor_day,
                window_days,
                mode,
                core_months,
                spells_by_character,
                all_spells,
                activity,
                birthdays,
                official_rows,
                official_dates,
                include_pilot_sets=True,
            )
            cache_metric_keys = set(PILOT_LIST_METRICS) if scope == "full" else set(INDICATOR_GROUPS.get(scope, set()))
            for metric, pilot_ids in pilot_sets.items():
                if metric in PILOT_LIST_METRICS and metric in cache_metric_keys:
                    # Corporation ids use a negative cache namespace so they can
                    # never collide with alliance ids.
                    _pilot_set_cache_put(
                        _pilot_set_cache_key(
                            -corporation_id, metric, anchor_day, window_days, mode, core_months
                        ),
                        pilot_ids,
                    )
        else:
            snapshot = _compute_snapshot(
                anchor_day,
                window_days,
                mode,
                core_months,
                spells_by_character,
                all_spells,
                activity,
                birthdays,
                official_rows,
                official_dates,
            )
        snapshots.append(snapshot)

    if scope in {"first_pvp", "full"}:
        candidates = []
        current_by_date = {}
        candidate_keys = {}
        for anchor_day in normalized_dates:
            window_start = anchor_day - timedelta(days=window_days - 1)
            anchor_end = datetime.combine(anchor_day + timedelta(days=1), time.min, tzinfo=UTC)
            current_members = _member_state(spells_by_character, anchor_day)
            current_by_date[anchor_day] = current_members
            for character_id, spell in current_members.items():
                if not (window_start <= spell[0].date() <= anchor_day):
                    continue
                bounded_end = anchor_end if spell[1].year >= 9999 or spell[1] > anchor_end else spell[1]
                candidate_key = f"corp:{corporation_id}:{anchor_day.isoformat()}:{int(character_id)}:{spell[0].isoformat()}"
                candidate_keys[(anchor_day, int(character_id), spell[0])] = candidate_key
                candidates.append((candidate_key, character_id, spell[0], bounded_end))

        first_dates = _load_first_activity_for_anchor_candidates(conn, candidates, mode)
        by_date = {row["date"]: row for row in snapshots}
        for anchor_day in normalized_dates:
            window_start = anchor_day - timedelta(days=window_days - 1)
            current_members = current_by_date[anchor_day]
            delays = []
            for character_id, spell in current_members.items():
                if not (window_start <= spell[0].date() <= anchor_day):
                    continue
                candidate_key = candidate_keys.get((anchor_day, int(character_id), spell[0]))
                first_day = first_dates.get(candidate_key) if candidate_key else None
                if first_day is None or first_day > anchor_day or first_day < window_start:
                    continue
                delays.append(max(0, (first_day - spell[0].date()).days))
            target = by_date[anchor_day.isoformat()]
            target["median_activation_delay_days"] = _serialize_number(median(delays) if delays else None)

    return snapshots


def _load_corporation_indicator_pilot_ids(conn, corporation_id, metric, anchor_day, window_days, mode, core_months):
    corporation_id = int(corporation_id)
    cache_key = _pilot_set_cache_key(-corporation_id, metric, anchor_day, window_days, mode, core_months)
    cached = _pilot_set_cache_get(cache_key)
    if cached is not None:
        return cached

    official_rows, official_dates = _load_corporation_official_rows(conn, corporation_id)
    spells_by_character, all_spells = _load_corporation_membership_spells(conn, corporation_id, anchor_day)
    scope = _pilot_metric_scope(metric)
    if scope in {"activity", "movement"}:
        event_start = anchor_day - timedelta(days=window_days - 1)
    elif scope == "retention":
        event_start = anchor_day - timedelta(days=(window_days * 2) - 1)
    elif scope == "core":
        event_start = _shift_months(anchor_day, -core_months)
    else:
        event_start = anchor_day - timedelta(days=window_days - 1)

    candidate_ids = _candidate_ids_for_range(spells_by_character, event_start, anchor_day)
    activity = _load_member_activity_days(conn, candidate_ids, event_start, anchor_day)
    snapshot, pilot_sets = _compute_snapshot(
        anchor_day,
        window_days,
        mode,
        core_months,
        spells_by_character,
        all_spells,
        activity,
        {},
        official_rows,
        official_dates,
        include_pilot_sets=True,
    )
    pilot_ids = set(pilot_sets.get(metric) or set())
    expected = snapshot.get(metric)
    if expected is not None and int(expected) != len(pilot_ids):
        raise RuntimeError(
            f"corporation_population_indicator_pilot_count_mismatch:{metric}:{int(expected)}:{len(pilot_ids)}"
        )
    _pilot_set_cache_put(cache_key, pilot_ids)
    return pilot_ids


def get_corporation_population_indicator_pilots(
    corporation_id,
    metric,
    anchor_date,
    activity_window_days=DEFAULT_ACTIVITY_WINDOW_DAYS,
    activity_mode="any",
    core_months=DEFAULT_CORE_MONTHS,
    page=1,
    per_page=50,
    query="",
    sort="",
    direction="",
):
    corporation_id = int(corporation_id)
    metric = str(metric or "").strip()
    if metric not in PILOT_LIST_METRICS:
        raise ValueError("pilot_list_metric_invalid")

    normalized_dates, window_days, mode, months = _normalize_activity_request(
        [anchor_date], activity_window_days, activity_mode, core_months
    )
    anchor_day = normalized_dates[0]
    page = max(1, int(page or 1))
    per_page = max(1, min(50, int(per_page or 50)))
    query = str(query or "").strip()
    sort = str(sort or "name").strip().lower()
    if sort not in {"name", "corporation", "alliance"}:
        sort = "name"
    direction = "desc" if str(direction or "asc").strip().lower() == "desc" else "asc"

    with db() as conn:
        pilot_ids = _load_corporation_indicator_pilot_ids(
            conn, corporation_id, metric, anchor_day, window_days, mode, months
        )
        pilots, total_rows, page = _pilot_affiliation_rows(
            conn,
            pilot_ids,
            anchor_day,
            query,
            sort,
            direction,
            page,
            per_page,
        )

    total_pages = max(1, ceil(total_rows / per_page)) if total_rows else 1
    return {
        "entity_type": "corporation",
        "entity_id": corporation_id,
        "corporation_id": corporation_id,
        "metric": metric,
        "metric_label": PILOT_LIST_METRICS[metric],
        "date": anchor_day.isoformat(),
        "activity_window_days": window_days,
        "activity_mode": mode,
        "core_months": months,
        "view": "pilots",
        "query": query,
        "sort": sort,
        "direction": direction,
        "total_pilots": len(pilot_ids),
        "corporations": [],
        "outside_count": 0,
        "pilots": pilots,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total_rows": total_rows,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
            "prev_page": page - 1 if page > 1 else None,
            "next_page": page + 1 if page < total_pages else None,
        },
    }


def get_corporation_population_intelligence_group(
    corporation_id,
    group,
    dates,
    activity_window_days=DEFAULT_ACTIVITY_WINDOW_DAYS,
    activity_mode="any",
    core_months=DEFAULT_CORE_MONTHS,
):
    corporation_id = int(corporation_id)
    group = str(group or "").strip().lower()
    if group not in INDICATOR_GROUPS:
        raise ValueError("indicator_group_invalid")
    normalized_dates, window_days, mode, months = _normalize_activity_request(
        dates, activity_window_days, activity_mode, core_months
    )
    with db() as conn:
        snapshots = _calculate_corporation_intelligence(
            conn, corporation_id, normalized_dates, window_days, mode, months,
            scope=group, cache_pilot_sets=True,
        )
    metric_keys = INDICATOR_GROUPS[group]
    rows = []
    for snapshot in snapshots:
        row = {"date": snapshot["date"]}
        for key in metric_keys:
            row[key] = snapshot.get(key)
        rows.append(row)
    return {
        "corporation_id": corporation_id,
        "group": group,
        "activity_window_days": window_days,
        "activity_mode": mode,
        "core_months": months,
        "rows": rows,
    }


def get_corporation_population_intelligence(
    corporation_id,
    dates,
    activity_window_days=DEFAULT_ACTIVITY_WINDOW_DAYS,
    activity_mode="any",
    core_months=DEFAULT_CORE_MONTHS,
):
    corporation_id = int(corporation_id)
    normalized_dates, window_days, mode, months = _normalize_activity_request(
        dates, activity_window_days, activity_mode, core_months
    )
    with db() as conn:
        snapshots = _calculate_corporation_intelligence(
            conn, corporation_id, normalized_dates, window_days, mode, months,
            cache_pilot_sets=True,
        )
    return {
        "corporation_id": corporation_id,
        "activity_window_days": window_days,
        "activity_mode": mode,
        "core_months": months,
        "rows": snapshots,
        "definitions": {
            "membership": "A character is counted only while they belong to this corporation at that date.",
            "activity": "PvP activity before joining or after leaving the corporation is not counted.",
            "total_population": "Total population is the corporation population stored for that date.",
        },
    }


def get_corporation_population_intelligence_series(
    corporation_id,
    metric,
    date_from,
    date_to,
    activity_window_days=DEFAULT_ACTIVITY_WINDOW_DAYS,
    activity_mode="any",
    core_months=DEFAULT_CORE_MONTHS,
    max_points=None,
):
    corporation_id = int(corporation_id)
    metric = str(metric or "").strip()
    if metric not in ALL_METRICS:
        raise ValueError("metric_invalid")
    if metric in {"corporation_count", "sovereignty_count"}:
        raise ValueError("metric_not_available_for_corporation")

    start_day = _coerce_date(date_from)
    end_day = _coerce_date(date_to)
    if metric == "member_count":
        with db() as conn:
            official_rows, _official_dates = _load_corporation_official_rows(conn, corporation_id)
        rows = [
            {"date": row["date"].isoformat(), "value": row[metric]}
            for row in official_rows
            if start_day <= row["date"] <= end_day
        ]
        return {
            "corporation_id": corporation_id,
            "metric": metric,
            "activity_window_days": None,
            "activity_mode": None,
            "core_months": None,
            "step_days": 1,
            "rows": rows,
        }

    normalized_dates, window_days, mode, months = _normalize_activity_request(
        [start_day], activity_window_days, activity_mode, core_months
    )
    del normalized_dates
    anchors, step_days = _series_dates(start_day, end_day, max_points)
    metric_scope = next(
        (group for group, metric_keys in INDICATOR_GROUPS.items() if metric in metric_keys),
        "activity",
    )
    with db() as conn:
        snapshots = _calculate_corporation_intelligence(
            conn, corporation_id, anchors, window_days, mode, months, scope=metric_scope
        )
    rows = [{"date": row["date"], "value": row.get(metric)} for row in snapshots]
    return {
        "corporation_id": corporation_id,
        "metric": metric,
        "activity_window_days": window_days,
        "activity_mode": mode,
        "core_months": months,
        "step_days": step_days,
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# Coalition Population Intelligence
# ---------------------------------------------------------------------------

_COALITION_POP_CACHE = OrderedDict()
_COALITION_POP_CACHE_LOCK = Lock()
_COALITION_POP_CACHE_TTL_SECONDS = 600
_COALITION_POP_CACHE_MAX = 24
_COALITION_POP_FLOOR = date(2003, 1, 1)


def invalidate_coalition_population_cache(coalition_id):
    coalition_id = int(coalition_id)
    with _COALITION_POP_CACHE_LOCK:
        for key in list(_COALITION_POP_CACHE.keys()):
            if isinstance(key, tuple) and len(key) > 1 and key[1] == coalition_id:
                _COALITION_POP_CACHE.pop(key, None)
    with _PILOT_SET_CACHE_LOCK:
        for key in list(_PILOT_SET_CACHE.keys()):
            if (
                isinstance(key, tuple)
                and len(key) > 1
                and key[0] == "coalition"
                and key[1] == coalition_id
            ):
                _PILOT_SET_CACHE.pop(key, None)


def _coalition_pop_cache_get(key):
    now = monotonic()
    with _COALITION_POP_CACHE_LOCK:
        row = _COALITION_POP_CACHE.get(key)
        if not row:
            return None
        created_at, value = row
        if now - created_at > _COALITION_POP_CACHE_TTL_SECONDS:
            _COALITION_POP_CACHE.pop(key, None)
            return None
        _COALITION_POP_CACHE.move_to_end(key)
        return value


def _coalition_pop_cache_put(key, value):
    with _COALITION_POP_CACHE_LOCK:
        _COALITION_POP_CACHE[key] = (monotonic(), value)
        _COALITION_POP_CACHE.move_to_end(key)
        while len(_COALITION_POP_CACHE) > _COALITION_POP_CACHE_MAX:
            _COALITION_POP_CACHE.popitem(last=False)


def _load_coalition_rules(conn, coalition_id):
    coalition_id = int(coalition_id)
    cache_key = ("coalition_rules", coalition_id)
    cached = _coalition_pop_cache_get(cache_key)
    if cached is not None:
        return cached

    if not _table_exists(conn, "entities", "coalition_memberships"):
        raise RuntimeError("missing_tables:entities.coalition_memberships")

    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '15000ms'")
        cur.execute(
            """
            WITH RECURSIVE reachable(coalition_id) AS (
                SELECT %s::bigint
                UNION
                SELECT m.member_id::bigint
                FROM entities.coalition_memberships m
                JOIN reachable r ON r.coalition_id = m.coalition_id
                WHERE lower(m.member_type) = 'coalition'
            )
            SELECT
                m.coalition_id,
                lower(m.operation),
                lower(m.member_type),
                m.member_id,
                m.valid_from,
                m.valid_to
            FROM entities.coalition_memberships m
            JOIN reachable r ON r.coalition_id = m.coalition_id
            ORDER BY m.coalition_id, m.id
            """,
            (coalition_id,),
        )
        rows = cur.fetchall()

    rules_by_coalition = {}
    for parent_id, operation, member_type, member_id, valid_from, valid_to in rows:
        rules_by_coalition.setdefault(int(parent_id), []).append({
            "operation": str(operation or "include").lower(),
            "member_type": str(member_type or "").lower(),
            "member_id": int(member_id),
            "valid_from": valid_from,
            "valid_to": valid_to,
        })

    _coalition_pop_cache_put(cache_key, rules_by_coalition)
    return rules_by_coalition


def _coalition_scope_at(rules_by_coalition, coalition_id, day, stack=frozenset()):
    coalition_id = int(coalition_id)
    if coalition_id in stack:
        return set()

    includes = set()
    excludes = set()
    next_stack = stack | {coalition_id}
    for rule in rules_by_coalition.get(coalition_id, []):
        valid_from = rule.get("valid_from")
        valid_to = rule.get("valid_to")
        if valid_from is not None and day < valid_from:
            continue
        if valid_to is not None and day > valid_to:
            continue

        member_type = rule.get("member_type")
        member_id = int(rule.get("member_id"))
        if member_type == "coalition":
            target = _coalition_scope_at(
                rules_by_coalition, member_id, day, next_stack
            )
        elif member_type in {"alliance", "corporation"}:
            target = {(member_type, member_id)}
        else:
            target = set()

        if rule.get("operation") == "exclude":
            excludes.update(target)
        else:
            includes.update(target)

    return includes - excludes


def _coalition_scope_intervals(conn, coalition_id, start_day, end_day):
    coalition_id = int(coalition_id)
    start_day = _coerce_date(start_day)
    end_day = _coerce_date(end_day)
    if start_day > end_day:
        return {}, {}

    rules_by_coalition = _load_coalition_rules(conn, coalition_id)
    excluded_alliance_ids = _coalition_population_excluded_alliance_ids(conn, coalition_id)
    boundaries = {start_day, end_day + timedelta(days=1)}
    for rules in rules_by_coalition.values():
        for rule in rules:
            valid_from = rule.get("valid_from")
            valid_to = rule.get("valid_to")
            if valid_from is not None and start_day < valid_from <= end_day:
                boundaries.add(valid_from)
            if valid_to is not None:
                after = valid_to + timedelta(days=1)
                if start_day < after <= end_day:
                    boundaries.add(after)

    ordered = sorted(boundaries)
    entity_intervals = {}
    segment_scopes = {}
    for index in range(len(ordered) - 1):
        segment_start = ordered[index]
        segment_end = ordered[index + 1]
        if segment_start >= segment_end:
            continue
        scope = _coalition_scope_at(
            rules_by_coalition, coalition_id, segment_start
        )
        if excluded_alliance_ids:
            scope = {
                entity_key
                for entity_key in scope
                if not (
                    entity_key[0] == "alliance"
                    and int(entity_key[1]) in excluded_alliance_ids
                )
            }
        segment_scopes[segment_start] = scope
        start_ts = datetime.combine(segment_start, time.min, tzinfo=UTC)
        end_ts = datetime.combine(segment_end, time.min, tzinfo=UTC)
        for entity_key in scope:
            intervals = entity_intervals.setdefault(entity_key, [])
            if intervals and intervals[-1][1] == start_ts:
                intervals[-1] = (intervals[-1][0], end_ts)
            else:
                intervals.append((start_ts, end_ts))

    return entity_intervals, segment_scopes


def _split_ts_interval(start_at, end_at, chunk_days=730):
    current = start_at
    while current < end_at:
        next_at = min(end_at, current + timedelta(days=chunk_days))
        yield current, next_at
        current = next_at


def _query_coalition_membership_rows(conn, entity_type, entity_id, start_at, end_at):
    """Read one coalition member interval with adaptive time splitting.

    A single database statement is capped well below the reverse-proxy timeout.
    Only a slow interval is split; normal members stay one query.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '12000ms'")
            if entity_type == "alliance":
                cur.execute(
                    """
                    SELECT
                        cch.character_id,
                        GREATEST(cch.start_date, cah.start_date, %s::timestamptz) AS start_at,
                        LEAST(
                            COALESCE(cch.end_date, 'infinity'::timestamptz),
                            COALESCE(cah.end_date, 'infinity'::timestamptz),
                            %s::timestamptz
                        ) AS end_at
                    FROM entities.corporation_alliance_history cah
                    JOIN entities.character_corporation_history cch
                      ON cch.corporation_id = cah.corporation_id
                    WHERE cah.alliance_id = %s
                      AND COALESCE(cah.is_deleted, FALSE) = FALSE
                      AND COALESCE(cch.is_deleted, FALSE) = FALSE
                      AND cch.start_date < %s
                      AND cah.start_date < %s
                      AND COALESCE(cch.end_date, 'infinity'::timestamptz) > %s
                      AND COALESCE(cah.end_date, 'infinity'::timestamptz) > %s
                    ORDER BY cch.character_id, start_at, end_at
                    """,
                    (start_at, end_at, int(entity_id), end_at, end_at, start_at, start_at),
                )
            else:
                cur.execute(
                    """
                    SELECT
                        cch.character_id,
                        GREATEST(cch.start_date, %s::timestamptz) AS start_at,
                        LEAST(
                            COALESCE(cch.end_date, 'infinity'::timestamptz),
                            %s::timestamptz
                        ) AS end_at
                    FROM entities.character_corporation_history cch
                    WHERE cch.corporation_id = %s
                      AND COALESCE(cch.is_deleted, FALSE) = FALSE
                      AND cch.start_date < %s
                      AND COALESCE(cch.end_date, 'infinity'::timestamptz) > %s
                    ORDER BY cch.character_id, start_at, end_at
                    """,
                    (start_at, end_at, int(entity_id), end_at, start_at),
                )
            return cur.fetchall()
    except QueryCanceled:
        conn.rollback()
        duration = end_at - start_at
        if duration <= timedelta(days=31):
            raise
        midpoint = start_at + (duration / 2)
        return (
            _query_coalition_membership_rows(
                conn, entity_type, entity_id, start_at, midpoint
            )
            + _query_coalition_membership_rows(
                conn, entity_type, entity_id, midpoint, end_at
            )
        )


def _load_coalition_membership_spells(conn, coalition_id, max_day):
    coalition_id = int(coalition_id)
    max_day = _coerce_date(max_day)
    cache_key = ("coalition_spells_latest", coalition_id)
    cached = _coalition_pop_cache_get(cache_key)
    if cached is not None:
        cached_max_day, cached_value = cached
        if cached_max_day >= max_day:
            return cached_value

    entity_intervals, _segment_scopes = _coalition_scope_intervals(
        conn, coalition_id, _COALITION_POP_FLOOR, max_day
    )
    spans_by_character = {}

    for (entity_type, entity_id), intervals in sorted(entity_intervals.items()):
        for interval_start, interval_end in intervals:
            rows = _query_coalition_membership_rows(
                conn, entity_type, entity_id, interval_start, interval_end
            )
            for character_id, start_at, end_at in rows:
                if start_at >= end_at:
                    continue
                if getattr(start_at, "tzinfo", None) is None:
                    start_at = start_at.replace(tzinfo=UTC)
                if getattr(end_at, "tzinfo", None) is None and end_at.year < 9999:
                    end_at = end_at.replace(tzinfo=UTC)
                spans_by_character.setdefault(int(character_id), []).append(
                    (start_at, end_at)
                )

    spells_by_character = {}
    all_spells = []
    for character_id, spans in spans_by_character.items():
        spans.sort(key=lambda row: (row[0], row[1]))
        merged = []
        for start_at, end_at in spans:
            if not merged:
                merged.append([start_at, end_at])
                continue
            previous = merged[-1]
            previous_end = previous[1]
            if previous_end.year >= 9999 or start_at <= previous_end + timedelta(seconds=1):
                if previous_end.year < 9999 and end_at > previous_end:
                    previous[1] = end_at
            else:
                merged.append([start_at, end_at])
        normalized = [(row[0], row[1]) for row in merged]
        spells_by_character[int(character_id)] = normalized
        all_spells.extend(
            (int(character_id), start_at, end_at)
            for start_at, end_at in normalized
        )

    all_spells.sort(key=lambda row: (row[0], row[1], row[2]))
    result = (spells_by_character, all_spells)
    _coalition_pop_cache_put(cache_key, (max_day, result))
    return result


def _merge_activity_maps(target, source):
    for character_id, source_bucket in source.items():
        cid = int(character_id)
        target_bucket = target.setdefault(
            cid, {"dates": [], "any": [], "attacker": []}
        )
        combined = {}
        for day, any_count, attacker_count in zip(
            target_bucket["dates"], target_bucket["any"], target_bucket["attacker"]
        ):
            combined[day] = [int(any_count or 0), int(attacker_count or 0)]
        for day, any_count, attacker_count in zip(
            source_bucket["dates"], source_bucket["any"], source_bucket["attacker"]
        ):
            row = combined.setdefault(day, [0, 0])
            row[0] += int(any_count or 0)
            row[1] += int(attacker_count or 0)
        ordered = sorted(combined.items())
        target_bucket["dates"] = [row[0] for row in ordered]
        target_bucket["any"] = [row[1][0] for row in ordered]
        target_bucket["attacker"] = [row[1][1] for row in ordered]


def _query_coalition_activity_chunk(conn, character_ids, start_day, end_day):
    ids = sorted({int(value) for value in character_ids if value is not None})
    if not ids:
        return {}

    start_ts = datetime.combine(start_day, time.min, tzinfo=UTC)
    end_ts = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=UTC)
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '12000ms'")
            cur.execute(
                """
                WITH scoped_events AS (
                    SELECT
                        km.victim_character_id::bigint AS character_id,
                        km.killmail_id::bigint AS killmail_id,
                        km.killmail_time::date AS activity_date,
                        FALSE AS is_attacker
                    FROM rawkm.killmails km
                    WHERE km.killmail_time >= %s
                      AND km.killmail_time < %s
                      AND km.victim_character_id = ANY(%s)

                    UNION ALL

                    SELECT
                        ka.character_id::bigint AS character_id,
                        ka.killmail_id::bigint AS killmail_id,
                        ka.killmail_time::date AS activity_date,
                        TRUE AS is_attacker
                    FROM rawkm.killmail_attackers ka
                    WHERE ka.killmail_time >= %s
                      AND ka.killmail_time < %s
                      AND ka.character_id = ANY(%s)
                ), deduped AS (
                    SELECT DISTINCT character_id, killmail_id, activity_date, is_attacker
                    FROM scoped_events
                )
                SELECT
                    character_id,
                    activity_date,
                    COUNT(DISTINCT killmail_id)::integer AS any_killmails,
                    COUNT(DISTINCT killmail_id) FILTER (WHERE is_attacker)::integer AS attacker_killmails
                FROM deduped
                GROUP BY character_id, activity_date
                ORDER BY character_id, activity_date
                """,
                (start_ts, end_ts, ids, start_ts, end_ts, ids),
            )
            rows = cur.fetchall()
    except QueryCanceled:
        conn.rollback()
        if len(ids) <= 100:
            raise
        midpoint = max(1, len(ids) // 2)
        left = _query_coalition_activity_chunk(conn, ids[:midpoint], start_day, end_day)
        right = _query_coalition_activity_chunk(conn, ids[midpoint:], start_day, end_day)
        _merge_activity_maps(left, right)
        return left

    activity = {}
    for character_id, activity_date, any_count, attacker_count in rows:
        cid = int(character_id)
        bucket = activity.setdefault(cid, {"dates": [], "any": [], "attacker": []})
        bucket["dates"].append(activity_date)
        bucket["any"].append(int(any_count or 0))
        bucket["attacker"].append(int(attacker_count or 0))
    return activity


def _load_coalition_activity_days(
    conn, coalition_id, character_ids, start_day, end_day
):
    coalition_id = int(coalition_id)
    ids = sorted({int(value) for value in character_ids if value is not None})
    if not ids or start_day > end_day:
        return {}

    cache_key = (
        "coalition_activity",
        coalition_id,
        start_day.isoformat(),
        end_day.isoformat(),
    )
    cached = _coalition_pop_cache_get(cache_key)
    if cached is not None:
        return cached

    activity = {}
    # Keep each SQL request small enough to use the character/time indexes and
    # return well below the reverse-proxy timeout. The global result is merged
    # only after every bounded chunk has completed.
    chunk_size = 2500
    for offset in range(0, len(ids), chunk_size):
        partial = _query_coalition_activity_chunk(
            conn, ids[offset:offset + chunk_size], start_day, end_day
        )
        _merge_activity_maps(activity, partial)

    _coalition_pop_cache_put(cache_key, activity)
    return activity


def _load_coalition_official_rows(conn, coalition_id):
    coalition_id = int(coalition_id)
    cache_key = ("coalition_official", coalition_id)
    cached = _coalition_pop_cache_get(cache_key)
    if cached is not None:
        return cached

    today = date.today()
    entity_intervals, _segment_scopes = _coalition_scope_intervals(
        conn, coalition_id, _COALITION_POP_FLOOR, today
    )
    alliance_ids = sorted(
        entity_id for entity_type, entity_id in entity_intervals if entity_type == "alliance"
    )
    corporation_ids = sorted(
        entity_id for entity_type, entity_id in entity_intervals if entity_type == "corporation"
    )

    alliance_rows = {}
    alliance_dates = {}
    corporation_rows = {}
    corporation_dates = {}
    all_days = set()

    if alliance_ids and _table_exists(conn, "population", "alliance_daily"):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT alliance_id, snapshot_date, member_count, corporation_count, sovereignty_count
                FROM population.alliance_daily
                WHERE alliance_id = ANY(%s)
                ORDER BY alliance_id, snapshot_date
                """,
                (alliance_ids,),
            )
            for aid, snapshot_date, members, corps, sov in cur.fetchall():
                row = {
                    "date": snapshot_date,
                    "member_count": int(members) if members is not None else None,
                    "corporation_count": int(corps) if corps is not None else None,
                    "sovereignty_count": int(sov) if sov is not None else None,
                }
                alliance_rows.setdefault(int(aid), []).append(row)
                all_days.add(snapshot_date)
        alliance_dates = {
            aid: [row["date"] for row in rows]
            for aid, rows in alliance_rows.items()
        }

    if corporation_ids and _table_exists(conn, "population", "corporation_daily"):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT corporation_id, snapshot_date, member_count
                FROM population.corporation_daily
                WHERE corporation_id = ANY(%s)
                ORDER BY corporation_id, snapshot_date
                """,
                (corporation_ids,),
            )
            for cid, snapshot_date, members in cur.fetchall():
                row = {
                    "date": snapshot_date,
                    "member_count": int(members) if members is not None else None,
                    "corporation_count": 1,
                    "sovereignty_count": 0,
                }
                corporation_rows.setdefault(int(cid), []).append(row)
                all_days.add(snapshot_date)
        corporation_dates = {
            cid: [row["date"] for row in rows]
            for cid, rows in corporation_rows.items()
        }

    corporation_alliance_timelines = (
        _load_corporation_alliance_timelines(conn, corporation_ids)
        if corporation_ids
        else {}
    )
    rules_by_coalition = _load_coalition_rules(conn, coalition_id)

    alliance_id_set = set(alliance_ids)
    rows = []
    for day in sorted(all_days):
        scope = _coalition_scope_at(rules_by_coalition, coalition_id, day)
        if not scope:
            continue
        active_alliances = {
            entity_id for entity_type, entity_id in scope
            if entity_type == "alliance" and entity_id in alliance_id_set
        }
        active_corporations = {
            entity_id for entity_type, entity_id in scope if entity_type == "corporation"
        }

        member_count = 0
        alliance_count = len(active_alliances)
        corporation_count = 0
        sovereignty_count = 0
        has_member_value = False
        has_alliance_value = True
        has_corporation_value = False
        has_sov_value = False

        for alliance_id in active_alliances:
            row = _official_at_or_before(
                alliance_rows.get(alliance_id, []),
                alliance_dates.get(alliance_id, []),
                day,
            )
            if not row:
                continue
            if row.get("member_count") is not None:
                member_count += int(row["member_count"])
                has_member_value = True
            if row.get("corporation_count") is not None:
                corporation_count += int(row["corporation_count"])
                has_corporation_value = True
            if row.get("sovereignty_count") is not None:
                sovereignty_count += int(row["sovereignty_count"])
                has_sov_value = True

        day_probe = _at_end_of_day(day)
        for corporation_id in active_corporations:
            # A corporation directly included in the coalition can also be
            # inside an included alliance. Do not count its population twice.
            corp_alliance = _alliance_for_corporation_at(
                corporation_alliance_timelines, corporation_id, day_probe
            )
            if (
                corp_alliance
                and corp_alliance.get("alliance_id") is not None
                and int(corp_alliance["alliance_id"]) in active_alliances
            ):
                continue
            row = _official_at_or_before(
                corporation_rows.get(corporation_id, []),
                corporation_dates.get(corporation_id, []),
                day,
            )
            if not row:
                continue
            if row.get("member_count") is not None:
                member_count += int(row["member_count"])
                has_member_value = True
            corporation_count += 1
            has_corporation_value = True

        if not (has_member_value or has_alliance_value or has_corporation_value or has_sov_value):
            continue
        rows.append({
            "date": day,
            "member_count": member_count if has_member_value else None,
            "alliance_count": alliance_count,
            "corporation_count": corporation_count if has_corporation_value else None,
            "sovereignty_count": sovereignty_count if has_sov_value else None,
        })

    result = (rows, [row["date"] for row in rows])
    _coalition_pop_cache_put(cache_key, result)
    return result


def get_coalition_population_dependencies(coalition_id):
    coalition_id = int(coalition_id)
    with db() as conn:
        rules_by_coalition = _load_coalition_rules(conn, coalition_id)
    alliance_ids = set()
    corporation_ids = set()
    for rules in rules_by_coalition.values():
        for rule in rules:
            if rule.get("member_type") == "alliance":
                alliance_ids.add(int(rule["member_id"]))
            elif rule.get("member_type") == "corporation":
                corporation_ids.add(int(rule["member_id"]))
    return {
        "alliance_ids": sorted(alliance_ids),
        "corporation_ids": sorted(corporation_ids),
    }



def _coalition_population_excluded_alliances(conn, coalition_id):
    """Alliances intentionally excluded from coalition population analysis."""
    coalition_id = int(coalition_id)
    rules_by_coalition = _load_coalition_rules(conn, coalition_id)
    alliance_ids = sorted({
        int(rule["member_id"])
        for rules in rules_by_coalition.values()
        for rule in rules
        if rule.get("member_type") == "alliance"
    })
    if not alliance_ids or not _table_exists(conn, "population", "alliance_sync"):
        return []

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                s.alliance_id,
                s.last_error,
                COALESCE(NULLIF(a.name, ''), 'Alliance ' || s.alliance_id::text) AS alliance_name,
                a.ticker
            FROM population.alliance_sync s
            LEFT JOIN entities.alliances a
              ON a.alliance_id = s.alliance_id
            WHERE s.alliance_id = ANY(%s)
              AND s.last_error LIKE %s
            ORDER BY lower(COALESCE(NULLIF(a.name, ''), '')), s.alliance_id
            """,
            (alliance_ids, "dotlan_alliance_id_mismatch:%"),
        )
        return [
            {
                "alliance_id": int(alliance_id),
                "name": alliance_name or f"Alliance {int(alliance_id)}",
                "ticker": ticker,
                "reason": last_error,
            }
            for alliance_id, last_error, alliance_name, ticker in cur.fetchall()
        ]


def _coalition_population_excluded_alliance_ids(conn, coalition_id):
    return {
        int(row["alliance_id"])
        for row in _coalition_population_excluded_alliances(conn, coalition_id)
    }



def get_coalition_population_initialization_state(coalition_id):
    coalition_id = int(coalition_id)
    with db() as conn:
        rules_by_coalition = _load_coalition_rules(conn, coalition_id)
        alliance_ids = sorted({
            int(rule["member_id"])
            for rules in rules_by_coalition.values()
            for rule in rules
            if rule.get("member_type") == "alliance"
        })
        corporation_ids = sorted({
            int(rule["member_id"])
            for rules in rules_by_coalition.values()
            for rule in rules
            if rule.get("member_type") == "corporation"
        })
        excluded_alliances = _coalition_population_excluded_alliances(conn, coalition_id)
        excluded_ids = {int(row["alliance_id"]) for row in excluded_alliances}

        pending = 0
        errors = []
        if alliance_ids and _table_exists(conn, "population", "alliance_sync"):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT alliance_id, initialization_done, last_error
                    FROM population.alliance_sync
                    WHERE alliance_id = ANY(%s)
                    """,
                    (alliance_ids,),
                )
                states = {int(row[0]): (bool(row[1]), row[2]) for row in cur.fetchall()}
            for alliance_id in alliance_ids:
                if alliance_id in excluded_ids:
                    continue
                state = states.get(alliance_id)
                if not state or not state[0]:
                    pending += 1
                if state and state[1]:
                    errors.append(f"alliance {alliance_id}: {state[1]}")
        else:
            pending += len([aid for aid in alliance_ids if aid not in excluded_ids])

        if corporation_ids and _table_exists(conn, "population", "corporation_sync"):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT corporation_id, initialization_done, last_error
                    FROM population.corporation_sync
                    WHERE corporation_id = ANY(%s)
                    """,
                    (corporation_ids,),
                )
                states = {int(row[0]): (bool(row[1]), row[2]) for row in cur.fetchall()}
            for corporation_id in corporation_ids:
                state = states.get(corporation_id)
                if not state or not state[0]:
                    pending += 1
                if state and state[1]:
                    errors.append(f"corporation {corporation_id}: {state[1]}")
        else:
            pending += len(corporation_ids)

    return {
        "coalition_id": coalition_id,
        "initialization_done": pending == 0,
        "pending_entities": pending,
        "alliance_count": len(alliance_ids),
        "corporation_count": len(corporation_ids),
        "excluded_alliance_count": len(excluded_alliances),
        "excluded_alliances": excluded_alliances,
        "last_error": " | ".join(errors[:5]) if errors else None,
    }

def get_coalition_population_summary(coalition_id):
    coalition_id = int(coalition_id)
    with db() as conn:
        rows, _dates = _load_coalition_official_rows(conn, coalition_id)
    if not rows:
        return None
    row = rows[-1]
    parts = []
    if row.get("member_count") is not None:
        parts.append(f"{_format_count(row['member_count'])} members")
    if row.get("alliance_count") is not None:
        parts.append(f"{_format_count(row['alliance_count'])} alliances")
    if row.get("corporation_count") is not None:
        parts.append(f"{_format_count(row['corporation_count'])} corporations")
    if row.get("sovereignty_count") is not None:
        parts.append(f"{_format_count(row['sovereignty_count'])} systems")
    return {
        "snapshot_date": row["date"].isoformat(),
        "member_count": row.get("member_count"),
        "alliance_count": row.get("alliance_count"),
        "corporation_count": row.get("corporation_count"),
        "sovereignty_count": row.get("sovereignty_count"),
        "display": " · ".join(parts),
    }



def get_coalition_population_history(coalition_id):
    coalition_id = int(coalition_id)
    with db() as conn:
        rows, _dates = _load_coalition_official_rows(conn, coalition_id)
        rules_by_coalition = _load_coalition_rules(conn, coalition_id)
        alliance_ids = sorted({
            int(rule["member_id"])
            for rules in rules_by_coalition.values()
            for rule in rules
            if rule.get("member_type") == "alliance"
        })
        corporation_ids = sorted({
            int(rule["member_id"])
            for rules in rules_by_coalition.values()
            for rule in rules
            if rule.get("member_type") == "corporation"
        })
        excluded_alliances = _coalition_population_excluded_alliances(conn, coalition_id)
        excluded_ids = {int(row["alliance_id"]) for row in excluded_alliances}

        pending = 0
        errors = []
        if alliance_ids and _table_exists(conn, "population", "alliance_sync"):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT alliance_id, initialization_done, last_error
                    FROM population.alliance_sync
                    WHERE alliance_id = ANY(%s)
                    """,
                    (alliance_ids,),
                )
                states = {int(row[0]): (bool(row[1]), row[2]) for row in cur.fetchall()}
            for alliance_id in alliance_ids:
                if alliance_id in excluded_ids:
                    continue
                state = states.get(alliance_id)
                if not state or not state[0]:
                    pending += 1
                if state and state[1]:
                    errors.append(f"alliance {alliance_id}: {state[1]}")
        else:
            pending += len([aid for aid in alliance_ids if aid not in excluded_ids])

        if corporation_ids and _table_exists(conn, "population", "corporation_sync"):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT corporation_id, initialization_done, last_error
                    FROM population.corporation_sync
                    WHERE corporation_id = ANY(%s)
                    """,
                    (corporation_ids,),
                )
                states = {int(row[0]): (bool(row[1]), row[2]) for row in cur.fetchall()}
            for corporation_id in corporation_ids:
                state = states.get(corporation_id)
                if not state or not state[0]:
                    pending += 1
                if state and state[1]:
                    errors.append(f"corporation {corporation_id}: {state[1]}")
        else:
            pending += len(corporation_ids)

    serialized = [
        {
            "date": row["date"].isoformat(),
            "member_count": row.get("member_count"),
            "alliance_count": row.get("alliance_count"),
            "corporation_count": row.get("corporation_count"),
            "sovereignty_count": row.get("sovereignty_count"),
        }
        for row in rows
    ]
    return {
        "coalition_id": coalition_id,
        "available": bool(serialized),
        "source": "dotlan_aggregated",
        "initialization_done": pending == 0,
        "first_available_date": serialized[0]["date"] if serialized else None,
        "oldest_synced_date": serialized[0]["date"] if serialized else None,
        "last_synced_date": serialized[-1]["date"] if serialized else None,
        "excluded_alliance_count": len(excluded_alliances),
        "excluded_alliances": excluded_alliances,
        "last_error": " | ".join(errors[:5]) if errors else None,
        "rows": serialized,
    }

def _calculate_coalition_intelligence(
    conn,
    coalition_id,
    normalized_dates,
    window_days,
    mode,
    core_months,
    scope="full",
    cache_pilot_sets=False,
):
    coalition_id = int(coalition_id)
    official_rows, official_dates = _load_coalition_official_rows(conn, coalition_id)
    max_day = max(normalized_dates)

    required = [
        ("entities", "character_corporation_history"),
        ("entities", "corporation_alliance_history"),
    ]
    if scope != "membership":
        required.extend([("rawkm", "killmails"), ("rawkm", "killmail_attackers")])
    missing = [
        f"{schema}.{table}"
        for schema, table in required
        if not _table_exists(conn, schema, table)
    ]
    if missing:
        raise RuntimeError("missing_tables:" + ",".join(missing))

    spells_by_character, all_spells = _load_coalition_membership_spells(
        conn, coalition_id, max_day
    )

    activity = {}
    birthdays = {}
    needs_daily_activity = scope in {
        "activity", "retention", "movement", "core", "full"
    }
    if needs_daily_activity:
        min_anchor = min(normalized_dates)
        if scope in {"activity", "movement"}:
            event_start = min_anchor - timedelta(days=window_days - 1)
        elif scope == "retention":
            event_start = min_anchor - timedelta(days=(window_days * 2) - 1)
        elif scope == "core":
            event_start = _shift_months(min_anchor, -core_months)
        else:
            retention_start = min_anchor - timedelta(days=(window_days * 2) - 1)
            core_start = _shift_months(min_anchor, -core_months)
            event_start = min(retention_start, core_start)

        candidate_ids = _candidate_ids_for_range(
            spells_by_character, event_start, max_day
        )
        activity = _load_coalition_activity_days(
            conn, coalition_id, candidate_ids, event_start, max_day
        )
        if scope in {"activity", "full"}:
            birthdays = _load_birthdays(conn, candidate_ids)

    snapshots = []
    for anchor_day in normalized_dates:
        if cache_pilot_sets:
            snapshot, pilot_sets = _compute_snapshot(
                anchor_day,
                window_days,
                mode,
                core_months,
                spells_by_character,
                all_spells,
                activity,
                birthdays,
                official_rows,
                official_dates,
                include_pilot_sets=True,
            )
            cache_metric_keys = (
                set(PILOT_LIST_METRICS)
                if scope == "full"
                else set(INDICATOR_GROUPS.get(scope, set()))
            )
            for metric, pilot_ids in pilot_sets.items():
                if metric in PILOT_LIST_METRICS and metric in cache_metric_keys:
                    _pilot_set_cache_put(
                        (
                            "coalition",
                            coalition_id,
                            metric,
                            anchor_day.isoformat(),
                            int(window_days),
                            str(mode),
                            int(core_months),
                        ),
                        pilot_ids,
                    )
        else:
            snapshot = _compute_snapshot(
                anchor_day,
                window_days,
                mode,
                core_months,
                spells_by_character,
                all_spells,
                activity,
                birthdays,
                official_rows,
                official_dates,
            )
        snapshots.append(snapshot)

    if scope in {"first_pvp", "full"}:
        candidates = []
        current_by_date = {}
        candidate_keys = {}
        for anchor_day in normalized_dates:
            window_start = anchor_day - timedelta(days=window_days - 1)
            anchor_end = datetime.combine(
                anchor_day + timedelta(days=1), time.min, tzinfo=UTC
            )
            current_members = _member_state(spells_by_character, anchor_day)
            current_by_date[anchor_day] = current_members
            for character_id, spell in current_members.items():
                if not (window_start <= spell[0].date() <= anchor_day):
                    continue
                bounded_end = (
                    anchor_end
                    if spell[1].year >= 9999 or spell[1] > anchor_end
                    else spell[1]
                )
                candidate_key = (
                    f"coalition:{coalition_id}:{anchor_day.isoformat()}:"
                    f"{int(character_id)}:{spell[0].isoformat()}"
                )
                candidate_keys[(anchor_day, int(character_id), spell[0])] = candidate_key
                candidates.append((candidate_key, character_id, spell[0], bounded_end))

        first_dates = _load_first_activity_for_anchor_candidates(conn, candidates, mode)
        by_date = {row["date"]: row for row in snapshots}
        for anchor_day in normalized_dates:
            window_start = anchor_day - timedelta(days=window_days - 1)
            current_members = current_by_date[anchor_day]
            delays = []
            for character_id, spell in current_members.items():
                if not (window_start <= spell[0].date() <= anchor_day):
                    continue
                candidate_key = candidate_keys.get(
                    (anchor_day, int(character_id), spell[0])
                )
                first_day = first_dates.get(candidate_key) if candidate_key else None
                if first_day is None or first_day > anchor_day or first_day < window_start:
                    continue
                delays.append(max(0, (first_day - spell[0].date()).days))
            target = by_date[anchor_day.isoformat()]
            target["median_activation_delay_days"] = _serialize_number(
                median(delays) if delays else None
            )

    return snapshots


def _load_coalition_indicator_pilot_ids(
    conn, coalition_id, metric, anchor_day, window_days, mode, core_months
):
    coalition_id = int(coalition_id)
    cache_key = (
        "coalition",
        coalition_id,
        metric,
        anchor_day.isoformat(),
        int(window_days),
        str(mode),
        int(core_months),
    )
    cached = _pilot_set_cache_get(cache_key)
    if cached is not None:
        return cached

    scope = _pilot_metric_scope(metric)
    snapshots = _calculate_coalition_intelligence(
        conn,
        coalition_id,
        [anchor_day],
        window_days,
        mode,
        core_months,
        scope=scope,
        cache_pilot_sets=True,
    )
    cached = _pilot_set_cache_get(cache_key)
    if cached is not None:
        return cached
    expected = snapshots[0].get(metric) if snapshots else 0
    if expected in (None, 0):
        return set()
    raise RuntimeError(f"coalition_population_indicator_cache_missing:{metric}")



def _pilot_coalition_summary_chunk(conn, pilot_ids, anchor_day, group_by):
    ids = sorted({int(value) for value in pilot_ids if value is not None})
    if not ids:
        return []

    anchor_ts = _at_end_of_day(anchor_day)
    base_sql = _pilot_affiliation_base_sql()
    if group_by == "alliances":
        select_sql = """
            SELECT
                alliance_id,
                CASE WHEN alliance_id IS NULL THEN 'No alliance' ELSE alliance_name END AS group_name,
                alliance_ticker,
                COUNT(*)::bigint AS pilot_count
            FROM resolved
            GROUP BY alliance_id, group_name, alliance_ticker
        """
    elif group_by == "corporations":
        select_sql = """
            SELECT
                corporation_id,
                corporation_name,
                corporation_ticker,
                alliance_id,
                CASE WHEN alliance_id IS NULL THEN 'No alliance' ELSE alliance_name END AS alliance_group_name,
                alliance_ticker,
                COUNT(*)::bigint AS pilot_count
            FROM resolved
            GROUP BY
                corporation_id, corporation_name, corporation_ticker,
                alliance_id, alliance_group_name, alliance_ticker
        """
    else:
        raise ValueError("coalition_pilot_summary_group_invalid")

    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '12000ms'")
            cur.execute(
                base_sql + select_sql,
                (ids, anchor_ts, anchor_ts, anchor_ts, anchor_ts),
            )
            return cur.fetchall()
    except QueryCanceled:
        conn.rollback()
        if len(ids) <= 250:
            raise
        midpoint = max(1, len(ids) // 2)
        return (
            _pilot_coalition_summary_chunk(conn, ids[:midpoint], anchor_day, group_by)
            + _pilot_coalition_summary_chunk(conn, ids[midpoint:], anchor_day, group_by)
        )


def _pilot_coalition_alliance_summary_rows(
    conn, pilot_ids, anchor_day, query, sort_key, direction
):
    ids = sorted({int(value) for value in pilot_ids if value is not None})
    merged = {}
    chunk_size = 5000
    for offset in range(0, len(ids), chunk_size):
        rows = _pilot_coalition_summary_chunk(
            conn, ids[offset:offset + chunk_size], anchor_day, "alliances"
        )
        for alliance_id, name, ticker, pilot_count in rows:
            key = int(alliance_id) if alliance_id is not None else None
            bucket = merged.setdefault(key, {
                "alliance_id": key,
                "name": name or ("No alliance" if key is None else "Unknown"),
                "ticker": ticker,
                "pilot_count": 0,
            })
            bucket["pilot_count"] += int(pilot_count or 0)

    rows = list(merged.values())
    needle = str(query or "").strip().lower()
    if needle:
        rows = [
            row for row in rows
            if needle in (row.get("name") or "").lower()
            or needle in (row.get("ticker") or "").lower()
        ]

    sort_key = sort_key if sort_key in {"alliance", "pilots"} else "pilots"
    direction = "desc" if str(direction or "").lower() == "desc" else "asc"
    reverse = direction == "desc"
    if sort_key == "alliance":
        rows.sort(
            key=lambda row: (
                row.get("alliance_id") is None,
                (row.get("name") or "").lower(),
                int(row.get("alliance_id") or 0),
            ),
            reverse=reverse,
        )
    else:
        rows.sort(
            key=lambda row: (
                int(row.get("pilot_count") or 0),
                (row.get("name") or "").lower(),
            ),
            reverse=reverse,
        )
    return rows


def _pilot_coalition_corporation_summary_rows(
    conn, pilot_ids, anchor_day, query, sort_key, direction
):
    ids = sorted({int(value) for value in pilot_ids if value is not None})
    merged = {}
    chunk_size = 5000
    for offset in range(0, len(ids), chunk_size):
        rows = _pilot_coalition_summary_chunk(
            conn, ids[offset:offset + chunk_size], anchor_day, "corporations"
        )
        for (
            corporation_id,
            corporation_name,
            corporation_ticker,
            alliance_id,
            alliance_name,
            alliance_ticker,
            pilot_count,
        ) in rows:
            key = int(corporation_id) if corporation_id is not None else None
            bucket = merged.setdefault(key, {
                "corporation_id": key,
                "name": corporation_name or ("Unknown corporation" if key is None else "Unknown"),
                "ticker": corporation_ticker,
                "alliance": (
                    {
                        "alliance_id": int(alliance_id),
                        "name": alliance_name or "Unknown",
                        "ticker": alliance_ticker,
                    }
                    if alliance_id is not None
                    else None
                ),
                "pilot_count": 0,
            })
            bucket["pilot_count"] += int(pilot_count or 0)

    rows = list(merged.values())
    needle = str(query or "").strip().lower()
    if needle:
        rows = [
            row for row in rows
            if needle in (row.get("name") or "").lower()
            or needle in (row.get("ticker") or "").lower()
            or needle in ((row.get("alliance") or {}).get("name") or "No alliance").lower()
            or needle in ((row.get("alliance") or {}).get("ticker") or "").lower()
        ]

    sort_key = sort_key if sort_key in {"corporation", "alliance", "pilots"} else "pilots"
    direction = "desc" if str(direction or "").lower() == "desc" else "asc"
    reverse = direction == "desc"
    if sort_key == "corporation":
        rows.sort(
            key=lambda row: (
                row.get("corporation_id") is None,
                (row.get("name") or "").lower(),
                int(row.get("corporation_id") or 0),
            ),
            reverse=reverse,
        )
    elif sort_key == "alliance":
        rows.sort(
            key=lambda row: (
                row.get("alliance") is None,
                ((row.get("alliance") or {}).get("name") or "No alliance").lower(),
                (row.get("name") or "").lower(),
            ),
            reverse=reverse,
        )
    else:
        rows.sort(
            key=lambda row: (
                int(row.get("pilot_count") or 0),
                (row.get("name") or "").lower(),
            ),
            reverse=reverse,
        )
    return rows



def get_coalition_population_indicator_pilots(
    coalition_id,
    metric,
    anchor_date,
    activity_window_days=DEFAULT_ACTIVITY_WINDOW_DAYS,
    activity_mode="any",
    core_months=DEFAULT_CORE_MONTHS,
    page=1,
    per_page=50,
    query="",
    sort="",
    direction="",
    view="alliances",
):
    coalition_id = int(coalition_id)
    metric = str(metric or "").strip()
    if metric not in PILOT_LIST_METRICS:
        raise ValueError("pilot_list_metric_invalid")

    normalized_dates, window_days, mode, months = _normalize_activity_request(
        [anchor_date], activity_window_days, activity_mode, core_months
    )
    anchor_day = normalized_dates[0]
    page = max(1, int(page or 1))
    per_page = max(1, min(50, int(per_page or 50)))
    query = str(query or "").strip()[:120]
    view = str(view or "alliances").strip().lower()
    if view not in {"alliances", "corporations", "pilots"}:
        view = "alliances"

    with db() as conn:
        pilot_ids = _load_coalition_indicator_pilot_ids(
            conn, coalition_id, metric, anchor_day, window_days, mode, months
        )

        alliances = []
        corporations = []
        pilots = []
        total_rows = len(pilot_ids)

        if view == "alliances":
            sort = str(sort or "pilots").strip().lower()
            if sort not in {"alliance", "pilots"}:
                sort = "pilots"
            direction = str(
                direction or ("desc" if sort == "pilots" else "asc")
            ).strip().lower()
            direction = "desc" if direction == "desc" else "asc"
            alliances = _pilot_coalition_alliance_summary_rows(
                conn, pilot_ids, anchor_day, query, sort, direction
            )
            page = 1

        elif view == "corporations":
            sort = str(sort or "pilots").strip().lower()
            if sort not in {"corporation", "alliance", "pilots"}:
                sort = "pilots"
            direction = str(
                direction or ("desc" if sort == "pilots" else "asc")
            ).strip().lower()
            direction = "desc" if direction == "desc" else "asc"
            corporations = _pilot_coalition_corporation_summary_rows(
                conn, pilot_ids, anchor_day, query, sort, direction
            )
            page = 1

        else:
            sort = str(sort or "name").strip().lower()
            if sort not in {"name", "corporation", "alliance"}:
                sort = "name"
            direction = (
                "desc"
                if str(direction or "asc").strip().lower() == "desc"
                else "asc"
            )
            pilots, total_rows, page = _pilot_affiliation_rows(
                conn,
                pilot_ids,
                anchor_day,
                query,
                sort,
                direction,
                page,
                per_page,
            )

    total_pages = max(1, ceil(total_rows / per_page)) if total_rows else 1
    return {
        "entity_type": "coalition",
        "entity_id": coalition_id,
        "coalition_id": coalition_id,
        "metric": metric,
        "metric_label": PILOT_LIST_METRICS[metric],
        "date": anchor_day.isoformat(),
        "activity_window_days": window_days,
        "activity_mode": mode,
        "core_months": months,
        "view": view,
        "query": query,
        "sort": sort,
        "direction": direction,
        "total_pilots": len(pilot_ids),
        "alliances": alliances,
        "corporations": corporations,
        "outside_count": 0,
        "pilots": pilots,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total_rows": total_rows,
            "total_pages": total_pages,
            "has_prev": view == "pilots" and page > 1,
            "has_next": view == "pilots" and page < total_pages,
            "prev_page": page - 1 if view == "pilots" and page > 1 else None,
            "next_page": page + 1 if view == "pilots" and page < total_pages else None,
        },
    }

def get_coalition_population_intelligence_group(
    coalition_id,
    group,
    dates,
    activity_window_days=DEFAULT_ACTIVITY_WINDOW_DAYS,
    activity_mode="any",
    core_months=DEFAULT_CORE_MONTHS,
):
    coalition_id = int(coalition_id)
    group = str(group or "").strip().lower()
    if group not in INDICATOR_GROUPS:
        raise ValueError("indicator_group_invalid")
    normalized_dates, window_days, mode, months = _normalize_activity_request(
        dates, activity_window_days, activity_mode, core_months
    )
    with db() as conn:
        snapshots = _calculate_coalition_intelligence(
            conn,
            coalition_id,
            normalized_dates,
            window_days,
            mode,
            months,
            scope=group,
            cache_pilot_sets=True,
        )
    metric_keys = INDICATOR_GROUPS[group]
    rows = []
    for snapshot in snapshots:
        row = {"date": snapshot["date"]}
        for key in metric_keys:
            row[key] = snapshot.get(key)
        rows.append(row)
    return {
        "coalition_id": coalition_id,
        "group": group,
        "activity_window_days": window_days,
        "activity_mode": mode,
        "core_months": months,
        "rows": rows,
    }


def get_coalition_population_intelligence(
    coalition_id,
    dates,
    activity_window_days=DEFAULT_ACTIVITY_WINDOW_DAYS,
    activity_mode="any",
    core_months=DEFAULT_CORE_MONTHS,
):
    coalition_id = int(coalition_id)
    normalized_dates, window_days, mode, months = _normalize_activity_request(
        dates, activity_window_days, activity_mode, core_months
    )
    with db() as conn:
        snapshots = _calculate_coalition_intelligence(
            conn,
            coalition_id,
            normalized_dates,
            window_days,
            mode,
            months,
            cache_pilot_sets=True,
        )
    return {
        "coalition_id": coalition_id,
        "activity_window_days": window_days,
        "activity_mode": mode,
        "core_months": months,
        "rows": snapshots,
        "definitions": {
            "membership": "A character is counted only while they belong to this coalition at that date.",
            "activity": "PvP activity before joining or after leaving the coalition is not counted. Transfers between member alliances remain continuous coalition membership.",
            "total_population": "Total population is aggregated from member alliance/corporation population histories at that date.",
        },
    }


def get_coalition_population_intelligence_series(
    coalition_id,
    metric,
    date_from,
    date_to,
    activity_window_days=DEFAULT_ACTIVITY_WINDOW_DAYS,
    activity_mode="any",
    core_months=DEFAULT_CORE_MONTHS,
    max_points=None,
):
    coalition_id = int(coalition_id)
    metric = str(metric or "").strip()
    if metric not in (COALITION_OFFICIAL_METRICS | ACTIVITY_METRICS):
        raise ValueError("metric_invalid")
    start_day = _coerce_date(date_from)
    end_day = _coerce_date(date_to)

    if metric in COALITION_OFFICIAL_METRICS:
        with db() as conn:
            official_rows, _official_dates = _load_coalition_official_rows(
                conn, coalition_id
            )
        rows = [
            {"date": row["date"].isoformat(), "value": row[metric]}
            for row in official_rows
            if start_day <= row["date"] <= end_day
        ]
        return {
            "coalition_id": coalition_id,
            "metric": metric,
            "activity_window_days": None,
            "activity_mode": None,
            "core_months": None,
            "step_days": 1,
            "rows": rows,
        }

    normalized_dates, window_days, mode, months = _normalize_activity_request(
        [start_day], activity_window_days, activity_mode, core_months
    )
    del normalized_dates
    anchors, step_days = _series_dates(start_day, end_day, max_points)
    metric_scope = next(
        (group for group, metric_keys in INDICATOR_GROUPS.items() if metric in metric_keys),
        "activity",
    )
    with db() as conn:
        snapshots = _calculate_coalition_intelligence(
            conn,
            coalition_id,
            anchors,
            window_days,
            mode,
            months,
            scope=metric_scope,
        )
    rows = [{"date": row["date"], "value": row.get(metric)} for row in snapshots]
    return {
        "coalition_id": coalition_id,
        "metric": metric,
        "activity_window_days": window_days,
        "activity_mode": mode,
        "core_months": months,
        "step_days": step_days,
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# Population Intelligence · PvP population flows
# ---------------------------------------------------------------------------

_FLOW_SPELL_CACHE = OrderedDict()
_FLOW_SPELL_CACHE_LOCK = Lock()
_FLOW_SPELL_CACHE_TTL_SECONDS = 900
_FLOW_SPELL_CACHE_MAX = 8


def _flow_cache_get(key):
    now = monotonic()
    with _FLOW_SPELL_CACHE_LOCK:
        row = _FLOW_SPELL_CACHE.get(key)
        if not row:
            return None
        created_at, value = row
        if now - created_at > _FLOW_SPELL_CACHE_TTL_SECONDS:
            _FLOW_SPELL_CACHE.pop(key, None)
            return None
        _FLOW_SPELL_CACHE.move_to_end(key)
        return value


def _flow_cache_put(key, value):
    with _FLOW_SPELL_CACHE_LOCK:
        _FLOW_SPELL_CACHE[key] = (monotonic(), value)
        _FLOW_SPELL_CACHE.move_to_end(key)
        while len(_FLOW_SPELL_CACHE) > _FLOW_SPELL_CACHE_MAX:
            _FLOW_SPELL_CACHE.popitem(last=False)


def _flow_membership_spells(conn, alliance_id, analysis_end_day):
    key = (int(alliance_id), analysis_end_day.isoformat())
    cached = _flow_cache_get(key)
    if cached is not None:
        return cached
    value = _load_alliance_membership_spells(conn, alliance_id, analysis_end_day)
    _flow_cache_put(key, value)
    return value


def _normalize_flow_chunk_request(direction, start_date, end_date, analysis_start_date, analysis_end_date, activity_mode):
    direction = str(direction or "").strip().lower()
    if direction not in {"arrivals", "departures"}:
        raise ValueError("flow_direction_must_be_arrivals_or_departures")

    start_day = _coerce_date(start_date)
    end_day = _coerce_date(end_date)
    analysis_start_day = _coerce_date(analysis_start_date)
    analysis_end_day = _coerce_date(analysis_end_date)
    if analysis_start_day > analysis_end_day:
        raise ValueError("flow_analysis_start_after_end")
    if start_day < analysis_start_day:
        raise ValueError("flow_chunk_before_analysis_start")
    if start_day > end_day:
        raise ValueError("flow_start_after_end")
    if end_day > analysis_end_day:
        raise ValueError("flow_chunk_after_analysis_end")
    # The browser deliberately requests small chunks so a long historical view
    # can progress without a reverse-proxy timeout. Do not silently sample.
    if (end_day - start_day).days > 120:
        raise ValueError("flow_chunk_too_large_max_121_days")

    mode = str(activity_mode or "any").strip().lower()
    if mode not in {"any", "attacker", "loss"}:
        raise ValueError("activity_mode_must_be_any_attacker_or_loss")
    return direction, start_day, end_day, analysis_start_day, analysis_end_day, mode


def _load_character_corporation_timelines(conn, character_ids):
    ids = sorted({int(value) for value in character_ids if value is not None})
    if not ids:
        return {}, {}, set()

    timelines = {}
    names = {}
    corporation_ids = set()
    chunk_size = 10000
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '45000ms'")
        for offset in range(0, len(ids), chunk_size):
            chunk = ids[offset:offset + chunk_size]
            cur.execute(
                """
                SELECT
                    cch.character_id,
                    COALESCE(NULLIF(ch.name, ''), 'Unknown') AS character_name,
                    cch.corporation_id,
                    cch.start_date,
                    cch.end_date,
                    COALESCE(NULLIF(corp.name, ''), 'Unknown') AS corporation_name,
                    corp.ticker,
                    COALESCE(corp.is_npc, FALSE) AS is_npc
                FROM entities.character_corporation_history cch
                LEFT JOIN entities.characters ch
                  ON ch.character_id = cch.character_id
                LEFT JOIN entities.corporations corp
                  ON corp.corporation_id = cch.corporation_id
                WHERE cch.character_id = ANY(%s)
                  AND COALESCE(cch.is_deleted, FALSE) = FALSE
                ORDER BY cch.character_id, cch.start_date, cch.end_date NULLS LAST
                """,
                (chunk,),
            )
            for character_id, character_name, corporation_id, start_at, end_at, corp_name, corp_ticker, is_npc in cur.fetchall():
                cid = int(character_id)
                if getattr(start_at, "tzinfo", None) is None:
                    start_at = start_at.replace(tzinfo=UTC)
                if end_at is not None and getattr(end_at, "tzinfo", None) is None:
                    end_at = end_at.replace(tzinfo=UTC)
                record = {
                    "corporation_id": int(corporation_id) if corporation_id is not None else None,
                    "start_at": start_at,
                    "end_at": end_at,
                    "name": corp_name or "Unknown",
                    "ticker": corp_ticker,
                    "is_npc": bool(is_npc),
                }
                timelines.setdefault(cid, []).append(record)
                names[cid] = character_name or "Unknown"
                if corporation_id is not None:
                    corporation_ids.add(int(corporation_id))
    return timelines, names, corporation_ids


def _load_corporation_alliance_timelines(conn, corporation_ids):
    ids = sorted({int(value) for value in corporation_ids if value is not None})
    if not ids:
        return {}
    result = {}
    chunk_size = 10000
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '45000ms'")
        for offset in range(0, len(ids), chunk_size):
            chunk = ids[offset:offset + chunk_size]
            cur.execute(
                """
                SELECT
                    cah.corporation_id,
                    cah.alliance_id,
                    cah.start_date,
                    cah.end_date,
                    COALESCE(NULLIF(a.name, ''), 'Unknown') AS alliance_name,
                    a.ticker
                FROM entities.corporation_alliance_history cah
                LEFT JOIN entities.alliances a
                  ON a.alliance_id = cah.alliance_id
                WHERE cah.corporation_id = ANY(%s)
                  AND COALESCE(cah.is_deleted, FALSE) = FALSE
                ORDER BY cah.corporation_id, cah.start_date, cah.end_date NULLS LAST
                """,
                (chunk,),
            )
            for corporation_id, alliance_id, start_at, end_at, alliance_name, alliance_ticker in cur.fetchall():
                if getattr(start_at, "tzinfo", None) is None:
                    start_at = start_at.replace(tzinfo=UTC)
                if end_at is not None and getattr(end_at, "tzinfo", None) is None:
                    end_at = end_at.replace(tzinfo=UTC)
                result.setdefault(int(corporation_id), []).append({
                    "alliance_id": int(alliance_id) if alliance_id is not None else None,
                    "start_at": start_at,
                    "end_at": end_at,
                    "name": alliance_name or "Unknown",
                    "ticker": alliance_ticker,
                })
    return result


def _timeline_index_at(records, when):
    if not records:
        return None
    for idx, record in enumerate(records):
        end_at = record.get("end_at")
        if record["start_at"] <= when and (end_at is None or when < end_at):
            return idx
    return None


def _timeline_index_before(records, when):
    if not records:
        return None
    active = _timeline_index_at(records, when)
    if active is not None:
        return active
    best = None
    for idx, record in enumerate(records):
        if record["start_at"] < when:
            best = idx
        else:
            break
    return best


def _timeline_index_after(records, when):
    if not records:
        return None
    active = _timeline_index_at(records, when)
    if active is not None:
        return active
    for idx, record in enumerate(records):
        if record["start_at"] >= when:
            return idx
    return None


def _records_touch(left, right):
    left_end = left.get("end_at")
    if left_end is None:
        return True
    return right["start_at"] <= left_end + timedelta(seconds=1)


def _alliance_for_corporation_at(alliance_timelines, corporation_id, when):
    if corporation_id is None:
        return None
    for record in alliance_timelines.get(int(corporation_id), []):
        end_at = record.get("end_at")
        if record["start_at"] <= when and (end_at is None or when < end_at):
            if record.get("alliance_id") is None:
                return None
            return {
                "alliance_id": int(record["alliance_id"]),
                "name": record.get("name") or "Unknown",
                "ticker": record.get("ticker"),
            }
    return None


def _long_npc_gap(start_at, end_at):
    if start_at is None or end_at is None or end_at <= start_at:
        return False
    threshold_day = _shift_months(start_at.date(), 3)
    return end_at.date() >= threshold_day


def _flow_counterparty(direction, event_at, corporation_records, alliance_timelines):
    """Resolve the player corp/alliance on the other side of a membership move.

    NPC corporations are traversed. If the NPC passage lasts at least three
    calendar months, npc_gap is retained as context. If there is no later player
    corporation for a departure, the final NPC corporation is returned with the
    NPC flag instead of inventing a destination.
    """
    if not corporation_records:
        return None

    if direction == "arrivals":
        probe = event_at - timedelta(microseconds=1)
        idx = _timeline_index_before(corporation_records, probe)
        if idx is None:
            return None
        immediate = corporation_records[idx]
        if not immediate.get("is_npc"):
            alliance = _alliance_for_corporation_at(
                alliance_timelines,
                immediate.get("corporation_id"),
                probe,
            )
            return {
                "corporation": immediate,
                "alliance": alliance,
                "npc_gap": False,
                "npc_final": False,
            }

        # Walk backwards through a contiguous NPC chain.
        npc_chain_start = immediate["start_at"]
        j = idx
        while j > 0:
            previous = corporation_records[j - 1]
            current = corporation_records[j]
            if not previous.get("is_npc") or not _records_touch(previous, current):
                break
            npc_chain_start = min(npc_chain_start, previous["start_at"])
            j -= 1

        player_idx = j - 1
        if player_idx >= 0 and _records_touch(corporation_records[player_idx], corporation_records[j]):
            player_record = corporation_records[player_idx]
            player_end = player_record.get("end_at") or npc_chain_start
            source_probe = player_end - timedelta(microseconds=1)
            alliance = _alliance_for_corporation_at(
                alliance_timelines,
                player_record.get("corporation_id"),
                source_probe,
            )
            return {
                "corporation": player_record,
                "alliance": alliance,
                "npc_gap": _long_npc_gap(npc_chain_start, event_at),
                "npc_final": False,
            }

        # No earlier player corporation is known: keep the last NPC state as the
        # source rather than claiming a player corporation that we cannot prove.
        alliance = _alliance_for_corporation_at(
            alliance_timelines,
            immediate.get("corporation_id"),
            probe,
        )
        return {
            "corporation": immediate,
            "alliance": alliance,
            "npc_gap": _long_npc_gap(npc_chain_start, event_at),
            "npc_final": True,
        }

    # departures
    probe = event_at + timedelta(microseconds=1)
    idx = _timeline_index_after(corporation_records, probe)
    if idx is None:
        return None
    immediate = corporation_records[idx]
    if not immediate.get("is_npc"):
        alliance = _alliance_for_corporation_at(
            alliance_timelines,
            immediate.get("corporation_id"),
            probe,
        )
        return {
            "corporation": immediate,
            "alliance": alliance,
            "npc_gap": False,
            "npc_final": False,
        }

    j = idx
    last_npc = immediate
    while j + 1 < len(corporation_records):
        current = corporation_records[j]
        following = corporation_records[j + 1]
        if not _records_touch(current, following):
            break
        if not following.get("is_npc"):
            destination_probe = following["start_at"] + timedelta(microseconds=1)
            alliance = _alliance_for_corporation_at(
                alliance_timelines,
                following.get("corporation_id"),
                destination_probe,
            )
            return {
                "corporation": following,
                "alliance": alliance,
                "npc_gap": _long_npc_gap(event_at, following["start_at"]),
                "npc_final": False,
            }
        last_npc = following
        j += 1

    # Character is still in an NPC corporation (or no later player corporation
    # exists in our history). This is intentionally displayed as an NPC
    # destination, per the Population Flow UI rule.
    final_probe = last_npc["start_at"] + timedelta(microseconds=1)
    alliance = _alliance_for_corporation_at(
        alliance_timelines,
        last_npc.get("corporation_id"),
        final_probe,
    )
    return {
        "corporation": last_npc,
        "alliance": alliance,
        "npc_gap": False,
        "npc_final": True,
    }



def _load_flow_activity_flags(conn, windows, mode):
    """Return event keys with qualifying PvP inside their exact event window.

    Windows are event-specific so a movement can be counted separately from
    whether the character actually PvPed after an arrival or before a departure
    during the selected analysis period. Nothing is persisted.
    """
    normalized = []
    for event_key, character_id, start_at, end_at in windows:
        if start_at is None or end_at is None or start_at >= end_at:
            continue
        normalized.append((str(event_key), int(character_id), start_at, end_at))
    if not normalized:
        return set()

    result = set()
    chunk_size = 500
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '45000ms'")
        for offset in range(0, len(normalized), chunk_size):
            chunk = normalized[offset:offset + chunk_size]
            event_keys = [row[0] for row in chunk]
            ids = [row[1] for row in chunk]
            starts = [row[2] for row in chunk]
            ends = [row[3] for row in chunk]

            if mode == "attacker":
                predicate = """
                    EXISTS (
                        SELECT 1
                        FROM rawkm.killmail_attackers ka
                        WHERE ka.character_id = c.character_id
                          AND ka.killmail_time >= c.window_start
                          AND ka.killmail_time < c.window_end
                    )
                """
            elif mode == "loss":
                predicate = """
                    EXISTS (
                        SELECT 1
                        FROM rawkm.killmails km
                        WHERE km.victim_character_id = c.character_id
                          AND km.killmail_time >= c.window_start
                          AND km.killmail_time < c.window_end
                    )
                    AND NOT EXISTS (
                        SELECT 1
                        FROM rawkm.killmail_attackers ka
                        WHERE ka.character_id = c.character_id
                          AND ka.killmail_time >= c.window_start
                          AND ka.killmail_time < c.window_end
                    )
                """
            else:
                predicate = """
                    EXISTS (
                        SELECT 1
                        FROM rawkm.killmails km
                        WHERE km.victim_character_id = c.character_id
                          AND km.killmail_time >= c.window_start
                          AND km.killmail_time < c.window_end
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM rawkm.killmail_attackers ka
                        WHERE ka.character_id = c.character_id
                          AND ka.killmail_time >= c.window_start
                          AND ka.killmail_time < c.window_end
                    )
                """

            cur.execute(
                """
                WITH candidates AS (
                    SELECT *
                    FROM unnest(
                        %s::text[],
                        %s::bigint[],
                        %s::timestamptz[],
                        %s::timestamptz[]
                    ) AS t(event_key, character_id, window_start, window_end)
                )
                SELECT c.event_key
                FROM candidates c
                WHERE """ + predicate,
                (event_keys, ids, starts, ends),
            )
            result.update(row[0] for row in cur.fetchall())
    return result

def get_alliance_population_flow_chunk(
    alliance_id,
    direction,
    start_date,
    end_date,
    analysis_start_date,
    analysis_end_date,
    activity_mode="any",
):
    alliance_id = int(alliance_id)
    direction, start_day, end_day, analysis_start_day, analysis_end_day, mode = _normalize_flow_chunk_request(
        direction, start_date, end_date, analysis_start_date, analysis_end_date, activity_mode
    )

    with db() as conn:
        _spells_by_character, all_spells = _flow_membership_spells(
            conn, alliance_id, analysis_end_day
        )

        first_spell_start_by_character = {}
        for character_id, spell_start, _spell_end in all_spells:
            character_id = int(character_id)
            current = first_spell_start_by_character.get(character_id)
            if current is None or spell_start < current:
                first_spell_start_by_character[character_id] = spell_start

        candidates = []
        seed_rows = []
        for character_id, spell_start, spell_end in all_spells:
            if direction == "arrivals":
                movement_at = spell_start
                movement_day = movement_at.date()
                if not (start_day <= movement_day <= end_day):
                    continue
            else:
                if spell_end.year >= 9999:
                    continue
                movement_at = spell_end
                movement_day = movement_at.date()
                if not (start_day <= movement_day <= end_day):
                    continue

            candidates.append((int(character_id), spell_start, spell_end))
            seed_rows.append((int(character_id), spell_start, spell_end, movement_at))

        if not candidates:
            return {
                "alliance_id": alliance_id,
                "direction": direction,
                "start_date": start_day.isoformat(),
                "end_date": end_day.isoformat(),
                "analysis_start_date": analysis_start_day.isoformat(),
                "analysis_end_date": analysis_end_day.isoformat(),
                "activity_mode": mode,
                "events": [],
            }

        # "Movements" is the movement count for characters already known to
        # have qualifying PvP while belonging to this alliance at least once by
        # the end of the analysis. It is deliberately NOT the count of active
        # joiners/leavers in the selected period. The extra PvP column below
        # answers that separate question.
        movement_character_ids = {row[0] for row in seed_rows}
        known_spell_candidates = []
        for character_id in movement_character_ids:
            for known_start, known_end in _spells_by_character.get(character_id, []):
                known_spell_candidates.append((character_id, known_start, known_end))
        known_activity = _load_first_activity_for_spells(
            conn, known_spell_candidates, analysis_end_day, mode
        )
        known_character_ids = {character_id for character_id, _spell_start in known_activity}
        qualified = [row for row in seed_rows if row[0] in known_character_ids]

        analysis_start_ts = datetime.combine(analysis_start_day, time.min, tzinfo=UTC)
        analysis_end_ts = datetime.combine(analysis_end_day + timedelta(days=1), time.min, tzinfo=UTC)
        activity_windows = []
        for character_id, spell_start, spell_end, movement_at in qualified:
            event_key = f"{direction}:{character_id}:{movement_at.isoformat()}"
            if direction == "arrivals":
                window_start = movement_at
                window_end = analysis_end_ts if spell_end.year >= 9999 or spell_end > analysis_end_ts else spell_end
            else:
                window_start = max(spell_start, analysis_start_ts)
                window_end = movement_at
            if window_start < window_end:
                activity_windows.append((event_key, character_id, window_start, window_end))

        period_active_event_keys = _load_flow_activity_flags(conn, activity_windows, mode)

        character_ids = {row[0] for row in qualified}
        timelines, names, corporation_ids = _load_character_corporation_timelines(
            conn, character_ids
        )
        alliance_timelines = _load_corporation_alliance_timelines(conn, corporation_ids)

    events = []
    for character_id, spell_start, spell_end, movement_at in qualified:
        counterparty = _flow_counterparty(
            direction,
            movement_at,
            timelines.get(character_id, []),
            alliance_timelines,
        )
        corporation = counterparty.get("corporation") if counterparty else None
        alliance = counterparty.get("alliance") if counterparty else None
        first_spell_start = first_spell_start_by_character.get(int(character_id), spell_start)
        is_rejoin = direction == "arrivals" and spell_start > first_spell_start
        is_internal_move = bool(
            alliance
            and alliance.get("alliance_id") is not None
            and int(alliance["alliance_id"]) == alliance_id
        )
        events.append({
            "event_key": f"{direction}:{character_id}:{movement_at.isoformat()}",
            "period_pvp": f"{direction}:{character_id}:{movement_at.isoformat()}" in period_active_event_keys,
            "first_join": bool(direction == "arrivals" and not is_rejoin),
            "rejoin": bool(is_rejoin),
            "internal_move": is_internal_move,
            "character_id": int(character_id),
            "character_name": names.get(character_id) or "Unknown",
            "movement_date": movement_at.date().isoformat(),
            "corporation": ({
                "corporation_id": int(corporation["corporation_id"]),
                "name": corporation.get("name") or "Unknown",
                "ticker": corporation.get("ticker"),
                "is_npc": bool(corporation.get("is_npc")),
            } if corporation and corporation.get("corporation_id") is not None else None),
            "alliance": ({
                "alliance_id": int(alliance["alliance_id"]),
                "name": alliance.get("name") or "Unknown",
                "ticker": alliance.get("ticker"),
            } if alliance and alliance.get("alliance_id") is not None else None),
            "npc_gap": bool(counterparty and counterparty.get("npc_gap")),
            "npc_final": bool(counterparty and counterparty.get("npc_final")),
        })

    events.sort(key=lambda row: (row["movement_date"], row["character_name"].lower(), row["character_id"]))
    return {
        "alliance_id": alliance_id,
        "direction": direction,
        "start_date": start_day.isoformat(),
        "end_date": end_day.isoformat(),
        "analysis_start_date": analysis_start_day.isoformat(),
        "analysis_end_date": analysis_end_day.isoformat(),
        "activity_mode": mode,
        "events": events,
    }


def _flow_corporation_membership_spells(conn, corporation_id, analysis_end_day):
    key = ("corporation", int(corporation_id), analysis_end_day.isoformat())
    cached = _flow_cache_get(key)
    if cached is not None:
        return cached
    value = _load_corporation_membership_spells(conn, corporation_id, analysis_end_day)
    _flow_cache_put(key, value)
    return value


def get_corporation_population_flow_chunk(
    corporation_id,
    direction,
    start_date,
    end_date,
    analysis_start_date,
    analysis_end_date,
    activity_mode="any",
):
    corporation_id = int(corporation_id)
    direction, start_day, end_day, analysis_start_day, analysis_end_day, mode = _normalize_flow_chunk_request(
        direction, start_date, end_date, analysis_start_date, analysis_end_date, activity_mode
    )

    with db() as conn:
        _spells_by_character, all_spells = _flow_corporation_membership_spells(
            conn, corporation_id, analysis_end_day
        )

        first_spell_start_by_character = {}
        for character_id, spell_start, _spell_end in all_spells:
            character_id = int(character_id)
            current = first_spell_start_by_character.get(character_id)
            if current is None or spell_start < current:
                first_spell_start_by_character[character_id] = spell_start

        candidates = []
        seed_rows = []
        for character_id, spell_start, spell_end in all_spells:
            if direction == "arrivals":
                movement_at = spell_start
                if not (start_day <= movement_at.date() <= end_day):
                    continue
            else:
                if spell_end.year >= 9999:
                    continue
                movement_at = spell_end
                if not (start_day <= movement_at.date() <= end_day):
                    continue
            candidates.append((int(character_id), spell_start, spell_end))
            seed_rows.append((int(character_id), spell_start, spell_end, movement_at))

        if not candidates:
            return {
                "corporation_id": corporation_id,
                "direction": direction,
                "start_date": start_day.isoformat(),
                "end_date": end_day.isoformat(),
                "analysis_start_date": analysis_start_day.isoformat(),
                "analysis_end_date": analysis_end_day.isoformat(),
                "activity_mode": mode,
                "events": [],
            }

        movement_character_ids = {row[0] for row in seed_rows}
        known_spell_candidates = []
        for character_id in movement_character_ids:
            for known_start, known_end in _spells_by_character.get(character_id, []):
                known_spell_candidates.append((character_id, known_start, known_end))
        known_activity = _load_first_activity_for_spells(
            conn, known_spell_candidates, analysis_end_day, mode
        )
        known_character_ids = {character_id for character_id, _spell_start in known_activity}
        qualified = [row for row in seed_rows if row[0] in known_character_ids]

        analysis_start_ts = datetime.combine(analysis_start_day, time.min, tzinfo=UTC)
        analysis_end_ts = datetime.combine(analysis_end_day + timedelta(days=1), time.min, tzinfo=UTC)
        activity_windows = []
        for character_id, spell_start, spell_end, movement_at in qualified:
            event_key = f"corp:{corporation_id}:{direction}:{character_id}:{movement_at.isoformat()}"
            if direction == "arrivals":
                window_start = movement_at
                window_end = analysis_end_ts if spell_end.year >= 9999 or spell_end > analysis_end_ts else spell_end
            else:
                window_start = max(spell_start, analysis_start_ts)
                window_end = movement_at
            if window_start < window_end:
                activity_windows.append((event_key, character_id, window_start, window_end))

        period_active_event_keys = _load_flow_activity_flags(conn, activity_windows, mode)

        character_ids = {row[0] for row in qualified}
        timelines, names, corporation_ids = _load_character_corporation_timelines(conn, character_ids)
        corporation_ids.add(corporation_id)
        alliance_timelines = _load_corporation_alliance_timelines(conn, corporation_ids)

    events = []
    for character_id, spell_start, spell_end, movement_at in qualified:
        counterparty = _flow_counterparty(
            direction,
            movement_at,
            timelines.get(character_id, []),
            alliance_timelines,
        )
        corporation = counterparty.get("corporation") if counterparty else None
        alliance = counterparty.get("alliance") if counterparty else None
        first_spell_start = first_spell_start_by_character.get(int(character_id), spell_start)
        is_rejoin = direction == "arrivals" and spell_start > first_spell_start

        target_probe = (
            movement_at + timedelta(microseconds=1)
            if direction == "arrivals"
            else movement_at - timedelta(microseconds=1)
        )
        target_alliance = _alliance_for_corporation_at(
            alliance_timelines, corporation_id, target_probe
        )
        is_internal_move = bool(
            alliance
            and target_alliance
            and alliance.get("alliance_id") is not None
            and target_alliance.get("alliance_id") is not None
            and int(alliance["alliance_id"]) == int(target_alliance["alliance_id"])
        )

        event_key = f"corp:{corporation_id}:{direction}:{character_id}:{movement_at.isoformat()}"
        events.append({
            "event_key": event_key,
            "period_pvp": event_key in period_active_event_keys,
            "first_join": bool(direction == "arrivals" and not is_rejoin),
            "rejoin": bool(is_rejoin),
            "internal_move": is_internal_move,
            "character_id": int(character_id),
            "character_name": names.get(character_id) or "Unknown",
            "movement_date": movement_at.date().isoformat(),
            "corporation": ({
                "corporation_id": int(corporation["corporation_id"]),
                "name": corporation.get("name") or "Unknown",
                "ticker": corporation.get("ticker"),
                "is_npc": bool(corporation.get("is_npc")),
            } if corporation and corporation.get("corporation_id") is not None else None),
            "alliance": ({
                "alliance_id": int(alliance["alliance_id"]),
                "name": alliance.get("name") or "Unknown",
                "ticker": alliance.get("ticker"),
            } if alliance and alliance.get("alliance_id") is not None else None),
            "npc_gap": bool(counterparty and counterparty.get("npc_gap")),
            "npc_final": bool(counterparty and counterparty.get("npc_final")),
        })

    events.sort(key=lambda row: (row["movement_date"], row["character_name"].lower(), row["character_id"]))
    return {
        "corporation_id": corporation_id,
        "direction": direction,
        "start_date": start_day.isoformat(),
        "end_date": end_day.isoformat(),
        "analysis_start_date": analysis_start_day.isoformat(),
        "analysis_end_date": analysis_end_day.isoformat(),
        "activity_mode": mode,
        "events": events,
    }


def get_coalition_population_flow_chunk(
    coalition_id,
    direction,
    start_date,
    end_date,
    analysis_start_date,
    analysis_end_date,
    activity_mode="any",
):
    """Population-flow chunk across the coalition boundary.

    Coalition membership spells are merged across member alliances/corporations,
    so an internal transfer never becomes a fake coalition departure+arrival.
    """
    coalition_id = int(coalition_id)
    direction, start_day, end_day, analysis_start_day, analysis_end_day, mode = _normalize_flow_chunk_request(
        direction, start_date, end_date, analysis_start_date, analysis_end_date, activity_mode
    )

    with db() as conn:
        spells_by_character, all_spells = _load_coalition_membership_spells(
            conn, coalition_id, analysis_end_day
        )

        first_spell_start_by_character = {}
        for character_id, spell_start, _spell_end in all_spells:
            character_id = int(character_id)
            current = first_spell_start_by_character.get(character_id)
            if current is None or spell_start < current:
                first_spell_start_by_character[character_id] = spell_start

        seed_rows = []
        for character_id, spell_start, spell_end in all_spells:
            if direction == "arrivals":
                movement_at = spell_start
                if not (start_day <= movement_at.date() <= end_day):
                    continue
            else:
                if spell_end.year >= 9999:
                    continue
                movement_at = spell_end
                if not (start_day <= movement_at.date() <= end_day):
                    continue
            seed_rows.append((int(character_id), spell_start, spell_end, movement_at))

        if not seed_rows:
            return {
                "coalition_id": coalition_id,
                "direction": direction,
                "start_date": start_day.isoformat(),
                "end_date": end_day.isoformat(),
                "analysis_start_date": analysis_start_day.isoformat(),
                "analysis_end_date": analysis_end_day.isoformat(),
                "activity_mode": mode,
                "events": [],
            }

        movement_character_ids = {row[0] for row in seed_rows}
        known_spell_candidates = []
        for character_id in movement_character_ids:
            for known_start, known_end in spells_by_character.get(character_id, []):
                known_spell_candidates.append((character_id, known_start, known_end))
        known_activity = _load_first_activity_for_spells(
            conn, known_spell_candidates, analysis_end_day, mode
        )
        known_character_ids = {character_id for character_id, _spell_start in known_activity}
        qualified = [row for row in seed_rows if row[0] in known_character_ids]

        analysis_start_ts = datetime.combine(analysis_start_day, time.min, tzinfo=UTC)
        analysis_end_ts = datetime.combine(analysis_end_day + timedelta(days=1), time.min, tzinfo=UTC)
        activity_windows = []
        for character_id, spell_start, spell_end, movement_at in qualified:
            event_key = f"coalition:{coalition_id}:{direction}:{character_id}:{movement_at.isoformat()}"
            if direction == "arrivals":
                window_start = movement_at
                window_end = analysis_end_ts if spell_end.year >= 9999 or spell_end > analysis_end_ts else spell_end
            else:
                window_start = max(spell_start, analysis_start_ts)
                window_end = movement_at
            if window_start < window_end:
                activity_windows.append((event_key, character_id, window_start, window_end))

        period_active_event_keys = _load_flow_activity_flags(conn, activity_windows, mode)
        character_ids = {row[0] for row in qualified}
        timelines, names, corporation_ids = _load_character_corporation_timelines(conn, character_ids)
        alliance_timelines = _load_corporation_alliance_timelines(conn, corporation_ids)

    events = []
    for character_id, spell_start, spell_end, movement_at in qualified:
        counterparty = _flow_counterparty(
            direction,
            movement_at,
            timelines.get(character_id, []),
            alliance_timelines,
        )
        corporation = counterparty.get("corporation") if counterparty else None
        alliance = counterparty.get("alliance") if counterparty else None
        first_spell_start = first_spell_start_by_character.get(int(character_id), spell_start)
        is_rejoin = direction == "arrivals" and spell_start > first_spell_start
        event_key = f"coalition:{coalition_id}:{direction}:{character_id}:{movement_at.isoformat()}"
        events.append({
            "event_key": event_key,
            "period_pvp": event_key in period_active_event_keys,
            "first_join": bool(direction == "arrivals" and not is_rejoin),
            "rejoin": bool(is_rejoin),
            # Coalition spells are merged across internal member transfers.
            # Therefore every event here crosses the coalition boundary.
            "internal_move": False,
            "character_id": int(character_id),
            "character_name": names.get(character_id) or "Unknown",
            "movement_date": movement_at.date().isoformat(),
            "corporation": ({
                "corporation_id": int(corporation["corporation_id"]),
                "name": corporation.get("name") or "Unknown",
                "ticker": corporation.get("ticker"),
                "is_npc": bool(corporation.get("is_npc")),
            } if corporation and corporation.get("corporation_id") is not None else None),
            "alliance": ({
                "alliance_id": int(alliance["alliance_id"]),
                "name": alliance.get("name") or "Unknown",
                "ticker": alliance.get("ticker"),
            } if alliance and alliance.get("alliance_id") is not None else None),
            "npc_gap": bool(counterparty and counterparty.get("npc_gap")),
            "npc_final": bool(counterparty and counterparty.get("npc_final")),
        })

    events.sort(key=lambda row: (row["movement_date"], row["character_name"].lower(), row["character_id"]))
    return {
        "coalition_id": coalition_id,
        "direction": direction,
        "start_date": start_day.isoformat(),
        "end_date": end_day.isoformat(),
        "analysis_start_date": analysis_start_day.isoformat(),
        "analysis_end_date": analysis_end_day.isoformat(),
        "activity_mode": mode,
        "events": events,
    }


# ---------------------------------------------------------------------------
# Coalition affiliation provenance / destinations
# ---------------------------------------------------------------------------

_AFFILIATION_FLOW_DEFAULT_DAYS = 365
_AFFILIATION_FLOW_MAX_ROWS = 250
_AFFILIATION_FLOW_MAX_SERIES = 12


def _normalize_affiliation_flow_dates(start_date=None, end_date=None):
    today = date.today()
    end_day = _coerce_date(end_date) if end_date else today
    if end_day > today:
        end_day = today
    if start_date:
        start_day = _coerce_date(start_date)
    else:
        start_day = end_day - timedelta(days=_AFFILIATION_FLOW_DEFAULT_DAYS - 1)
    if start_day < _COALITION_POP_FLOOR:
        start_day = _COALITION_POP_FLOOR
    if start_day > end_day:
        raise ValueError("affiliation_flow_start_after_end")
    return start_day, end_day


def _affiliation_entity_key(entity_type, entity_id):
    return (str(entity_type or "").lower(), int(entity_id))


def _affiliation_image_url(entity_type, entity_id):
    entity_type = str(entity_type or "").lower()
    entity_id = int(entity_id)
    if entity_type == "corporation":
        return f"https://images.evetech.net/corporations/{entity_id}/logo?size=64"
    if entity_type == "alliance":
        return f"https://images.evetech.net/alliances/{entity_id}/logo?size=64"
    if entity_type == "coalition":
        return f"/coalition/{entity_id}/logo"
    return None


def _load_affiliation_coalition_intervals(conn, wanted_entities, start_day, end_day):
    """Map corp/alliance entities to every coalition containing them over time.

    Coalition membership rules can nest coalitions.  The resolver deliberately
    uses the same include/exclude semantics as Coalition Population so a pilot
    can be attributed to a historical coalition only while that underlying
    corporation/alliance was actually in its resolved scope.
    """
    wanted = {
        (str(entity_type).lower(), int(entity_id))
        for entity_type, entity_id in (wanted_entities or set())
        if str(entity_type).lower() in {"corporation", "alliance"}
    }
    if not wanted:
        return {}, {}

    if not _table_exists(conn, "entities", "coalition_memberships"):
        return {}, {}

    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '30000ms'")
        cur.execute(
            """
            SELECT coalition_id, COALESCE(NULLIF(name, ''), 'Unknown coalition'), short_name
            FROM entities.coalitions
            ORDER BY coalition_id
            """
        )
        coalition_rows = cur.fetchall()
        cur.execute(
            """
            SELECT coalition_id, lower(operation), lower(member_type), member_id, valid_from, valid_to
            FROM entities.coalition_memberships
            ORDER BY coalition_id, id
            """
        )
        membership_rows = cur.fetchall()

    coalition_meta = {
        int(coalition_id): {
            "entity_type": "coalition",
            "entity_id": int(coalition_id),
            "name": name or "Unknown coalition",
            "ticker": ticker,
            "image_url": _affiliation_image_url("coalition", coalition_id),
            "is_npc": False,
        }
        for coalition_id, name, ticker in coalition_rows
    }

    rules_by_coalition = {}
    nested_by_coalition = {}
    for parent_id, operation, member_type, member_id, valid_from, valid_to in membership_rows:
        parent_id = int(parent_id)
        member_type = str(member_type or "").lower()
        member_id = int(member_id)
        rules_by_coalition.setdefault(parent_id, []).append({
            "operation": str(operation or "include").lower(),
            "member_type": member_type,
            "member_id": member_id,
            "valid_from": valid_from,
            "valid_to": valid_to,
        })
        if member_type == "coalition":
            nested_by_coalition.setdefault(parent_id, set()).add(member_id)

    def reachable_ids(root_id):
        seen = set()
        stack = [int(root_id)]
        while stack:
            current = int(stack.pop())
            if current in seen:
                continue
            seen.add(current)
            stack.extend(nested_by_coalition.get(current, ()))
        return seen

    entity_map = {}
    end_boundary = end_day + timedelta(days=1)
    for root_id in sorted(rules_by_coalition):
        relevant_coalitions = reachable_ids(root_id)
        boundaries = {start_day, end_boundary}
        for coalition_id in relevant_coalitions:
            for rule in rules_by_coalition.get(coalition_id, []):
                valid_from = rule.get("valid_from")
                valid_to = rule.get("valid_to")
                if valid_from is not None and start_day < valid_from < end_boundary:
                    boundaries.add(valid_from)
                if valid_to is not None:
                    after = valid_to + timedelta(days=1)
                    if start_day < after < end_boundary:
                        boundaries.add(after)

        ordered = sorted(boundaries)
        for idx in range(len(ordered) - 1):
            segment_start = ordered[idx]
            segment_end = ordered[idx + 1]
            if segment_start >= segment_end:
                continue
            scope = _coalition_scope_at(rules_by_coalition, root_id, segment_start)
            matching = scope & wanted
            if not matching:
                continue
            start_at = datetime.combine(segment_start, time.min, tzinfo=UTC)
            end_at = datetime.combine(segment_end, time.min, tzinfo=UTC)
            for entity_key in matching:
                rows = entity_map.setdefault(entity_key, [])
                if rows and rows[-1][0] == int(root_id) and rows[-1][2] == start_at:
                    rows[-1] = (int(root_id), rows[-1][1], end_at)
                else:
                    rows.append((int(root_id), start_at, end_at))

    return entity_map, coalition_meta


def _affiliation_period_overlap(start_at, end_at, other_start, other_end):
    return start_at < other_end and other_start < end_at


def _historical_affiliations_before(
    character_id,
    cutoff_at,
    character_timelines,
    alliance_timelines,
    coalition_intervals,
    entity_meta,
    target_coalition_id,
):
    records = character_timelines.get(int(character_id), [])
    keys = set()
    entity_periods = []

    for corp in records:
        start_at = corp["start_at"]
        if start_at >= cutoff_at:
            break
        raw_end = corp.get("end_at")
        end_at = cutoff_at if raw_end is None or raw_end > cutoff_at else raw_end
        if start_at >= end_at:
            continue

        corporation_id = corp.get("corporation_id")
        if corporation_id is not None:
            corp_key = _affiliation_entity_key("corporation", corporation_id)
            keys.add(corp_key)
            entity_periods.append((corp_key, start_at, end_at))
            entity_meta.setdefault(corp_key, {
                "entity_type": "corporation",
                "entity_id": int(corporation_id),
                "name": corp.get("name") or "Unknown",
                "ticker": corp.get("ticker"),
                "image_url": _affiliation_image_url("corporation", corporation_id),
                "is_npc": bool(corp.get("is_npc")),
            })

            for alliance in alliance_timelines.get(int(corporation_id), []):
                alliance_id = alliance.get("alliance_id")
                if alliance_id is None:
                    continue
                alliance_start = max(start_at, alliance["start_at"])
                alliance_raw_end = alliance.get("end_at")
                alliance_end = end_at if alliance_raw_end is None or alliance_raw_end > end_at else alliance_raw_end
                if alliance_start >= alliance_end:
                    continue
                alliance_key = _affiliation_entity_key("alliance", alliance_id)
                keys.add(alliance_key)
                entity_periods.append((alliance_key, alliance_start, alliance_end))
                entity_meta.setdefault(alliance_key, {
                    "entity_type": "alliance",
                    "entity_id": int(alliance_id),
                    "name": alliance.get("name") or "Unknown",
                    "ticker": alliance.get("ticker"),
                    "image_url": _affiliation_image_url("alliance", alliance_id),
                    "is_npc": False,
                })

    for entity_key, period_start, period_end in entity_periods:
        for coalition_id, coalition_start, coalition_end in coalition_intervals.get(entity_key, []):
            if int(coalition_id) == int(target_coalition_id):
                continue
            if _affiliation_period_overlap(period_start, period_end, coalition_start, coalition_end):
                keys.add(_affiliation_entity_key("coalition", coalition_id))

    return keys


def _current_affiliations(
    character_id,
    when,
    character_timelines,
    alliance_timelines,
    coalition_intervals,
    entity_meta,
):
    records = character_timelines.get(int(character_id), [])
    index = _timeline_index_at(records, when)
    if index is None:
        return set(), {"corporation": None, "alliance": None, "coalitions": []}

    corp = records[index]
    corporation_id = corp.get("corporation_id")
    keys = set()
    detail = {"corporation": None, "alliance": None, "coalitions": []}
    probe_end = when + timedelta(microseconds=1)

    if corporation_id is not None:
        corp_key = _affiliation_entity_key("corporation", corporation_id)
        keys.add(corp_key)
        entity_meta.setdefault(corp_key, {
            "entity_type": "corporation",
            "entity_id": int(corporation_id),
            "name": corp.get("name") or "Unknown",
            "ticker": corp.get("ticker"),
            "image_url": _affiliation_image_url("corporation", corporation_id),
            "is_npc": bool(corp.get("is_npc")),
        })
        detail["corporation"] = entity_meta[corp_key]

        alliance = _alliance_for_corporation_at(alliance_timelines, corporation_id, when)
        if alliance and alliance.get("alliance_id") is not None:
            alliance_id = int(alliance["alliance_id"])
            alliance_key = _affiliation_entity_key("alliance", alliance_id)
            keys.add(alliance_key)
            entity_meta.setdefault(alliance_key, {
                "entity_type": "alliance",
                "entity_id": alliance_id,
                "name": alliance.get("name") or "Unknown",
                "ticker": alliance.get("ticker"),
                "image_url": _affiliation_image_url("alliance", alliance_id),
                "is_npc": False,
            })
            detail["alliance"] = entity_meta[alliance_key]

        coalition_ids = set()
        for entity_key in list(keys):
            if entity_key[0] not in {"corporation", "alliance"}:
                continue
            for coalition_id, coalition_start, coalition_end in coalition_intervals.get(entity_key, []):
                if _affiliation_period_overlap(when, probe_end, coalition_start, coalition_end):
                    coalition_ids.add(int(coalition_id))
        for coalition_id in sorted(coalition_ids):
            coalition_key = _affiliation_entity_key("coalition", coalition_id)
            keys.add(coalition_key)
            if coalition_key in entity_meta:
                detail["coalitions"].append(entity_meta[coalition_key])

    return keys, detail


def _affiliation_rows(pilot_sets, entity_meta, denominator):
    rows = []
    denominator = int(denominator or 0)
    for key, pilots in pilot_sets.items():
        meta = entity_meta.get(key, {
            "entity_type": key[0],
            "entity_id": key[1],
            "name": "Unknown",
            "ticker": None,
            "image_url": None,
            "is_npc": False,
        })
        count = len(pilots)
        rows.append({
            **meta,
            "pilot_count": count,
            "share_pct": round((count * 100.0 / denominator), 2) if denominator else None,
        })
    rows.sort(key=lambda row: (-int(row["pilot_count"]), str(row.get("name") or "").casefold(), row["entity_type"], row["entity_id"]))
    return rows[:_AFFILIATION_FLOW_MAX_ROWS]


def _coalition_affiliation_flow_analysis(coalition_id, start_date=None, end_date=None):
    coalition_id = int(coalition_id)
    start_day, end_day = _normalize_affiliation_flow_dates(start_date, end_date)
    cache_key = ("coalition_affiliation_flow", coalition_id, start_day.isoformat(), end_day.isoformat())
    cached = _coalition_pop_cache_get(cache_key)
    if cached is not None:
        return cached

    today = date.today()
    now_ts = _at_end_of_day(today)

    with db() as conn:
        spells_by_character, all_spells = _load_coalition_membership_spells(conn, coalition_id, today)
        current_state = _member_state(spells_by_character, today)
        current_member_ids = set(int(value) for value in current_state)

        arrival_events = []
        departure_events = []
        for character_id, spell_start, spell_end in all_spells:
            character_id = int(character_id)
            if start_day <= spell_start.date() <= end_day:
                arrival_events.append((character_id, spell_start))
            if spell_end.year < 9999 and start_day <= spell_end.date() <= end_day:
                departure_events.append((character_id, spell_end))

        relevant_character_ids = current_member_ids | {row[0] for row in arrival_events} | {row[0] for row in departure_events}
        character_timelines, names, corporation_ids = _load_character_corporation_timelines(conn, relevant_character_ids)
        alliance_timelines = _load_corporation_alliance_timelines(conn, corporation_ids)

        wanted_entities = set()
        entity_meta = {}
        earliest_day = today
        for records in character_timelines.values():
            for corp in records:
                corporation_id = corp.get("corporation_id")
                if corporation_id is not None:
                    key = _affiliation_entity_key("corporation", corporation_id)
                    wanted_entities.add(key)
                    entity_meta.setdefault(key, {
                        "entity_type": "corporation",
                        "entity_id": int(corporation_id),
                        "name": corp.get("name") or "Unknown",
                        "ticker": corp.get("ticker"),
                        "image_url": _affiliation_image_url("corporation", corporation_id),
                        "is_npc": bool(corp.get("is_npc")),
                    })
                if corp.get("start_at") is not None:
                    earliest_day = min(earliest_day, corp["start_at"].date())
        for records in alliance_timelines.values():
            for alliance in records:
                alliance_id = alliance.get("alliance_id")
                if alliance_id is None:
                    continue
                key = _affiliation_entity_key("alliance", alliance_id)
                wanted_entities.add(key)
                entity_meta.setdefault(key, {
                    "entity_type": "alliance",
                    "entity_id": int(alliance_id),
                    "name": alliance.get("name") or "Unknown",
                    "ticker": alliance.get("ticker"),
                    "image_url": _affiliation_image_url("alliance", alliance_id),
                    "is_npc": False,
                })
                if alliance.get("start_at") is not None:
                    earliest_day = min(earliest_day, alliance["start_at"].date())

        coalition_intervals, coalition_meta_by_id = _load_affiliation_coalition_intervals(
            conn,
            wanted_entities,
            max(_COALITION_POP_FLOOR, earliest_day),
            today,
        )
        for coalition_meta in coalition_meta_by_id.values():
            key = _affiliation_entity_key("coalition", coalition_meta["entity_id"])
            entity_meta[key] = coalition_meta

    origin_current_sets = {}
    origin_join_date_by_character = {}
    current_detail_by_character = {}
    history_cache = {}

    def historical_keys(character_id, cutoff_at):
        cache_key = (int(character_id), cutoff_at)
        if cache_key not in history_cache:
            history_cache[cache_key] = _historical_affiliations_before(
                character_id,
                cutoff_at,
                character_timelines,
                alliance_timelines,
                coalition_intervals,
                entity_meta,
                coalition_id,
            )
        return history_cache[cache_key]

    unknown_origin_key = ("unknown", -1)
    entity_meta[unknown_origin_key] = {
        "entity_type": "unknown",
        "entity_id": -1,
        "name": "No prior affiliation found",
        "ticker": None,
        "image_url": None,
        "is_npc": False,
    }

    for character_id, spell in current_state.items():
        character_id = int(character_id)
        cutoff_at = spell[0]
        origin_join_date_by_character[character_id] = cutoff_at.date()
        keys = historical_keys(character_id, cutoff_at)
        if not keys:
            keys = {unknown_origin_key}
        for key in keys:
            origin_current_sets.setdefault(key, set()).add(character_id)

    incoming_period_sets = {}
    incoming_events_by_entity = {}
    for character_id, movement_at in arrival_events:
        keys = historical_keys(character_id, movement_at)
        if not keys:
            keys = {unknown_origin_key}
        for key in keys:
            incoming_period_sets.setdefault(key, set()).add(character_id)
            incoming_events_by_entity.setdefault(key, []).append((movement_at.date(), character_id))

    # Resolve present-day affiliations once for every pilot needed by either
    # drill-down table.  This also provides destinations for historical leavers.
    destination_sets = {}
    destination_events_by_entity = {}
    unknown_destination_key = ("unknown", -2)
    entity_meta[unknown_destination_key] = {
        "entity_type": "unknown",
        "entity_id": -2,
        "name": "No current affiliation found",
        "ticker": None,
        "image_url": None,
        "is_npc": False,
    }

    all_detail_ids = current_member_ids | {row[0] for row in arrival_events} | {row[0] for row in departure_events}
    for character_id in all_detail_ids:
        keys, detail = _current_affiliations(
            character_id,
            now_ts,
            character_timelines,
            alliance_timelines,
            coalition_intervals,
            entity_meta,
        )
        current_detail_by_character[int(character_id)] = detail

    departures_by_character = {}
    for character_id, movement_at in departure_events:
        departures_by_character.setdefault(int(character_id), []).append(movement_at.date())

    for character_id, movement_dates in departures_by_character.items():
        keys, _detail = _current_affiliations(
            character_id,
            now_ts,
            character_timelines,
            alliance_timelines,
            coalition_intervals,
            entity_meta,
        )
        if not keys:
            keys = {unknown_destination_key}
        for key in keys:
            destination_sets.setdefault(key, set()).add(character_id)
            for movement_day in movement_dates:
                destination_events_by_entity.setdefault(key, []).append((movement_day, character_id))

    analysis = {
        "coalition_id": coalition_id,
        "start_day": start_day,
        "end_day": end_day,
        "current_member_ids": current_member_ids,
        "arrival_events": arrival_events,
        "departure_events": departure_events,
        "origin_current_sets": origin_current_sets,
        "incoming_period_sets": incoming_period_sets,
        "destination_sets": destination_sets,
        "incoming_events_by_entity": incoming_events_by_entity,
        "destination_events_by_entity": destination_events_by_entity,
        "origin_join_date_by_character": origin_join_date_by_character,
        "departures_by_character": departures_by_character,
        "names": names,
        "entity_meta": entity_meta,
        "current_detail_by_character": current_detail_by_character,
    }
    _coalition_pop_cache_put(cache_key, analysis)
    return analysis


def get_coalition_affiliation_flow_summary(coalition_id, start_date=None, end_date=None):
    analysis = _coalition_affiliation_flow_analysis(coalition_id, start_date, end_date)
    arrival_pilot_ids = {row[0] for row in analysis["arrival_events"]}
    departure_pilot_ids = {row[0] for row in analysis["departure_events"]}
    return {
        "coalition_id": int(coalition_id),
        "start_date": analysis["start_day"].isoformat(),
        "end_date": analysis["end_day"].isoformat(),
        "current_member_count": len(analysis["current_member_ids"]),
        "arrival_pilot_count": len(arrival_pilot_ids),
        "arrival_event_count": len(analysis["arrival_events"]),
        "departure_pilot_count": len(departure_pilot_ids),
        "departure_event_count": len(analysis["departure_events"]),
        "origins_current": _affiliation_rows(
            analysis["origin_current_sets"],
            analysis["entity_meta"],
            len(analysis["current_member_ids"]),
        ),
        "incoming_period": _affiliation_rows(
            analysis["incoming_period_sets"],
            analysis["entity_meta"],
            len(arrival_pilot_ids),
        ),
        "destinations_current": _affiliation_rows(
            analysis["destination_sets"],
            analysis["entity_meta"],
            len(departure_pilot_ids),
        ),
    }


def _serialize_affiliation_current_detail(detail):
    detail = detail or {}
    corporation = detail.get("corporation")
    alliance = detail.get("alliance")
    coalitions = detail.get("coalitions") or []
    return {
        "corporation": corporation,
        "alliance": alliance,
        "coalitions": coalitions,
    }


def get_coalition_affiliation_flow_pilots(
    coalition_id,
    kind,
    entity_type,
    entity_id,
    start_date=None,
    end_date=None,
    page=1,
    per_page=100,
):
    analysis = _coalition_affiliation_flow_analysis(coalition_id, start_date, end_date)
    kind = str(kind or "origin").strip().lower()
    if kind not in {"origin", "incoming", "destination"}:
        raise ValueError("affiliation_flow_kind_invalid")

    key = _affiliation_entity_key(entity_type, entity_id)
    if kind == "origin":
        pilot_ids = set(analysis["origin_current_sets"].get(key, set()))
    elif kind == "incoming":
        pilot_ids = set(analysis["incoming_period_sets"].get(key, set()))
    else:
        pilot_ids = set(analysis["destination_sets"].get(key, set()))

    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = max(1, min(200, int(per_page)))
    except (TypeError, ValueError):
        per_page = 100

    names = analysis["names"]
    rows = []
    for character_id in pilot_ids:
        movement_date = None
        if kind == "origin":
            movement_date = analysis["origin_join_date_by_character"].get(character_id)
        elif kind == "incoming":
            dates = [day for day, cid in analysis["incoming_events_by_entity"].get(key, []) if cid == character_id]
            movement_date = max(dates) if dates else None
        else:
            dates = analysis["departures_by_character"].get(character_id, [])
            movement_date = max(dates) if dates else None
        rows.append({
            "character_id": int(character_id),
            "character_name": names.get(character_id) or "Unknown",
            "image_url": f"https://images.evetech.net/characters/{int(character_id)}/portrait?size=64",
            "url": f"/character/{int(character_id)}",
            "movement_date": movement_date.isoformat() if movement_date else None,
            "current": _serialize_affiliation_current_detail(
                analysis["current_detail_by_character"].get(int(character_id))
            ),
        })

    rows.sort(key=lambda row: (
        -(date.fromisoformat(row["movement_date"]).toordinal() if row["movement_date"] else 0),
        str(row["character_name"]).casefold(),
        row["character_id"],
    ))
    total = len(rows)
    start = (page - 1) * per_page
    page_rows = rows[start:start + per_page]
    return {
        "coalition_id": int(coalition_id),
        "kind": kind,
        "entity": analysis["entity_meta"].get(key),
        "start_date": analysis["start_day"].isoformat(),
        "end_date": analysis["end_day"].isoformat(),
        "page": page,
        "per_page": per_page,
        "total": total,
        "pages": max(1, int(ceil(total / per_page))) if total else 1,
        "pilots": page_rows,
    }


def _affiliation_bucket_start(day, bucket):
    bucket = str(bucket or "month").lower()
    if bucket == "year":
        return date(day.year, 1, 1)
    if bucket == "quarter":
        month = ((day.month - 1) // 3) * 3 + 1
        return date(day.year, month, 1)
    if bucket == "month":
        return date(day.year, day.month, 1)
    raise ValueError("affiliation_flow_bucket_invalid")


def _affiliation_next_bucket(day, bucket):
    if bucket == "year":
        return date(day.year + 1, 1, 1)
    if bucket == "quarter":
        month = day.month + 3
        year = day.year
        if month > 12:
            month -= 12
            year += 1
        return date(year, month, 1)
    month = day.month + 1
    year = day.year
    if month > 12:
        month = 1
        year += 1
    return date(year, month, 1)


def get_coalition_affiliation_flow_series(
    coalition_id,
    origins=None,
    destinations=None,
    start_date=None,
    end_date=None,
    bucket="month",
):
    analysis = _coalition_affiliation_flow_analysis(coalition_id, start_date, end_date)
    bucket = str(bucket or "month").strip().lower()
    if bucket not in {"month", "quarter", "year"}:
        raise ValueError("affiliation_flow_bucket_invalid")

    origin_keys = list(origins or [])[:_AFFILIATION_FLOW_MAX_SERIES]
    remaining = max(0, _AFFILIATION_FLOW_MAX_SERIES - len(origin_keys))
    destination_keys = list(destinations or [])[:remaining]

    first_bucket = _affiliation_bucket_start(analysis["start_day"], bucket)
    final_bucket = _affiliation_bucket_start(analysis["end_day"], bucket)
    buckets = []
    current = first_bucket
    while current <= final_bucket:
        buckets.append(current)
        current = _affiliation_next_bucket(current, bucket)

    def build_series(direction, key, event_map):
        bucket_sets = {value: set() for value in buckets}
        for movement_day, character_id in event_map.get(key, []):
            if not (analysis["start_day"] <= movement_day <= analysis["end_day"]):
                continue
            bucket_day = _affiliation_bucket_start(movement_day, bucket)
            bucket_sets.setdefault(bucket_day, set()).add(int(character_id))
        meta = analysis["entity_meta"].get(key, {
            "entity_type": key[0], "entity_id": key[1], "name": "Unknown", "ticker": None,
            "image_url": None, "is_npc": False,
        })
        return {
            "direction": direction,
            "entity": meta,
            "points": [
                {"date": bucket_day.isoformat(), "count": len(bucket_sets.get(bucket_day, set()))}
                for bucket_day in buckets
            ],
        }

    series = []
    for key in origin_keys:
        series.append(build_series("incoming", key, analysis["incoming_events_by_entity"]))
    for key in destination_keys:
        series.append(build_series("outgoing", key, analysis["destination_events_by_entity"]))

    return {
        "coalition_id": int(coalition_id),
        "start_date": analysis["start_day"].isoformat(),
        "end_date": analysis["end_day"].isoformat(),
        "bucket": bucket,
        "series": series,
    }
