from datetime import date, timedelta
from difflib import SequenceMatcher
from pathlib import Path
import re
import unicodedata

from .db import db


ALLOWED_MEMBER_TYPES = {"coalition", "alliance", "corporation"}
ALLOWED_OPERATIONS = {"include", "exclude"}

COALITION_LOGO_EXTENSIONS = ("png", "jpg", "jpeg", "webp")
COALITION_LOGO_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "coalition_logos"


def coalition_logo_url(coalition_id):
    coalition_id = int(coalition_id)
    for extension in COALITION_LOGO_EXTENSIONS:
        path = COALITION_LOGO_DIR / f"{coalition_id}.{extension}"
        if path.is_file():
            try:
                version = path.stat().st_mtime_ns
            except OSError:
                version = 0
            return f"/coalition/{coalition_id}/logo?v={version}"
    return None


def coalition_logo_file(coalition_id):
    coalition_id = int(coalition_id)
    for extension in COALITION_LOGO_EXTENSIONS:
        path = COALITION_LOGO_DIR / f"{coalition_id}.{extension}"
        if path.is_file():
            return path
    return None


def save_coalition_logo(coalition_id, payload, extension):
    coalition_id = int(coalition_id)
    extension = str(extension or "").strip().lower()
    if extension not in COALITION_LOGO_EXTENSIONS:
        raise ValueError("invalid_logo_type")

    COALITION_LOGO_DIR.mkdir(parents=True, exist_ok=True)
    target = COALITION_LOGO_DIR / f"{coalition_id}.{extension}"
    temporary = COALITION_LOGO_DIR / f".{coalition_id}.{extension}.tmp"
    temporary.write_bytes(payload)
    temporary.replace(target)

    for other_extension in COALITION_LOGO_EXTENSIONS:
        if other_extension == extension:
            continue
        other = COALITION_LOGO_DIR / f"{coalition_id}.{other_extension}"
        try:
            other.unlink()
        except FileNotFoundError:
            pass

    return coalition_logo_url(coalition_id)


def remove_coalition_logo(coalition_id):
    coalition_id = int(coalition_id)
    removed = False
    for extension in COALITION_LOGO_EXTENSIONS:
        path = COALITION_LOGO_DIR / f"{coalition_id}.{extension}"
        try:
            path.unlink()
            removed = True
        except FileNotFoundError:
            pass
    return removed


def _clean_optional(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _row_to_coalition(row):
    if not row:
        return None
    return {
        "coalition_id": row[0],
        "name": row[1],
        "short_name": row[2],
        "description": row[3],
        "is_active": row[4],
        "created_at": row[5],
        "updated_at": row[6],
        "logo_url": coalition_logo_url(row[0]),
    }


def list_coalitions(query=None):
    params = []
    where_sql = ""
    query = _clean_optional(query)

    if query:
        needle = f"%{query}%"
        if query.isdigit():
            where_sql = """
                WHERE c.coalition_id = %s
                   OR c.name ILIKE %s
                   OR COALESCE(c.short_name, '') ILIKE %s
            """
            params.extend([int(query), needle, needle])
        else:
            where_sql = """
                WHERE c.name ILIKE %s
                   OR COALESCE(c.short_name, '') ILIKE %s
            """
            params.extend([needle, needle])

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    c.coalition_id,
                    c.name,
                    c.short_name,
                    c.description,
                    c.is_active,
                    c.created_at,
                    c.updated_at,
                    COUNT(m.id) AS rule_count,
                    COUNT(m.id) FILTER (
                        WHERE (m.valid_from IS NULL OR m.valid_from <= CURRENT_DATE)
                          AND (m.valid_to IS NULL OR m.valid_to >= CURRENT_DATE)
                    ) AS current_rule_count
                FROM entities.coalitions c
                LEFT JOIN entities.coalition_memberships m
                  ON m.coalition_id = c.coalition_id
                {where_sql}
                GROUP BY
                    c.coalition_id,
                    c.name,
                    c.short_name,
                    c.description,
                    c.is_active,
                    c.created_at,
                    c.updated_at
                ORDER BY c.is_active DESC, lower(c.name), c.coalition_id
                """,
                params,
            )
            rows = cur.fetchall()

    return [
        {
            "coalition_id": row[0],
            "name": row[1],
            "short_name": row[2],
            "description": row[3],
            "is_active": row[4],
            "created_at": row[5],
            "updated_at": row[6],
            "rule_count": row[7],
            "current_rule_count": row[8],
            "logo_url": coalition_logo_url(row[0]),
        }
        for row in rows
    ]



def _entity_logo_url(member_type, member_id):
    if member_type == "coalition":
        return coalition_logo_url(member_id)
    if member_type == "alliance":
        return f"https://images.evetech.net/alliances/{int(member_id)}/logo?size=64"
    if member_type == "corporation":
        return f"https://images.evetech.net/corporations/{int(member_id)}/logo?size=64"
    return None


def list_coalition_overviews(query=None):
    """Return coalition rows enriched for the public Coalition index.

    Public status is data-driven: a coalition is considered active when its
    recursively resolved current membership contains at least one alliance or
    corporation. A coalition with zero resolved current entities is considered
    closed. ``closed_at`` is the latest date on which it still had at least one
    resolved member before becoming empty.
    """
    query = _clean_optional(query)
    today = date.today()

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT coalition_id, name, short_name, description,
                       is_active, created_at, updated_at
                FROM entities.coalitions
                ORDER BY coalition_id
                """
            )
            coalition_rows = cur.fetchall()

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    m.id,
                    m.coalition_id,
                    m.operation,
                    m.member_type,
                    m.member_id,
                    COALESCE(mc.name, a.name, corp.name, 'Unknown') AS member_name,
                    COALESCE(mc.short_name, a.ticker, corp.ticker) AS member_ticker,
                    m.valid_from,
                    m.valid_to
                FROM entities.coalition_memberships m
                LEFT JOIN entities.coalitions mc
                  ON m.member_type = 'coalition'
                 AND mc.coalition_id = m.member_id
                LEFT JOIN entities.alliances a
                  ON m.member_type = 'alliance'
                 AND a.alliance_id = m.member_id
                LEFT JOIN entities.corporations corp
                  ON m.member_type = 'corporation'
                 AND corp.corporation_id = m.member_id
                ORDER BY m.coalition_id, m.id
                """
            )
            membership_rows = cur.fetchall()

        alliance_ids = sorted({
            int(row[4]) for row in membership_rows if row[3] == "alliance"
        })
        corporation_ids = sorted({
            int(row[4]) for row in membership_rows if row[3] == "corporation"
        })

        alliance_population = {}
        corporation_population = {}
        corporation_alliance = {}

        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('population.alliance_daily')")
            has_alliance_population = cur.fetchone()[0] is not None
            if has_alliance_population and alliance_ids:
                cur.execute(
                    """
                    SELECT DISTINCT ON (alliance_id)
                           alliance_id, member_count, snapshot_date
                    FROM population.alliance_daily
                    WHERE alliance_id = ANY(%s)
                    ORDER BY alliance_id, snapshot_date DESC
                    """,
                    (alliance_ids,),
                )
                for alliance_id, member_count, snapshot_date in cur.fetchall():
                    alliance_population[int(alliance_id)] = {
                        "member_count": int(member_count) if member_count is not None else None,
                        "snapshot_date": snapshot_date,
                    }

        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('population.corporation_daily')")
            has_corporation_population = cur.fetchone()[0] is not None
            if has_corporation_population and corporation_ids:
                cur.execute(
                    """
                    SELECT DISTINCT ON (corporation_id)
                           corporation_id, member_count, snapshot_date
                    FROM population.corporation_daily
                    WHERE corporation_id = ANY(%s)
                    ORDER BY corporation_id, snapshot_date DESC
                    """,
                    (corporation_ids,),
                )
                for corporation_id, member_count, snapshot_date in cur.fetchall():
                    corporation_population[int(corporation_id)] = {
                        "member_count": int(member_count) if member_count is not None else None,
                        "snapshot_date": snapshot_date,
                    }

        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('entities.corporation_alliance_history')")
            has_corporation_alliance_history = cur.fetchone()[0] is not None
            if has_corporation_alliance_history and corporation_ids:
                cur.execute(
                    """
                    SELECT DISTINCT ON (corporation_id)
                           corporation_id, alliance_id
                    FROM entities.corporation_alliance_history
                    WHERE corporation_id = ANY(%s)
                      AND start_date <= NOW()
                      AND (end_date IS NULL OR end_date >= NOW())
                    ORDER BY corporation_id, start_date DESC, record_id DESC
                    """,
                    (corporation_ids,),
                )
                corporation_alliance = {
                    int(corporation_id): (int(alliance_id) if alliance_id is not None else None)
                    for corporation_id, alliance_id in cur.fetchall()
                }

    coalitions = {
        int(row[0]): {
            "coalition_id": int(row[0]),
            "name": row[1],
            "short_name": row[2],
            "description": row[3],
            "configured_is_active": bool(row[4]),
            "created_at": row[5],
            "updated_at": row[6],
        }
        for row in coalition_rows
    }

    memberships_by_coalition = {coalition_id: [] for coalition_id in coalitions}
    all_end_dates = set()
    for row in membership_rows:
        item = {
            "id": int(row[0]),
            "coalition_id": int(row[1]),
            "operation": row[2],
            "member_type": row[3],
            "member_id": int(row[4]),
            "member_name": row[5],
            "member_ticker": row[6],
            "valid_from": row[7],
            "valid_to": row[8],
        }
        memberships_by_coalition.setdefault(item["coalition_id"], []).append(item)
        if item["valid_to"] is not None and item["valid_to"] <= today:
            all_end_dates.add(item["valid_to"])

    def is_active_on(rule, day):
        return (
            (rule["valid_from"] is None or rule["valid_from"] <= day)
            and (rule["valid_to"] is None or rule["valid_to"] >= day)
        )

    resolved_cache = {}

    def resolve(coalition_id, day, stack=frozenset()):
        key = (int(coalition_id), day)
        if key in resolved_cache:
            return resolved_cache[key]
        if coalition_id in stack:
            return frozenset()

        includes = set()
        excludes = set()
        next_stack = stack | {coalition_id}

        for rule in memberships_by_coalition.get(coalition_id, []):
            if not is_active_on(rule, day):
                continue

            if rule["member_type"] == "coalition":
                target = set(resolve(rule["member_id"], day, next_stack))
            else:
                target = {(rule["member_type"], rule["member_id"])}

            if rule["operation"] == "exclude":
                excludes.update(target)
            else:
                includes.update(target)

        result = frozenset(includes - excludes)
        resolved_cache[key] = result
        return result

    current_resolved = {
        coalition_id: resolve(coalition_id, today)
        for coalition_id in coalitions
    }

    def count_members(resolved_entities):
        alliance_entity_ids = {
            entity_id for entity_type, entity_id in resolved_entities
            if entity_type == "alliance"
        }
        corporation_entity_ids = {
            entity_id for entity_type, entity_id in resolved_entities
            if entity_type == "corporation"
        }

        known_total = 0
        missing = 0

        for alliance_id in alliance_entity_ids:
            info = alliance_population.get(alliance_id)
            if info and info["member_count"] is not None:
                known_total += info["member_count"]
            else:
                missing += 1

        for corporation_id in corporation_entity_ids:
            # If the corporation currently belongs to an alliance already in
            # the resolved set, its pilots are already counted by that alliance.
            if corporation_alliance.get(corporation_id) in alliance_entity_ids:
                continue
            info = corporation_population.get(corporation_id)
            if info and info["member_count"] is not None:
                known_total += info["member_count"]
            else:
                missing += 1

        return known_total, missing

    member_counts = {}
    for coalition_id, resolved_entities in current_resolved.items():
        known_total, missing = count_members(resolved_entities)
        member_counts[coalition_id] = {
            "known_total": known_total,
            "missing": missing,
        }

    # Prefer a manually uploaded coalition logo. Keep the historical fallback
    # only when no coalition-specific logo exists.
    coalition_logo_urls = {}
    for coalition_id, coalition in coalitions.items():
        uploaded_logo = coalition_logo_url(coalition_id)
        if uploaded_logo:
            coalition_logo_urls[coalition_id] = uploaded_logo
            continue

        same_name_alliance_ids = {
            rule["member_id"]
            for rule in memberships_by_coalition.get(coalition_id, [])
            if rule["operation"] == "include"
            and rule["member_type"] == "alliance"
            and (rule["member_name"] or "").strip().casefold() == (coalition["name"] or "").strip().casefold()
        }
        if len(same_name_alliance_ids) == 1:
            coalition_logo_urls[coalition_id] = _entity_logo_url(
                "alliance", next(iter(same_name_alliance_ids))
            )

    # Determine closure from effective recursive membership, not from the
    # editable metadata flag. Dates are inclusive, so a valid_to day is the
    # last day the member still belonged to the coalition.
    end_dates_desc = sorted(all_end_dates, reverse=True)
    closed_at = {}
    for coalition_id, resolved_entities in current_resolved.items():
        if resolved_entities:
            closed_at[coalition_id] = None
            continue

        closure_date = None
        for end_day in end_dates_desc:
            if resolve(coalition_id, end_day) and not resolve(
                coalition_id, end_day + timedelta(days=1)
            ):
                closure_date = end_day
                break
        closed_at[coalition_id] = closure_date

    def direct_effective_current_rules(coalition_id):
        active_rules = [
            rule for rule in memberships_by_coalition.get(coalition_id, [])
            if is_active_on(rule, today)
        ]
        excluded = {
            (rule["member_type"], rule["member_id"])
            for rule in active_rules
            if rule["operation"] == "exclude"
        }
        grouped = {}
        for rule in active_rules:
            if rule["operation"] != "include":
                continue
            entity_key = (rule["member_type"], rule["member_id"])
            if entity_key in excluded:
                continue
            previous = grouped.get(entity_key)
            if previous is None:
                grouped[entity_key] = rule
                continue
            previous_from = previous["valid_from"] or date.min
            current_from = rule["valid_from"] or date.min
            if current_from < previous_from:
                grouped[entity_key] = rule
        return list(grouped.values())

    def detail_member_count(rule):
        member_type = rule["member_type"]
        member_id = rule["member_id"]
        if member_type == "alliance":
            info = alliance_population.get(member_id)
            return info["member_count"] if info else None
        if member_type == "corporation":
            info = corporation_population.get(member_id)
            return info["member_count"] if info else None
        if member_type == "coalition":
            return member_counts.get(member_id, {}).get("known_total")
        return None

    result = []
    for coalition_id, coalition in coalitions.items():
        if query:
            query_key = query.casefold()
            if (
                query_key not in (coalition["name"] or "").casefold()
                and query_key not in (coalition["short_name"] or "").casefold()
            ):
                continue

        resolved_entities = current_resolved[coalition_id]
        member_info = member_counts[coalition_id]
        is_open = bool(resolved_entities)
        current_direct = direct_effective_current_rules(coalition_id)

        history = []
        for rule in memberships_by_coalition.get(coalition_id, []):
            if rule["operation"] != "include":
                continue
            current = is_active_on(rule, today)
            member_count = detail_member_count(rule) if current else None
            history.append({
                "member_type": rule["member_type"],
                "member_id": rule["member_id"],
                "member_name": rule["member_name"],
                "member_ticker": rule["member_ticker"],
                "joined": rule["valid_from"],
                "left": rule["valid_to"],
                "current": current,
                "member_count": member_count,
                "logo_url": (
                    coalition_logo_urls.get(rule["member_id"])
                    if rule["member_type"] == "coalition"
                    else _entity_logo_url(rule["member_type"], rule["member_id"])
                ),
                "url": (
                    f"/coalition/{rule['member_id']}"
                    if rule["member_type"] == "coalition"
                    else f"/{rule['member_type']}/{rule['member_id']}"
                ),
            })

        history.sort(key=lambda item: (
            0 if item["current"] else 1,
            (
                -(item["member_count"] if item["member_count"] is not None else -1)
                if item["current"]
                else -(item["left"].toordinal() if item["left"] else 0)
            ),
            (item["member_name"] or "").casefold(),
        ))

        known_total = member_info["known_total"]
        missing = member_info["missing"]
        if resolved_entities and missing:
            member_count_display = f"{known_total:,}+" if known_total else "calculating / incomplete"
        elif resolved_entities:
            member_count_display = f"{known_total:,}"
        else:
            member_count_display = "0"

        result.append({
            **coalition,
            "is_open": is_open,
            "closed_at": closed_at[coalition_id],
            "member_count": known_total,
            "member_count_display": member_count_display,
            "member_count_complete": missing == 0,
            "missing_population_entities": missing,
            "entity_count": len(current_direct),
            "resolved_entity_count": len(resolved_entities),
            "logo_url": coalition_logo_urls.get(coalition_id),
            "entity_history": history,
        })

    active = [item for item in result if item["is_open"]]
    closed = [item for item in result if not item["is_open"]]

    active.sort(key=lambda item: (
        -item["member_count"],
        item["missing_population_entities"],
        (item["name"] or "").casefold(),
    ))
    closed.sort(key=lambda item: (
        -(item["closed_at"].toordinal() if item["closed_at"] else 0),
        (item["name"] or "").casefold(),
    ))

    return {
        "active": active,
        "closed": closed,
        "active_count": len(active),
        "closed_count": len(closed),
    }


def get_coalition(coalition_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT coalition_id, name, short_name, description, is_active, created_at, updated_at
                FROM entities.coalitions
                WHERE coalition_id = %s
                """,
                (coalition_id,),
            )
            return _row_to_coalition(cur.fetchone())


def create_coalition(name, short_name=None, description=None, actor_user_id=None):
    name = (name or "").strip()
    short_name = _clean_optional(short_name)
    description = _clean_optional(description)

    if not name:
        raise ValueError("name_required")

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO entities.coalitions
                    (name, short_name, description, created_by, updated_by)
                VALUES
                    (%s, %s, %s, %s, %s)
                RETURNING coalition_id
                """,
                (name, short_name, description, actor_user_id, actor_user_id),
            )
            coalition_id = cur.fetchone()[0]
        conn.commit()

    return coalition_id


def update_coalition(coalition_id, name, short_name=None, description=None, is_active=True, actor_user_id=None):
    name = (name or "").strip()
    short_name = _clean_optional(short_name)
    description = _clean_optional(description)

    if not name:
        raise ValueError("name_required")

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE entities.coalitions
                SET name = %s,
                    short_name = %s,
                    description = %s,
                    is_active = %s,
                    updated_at = NOW(),
                    updated_by = %s
                WHERE coalition_id = %s
                """,
                (name, short_name, description, bool(is_active), actor_user_id, coalition_id),
            )
            if cur.rowcount != 1:
                raise ValueError("coalition_not_found")
        conn.commit()


def _validate_dates(valid_from, valid_to):
    if valid_from and valid_to and valid_to < valid_from:
        raise ValueError("invalid_date_range")


def _member_exists(cur, member_type, member_id):
    if member_type == "coalition":
        cur.execute(
            "SELECT 1 FROM entities.coalitions WHERE coalition_id = %s",
            (member_id,),
        )
    elif member_type == "alliance":
        cur.execute(
            "SELECT 1 FROM entities.alliances WHERE alliance_id = %s",
            (member_id,),
        )
    elif member_type == "corporation":
        cur.execute(
            "SELECT 1 FROM entities.corporations WHERE corporation_id = %s",
            (member_id,),
        )
    else:
        return False

    return cur.fetchone() is not None


def _load_coalition_edges(cur, excluded_membership_id=None):
    sql = """
        SELECT id, coalition_id, member_id, valid_from, valid_to
        FROM entities.coalition_memberships
        WHERE member_type = 'coalition'
    """
    params = []

    if excluded_membership_id is not None:
        sql += " AND id <> %s"
        params.append(excluded_membership_id)

    cur.execute(sql, params)
    return cur.fetchall()


def _bounds(valid_from, valid_to):
    return valid_from or date.min, valid_to or date.max


def _overlap(left_from, left_to, right_from, right_to):
    start = max(left_from, right_from)
    end = min(left_to, right_to)
    if start > end:
        return None
    return start, end


def _would_create_cycle(cur, coalition_id, child_coalition_id, valid_from, valid_to, excluded_membership_id=None):
    if coalition_id == child_coalition_id:
        return True

    candidate_from, candidate_to = _bounds(valid_from, valid_to)
    adjacency = {}

    for _membership_id, parent_id, child_id, edge_from, edge_to in _load_coalition_edges(
        cur,
        excluded_membership_id=excluded_membership_id,
    ):
        edge_bounds = _bounds(edge_from, edge_to)
        adjacency.setdefault(parent_id, []).append((child_id, edge_bounds[0], edge_bounds[1]))

    stack = [(child_coalition_id, candidate_from, candidate_to, frozenset({child_coalition_id}))]

    while stack:
        node_id, active_from, active_to, path = stack.pop()

        for next_id, edge_from, edge_to in adjacency.get(node_id, []):
            overlap = _overlap(active_from, active_to, edge_from, edge_to)
            if not overlap:
                continue

            if next_id == coalition_id:
                return True

            if next_id in path:
                continue

            stack.append((next_id, overlap[0], overlap[1], path | {next_id}))

    return False


def _check_duplicate(cur, coalition_id, operation, member_type, member_id, valid_from, valid_to, excluded_membership_id=None):
    sql = """
        SELECT 1
        FROM entities.coalition_memberships
        WHERE coalition_id = %s
          AND operation = %s
          AND member_type = %s
          AND member_id = %s
          AND valid_from IS NOT DISTINCT FROM %s
          AND valid_to IS NOT DISTINCT FROM %s
    """
    params = [coalition_id, operation, member_type, member_id, valid_from, valid_to]

    if excluded_membership_id is not None:
        sql += " AND id <> %s"
        params.append(excluded_membership_id)

    sql += " LIMIT 1"
    cur.execute(sql, params)
    return cur.fetchone() is not None


def add_membership(
    coalition_id,
    operation,
    member_type,
    member_id,
    valid_from=None,
    valid_to=None,
    source=None,
    notes=None,
    actor_user_id=None,
):
    operation = (operation or "").strip().lower()
    member_type = (member_type or "").strip().lower()
    source = _clean_optional(source)
    notes = _clean_optional(notes)
    member_id = int(member_id)

    if operation not in ALLOWED_OPERATIONS:
        raise ValueError("invalid_operation")
    if member_type not in ALLOWED_MEMBER_TYPES:
        raise ValueError("invalid_member_type")

    _validate_dates(valid_from, valid_to)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM entities.coalitions WHERE coalition_id = %s", (coalition_id,))
            if not cur.fetchone():
                raise ValueError("coalition_not_found")

            if not _member_exists(cur, member_type, member_id):
                raise ValueError("member_not_found")

            if member_type == "coalition" and _would_create_cycle(
                cur,
                coalition_id,
                member_id,
                valid_from,
                valid_to,
            ):
                raise ValueError("coalition_cycle")

            if _check_duplicate(
                cur,
                coalition_id,
                operation,
                member_type,
                member_id,
                valid_from,
                valid_to,
            ):
                raise ValueError("duplicate_rule")

            cur.execute(
                """
                INSERT INTO entities.coalition_memberships (
                    coalition_id,
                    operation,
                    member_type,
                    member_id,
                    valid_from,
                    valid_to,
                    source,
                    notes,
                    created_by,
                    updated_by
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    coalition_id,
                    operation,
                    member_type,
                    member_id,
                    valid_from,
                    valid_to,
                    source,
                    notes,
                    actor_user_id,
                    actor_user_id,
                ),
            )
            membership_id = cur.fetchone()[0]
        conn.commit()

    return membership_id


def update_membership(
    coalition_id,
    membership_id,
    operation,
    valid_from=None,
    valid_to=None,
    source=None,
    notes=None,
    actor_user_id=None,
    replacement_member_type=None,
    replacement_member_id=None,
):
    operation = (operation or "").strip().lower()
    source = _clean_optional(source)
    notes = _clean_optional(notes)
    replacement_member_type = (replacement_member_type or "").strip().lower() or None
    replacement_member_id = (
        int(replacement_member_id)
        if replacement_member_id not in (None, "")
        else None
    )

    if operation not in ALLOWED_OPERATIONS:
        raise ValueError("invalid_operation")
    if (replacement_member_type is None) != (replacement_member_id is None):
        raise ValueError("member_not_found")
    if replacement_member_type is not None and replacement_member_type not in ALLOWED_MEMBER_TYPES:
        raise ValueError("invalid_member_type")

    _validate_dates(valid_from, valid_to)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT member_type, member_id
                FROM entities.coalition_memberships
                WHERE id = %s
                  AND coalition_id = %s
                """,
                (membership_id, coalition_id),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("membership_not_found")

            current_member_type, current_member_id = row
            member_type = replacement_member_type or current_member_type
            member_id = replacement_member_id if replacement_member_id is not None else current_member_id

            if replacement_member_type is not None and not _member_exists(cur, member_type, member_id):
                raise ValueError("member_not_found")

            if member_type == "coalition" and _would_create_cycle(
                cur,
                coalition_id,
                member_id,
                valid_from,
                valid_to,
                excluded_membership_id=membership_id,
            ):
                raise ValueError("coalition_cycle")

            if _check_duplicate(
                cur,
                coalition_id,
                operation,
                member_type,
                member_id,
                valid_from,
                valid_to,
                excluded_membership_id=membership_id,
            ):
                raise ValueError("duplicate_rule")

            cur.execute(
                """
                UPDATE entities.coalition_memberships
                SET operation = %s,
                    member_type = %s,
                    member_id = %s,
                    valid_from = %s,
                    valid_to = %s,
                    source = %s,
                    notes = %s,
                    updated_at = NOW(),
                    updated_by = %s
                WHERE id = %s
                  AND coalition_id = %s
                """,
                (
                    operation,
                    member_type,
                    member_id,
                    valid_from,
                    valid_to,
                    source,
                    notes,
                    actor_user_id,
                    membership_id,
                    coalition_id,
                ),
            )
        conn.commit()


def delete_membership(coalition_id, membership_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM entities.coalition_memberships
                WHERE id = %s
                  AND coalition_id = %s
                """,
                (membership_id, coalition_id),
            )
            if cur.rowcount != 1:
                raise ValueError("membership_not_found")
        conn.commit()


def list_memberships(coalition_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    m.id,
                    m.operation,
                    m.member_type,
                    m.member_id,
                    COALESCE(mc.name, a.name, corp.name, 'Unknown') AS member_name,
                    COALESCE(mc.short_name, a.ticker, corp.ticker) AS member_ticker,
                    m.valid_from,
                    m.valid_to,
                    m.source,
                    m.notes,
                    m.created_at,
                    m.updated_at,
                    creator.username AS created_by_name,
                    updater.username AS updated_by_name
                FROM entities.coalition_memberships m
                LEFT JOIN entities.coalitions mc
                  ON m.member_type = 'coalition'
                 AND mc.coalition_id = m.member_id
                LEFT JOIN entities.alliances a
                  ON m.member_type = 'alliance'
                 AND a.alliance_id = m.member_id
                LEFT JOIN entities.corporations corp
                  ON m.member_type = 'corporation'
                 AND corp.corporation_id = m.member_id
                LEFT JOIN web.users creator
                  ON creator.id = m.created_by
                LEFT JOIN web.users updater
                  ON updater.id = m.updated_by
                WHERE m.coalition_id = %s
                ORDER BY
                    CASE
                        WHEN (m.valid_from IS NULL OR m.valid_from <= CURRENT_DATE)
                         AND (m.valid_to IS NULL OR m.valid_to >= CURRENT_DATE) THEN 0
                        WHEN m.valid_from > CURRENT_DATE THEN 1
                        ELSE 2
                    END,
                    COALESCE(m.valid_from, DATE '0001-01-01') DESC,
                    lower(COALESCE(mc.name, a.name, corp.name, '')),
                    m.id DESC
                """,
                (coalition_id,),
            )
            rows = cur.fetchall()

    today = date.today()
    result = []

    for row in rows:
        valid_from = row[6]
        valid_to = row[7]
        if valid_from and valid_from > today:
            status = "future"
        elif valid_to and valid_to < today:
            status = "ended"
        else:
            status = "current"

        result.append({
            "id": row[0],
            "operation": row[1],
            "member_type": row[2],
            "member_id": row[3],
            "member_name": row[4],
            "member_ticker": row[5],
            "valid_from": valid_from,
            "valid_to": valid_to,
            "source": row[8],
            "notes": row[9],
            "created_at": row[10],
            "updated_at": row[11],
            "created_by_name": row[12],
            "updated_by_name": row[13],
            "status": status,
        })

    # Public/user-facing membership is about effective members, not +/- rules.
    # A currently active INCLUDE hidden by a matching active EXCLUDE is not
    # presented as a current member. Future rules and EXCLUDE rules are admin
    # details and stay out of the public Membership table.
    current_excludes = {
        (item["member_type"], int(item["member_id"]))
        for item in result
        if item["operation"] == "exclude" and item["status"] == "current"
    }

    for item in result:
        key = (item["member_type"], int(item["member_id"]))
        item["public_current"] = (
            item["operation"] == "include"
            and item["status"] == "current"
            and key not in current_excludes
        )
        item["public_show"] = item["public_current"] or (
            item["operation"] == "include" and item["status"] == "ended"
        )
        item["member_count"] = None
        item["logo_url"] = _entity_logo_url(item["member_type"], item["member_id"])
        item["url"] = (
            f"/coalition/{item['member_id']}"
            if item["member_type"] == "coalition"
            else f"/{item['member_type']}/{item['member_id']}"
        )

    # Current member population is only meaningful for rows that are current.
    current_alliance_ids = sorted({
        int(item["member_id"])
        for item in result
        if item["public_current"] and item["member_type"] == "alliance"
    })
    current_corporation_ids = sorted({
        int(item["member_id"])
        for item in result
        if item["public_current"] and item["member_type"] == "corporation"
    })
    current_coalition_ids = sorted({
        int(item["member_id"])
        for item in result
        if item["public_current"] and item["member_type"] == "coalition"
    })

    alliance_population = {}
    corporation_population = {}

    if current_alliance_ids or current_corporation_ids:
        with db() as conn:
            if current_alliance_ids:
                with conn.cursor() as cur:
                    cur.execute("SELECT to_regclass('population.alliance_daily')")
                    if cur.fetchone()[0] is not None:
                        cur.execute(
                            """
                            SELECT DISTINCT ON (alliance_id)
                                   alliance_id, member_count
                            FROM population.alliance_daily
                            WHERE alliance_id = ANY(%s)
                            ORDER BY alliance_id, snapshot_date DESC
                            """,
                            (current_alliance_ids,),
                        )
                        alliance_population = {
                            int(entity_id): (int(member_count) if member_count is not None else None)
                            for entity_id, member_count in cur.fetchall()
                        }

            if current_corporation_ids:
                with conn.cursor() as cur:
                    cur.execute("SELECT to_regclass('population.corporation_daily')")
                    if cur.fetchone()[0] is not None:
                        cur.execute(
                            """
                            SELECT DISTINCT ON (corporation_id)
                                   corporation_id, member_count
                            FROM population.corporation_daily
                            WHERE corporation_id = ANY(%s)
                            ORDER BY corporation_id, snapshot_date DESC
                            """,
                            (current_corporation_ids,),
                        )
                        corporation_population = {
                            int(entity_id): (int(member_count) if member_count is not None else None)
                            for entity_id, member_count in cur.fetchall()
                        }

    coalition_overview = {}
    if current_coalition_ids:
        overview_payload = list_coalition_overviews()
        coalition_overview = {
            int(item["coalition_id"]): item
            for item in overview_payload["active"] + overview_payload["closed"]
            if int(item["coalition_id"]) in current_coalition_ids
        }

    for item in result:
        if not item["public_current"]:
            continue
        member_id = int(item["member_id"])
        if item["member_type"] == "alliance":
            item["member_count"] = alliance_population.get(member_id)
        elif item["member_type"] == "corporation":
            item["member_count"] = corporation_population.get(member_id)
        elif item["member_type"] == "coalition":
            overview = coalition_overview.get(member_id)
            if overview:
                item["member_count"] = overview.get("member_count")
                item["logo_url"] = overview.get("logo_url")

    # Rank for the user-facing table without changing the technical/admin
    # ordering returned by this function. Current members: population DESC.
    # Former members: most recent left date DESC.
    public_rows = [item for item in result if item["public_show"]]
    public_rows.sort(key=lambda item: (
        0 if item["public_current"] else 1,
        (
            -(item["member_count"] if item["member_count"] is not None else -1)
            if item["public_current"]
            else -(item["valid_to"].toordinal() if item["valid_to"] else 0)
        ),
        (item["member_name"] or "").casefold(),
    ))
    for public_order, item in enumerate(public_rows):
        item["public_order"] = public_order
    for item in result:
        item.setdefault("public_order", 10**9)

    return result


def search_member_entities(query, limit=20, excluded_coalition_id=None):
    query = (query or "").strip()
    if not query:
        return []

    limit = max(1, min(int(limit), 50))
    needle = f"%{query}%"
    numeric_id = int(query) if query.isdigit() else None

    conditions = []
    params = []

    coalition_where = "(c.name ILIKE %s OR COALESCE(c.short_name, '') ILIKE %s"
    params.extend([needle, needle])
    if numeric_id is not None:
        coalition_where += " OR c.coalition_id = %s"
        params.append(numeric_id)
    coalition_where += ")"
    if excluded_coalition_id is not None:
        coalition_where += " AND c.coalition_id <> %s"
        params.append(excluded_coalition_id)

    conditions.append(f"""
        SELECT 'coalition'::text AS entity_type,
               c.coalition_id::bigint AS entity_id,
               c.name,
               c.short_name AS ticker,
               0 AS type_rank
        FROM entities.coalitions c
        WHERE {coalition_where}
    """)

    alliance_where = "(a.name ILIKE %s OR COALESCE(a.ticker, '') ILIKE %s"
    params.extend([needle, needle])
    if numeric_id is not None:
        alliance_where += " OR a.alliance_id = %s"
        params.append(numeric_id)
    alliance_where += ")"

    conditions.append(f"""
        SELECT 'alliance'::text AS entity_type,
               a.alliance_id::bigint AS entity_id,
               a.name,
               a.ticker,
               1 AS type_rank
        FROM entities.alliances a
        WHERE {alliance_where}
    """)

    corporation_where = "(corp.name ILIKE %s OR COALESCE(corp.ticker, '') ILIKE %s"
    params.extend([needle, needle])
    if numeric_id is not None:
        corporation_where += " OR corp.corporation_id = %s"
        params.append(numeric_id)
    corporation_where += ")"

    conditions.append(f"""
        SELECT 'corporation'::text AS entity_type,
               corp.corporation_id::bigint AS entity_id,
               corp.name,
               corp.ticker,
               2 AS type_rank
        FROM entities.corporations corp
        WHERE {corporation_where}
    """)

    sql = f"""
        SELECT entity_type, entity_id, name, ticker
        FROM (
            {' UNION ALL '.join(conditions)}
        ) candidates
        ORDER BY
            CASE
                WHEN lower(name) = lower(%s) THEN 0
                WHEN lower(COALESCE(ticker, '')) = lower(%s) THEN 1
                ELSE 2
            END,
            type_rank,
            lower(name)
        LIMIT %s
    """
    params.extend([query, query, limit])

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    return [
        {
            "entity_type": row[0],
            "entity_id": row[1],
            "name": row[2],
            "ticker": row[3],
            "logo_url": _entity_logo_url(row[0], row[1]),
        }
        for row in rows
    ]


def _bulk_clean_name(value):
    return str(value or "").strip()


def _bulk_name_key(value):
    return _bulk_clean_name(value).lower()


def _bulk_match_key(value):
    value = _bulk_clean_name(value)
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value).casefold()
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", value)


def _bulk_parse_date(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("Date must be YYYY-MM-DD or null")
    value = value.strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Date must be YYYY-MM-DD or null") from exc


def _bulk_member_type(value):
    raw = str(value or "").strip().upper()
    mapping = {
        "COA": "coalition",
        "COALITION": "coalition",
        "ALLIANCE": "alliance",
        "CORP": "corporation",
        "CORPORATION": "corporation",
    }
    return mapping.get(raw)


def _bulk_operation(value):
    raw = str(value or "include").strip().lower()
    mapping = {
        "+": "include",
        "include": "include",
        "included": "include",
        "-": "exclude",
        "exclude": "exclude",
        "excluded": "exclude",
    }
    return mapping.get(raw)


def _bulk_load_name_map(cur, table_name, id_column, names):
    keys = sorted({_bulk_name_key(name) for name in names if _bulk_clean_name(name)})
    if not keys:
        return {}

    cur.execute(
        f"""
        SELECT {id_column}, name
        FROM {table_name}
        WHERE lower(name) = ANY(%s)
        ORDER BY {id_column}
        """,
        (keys,),
    )

    result = {}
    for entity_id, entity_name in cur.fetchall():
        result.setdefault(_bulk_name_key(entity_name), []).append(
            {"id": int(entity_id), "name": entity_name}
        )
    return result


def _bulk_load_alliance_catalog(cur):
    cur.execute(
        """
        SELECT alliance_id, name, ticker, date_founded, is_deleted
        FROM entities.alliances
        WHERE name IS NOT NULL
          AND btrim(name) <> ''
        ORDER BY alliance_id
        """
    )

    catalog = []
    exact = {}
    normalized = {}
    by_id = {}

    for alliance_id, name, ticker, date_founded, is_deleted in cur.fetchall():
        founded_date = date_founded.date() if date_founded is not None else None
        item = {
            "id": int(alliance_id),
            "name": name,
            "ticker": ticker,
            "date_founded_date": founded_date,
            "is_deleted": bool(is_deleted),
            "name_key": _bulk_name_key(name),
            "match_key": _bulk_match_key(name),
            "ticker_key": _bulk_match_key(ticker),
        }
        catalog.append(item)
        exact.setdefault(item["name_key"], []).append(item)
        if item["match_key"]:
            normalized.setdefault(item["match_key"], []).append(item)
        by_id[item["id"]] = item

    return catalog, exact, normalized, by_id


def _bulk_public_candidate(candidate, *, score=None, reason=None, recommended=False, reference_date=None):
    founded = candidate.get("date_founded_date")
    if reference_date is None or founded is None:
        date_compatible = None
    else:
        date_compatible = founded <= reference_date

    return {
        "id": candidate["id"],
        "name": candidate.get("name") or "",
        "ticker": candidate.get("ticker") or "",
        "date_founded": founded.isoformat() if founded else None,
        "is_deleted": bool(candidate.get("is_deleted")),
        "date_compatible": date_compatible,
        "score": round(float(score), 4) if score is not None else None,
        "reason": reason,
        "recommended": bool(recommended),
    }


def _bulk_relation_reference_date(row):
    return row.get("valid_from") or row.get("valid_to")


def _bulk_rank_historical_candidates(candidates, reference_date):
    """Return a unique historically plausible candidate when the evidence is deterministic."""
    if not candidates:
        return None

    if len(candidates) == 1:
        candidate = candidates[0]
        founded = candidate.get("date_founded_date")
        if reference_date is not None and founded is not None and founded > reference_date:
            return None
        return candidate

    if reference_date is None:
        return None

    unknown_date = [c for c in candidates if c.get("date_founded_date") is None]
    if unknown_date:
        return None

    eligible = [c for c in candidates if c["date_founded_date"] <= reference_date]
    if not eligible:
        return None

    eligible.sort(key=lambda c: (c["date_founded_date"], c["id"]), reverse=True)
    if len(eligible) == 1:
        return eligible[0]

    if eligible[0]["date_founded_date"] > eligible[1]["date_founded_date"]:
        return eligible[0]

    return None


def _bulk_sort_candidates(candidates, reference_date):
    def key(candidate):
        founded = candidate.get("date_founded_date")
        compatible = reference_date is not None and founded is not None and founded <= reference_date
        known = founded is not None
        founded_ord = founded.toordinal() if founded else -1
        return (1 if compatible else 0, 1 if known else 0, founded_ord, 0 if candidate.get("is_deleted") else 1, candidate["id"])

    return sorted(candidates, key=key, reverse=True)


def _bulk_fuzzy_alliance_candidates(member_name, catalog, reference_date, limit=8):
    query_key = _bulk_match_key(member_name)
    if not query_key:
        return []

    scored = []
    for candidate in catalog:
        name_key = candidate.get("match_key") or ""
        ticker_key = candidate.get("ticker_key") or ""

        name_score = SequenceMatcher(None, query_key, name_key).ratio() if name_key else 0.0
        ticker_score = 0.0
        ticker_exact = False
        if ticker_key:
            if query_key == ticker_key:
                ticker_score = 1.0
                ticker_exact = True
            elif len(query_key) >= 3 and len(ticker_key) >= 3:
                ticker_score = SequenceMatcher(None, query_key, ticker_key).ratio() * 0.94

        score = max(name_score, ticker_score)
        if score < 0.46:
            continue

        founded = candidate.get("date_founded_date")
        compatible = reference_date is not None and founded is not None and founded <= reference_date
        future = reference_date is not None and founded is not None and founded > reference_date

        adjusted = score
        if compatible:
            adjusted += 0.035
        elif future:
            adjusted -= 0.06
        if ticker_exact:
            adjusted += 0.05

        reason = "Exact ticker" if ticker_exact else "Similar name"
        scored.append((adjusted, score, reason, candidate))

    scored.sort(
        key=lambda item: (
            item[0],
            item[3].get("date_founded_date") or date.min,
            0 if item[3].get("is_deleted") else 1,
            item[3]["id"],
        ),
        reverse=True,
    )

    result = []
    seen_ids = set()
    for adjusted, score, reason, candidate in scored:
        if candidate["id"] in seen_ids:
            continue
        seen_ids.add(candidate["id"])
        result.append((candidate, score, reason, adjusted))
        if len(result) >= limit:
            break
    return result


def _bulk_unresolved_error(row, code, message, candidates, recommended_id=None):
    reference_date = _bulk_relation_reference_date(row)
    public_candidates = []
    for candidate, score, reason in candidates:
        public_candidates.append(
            _bulk_public_candidate(
                candidate,
                score=score,
                reason=reason,
                recommended=(candidate["id"] == recommended_id),
                reference_date=reference_date,
            )
        )

    return {
        "row": row["row"],
        "code": code,
        "message": message,
        # Every unresolved alliance can be manually resolved by an admin,
        # even when the smart resolver found no candidate.
        "resolution_required": True,
        "resolution_group": f"alliance|{_bulk_match_key(row['member_name'])}",
        "member_type": "ALLIANCE",
        "member_name": row["member_name"],
        "date_from": row["valid_from"].isoformat() if row.get("valid_from") else None,
        "date_to": row["valid_to"].isoformat() if row.get("valid_to") else None,
        "recommended_id": recommended_id,
        "candidates": public_candidates,
    }


def _bulk_resolve_alliance(row, exact_map, normalized_map, catalog, by_id, resolutions, fuzzy_cache):
    override_id = resolutions.get(row["row"])
    if override_id is not None:
        candidate = by_id.get(int(override_id))
        if candidate is None:
            return None, {
                "row": row["row"],
                "code": "invalid_resolution",
                "message": f"Selected alliance ID does not exist: {override_id}",
            }, "user"
        return candidate["id"], None, "user"

    reference_date = _bulk_relation_reference_date(row)
    exact_matches = exact_map.get(row["member_key"], [])
    if exact_matches:
        chosen = _bulk_rank_historical_candidates(exact_matches, reference_date)
        if chosen is not None:
            return chosen["id"], None, "exact" if len(exact_matches) == 1 else "historical"

        ordered = _bulk_sort_candidates(exact_matches, reference_date)
        recommended_id = ordered[0]["id"] if ordered else None
        candidates = [(c, 1.0, "Exact name") for c in ordered]
        return None, _bulk_unresolved_error(
            row,
            "alliance_ambiguous",
            f"Alliance name is ambiguous: {row['member_name']}",
            candidates,
            recommended_id=recommended_id,
        ), None

    match_key = _bulk_match_key(row["member_name"])
    normalized_matches = normalized_map.get(match_key, [])
    if normalized_matches:
        chosen = _bulk_rank_historical_candidates(normalized_matches, reference_date)
        if chosen is not None:
            return chosen["id"], None, "normalized" if len(normalized_matches) == 1 else "historical_normalized"

        ordered = _bulk_sort_candidates(normalized_matches, reference_date)
        recommended_id = ordered[0]["id"] if ordered else None
        candidates = [(c, 0.99, "Same normalized name") for c in ordered]
        return None, _bulk_unresolved_error(
            row,
            "alliance_ambiguous",
            f"Alliance name is ambiguous after normalization: {row['member_name']}",
            candidates,
            recommended_id=recommended_id,
        ), None

    fuzzy_cache_key = (match_key, reference_date)
    if fuzzy_cache_key not in fuzzy_cache:
        fuzzy_cache[fuzzy_cache_key] = _bulk_fuzzy_alliance_candidates(
            row["member_name"],
            catalog,
            reference_date,
            limit=8,
        )

    fuzzy = fuzzy_cache[fuzzy_cache_key]
    if not fuzzy:
        return None, _bulk_unresolved_error(
            row,
            "alliance_not_found",
            f"Alliance not found: {row['member_name']}",
            [],
            recommended_id=None,
        ), None

    top = fuzzy[0]
    recommended_id = top[0]["id"] if top[3] >= 0.72 else None
    candidates = [(candidate, score, reason) for candidate, score, reason, _adjusted in fuzzy]
    return None, _bulk_unresolved_error(
        row,
        "alliance_not_found",
        f"No exact alliance match: {row['member_name']}. Candidate suggestions are available.",
        candidates,
        recommended_id=recommended_id,
    ), None


def _bulk_parse_rows(payload):
    errors = []
    rows = []

    if not isinstance(payload, list):
        return [], [{"row": None, "code": "invalid_root", "message": "JSON root must be an array."}]

    if not payload:
        return [], [{"row": None, "code": "empty_file", "message": "JSON array is empty."}]

    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            errors.append({"row": index, "code": "invalid_row", "message": "Row must be a JSON object."})
            continue

        coalition_name = _bulk_clean_name(item.get("coalition"))
        member_name = _bulk_clean_name(item.get("member"))
        member_type = _bulk_member_type(item.get("type"))
        operation = _bulk_operation(item.get("operation", "include"))
        notes = _clean_optional(item.get("notes"))

        if not coalition_name:
            errors.append({"row": index, "code": "coalition_required", "message": "Missing coalition name."})
        if not member_name:
            errors.append({"row": index, "code": "member_required", "message": "Missing member name."})
        if member_type is None:
            errors.append({"row": index, "code": "invalid_member_type", "message": "type must be ALLIANCE, COA/COALITION, CORP or CORPORATION."})
        if operation is None:
            errors.append({"row": index, "code": "invalid_operation", "message": "operation must be include/+ or exclude/-."})

        try:
            valid_from = _bulk_parse_date(item.get("date_from"))
        except ValueError as exc:
            errors.append({"row": index, "code": "invalid_date_from", "message": f"date_from: {exc}"})
            valid_from = None

        try:
            valid_to = _bulk_parse_date(item.get("date_to"))
        except ValueError as exc:
            errors.append({"row": index, "code": "invalid_date_to", "message": f"date_to: {exc}"})
            valid_to = None

        if valid_from and valid_to and valid_to < valid_from:
            errors.append({"row": index, "code": "invalid_date_range", "message": "date_to is before date_from."})

        if coalition_name and member_name and member_type and operation:
            rows.append({
                "row": index,
                "coalition_name": coalition_name,
                "coalition_key": _bulk_name_key(coalition_name),
                "member_name": member_name,
                "member_key": _bulk_name_key(member_name),
                "member_type": member_type,
                "operation": operation,
                "valid_from": valid_from,
                "valid_to": valid_to,
                "notes": notes,
            })

    return rows, errors


def _bulk_cycle_exists(edges):
    adjacency = {}
    for parent_id, child_id, valid_from, valid_to, row_number in edges:
        edge_from, edge_to = _bounds(valid_from, valid_to)
        adjacency.setdefault(parent_id, []).append((child_id, edge_from, edge_to, row_number))

    for start_parent, children in adjacency.items():
        for child_id, edge_from, edge_to, row_number in children:
            if start_parent == child_id:
                return row_number

            stack = [(child_id, edge_from, edge_to, frozenset({start_parent, child_id}))]
            while stack:
                node_id, active_from, active_to, path = stack.pop()
                for next_id, next_from, next_to, next_row in adjacency.get(node_id, []):
                    overlap = _overlap(active_from, active_to, next_from, next_to)
                    if not overlap:
                        continue
                    if next_id == start_parent:
                        return next_row or row_number
                    if next_id in path:
                        continue
                    stack.append((next_id, overlap[0], overlap[1], path | {next_id}))
    return None


def _analyze_bulk_import(cur, payload, source, resolutions=None):
    source = _clean_optional(source)
    resolutions = resolutions or {}
    rows, errors = _bulk_parse_rows(payload)

    if not source:
        errors.append({"row": None, "code": "source_required", "message": "Source is required."})

    parent_names = [row["coalition_name"] for row in rows]
    coalition_member_names = [row["member_name"] for row in rows if row["member_type"] == "coalition"]
    coalition_names = parent_names + coalition_member_names
    corporation_names = [row["member_name"] for row in rows if row["member_type"] == "corporation"]

    coalition_db = _bulk_load_name_map(cur, "entities.coalitions", "coalition_id", coalition_names)
    corporation_db = _bulk_load_name_map(cur, "entities.corporations", "corporation_id", corporation_names)
    alliance_catalog, alliance_exact, alliance_normalized, alliance_by_id = _bulk_load_alliance_catalog(cur)

    file_coalitions = {}
    for name in coalition_names:
        key = _bulk_name_key(name)
        file_coalitions.setdefault(key, name)

    for key, matches in coalition_db.items():
        if len(matches) > 1:
            errors.append({
                "row": None,
                "code": "ambiguous_coalition",
                "message": f"Coalition name '{file_coalitions.get(key, key)}' matches multiple existing coalitions.",
            })

    resolved_rows = []
    seen_file_rules = set()
    fuzzy_cache = {}
    auto_resolution_count = 0
    user_resolution_count = 0

    for row in rows:
        resolution_method = None

        if row["member_type"] == "alliance":
            member_id, resolution_error, resolution_method = _bulk_resolve_alliance(
                row,
                alliance_exact,
                alliance_normalized,
                alliance_catalog,
                alliance_by_id,
                resolutions,
                fuzzy_cache,
            )
            if resolution_error is not None:
                errors.append(resolution_error)
                continue
            if resolution_method == "user":
                user_resolution_count += 1
            elif resolution_method in {"historical", "normalized", "historical_normalized"}:
                auto_resolution_count += 1

        elif row["member_type"] == "corporation":
            matches = corporation_db.get(row["member_key"], [])
            if not matches:
                errors.append({"row": row["row"], "code": "corporation_not_found", "message": f"Corporation not found: {row['member_name']}"})
                continue
            if len(matches) > 1:
                errors.append({"row": row["row"], "code": "corporation_ambiguous", "message": f"Corporation name is ambiguous: {row['member_name']}"})
                continue
            member_id = matches[0]["id"]
        else:
            member_id = None

        rule_key = (
            row["coalition_key"],
            row["operation"],
            row["member_type"],
            row["member_key"] if row["member_type"] == "coalition" else member_id,
            row["valid_from"],
            row["valid_to"],
        )
        if rule_key in seen_file_rules:
            errors.append({"row": row["row"], "code": "duplicate_in_file", "message": "Duplicate rule inside the JSON file."})
            continue
        seen_file_rules.add(rule_key)

        resolved = dict(row)
        resolved["member_id"] = member_id
        resolved_rows.append(resolved)

    # Assign stable temporary negative IDs to coalitions that do not exist yet.
    coalition_ids = {}
    next_temp_id = -1
    for key, display_name in sorted(file_coalitions.items()):
        matches = coalition_db.get(key, [])
        if len(matches) == 1:
            coalition_ids[key] = matches[0]["id"]
        elif len(matches) == 0:
            coalition_ids[key] = next_temp_id
            next_temp_id -= 1

    for row in resolved_rows:
        row["coalition_id"] = coalition_ids.get(row["coalition_key"])
        if row["member_type"] == "coalition":
            row["member_id"] = coalition_ids.get(row["member_key"])

    # Existing exact duplicates.
    for row in resolved_rows:
        if row["coalition_id"] is None or row["member_id"] is None:
            continue
        if row["coalition_id"] < 0 or (row["member_type"] == "coalition" and row["member_id"] < 0):
            continue
        if _check_duplicate(
            cur,
            row["coalition_id"],
            row["operation"],
            row["member_type"],
            row["member_id"],
            row["valid_from"],
            row["valid_to"],
        ):
            errors.append({"row": row["row"], "code": "duplicate_existing", "message": "The exact same rule already exists in EVEOSINT."})

    # Time-aware cycle validation against existing and candidate coalition edges.
    cur.execute(
        """
        SELECT coalition_id, member_id, valid_from, valid_to
        FROM entities.coalition_memberships
        WHERE member_type = 'coalition'
        """
    )
    edges = [(int(parent), int(child), valid_from, valid_to, None) for parent, child, valid_from, valid_to in cur.fetchall()]
    for row in resolved_rows:
        if row["member_type"] == "coalition" and row["coalition_id"] is not None and row["member_id"] is not None:
            edges.append((row["coalition_id"], row["member_id"], row["valid_from"], row["valid_to"], row["row"]))

    cycle_row = _bulk_cycle_exists(edges)
    if cycle_row is not None:
        errors.append({"row": cycle_row, "code": "coalition_cycle", "message": "Coalition nesting creates a recursive cycle during an overlapping date range."})

    type_counts = {"ALLIANCE": 0, "COA": 0, "CORPORATION": 0}
    operation_counts = {"include": 0, "exclude": 0}
    for row in rows:
        if row["member_type"] == "alliance":
            type_counts["ALLIANCE"] += 1
        elif row["member_type"] == "coalition":
            type_counts["COA"] += 1
        elif row["member_type"] == "corporation":
            type_counts["CORPORATION"] += 1
        if row["operation"] in operation_counts:
            operation_counts[row["operation"]] += 1

    existing_coalition_count = sum(1 for key in file_coalitions if len(coalition_db.get(key, [])) == 1)
    new_coalition_count = sum(1 for key in file_coalitions if len(coalition_db.get(key, [])) == 0)
    pending_resolution_count = sum(1 for error in errors if error.get("resolution_required"))
    hard_error_count = len(errors) - pending_resolution_count

    return {
        "ready": len(errors) == 0,
        "source": source,
        "total_rows": len(payload) if isinstance(payload, list) else 0,
        "parsed_rows": len(rows),
        "resolved_rows": len(resolved_rows),
        "coalition_count": len(file_coalitions),
        "existing_coalition_count": existing_coalition_count,
        "new_coalition_count": new_coalition_count,
        "type_counts": type_counts,
        "operation_counts": operation_counts,
        "auto_resolution_count": auto_resolution_count,
        "user_resolution_count": user_resolution_count,
        "pending_resolution_count": pending_resolution_count,
        "hard_error_count": hard_error_count,
        "error_count": len(errors),
        "errors": errors,
        "rows": resolved_rows,
        "coalition_ids": coalition_ids,
        "file_coalitions": file_coalitions,
    }


def analyze_bulk_import(payload, source, resolutions=None):
    with db() as conn:
        with conn.cursor() as cur:
            report = _analyze_bulk_import(cur, payload, source, resolutions=resolutions)
    report.pop("rows", None)
    report.pop("coalition_ids", None)
    report.pop("file_coalitions", None)
    return report


def import_bulk_memberships(payload, source, actor_user_id=None, resolutions=None):
    source = _clean_optional(source)
    with db() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("LOCK TABLE entities.coalitions IN SHARE ROW EXCLUSIVE MODE")
                cur.execute("LOCK TABLE entities.coalition_memberships IN SHARE ROW EXCLUSIVE MODE")
                report = _analyze_bulk_import(cur, payload, source, resolutions=resolutions)
                if not report["ready"]:
                    public_report = dict(report)
                    public_report.pop("rows", None)
                    public_report.pop("coalition_ids", None)
                    public_report.pop("file_coalitions", None)
                    return public_report

                coalition_ids = dict(report["coalition_ids"])
                file_coalitions = report["file_coalitions"]

                for key, coalition_id in list(coalition_ids.items()):
                    if coalition_id >= 0:
                        continue
                    cur.execute(
                        """
                        INSERT INTO entities.coalitions
                            (name, short_name, description, created_by, updated_by)
                        VALUES
                            (%s, NULL, NULL, %s, %s)
                        RETURNING coalition_id
                        """,
                        (file_coalitions[key], actor_user_id, actor_user_id),
                    )
                    coalition_ids[key] = int(cur.fetchone()[0])

                values = []
                for row in report["rows"]:
                    coalition_id = coalition_ids[row["coalition_key"]]
                    if row["member_type"] == "coalition":
                        member_id = coalition_ids[row["member_key"]]
                    else:
                        member_id = row["member_id"]

                    values.append((
                        coalition_id,
                        row["operation"],
                        row["member_type"],
                        member_id,
                        row["valid_from"],
                        row["valid_to"],
                        source,
                        row["notes"],
                        actor_user_id,
                        actor_user_id,
                    ))

                cur.executemany(
                    """
                    INSERT INTO entities.coalition_memberships (
                        coalition_id,
                        operation,
                        member_type,
                        member_id,
                        valid_from,
                        valid_to,
                        source,
                        notes,
                        created_by,
                        updated_by
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    values,
                )

            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return {
        "ready": True,
        "imported": True,
        "source": source,
        "total_rows": report["total_rows"],
        "parsed_rows": report["parsed_rows"],
        "resolved_rows": report["resolved_rows"],
        "coalition_count": report["coalition_count"],
        "existing_coalition_count": report["existing_coalition_count"],
        "new_coalition_count": report["new_coalition_count"],
        "type_counts": report["type_counts"],
        "operation_counts": report["operation_counts"],
        "auto_resolution_count": report["auto_resolution_count"],
        "user_resolution_count": report["user_resolution_count"],
        "pending_resolution_count": 0,
        "hard_error_count": 0,
        "error_count": 0,
        "errors": [],
    }
