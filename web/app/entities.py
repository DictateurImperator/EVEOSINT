import re
import json
import logging
from collections import OrderedDict
from threading import Lock
from time import perf_counter, monotonic
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from psycopg2.errors import QueryCanceled

from .db import db


logger = logging.getLogger(__name__)


def _timing_print(message):
    print(message, flush=True)


class EntityError(Exception):
    pass


ENTITY_TYPES = {"character", "corporation", "alliance", "system", "constellation", "region", "ship", "weapon", "skill", "commodity"}
LOCATION_ENTITY_TYPES = {"system", "constellation", "region"}
ITEM_ENTITY_TYPES = {"ship", "weapon", "skill", "commodity"}


def normalize_entity_type(entity_type):
    if entity_type in ("character", "player"):
        return "character"
    if entity_type == "corporation":
        return "corporation"
    if entity_type == "alliance":
        return "alliance"
    if entity_type in LOCATION_ENTITY_TYPES:
        return entity_type
    if entity_type in ITEM_ENTITY_TYPES:
        return entity_type
    raise EntityError("entity_type_invalid")


def profile_url(entity_type, entity_id):
    entity_type = normalize_entity_type(entity_type)
    return f"/{entity_type}/{int(entity_id)}"


def image_url(entity_type, entity_id, size=32):
    entity_type = normalize_entity_type(entity_type)
    entity_id = int(entity_id)
    if entity_type == "character":
        return f"https://images.evetech.net/characters/{entity_id}/portrait?size={size}"
    if entity_type == "corporation":
        return f"https://images.evetech.net/corporations/{entity_id}/logo?size={size}"
    if entity_type == "alliance":
        return f"https://images.evetech.net/alliances/{entity_id}/logo?size={size}"
    if entity_type in ITEM_ENTITY_TYPES:
        return f"https://images.evetech.net/types/{entity_id}/icon?size={size}"
    return None


def _table_exists(conn, schema, table):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema=%s AND table_name=%s
            )
            """,
            (schema, table),
        )
        return bool(cur.fetchone()[0])


def _format_result(entity_type, entity_id, name, ticker=None):
    entity_type = normalize_entity_type(entity_type)
    entity_id = int(entity_id)
    name = name or "Unknown"
    ticker = ticker or None

    if entity_type == "character":
        subtitle = "Character"
        label = name
    elif entity_type == "corporation":
        subtitle = "Corporation"
        label = f"{name} [{ticker}]" if ticker else name
    elif entity_type == "alliance":
        subtitle = "Alliance"
        label = f"{name} [{ticker}]" if ticker else name
    elif entity_type == "ship":
        subtitle = "Ship"
        label = name
    elif entity_type == "weapon":
        subtitle = "Weapon"
        label = name
    elif entity_type == "skill":
        subtitle = "Skill"
        label = name
    elif entity_type == "commodity":
        subtitle = "Commodity"
        label = name
    elif entity_type == "system":
        subtitle = "Solar system"
        label = name
    elif entity_type == "constellation":
        subtitle = "Constellation"
        label = name
    else:
        subtitle = "Region"
        label = name

    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "name": name,
        "ticker": ticker,
        "label": label,
        "subtitle": subtitle,
        "url": profile_url(entity_type, entity_id),
        "image_url": image_url(entity_type, entity_id, 32),
    }



def search_killmail_locations(query, limit=15):
    """Dedicated lightweight search for Killboard zones.

    Searches only solar systems, constellations and regions instead of running
    the full global entity search and filtering its result afterwards.
    """
    term = str(query or "").strip()

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 15

    limit = max(1, min(limit, 30))
    if not term:
        return []

    numeric_id = int(term) if term.isdigit() else None
    needle = term.lower()
    contains = f"%{needle}%"
    prefix = f"{needle}%"

    sql = """
        WITH candidates AS (
            SELECT
                'system'::text AS entity_type,
                COALESCE(NULLIF(s.data->>'_key',''), NULLIF(s.sde_key,''))::BIGINT AS entity_id,
                COALESCE(NULLIF(s.data->'name'->>'en',''), NULLIF(s.data->>'name',''), s.sde_key) AS name,
                COALESCE(NULLIF(r.data->'name'->>'en',''), NULLIF(r.data->>'name',''), r.sde_key) AS context_name,
                CASE
                    WHEN COALESCE(NULLIF(s.data->>'_key',''), NULLIF(s.sde_key,''))::BIGINT = %s THEN 0
                    WHEN lower(COALESCE(s.data->'name'->>'en', s.data->>'name', '')) = %s THEN 1
                    WHEN lower(COALESCE(s.data->'name'->>'en', s.data->>'name', '')) LIKE %s THEN 2
                    ELSE 3
                END AS rank_score,
                0 AS type_rank
            FROM public.sde_mapsolarsystems s
            LEFT JOIN public.sde_mapregions r
              ON NULLIF(r.sde_key,'')::BIGINT = NULLIF(s.data->>'regionID','')::BIGINT
            WHERE (
                COALESCE(NULLIF(s.data->>'_key',''), NULLIF(s.sde_key,''))::BIGINT = %s
                OR lower(COALESCE(s.data->'name'->>'en', s.data->>'name', '')) LIKE %s
            )

            UNION ALL

            SELECT
                'constellation'::text AS entity_type,
                COALESCE(NULLIF(c.data->>'_key',''), NULLIF(c.sde_key,''))::BIGINT AS entity_id,
                COALESCE(NULLIF(c.data->'name'->>'en',''), NULLIF(c.data->>'name',''), c.sde_key) AS name,
                COALESCE(NULLIF(r.data->'name'->>'en',''), NULLIF(r.data->>'name',''), r.sde_key) AS context_name,
                CASE
                    WHEN COALESCE(NULLIF(c.data->>'_key',''), NULLIF(c.sde_key,''))::BIGINT = %s THEN 0
                    WHEN lower(COALESCE(c.data->'name'->>'en', c.data->>'name', '')) = %s THEN 1
                    WHEN lower(COALESCE(c.data->'name'->>'en', c.data->>'name', '')) LIKE %s THEN 2
                    ELSE 3
                END AS rank_score,
                1 AS type_rank
            FROM public.sde_mapconstellations c
            LEFT JOIN public.sde_mapregions r
              ON NULLIF(r.sde_key,'')::BIGINT = NULLIF(c.data->>'regionID','')::BIGINT
            WHERE (
                COALESCE(NULLIF(c.data->>'_key',''), NULLIF(c.sde_key,''))::BIGINT = %s
                OR lower(COALESCE(c.data->'name'->>'en', c.data->>'name', '')) LIKE %s
            )

            UNION ALL

            SELECT
                'region'::text AS entity_type,
                COALESCE(NULLIF(r.data->>'_key',''), NULLIF(r.sde_key,''))::BIGINT AS entity_id,
                COALESCE(NULLIF(r.data->'name'->>'en',''), NULLIF(r.data->>'name',''), r.sde_key) AS name,
                NULL::text AS context_name,
                CASE
                    WHEN COALESCE(NULLIF(r.data->>'_key',''), NULLIF(r.sde_key,''))::BIGINT = %s THEN 0
                    WHEN lower(COALESCE(r.data->'name'->>'en', r.data->>'name', '')) = %s THEN 1
                    WHEN lower(COALESCE(r.data->'name'->>'en', r.data->>'name', '')) LIKE %s THEN 2
                    ELSE 3
                END AS rank_score,
                2 AS type_rank
            FROM public.sde_mapregions r
            WHERE (
                COALESCE(NULLIF(r.data->>'_key',''), NULLIF(r.sde_key,''))::BIGINT = %s
                OR lower(COALESCE(r.data->'name'->>'en', r.data->>'name', '')) LIKE %s
            )
        )
        SELECT entity_type, entity_id, name, context_name
        FROM candidates
        ORDER BY rank_score ASC, type_rank ASC, lower(name) ASC
        LIMIT %s
    """

    params = (
        numeric_id, needle, prefix, numeric_id, contains,
        numeric_id, needle, prefix, numeric_id, contains,
        numeric_id, needle, prefix, numeric_id, contains,
        limit,
    )

    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '1500ms'")
                cur.execute(sql, params)
                rows = cur.fetchall()
    except QueryCanceled:
        return []

    results = []
    seen = set()

    for entity_type, entity_id, name, context_name in rows:
        entity_id = int(entity_id)
        key = (entity_type, entity_id)
        if key in seen:
            continue
        seen.add(key)

        item = _format_result(
            entity_type,
            entity_id,
            name,
            None,
        )

        if entity_type == "system":
            item["subtitle"] = (
                f"Solar system · {context_name}"
                if context_name
                else "Solar system"
            )
        elif entity_type == "constellation":
            item["subtitle"] = (
                f"Constellation · {context_name}"
                if context_name
                else "Constellation"
            )
        else:
            item["subtitle"] = "Region"

        item["context_name"] = context_name or None
        results.append(item)

    return results

def search_entities(query, limit=8):
    term = (query or "").strip()
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 8
    limit = max(1, min(limit, 10))

    if not term:
        return []

    numeric_id = int(term) if term.isdigit() else None
    needle = term.lower()
    prefix = f"{needle}%"

    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '500ms'")

                if numeric_id is not None:
                    cur.execute(
                        """
                        SELECT entity_type, entity_id, name, ticker, rank_score, type_rank
                        FROM (
                            SELECT 'character'::text AS entity_type,
                                   character_id::bigint AS entity_id,
                                   COALESCE(NULLIF(name, ''), 'Unknown') AS name,
                                   NULL::text AS ticker,
                                   0 AS rank_score,
                                   0 AS type_rank
                            FROM entities.characters
                            WHERE character_id = %s
                            UNION ALL
                            SELECT 'corporation'::text AS entity_type,
                                   corporation_id::bigint AS entity_id,
                                   COALESCE(NULLIF(name, ''), 'Unknown') AS name,
                                   ticker,
                                   0 AS rank_score,
                                   1 AS type_rank
                            FROM entities.corporations
                            WHERE corporation_id = %s
                            UNION ALL
                            SELECT 'alliance'::text AS entity_type,
                                   alliance_id::bigint AS entity_id,
                                   COALESCE(NULLIF(name, ''), 'Unknown') AS name,
                                   ticker,
                                   0 AS rank_score,
                                   2 AS type_rank
                            FROM entities.alliances
                            WHERE alliance_id = %s
                        ) x
                        ORDER BY type_rank ASC
                        LIMIT %s
                        """,
                        (numeric_id, numeric_id, numeric_id, limit),
                    )
                    rows = cur.fetchall()
                    if rows:
                        return [_format_result(row[0], row[1], row[2], row[3]) for row in rows[:limit]]

                cur.execute(
                    """
                    WITH candidates AS (
                        (
                            SELECT 'character'::text AS entity_type,
                                   character_id::bigint AS entity_id,
                                   COALESCE(NULLIF(name, ''), 'Unknown') AS name,
                                   NULL::text AS ticker,
                                   CASE
                                       WHEN lower(COALESCE(name, '')) = %s THEN 0
                                       ELSE 3
                                   END AS rank_score,
                                   0 AS type_rank
                            FROM entities.characters
                            WHERE lower(COALESCE(name, '')) LIKE %s
                            ORDER BY rank_score ASC, lower(COALESCE(name, '')) ASC
                            LIMIT %s
                        )
                        UNION ALL
                        (
                            SELECT 'corporation'::text AS entity_type,
                                   corporation_id::bigint AS entity_id,
                                   COALESCE(NULLIF(name, ''), 'Unknown') AS name,
                                   ticker,
                                   CASE
                                       WHEN lower(COALESCE(ticker, '')) = %s THEN 0
                                       WHEN lower(COALESCE(name, '')) = %s THEN 1
                                       WHEN lower(COALESCE(ticker, '')) LIKE %s THEN 2
                                       ELSE 4
                                   END AS rank_score,
                                   1 AS type_rank
                            FROM entities.corporations
                            WHERE lower(COALESCE(name, '')) LIKE %s
                               OR lower(COALESCE(ticker, '')) LIKE %s
                            ORDER BY rank_score ASC, lower(COALESCE(name, '')) ASC
                            LIMIT %s
                        )
                        UNION ALL
                        (
                            SELECT 'alliance'::text AS entity_type,
                                   alliance_id::bigint AS entity_id,
                                   COALESCE(NULLIF(name, ''), 'Unknown') AS name,
                                   ticker,
                                   CASE
                                       WHEN lower(COALESCE(ticker, '')) = %s THEN 0
                                       WHEN lower(COALESCE(name, '')) = %s THEN 1
                                       WHEN lower(COALESCE(ticker, '')) LIKE %s THEN 2
                                       ELSE 4
                                   END AS rank_score,
                                   2 AS type_rank
                            FROM entities.alliances
                            WHERE lower(COALESCE(name, '')) LIKE %s
                               OR lower(COALESCE(ticker, '')) LIKE %s
                            ORDER BY rank_score ASC, lower(COALESCE(name, '')) ASC
                            LIMIT %s
                        )
                    )
                    SELECT entity_type, entity_id, name, ticker, rank_score, type_rank
                    FROM candidates
                    ORDER BY rank_score ASC, type_rank ASC, lower(name) ASC
                    LIMIT %s
                    """,
                    (
                        needle, prefix, limit,
                        needle, needle, prefix, prefix, prefix, limit,
                        needle, needle, prefix, prefix, prefix, limit,
                        limit,
                    ),
                )
                rows = cur.fetchall()
    except QueryCanceled:
        return []

    seen = set()
    results = []
    for entity_type, entity_id, name, ticker, _rank_score, _type_rank in rows:
        key = (entity_type, int(entity_id))
        if key in seen:
            continue
        seen.add(key)
        results.append(_format_result(entity_type, entity_id, name, ticker))
        if len(results) >= limit:
            break

    return results


GLOBAL_SEARCH_SECTIONS = {
    "system": "SYSTEMS",
    "ship": "SHIPS",
    "ship_group": "SHIP TYPES",
    "alliance": "ALLIANCES",
    "corporation": "CORPORATIONS",
    "character": "CHARACTERS",
    "constellation": "CONSTELLATIONS",
    "region": "REGIONS",
}


def search_global_entities(query, limit=6):
    """Grouped top-bar search. Keeps /api/entity-search default behavior unchanged."""
    term = (query or "").strip()
    try:
        per_group_limit = int(limit)
    except (TypeError, ValueError):
        per_group_limit = 6
    per_group_limit = max(1, min(per_group_limit, 9))

    if not term:
        return []

    numeric_id = int(term) if term.isdigit() else None
    needle = term.lower()
    prefix = f"{needle}%"

    sql = """
        WITH candidates AS (
            (
                SELECT 'system'::text AS entity_type,
                       s.sde_key::bigint AS entity_id,
                       COALESCE(NULLIF(s.data->'name'->>'en', ''), s.sde_key) AS name,
                       NULL::text AS ticker,
                       COALESCE(NULLIF(r.data->'name'->>'en', ''), r.sde_key) AS context_name,
                       CASE
                           WHEN s.sde_key::bigint = %s THEN 0
                           WHEN lower(COALESCE(s.data->'name'->>'en', '')) = %s THEN 1
                           ELSE 3
                       END AS rank_score,
                       0 AS type_rank
                FROM sde_mapsolarsystems s
                LEFT JOIN sde_mapregions r ON r.sde_key = s.data->>'regionID'
                WHERE s.sde_key::bigint = %s
                   OR lower(COALESCE(s.data->'name'->>'en', '')) LIKE %s
                ORDER BY rank_score ASC, lower(COALESCE(s.data->'name'->>'en', '')) ASC
                LIMIT %s
            )
            UNION ALL
            (
                SELECT 'ship'::text AS entity_type,
                       se.entity_id::bigint AS entity_id,
                       COALESCE(NULLIF(se.name, ''), 'Unknown') AS name,
                       NULL::text AS ticker,
                       COALESCE(NULLIF(se.group_name, ''), 'Ship') AS context_name,
                       CASE
                           WHEN se.entity_id = %s THEN 0
                           WHEN lower(COALESCE(se.name, '')) = %s THEN 1
                           ELSE 3
                       END AS rank_score,
                       1 AS type_rank
                FROM sde_work.ship_entities se
                WHERE se.entity_id = %s
                   OR lower(COALESCE(se.name, '')) LIKE %s
                ORDER BY rank_score ASC, lower(COALESCE(se.name, '')) ASC
                LIMIT %s
            )
            UNION ALL
            (
                SELECT 'ship_group'::text AS entity_type,
                       MIN(se.entity_id)::bigint AS entity_id,
                       se.group_name AS name,
                       NULL::text AS ticker,
                       MIN(se.image_url)::text AS context_name,
                       CASE
                           WHEN lower(COALESCE(se.group_name, '')) = %s THEN 1
                           ELSE 3
                       END AS rank_score,
                       2 AS type_rank
                FROM sde_work.ship_entities se
                WHERE se.group_name IS NOT NULL
                  AND lower(se.group_name) LIKE %s
                GROUP BY se.group_name
                ORDER BY rank_score ASC, lower(se.group_name) ASC
                LIMIT %s
            )
            UNION ALL
            (
                SELECT 'alliance'::text AS entity_type,
                       alliance_id::bigint AS entity_id,
                       COALESCE(NULLIF(name, ''), 'Unknown') AS name,
                       ticker,
                       NULL::text AS context_name,
                       CASE
                           WHEN alliance_id = %s THEN 0
                           WHEN lower(COALESCE(ticker, '')) = %s THEN 1
                           WHEN lower(COALESCE(name, '')) = %s THEN 2
                           WHEN lower(COALESCE(ticker, '')) LIKE %s THEN 3
                           ELSE 4
                       END AS rank_score,
                       3 AS type_rank
                FROM entities.alliances
                WHERE alliance_id = %s
                   OR lower(COALESCE(name, '')) LIKE %s
                   OR lower(COALESCE(ticker, '')) LIKE %s
                ORDER BY rank_score ASC, lower(COALESCE(name, '')) ASC
                LIMIT %s
            )
            UNION ALL
            (
                SELECT 'corporation'::text AS entity_type,
                       corporation_id::bigint AS entity_id,
                       COALESCE(NULLIF(name, ''), 'Unknown') AS name,
                       ticker,
                       NULL::text AS context_name,
                       CASE
                           WHEN corporation_id = %s THEN 0
                           WHEN lower(COALESCE(ticker, '')) = %s THEN 1
                           WHEN lower(COALESCE(name, '')) = %s THEN 2
                           WHEN lower(COALESCE(ticker, '')) LIKE %s THEN 3
                           ELSE 4
                       END AS rank_score,
                       4 AS type_rank
                FROM entities.corporations
                WHERE corporation_id = %s
                   OR lower(COALESCE(name, '')) LIKE %s
                   OR lower(COALESCE(ticker, '')) LIKE %s
                ORDER BY rank_score ASC, lower(COALESCE(name, '')) ASC
                LIMIT %s
            )
            UNION ALL
            (
                SELECT 'character'::text AS entity_type,
                       character_id::bigint AS entity_id,
                       COALESCE(NULLIF(name, ''), 'Unknown') AS name,
                       NULL::text AS ticker,
                       NULL::text AS context_name,
                       CASE
                           WHEN character_id = %s THEN 0
                           WHEN lower(COALESCE(name, '')) = %s THEN 1
                           ELSE 3
                       END AS rank_score,
                       5 AS type_rank
                FROM entities.characters
                WHERE character_id = %s
                   OR lower(COALESCE(name, '')) LIKE %s
                ORDER BY rank_score ASC, lower(COALESCE(name, '')) ASC
                LIMIT %s
            )
            UNION ALL
            (
                SELECT 'constellation'::text AS entity_type,
                       c.sde_key::bigint AS entity_id,
                       COALESCE(NULLIF(c.data->'name'->>'en', ''), c.sde_key) AS name,
                       NULL::text AS ticker,
                       COALESCE(NULLIF(r.data->'name'->>'en', ''), r.sde_key) AS context_name,
                       CASE
                           WHEN c.sde_key::bigint = %s THEN 0
                           WHEN lower(COALESCE(c.data->'name'->>'en', '')) = %s THEN 1
                           ELSE 3
                       END AS rank_score,
                       6 AS type_rank
                FROM sde_mapconstellations c
                LEFT JOIN sde_mapregions r ON r.sde_key = c.data->>'regionID'
                WHERE c.sde_key::bigint = %s
                   OR lower(COALESCE(c.data->'name'->>'en', '')) LIKE %s
                ORDER BY rank_score ASC, lower(COALESCE(c.data->'name'->>'en', '')) ASC
                LIMIT %s
            )
            UNION ALL
            (
                SELECT 'region'::text AS entity_type,
                       r.sde_key::bigint AS entity_id,
                       COALESCE(NULLIF(r.data->'name'->>'en', ''), r.sde_key) AS name,
                       NULL::text AS ticker,
                       NULL::text AS context_name,
                       CASE
                           WHEN r.sde_key::bigint = %s THEN 0
                           WHEN lower(COALESCE(r.data->'name'->>'en', '')) = %s THEN 1
                           ELSE 3
                       END AS rank_score,
                       7 AS type_rank
                FROM sde_mapregions r
                WHERE r.sde_key::bigint = %s
                   OR lower(COALESCE(r.data->'name'->>'en', '')) LIKE %s
                ORDER BY rank_score ASC, lower(COALESCE(r.data->'name'->>'en', '')) ASC
                LIMIT %s
            )
        )
        SELECT entity_type, entity_id, name, ticker, context_name, rank_score, type_rank
        FROM candidates
        ORDER BY type_rank ASC, rank_score ASC, lower(name) ASC
    """

    params = (
        numeric_id, needle, numeric_id, prefix, per_group_limit,
        numeric_id, needle, numeric_id, prefix, per_group_limit,
        needle, prefix, per_group_limit,
        numeric_id, needle, needle, prefix, numeric_id, prefix, prefix, per_group_limit,
        numeric_id, needle, needle, prefix, numeric_id, prefix, prefix, per_group_limit,
        numeric_id, needle, numeric_id, prefix, per_group_limit,
        numeric_id, needle, numeric_id, prefix, per_group_limit,
        numeric_id, needle, numeric_id, prefix, per_group_limit,
    )

    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '900ms'")
                cur.execute(sql, params)
                rows = cur.fetchall()
    except QueryCanceled:
        return []

    results = []
    seen = set()
    for entity_type, entity_id, name, ticker, context_name, _rank_score, _type_rank in rows:
        key = (entity_type, int(entity_id))
        if key in seen:
            continue
        seen.add(key)
        if entity_type == "ship_group":
            group_name = name or "Unknown"
            item = {
                "entity_type": "ship_group",
                "entity_id": int(entity_id),
                "name": group_name,
                "ticker": None,
                "label": group_name,
                "subtitle": "Ship type",
                "url": f"/ship-category?group={quote(group_name)}",
                "image_url": context_name or image_url("ship", entity_id, 32),
                "section": GLOBAL_SEARCH_SECTIONS["ship_group"],
                "context_name": None,
            }
        else:
            item = _format_result(entity_type, entity_id, name, ticker)
            item["section"] = GLOBAL_SEARCH_SECTIONS.get(entity_type, entity_type.upper())
            item["context_name"] = context_name or None
            if entity_type in {"system", "constellation"} and context_name:
                item["label"] = f"{name} ({context_name})"
            elif entity_type == "ship" and context_name:
                item["subtitle"] = context_name
        results.append(item)

    return results


def _entity_table_lookup(conn, entity_type, entity_id):
    entity_type = normalize_entity_type(entity_type)
    if entity_type == "character":
        table = "characters"
        id_column = "character_id"
        subtitle = "Character profile"
    elif entity_type == "corporation":
        table = "corporations"
        id_column = "corporation_id"
        subtitle = "Corporation profile"
    else:
        table = "alliances"
        id_column = "alliance_id"
        subtitle = "Alliance profile"

    if not _table_exists(conn, "entities", table):
        return None, subtitle, None

    if entity_type == "character":
        select_sql = f'SELECT name, NULL::text AS ticker FROM entities."{table}" WHERE "{id_column}" = %s LIMIT 1'
    else:
        select_sql = f'SELECT name, ticker FROM entities."{table}" WHERE "{id_column}" = %s LIMIT 1'

    with conn.cursor() as cur:
        cur.execute(
            select_sql,
            (entity_id,),
        )
        row = cur.fetchone()

    if not row:
        return None, subtitle, None

    return row[0], subtitle, row[1] if len(row) > 1 else None


def _current_corporation_profile(conn, corporation_id):
    if not _table_exists(conn, "entities", "corporation_current_affiliation"):
        return None

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                cca.alliance_id,
                a.name AS alliance_name,
                a.ticker AS alliance_ticker
            FROM entities.corporation_current_affiliation cca
            LEFT JOIN entities.alliances a
              ON a.alliance_id = cca.alliance_id
            WHERE cca.corporation_id = %s
            LIMIT 1
            """,
            (corporation_id,),
        )
        row = cur.fetchone()

    if not row or row[0] is None:
        return None

    return {
        "entity_type": "alliance",
        "entity_id": int(row[0]),
        "name": row[1] or "Unknown",
        "ticker": row[2],
        "url": profile_url("alliance", row[0]),
    }


def _current_character_profile(conn, character_id):
    if not _table_exists(conn, "entities", "character_current_affiliation"):
        return None, None

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                cca.corporation_id,
                c.name AS corporation_name,
                c.ticker AS corporation_ticker,
                COALESCE(cca.alliance_id, corp_aff.alliance_id) AS alliance_id,
                a.name AS alliance_name,
                a.ticker AS alliance_ticker
            FROM entities.character_current_affiliation cca
            LEFT JOIN entities.corporations c
              ON c.corporation_id = cca.corporation_id
            LEFT JOIN entities.corporation_current_affiliation corp_aff
              ON corp_aff.corporation_id = cca.corporation_id
            LEFT JOIN entities.alliances a
              ON a.alliance_id = COALESCE(cca.alliance_id, corp_aff.alliance_id)
            WHERE cca.character_id = %s
            LIMIT 1
            """,
            (character_id,),
        )
        row = cur.fetchone()

    if not row:
        return None, None

    corporation = None
    if row[0] is not None:
        corporation = {
            "entity_type": "corporation",
            "entity_id": int(row[0]),
            "name": row[1] or "Unknown",
            "ticker": row[2],
            "url": profile_url("corporation", row[0]),
        }

    alliance = None
    if row[3] is not None:
        alliance = {
            "entity_type": "alliance",
            "entity_id": int(row[3]),
            "name": row[4] or "Unknown",
            "ticker": row[5],
            "url": profile_url("alliance", row[3]),
        }

    return corporation, alliance



def type_icon_url(type_id, size=64):
    if type_id is None:
        return None
    return f"https://images.evetech.net/types/{int(type_id)}/icon?size={int(size)}"


def _format_type_name(type_id):
    if type_id is None:
        return "Unknown"
    return f"Type {int(type_id)}"


def _format_system_name(system_id):
    if system_id is None:
        return "Unknown"
    return f"System {int(system_id)}"


def _sde_json_table(conn, preferred_names):
    lowered = [name.lower() for name in preferred_names]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.table_name
            FROM information_schema.columns c
            WHERE c.table_schema = 'public'
              AND c.column_name = 'data'
              AND lower(c.table_name) = ANY(%s)
            LIMIT 1
            """,
            (lowered,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _quote_ident(name):
    return '"' + str(name).replace('"', '""') + '"'


def _lookup_type_names(conn, type_ids):
    ids = sorted({int(value) for value in type_ids if value is not None})
    if not ids:
        return {}

    table = _sde_json_table(conn, ["sde_types"])
    if not table:
        return {}

    query = f"""
        SELECT
            (data->>'_key')::BIGINT AS type_id,
            COALESCE(
                data->'name'->>'en',
                data->>'typeName',
                data->>'name',
                'Type ' || (data->>'_key')
            ) AS type_name
        FROM public.{_quote_ident(table)}
        WHERE (data->>'_key')::BIGINT = ANY(%s)
    """
    with conn.cursor() as cur:
        cur.execute(query, (ids,))
        return {int(row[0]): row[1] for row in cur.fetchall()}


def _sde_int_expr(*keys):
    parts = []
    for key in keys:
        escaped = str(key).replace("'", "''")
        parts.append(f"NULLIF(data->>'{escaped}', '')")
    return "COALESCE(" + ", ".join(parts) + ")::BIGINT"


def _lookup_constellations(conn, constellation_ids):
    ids = sorted({int(value) for value in constellation_ids if value is not None})
    if not ids:
        return {}

    table = _sde_json_table(conn, [
        "sde_mapconstellations",
        "sde_mapConstellations",
        "sde_constellations",
    ])
    if not table:
        return {}

    id_expr = _sde_int_expr("_key", "constellation_id", "constellationID")
    region_expr = _sde_int_expr("region_id", "regionID")
    query = f"""
        SELECT
            {id_expr} AS constellation_id,
            COALESCE(
                data->'name'->>'en',
                data->>'constellationName',
                data->>'name',
                'Constellation ' || ({id_expr})::TEXT
            ) AS constellation_name,
            {region_expr} AS region_id
        FROM public.{_quote_ident(table)}
        WHERE {id_expr} = ANY(%s)
    """
    with conn.cursor() as cur:
        cur.execute(query, (ids,))
        return {int(row[0]): {"id": int(row[0]), "name": row[1], "region_id": int(row[2]) if row[2] is not None else None} for row in cur.fetchall()}


def _lookup_regions(conn, region_ids):
    ids = sorted({int(value) for value in region_ids if value is not None})
    if not ids:
        return {}

    table = _sde_json_table(conn, [
        "sde_mapregions",
        "sde_mapRegions",
        "sde_regions",
    ])
    if not table:
        return {}

    id_expr = _sde_int_expr("_key", "region_id", "regionID")
    query = f"""
        SELECT
            {id_expr} AS region_id,
            COALESCE(
                data->'name'->>'en',
                data->>'regionName',
                data->>'name',
                'Region ' || ({id_expr})::TEXT
            ) AS region_name
        FROM public.{_quote_ident(table)}
        WHERE {id_expr} = ANY(%s)
    """
    with conn.cursor() as cur:
        cur.execute(query, (ids,))
        return {int(row[0]): {"id": int(row[0]), "name": row[1]} for row in cur.fetchall()}


def _lookup_system_locations(conn, system_ids):
    ids = sorted({int(value) for value in system_ids if value is not None})
    if not ids:
        return {}

    table = _sde_json_table(conn, [
        "sde_mapsolarsystems",
        "sde_mapSolarSystems",
        "sde_solar_systems",
        "sde_solarsystems",
    ])
    if not table:
        return {}

    system_expr = _sde_int_expr("_key", "solar_system_id", "solarSystemID", "solarSystemId")
    constellation_expr = _sde_int_expr("constellation_id", "constellationID", "constellationId")
    region_expr = _sde_int_expr("region_id", "regionID", "regionId")
    query = f"""
        SELECT
            {system_expr} AS solar_system_id,
            COALESCE(
                data->'name'->>'en',
                data->>'solarSystemName',
                data->>'name',
                'System ' || ({system_expr})::TEXT
            ) AS system_name,
            {constellation_expr} AS constellation_id,
            {region_expr} AS region_id
        FROM public.{_quote_ident(table)}
        WHERE {system_expr} = ANY(%s)
    """
    with conn.cursor() as cur:
        cur.execute(query, (ids,))
        rows = cur.fetchall()

    systems = {}
    constellation_ids = set()
    region_ids = set()
    for system_id, system_name, constellation_id, region_id in rows:
        system_id = int(system_id)
        constellation_id = int(constellation_id) if constellation_id is not None else None
        region_id = int(region_id) if region_id is not None else None
        if constellation_id is not None:
            constellation_ids.add(constellation_id)
        if region_id is not None:
            region_ids.add(region_id)
        systems[system_id] = {
            "system": _location_ref("system", system_id, system_name),
            "constellation": None,
            "region": None,
            "constellation_id": constellation_id,
            "region_id": region_id,
        }

    constellations = _lookup_constellations(conn, constellation_ids)
    for data in constellations.values():
        if data.get("region_id") is not None:
            region_ids.add(data["region_id"])
    regions = _lookup_regions(conn, region_ids)

    for system_id, payload in systems.items():
        constellation_id = payload.get("constellation_id")
        constellation = constellations.get(constellation_id) if constellation_id is not None else None
        if constellation:
            payload["constellation"] = _location_ref("constellation", constellation["id"], constellation["name"])
            if payload.get("region_id") is None:
                payload["region_id"] = constellation.get("region_id")
        region_id = payload.get("region_id")
        region = regions.get(region_id) if region_id is not None else None
        if region:
            payload["region"] = _location_ref("region", region["id"], region["name"])

    return systems


def _lookup_system_names(conn, system_ids):
    # Backward-compatible wrapper. Prefer _lookup_system_locations for new code.
    return {
        system_id: payload["system"]["name"]
        for system_id, payload in _lookup_system_locations(conn, system_ids).items()
        if payload.get("system")
    }


def _location_profile_lookup(conn, entity_type, entity_id):
    entity_type = normalize_entity_type(entity_type)
    entity_id = int(entity_id)

    if entity_type == "system":
        payload = _lookup_system_locations(conn, [entity_id]).get(entity_id)
        if not payload:
            return {
                "name": _format_system_name(entity_id),
                "subtitle": "Solar system profile",
                "ticker": None,
                "current_constellation": None,
                "current_region": None,
                "image_url": image_url("system", entity_id, 128),
            }
        system = payload.get("system") or _location_ref("system", entity_id, _format_system_name(entity_id))
        return {
            "name": system.get("name") or _format_system_name(entity_id),
            "subtitle": "Solar system profile",
            "ticker": None,
            "current_constellation": payload.get("constellation"),
            "current_region": payload.get("region"),
            "image_url": system.get("image_url"),
        }

    if entity_type == "constellation":
        constellation = _lookup_constellations(conn, [entity_id]).get(entity_id)
        region_ref = None
        if constellation and constellation.get("region_id") is not None:
            region = _lookup_regions(conn, [constellation["region_id"]]).get(constellation["region_id"])
            if region:
                region_ref = _location_ref("region", region["id"], region["name"])
        return {
            "name": constellation.get("name") if constellation else f"Constellation {entity_id}",
            "subtitle": "Constellation profile",
            "ticker": None,
            "current_constellation": None,
            "current_region": region_ref,
            "image_url": image_url("constellation", entity_id, 128),
        }

    if entity_type == "region":
        region = _lookup_regions(conn, [entity_id]).get(entity_id)
        return {
            "name": region.get("name") if region else f"Region {entity_id}",
            "subtitle": "Region profile",
            "ticker": None,
            "current_constellation": None,
            "current_region": None,
            "image_url": image_url("region", entity_id, 128),
        }

    raise EntityError("entity_type_invalid")


def _format_isk(value):
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    abs_value = abs(value)
    if abs_value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}b"
    if abs_value >= 1_000_000:
        return f"{value / 1_000_000:.2f}m"
    if abs_value >= 1_000:
        return f"{value / 1_000:.2f}k"
    return str(int(value))


def _damage_percent(damage_done, victim_damage_taken):
    damage = int(damage_done or 0)
    total = int(victim_damage_taken or 0)
    if damage <= 0:
        return "0%"
    if total <= 0:
        return None
    return f"{(damage / total) * 100:.1f}%"


def _location_ref(entity_type, entity_id, name):
    if entity_id is None:
        return None
    entity_id = int(entity_id)
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "name": name or f"{entity_type.title()} {entity_id}",
        "ticker": None,
        "url": f"/{entity_type}/{entity_id}",
        "image_url": None,
    }


def _entity_ref(entity_type, entity_id, name, ticker=None):
    if entity_id is None:
        return None
    entity_type = normalize_entity_type(entity_type)
    entity_id = int(entity_id)
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "name": name or "Unknown",
        "ticker": ticker,
        "url": profile_url(entity_type, entity_id),
        "image_url": image_url(entity_type, entity_id, 64),
    }


def _resolved_entity_ref(entity_type, entity_id, fallback_name, lookup):
    if entity_id is None:
        return None
    entity_id = int(entity_id)
    resolved = (lookup or {}).get(entity_id) or {}
    return _entity_ref(
        entity_type,
        entity_id,
        resolved.get("name") or fallback_name,
        resolved.get("ticker"),
    )



def _normalize_killmail_filters(filters):
    raw = filters or {}

    def scalar(name, allowed=None, default=None):
        value = str(raw.get(name) or "").strip()
        if not value:
            return default
        if allowed is not None and value not in allowed:
            raise EntityError(f"killmail_filter_invalid:{name}")
        return value

    def ids(name):
        value = raw.get(name) or []
        if isinstance(value, str):
            value = value.split(",")
        result = []
        for item in value:
            text = str(item or "").strip()
            if not text:
                continue
            try:
                parsed = int(text)
            except ValueError as exc:
                raise EntityError(f"killmail_filter_invalid:{name}") from exc
            if parsed not in result:
                result.append(parsed)
        return result

    def parsed_date(name):
        value = scalar(name)
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise EntityError(f"killmail_filter_invalid:{name}") from exc

    def parsed_datetime(name):
        value = scalar(name)
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise EntityError(f"killmail_filter_invalid:{name}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def parsed_int(name):
        value = scalar(name)
        if not value:
            return None
        try:
            return int(value)
        except ValueError as exc:
            raise EntityError(f"killmail_filter_invalid:{name}") from exc

    def raw_list(name):
        value = raw.get(name) or []
        if isinstance(value, str):
            value = [value]
        return [str(item).strip() for item in value if str(item or "").strip()]

    def ship_terms(name):
        result = []
        seen = set()
        for item in raw_list(name):
            parts = item.split(":", 1)
            if len(parts) != 2:
                raise EntityError(f"killmail_filter_invalid:{name}")
            role, raw_id = parts
            role = role.strip().lower()
            if role not in {"both", "attacker", "victim"}:
                raise EntityError(f"killmail_filter_invalid:{name}")
            try:
                type_id = int(raw_id)
            except ValueError as exc:
                raise EntityError(f"killmail_filter_invalid:{name}") from exc
            key = (role, type_id)
            if key not in seen:
                seen.add(key)
                result.append({"role": role, "type_id": type_id})
        return result

    def entity_terms(name):
        result = []
        seen = set()
        for item in raw_list(name):
            parts = item.split(":", 2)
            if len(parts) != 3:
                raise EntityError(f"killmail_filter_invalid:{name}")
            role, entity_type, raw_id = parts
            role = role.strip().lower()
            entity_type = entity_type.strip().lower()
            if entity_type == "player":
                entity_type = "character"
            if role not in {"both", "attacker", "victim"} or entity_type not in {"character", "corporation", "alliance"}:
                raise EntityError(f"killmail_filter_invalid:{name}")
            try:
                entity_id = int(raw_id)
            except ValueError as exc:
                raise EntityError(f"killmail_filter_invalid:{name}") from exc
            key = (role, entity_type, entity_id)
            if key not in seen:
                seen.add(key)
                result.append({"role": role, "entity_type": entity_type, "entity_id": entity_id})
        return result

    def zone_terms(name):
        result = []
        seen = set()
        security_zones = {"highsec", "lowsec", "nullsec", "pochven", "wormhole"}
        for item in raw_list(name):
            parts = item.split(":", 1)
            if len(parts) != 2:
                raise EntityError(f"killmail_filter_invalid:{name}")
            entity_type, raw_id = parts
            entity_type = entity_type.strip().lower()
            if entity_type == "security":
                entity_id = str(raw_id or "").strip().lower()
                if entity_id not in security_zones:
                    raise EntityError(f"killmail_filter_invalid:{name}")
            elif entity_type in LOCATION_ENTITY_TYPES:
                try:
                    entity_id = int(raw_id)
                except ValueError as exc:
                    raise EntityError(f"killmail_filter_invalid:{name}") from exc
            else:
                raise EntityError(f"killmail_filter_invalid:{name}")
            key = (entity_type, entity_id)
            if key not in seen:
                seen.add(key)
                result.append({"entity_type": entity_type, "entity_id": entity_id})
        return result

    result = {
        "participation": scalar("participation", {"both", "kills", "losses"}, "both"),
        "date_from": parsed_date("date_from"),
        "date_to": parsed_date("date_to"),
        "datetime_from": parsed_datetime("datetime_from"),
        "datetime_to": parsed_datetime("datetime_to"),
        "affiliation_corporation_ids": ids("affiliation_corporation_ids"),
        "affiliation_alliance_ids": ids("affiliation_alliance_ids"),
        "involved_corporation_ids": ids("involved_corporation_ids"),
        "involved_alliance_ids": ids("involved_alliance_ids"),
        "involved_role": scalar("involved_role", {"both", "attacker", "victim"}, "both"),
        "type_ids": ids("type_ids"),
        "type_role": scalar("type_role", {"both", "attacker", "victim"}, "both"),
        "module_type_ids": ids("module_type_ids"),
        "scan_before": parsed_datetime("scan_before"),
        "scan_month": parsed_date("scan_month"),
        "scan_row": parsed_int("scan_row"),
        "builder_ship_include": ship_terms("builder_ship_include"),
        "builder_ship_exclude": ship_terms("builder_ship_exclude"),
        "builder_entity_include": entity_terms("builder_entity_include"),
        "builder_entity_exclude": entity_terms("builder_entity_exclude"),
        "builder_zone_include": zone_terms("builder_zone_include"),
        "builder_zone_exclude": zone_terms("builder_zone_exclude"),
    }
    if result["date_from"] and result["date_to"] and result["date_from"] > result["date_to"]:
        raise EntityError("killmail_filter_date_range_invalid")
    if (
        result["datetime_from"]
        and result["datetime_to"]
        and result["datetime_from"] >= result["datetime_to"]
    ):
        raise EntityError("killmail_filter_datetime_range_invalid")
    result["active"] = bool(
        result["participation"] != "both"
        or result["date_from"]
        or result["date_to"]
        or result["datetime_from"]
        or result["datetime_to"]
        or result["affiliation_corporation_ids"]
        or result["affiliation_alliance_ids"]
        or result["involved_corporation_ids"]
        or result["involved_alliance_ids"]
        or result["type_ids"]
        or result["module_type_ids"]
        or result["builder_ship_include"]
        or result["builder_ship_exclude"]
        or result["builder_entity_include"]
        or result["builder_entity_exclude"]
        or result["builder_zone_include"]
        or result["builder_zone_exclude"]
    )
    return result


def _killmail_builder_entity_predicate(
    term,
    victim_alias="km",
    attacker_table_sql="rawkm.killmail_attackers",
):
    role = term["role"]
    entity_type = term["entity_type"]
    entity_id = int(term["entity_id"])

    if entity_type == "character":
        victim_predicate = f"{victim_alias}.victim_character_id = %s"
        victim_params = [entity_id]
        attacker_predicate = "ba.character_id = %s"
        attacker_params = [entity_id]
    elif entity_type == "corporation":
        victim_predicate = f"{victim_alias}.victim_corporation_id = %s"
        victim_params = [entity_id]
        attacker_predicate = "ba.corporation_id = %s"
        attacker_params = [entity_id]
    elif entity_type == "alliance":
        victim_predicate = _historical_alliance_scope_predicate(
            victim_alias,
            "victim_corporation_id",
            "victim_alliance_id",
            "killmail_time",
        )
        victim_params = [entity_id, entity_id]
        attacker_predicate = _historical_alliance_scope_predicate(
            "ba",
            "corporation_id",
            "alliance_id",
            "killmail_time",
        )
        attacker_params = [entity_id, entity_id]
    else:
        raise EntityError("killmail_filter_invalid:builder_entity")

    attacker_sql = (
        "EXISTS ("
        f"SELECT 1 FROM {attacker_table_sql} ba "
        f"WHERE ba.killmail_id = {victim_alias}.killmail_id "
        f"AND ba.killmail_time = {victim_alias}.killmail_time "
        f"AND {attacker_predicate}"
        ")"
    )

    if role == "victim":
        return victim_predicate, victim_params
    if role == "attacker":
        return attacker_sql, attacker_params
    return f"(({victim_predicate}) OR ({attacker_sql}))", victim_params + attacker_params


def _killmail_builder_ship_predicate(
    term,
    victim_alias="km",
    attacker_table_sql="rawkm.killmail_attackers",
):
    role = term["role"]
    type_id = int(term["type_id"])
    victim_predicate = f"{victim_alias}.victim_ship_type_id = %s"
    attacker_predicate = (
        "EXISTS ("
        f"SELECT 1 FROM {attacker_table_sql} bsa "
        f"WHERE bsa.killmail_id = {victim_alias}.killmail_id "
        f"AND bsa.killmail_time = {victim_alias}.killmail_time "
        "AND bsa.ship_type_id = %s"
        ")"
    )
    if role == "victim":
        return victim_predicate, [type_id]
    if role == "attacker":
        return attacker_predicate, [type_id]
    return f"(({victim_predicate}) OR ({attacker_predicate}))", [type_id, type_id]


def _killmail_security_zone_system_ids(conn, zone_key):
    """Resolve the quick zone presets to solar-system IDs.

    Rules:
    - Pochven is the canonical SDE region 10000070.
    - Wormhole space is Anoikis region range 11000001..11000033.
    - High/Low/Null use the real SDE securityStatus, but explicitly exclude
      Pochven and Anoikis.
    - Missing securityStatus is never silently treated as nullsec.
    """
    zone_key = str(zone_key or "").strip().lower()

    with conn.cursor() as cur:
        if zone_key == "pochven":
            cur.execute(
                """
                SELECT COALESCE(NULLIF(s.data->>'_key',''), NULLIF(s.sde_key,''))::BIGINT
                FROM public.sde_mapsolarsystems s
                WHERE NULLIF(s.data->>'regionID','')::BIGINT = 10000070
                ORDER BY 1
                """
            )

        elif zone_key == "wormhole":
            cur.execute(
                """
                SELECT COALESCE(NULLIF(s.data->>'_key',''), NULLIF(s.sde_key,''))::BIGINT
                FROM public.sde_mapsolarsystems s
                WHERE NULLIF(s.data->>'regionID','')::BIGINT
                      BETWEEN 11000001 AND 11000033
                ORDER BY 1
                """
            )

        elif zone_key in {"highsec", "lowsec", "nullsec"}:
            if zone_key == "highsec":
                security_sql = """
                    NULLIF(s.data->>'securityStatus','') IS NOT NULL
                    AND NULLIF(s.data->>'securityStatus','')::numeric >= 0.45
                """
            elif zone_key == "lowsec":
                security_sql = """
                    NULLIF(s.data->>'securityStatus','') IS NOT NULL
                    AND NULLIF(s.data->>'securityStatus','')::numeric > 0
                    AND NULLIF(s.data->>'securityStatus','')::numeric < 0.45
                """
            else:
                security_sql = """
                    NULLIF(s.data->>'securityStatus','') IS NOT NULL
                    AND NULLIF(s.data->>'securityStatus','')::numeric <= 0
                """

            cur.execute(
                f"""
                SELECT COALESCE(NULLIF(s.data->>'_key',''), NULLIF(s.sde_key,''))::BIGINT
                FROM public.sde_mapsolarsystems s
                WHERE {security_sql}
                  AND NULLIF(s.data->>'regionID','')::BIGINT <> 10000070
                  AND NOT (
                      NULLIF(s.data->>'regionID','')::BIGINT
                      BETWEEN 11000001 AND 11000033
                  )
                ORDER BY 1
                """
            )

        else:
            return []

        return [
            int(row[0])
            for row in cur.fetchall()
            if row[0] is not None
        ]


def _killmail_builder_zone_system_ids(conn, terms):
    ids = set()
    for term in terms:
        entity_type = term["entity_type"]
        entity_id = term["entity_id"]
        if entity_type == "security":
            values = _killmail_security_zone_system_ids(conn, entity_id)
        else:
            values = _location_system_ids(conn, entity_type, int(entity_id))
        for system_id in values:
            ids.add(int(system_id))
    return sorted(ids)


def _append_killmail_builder_clauses(
    conn,
    filters,
    clauses,
    params,
    victim_alias="km",
    attacker_table_sql="rawkm.killmail_attackers",
):
    ship_include = filters.get("builder_ship_include") or []
    ship_exclude = filters.get("builder_ship_exclude") or []
    entity_include = filters.get("builder_entity_include") or []
    entity_exclude = filters.get("builder_entity_exclude") or []
    zone_include = filters.get("builder_zone_include") or []
    zone_exclude = filters.get("builder_zone_exclude") or []

    if ship_include:
        # OR inside one side, AND between sides.
        # Example: +Avatar attacker +Ragnarok attacker = either attacking hull;
        # +Titan attacker +Supercarrier victim = both conditions must hold.
        by_role = {"attacker": [], "victim": [], "both": []}
        for term in ship_include:
            by_role[term["role"]].append(term)
        for role_terms in by_role.values():
            if not role_terms:
                continue
            parts = []
            part_params = []
            for term in role_terms:
                sql, values = _killmail_builder_ship_predicate(
                    term,
                    victim_alias=victim_alias,
                    attacker_table_sql=attacker_table_sql,
                )
                parts.append(sql)
                part_params.extend(values)
            clauses.append("(" + " OR ".join(parts) + ")")
            params.extend(part_params)

    for term in ship_exclude:
        sql, values = _killmail_builder_ship_predicate(
            term,
            victim_alias=victim_alias,
            attacker_table_sql=attacker_table_sql,
        )
        clauses.append(f"NOT ({sql})")
        params.extend(values)

    if entity_include:
        by_role = {"attacker": [], "victim": [], "both": []}
        for term in entity_include:
            by_role[term["role"]].append(term)
        for role_terms in by_role.values():
            if not role_terms:
                continue
            parts = []
            part_params = []
            for term in role_terms:
                sql, values = _killmail_builder_entity_predicate(
                    term,
                    victim_alias=victim_alias,
                    attacker_table_sql=attacker_table_sql,
                )
                parts.append(sql)
                part_params.extend(values)
            clauses.append("(" + " OR ".join(parts) + ")")
            params.extend(part_params)

    for term in entity_exclude:
        sql, values = _killmail_builder_entity_predicate(
            term,
            victim_alias=victim_alias,
            attacker_table_sql=attacker_table_sql,
        )
        clauses.append(f"NOT ({sql})")
        params.extend(values)

    if zone_include:
        system_ids = _killmail_builder_zone_system_ids(conn, zone_include)
        if system_ids:
            clauses.append(f"{victim_alias}.solar_system_id = ANY(%s)")
            params.append(system_ids)
        else:
            clauses.append("FALSE")

    if zone_exclude:
        system_ids = _killmail_builder_zone_system_ids(conn, zone_exclude)
        if system_ids:
            clauses.append(f"NOT ({victim_alias}.solar_system_id = ANY(%s))")
            params.append(system_ids)



def _type_only_entity_killmail_page_entries(conn, entity_type, entity_id, page, per_page, filters):
    """Fast all-history path for profile KBs filtered only by ship type.

    Scan rawkm month partitions newest -> oldest and stop as soon as the
    requested page is filled. This avoids building the entity's complete
    historical killmail set before applying the Super/Titan filter.
    """
    if str(entity_type or "").strip().lower() == "coalition":
        return None
    entity_type = normalize_entity_type(entity_type)
    if entity_type not in {"character", "corporation", "alliance"}:
        return None

    type_ids = filters.get("type_ids") or []
    if not type_ids or filters.get("type_role") != "both":
        return None

    # This optimized path must preserve the normal "type filter only" semantics.
    if (
        filters.get("participation") != "both"
        or filters.get("date_from")
        or filters.get("date_to")
        or filters.get("affiliation_corporation_ids")
        or filters.get("affiliation_alliance_ids")
        or filters.get("involved_corporation_ids")
        or filters.get("involved_alliance_ids")
        or filters.get("module_type_ids")
        or filters.get("builder_ship_include")
        or filters.get("builder_ship_exclude")
        or filters.get("builder_entity_include")
        or filters.get("builder_entity_exclude")
        or filters.get("builder_zone_include")
        or filters.get("builder_zone_exclude")
    ):
        return None

    type_ids = sorted({int(value) for value in type_ids if value is not None})
    if not type_ids:
        return None

    if entity_type == "character":
        victim_predicate = "km.victim_character_id = %s"
        victim_params = [entity_id]
        attacker_predicate = "owna.character_id = %s"
        attacker_params = [entity_id]
    elif entity_type == "corporation":
        victim_predicate = "km.victim_corporation_id = %s"
        victim_params = [entity_id]
        attacker_predicate = "owna.corporation_id = %s"
        attacker_params = [entity_id]
    else:
        victim_predicate = _historical_alliance_scope_predicate(
            "km",
            "victim_corporation_id",
            "victim_alliance_id",
            "killmail_time",
        )
        victim_params = [entity_id, entity_id]
        attacker_predicate = _historical_alliance_scope_predicate(
            "owna",
            "corporation_id",
            "alliance_id",
            "killmail_time",
        )
        attacker_params = [entity_id, entity_id]

    page = max(1, int(page or 1))
    per_page = max(1, min(100, int(per_page or 100)))
    offset = (page - 1) * per_page
    fetch_limit = offset + per_page + 1

    killmail_tables = _rawkm_partition_tables(conn, "killmails")
    if not killmail_tables or not _table_exists(conn, "rawkm", "killmail_attackers"):
        return [], False, page, per_page

    killmail_tables = _rawkm_server_month_tables_first(killmail_tables, "killmails")
    attacker_by_name = _attacker_partition_lookup(conn)

    merged = []
    seen = set()

    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '120000ms'")

        for killmail_schema, killmail_table in killmail_tables:
            if len(merged) >= fetch_limit:
                break

            killmail_relation = _qualified_rawkm_table(killmail_schema, killmail_table)
            attacker_table = _matching_attacker_table_for_killmail_table(
                attacker_by_name,
                killmail_table,
            )

            # Without the matching attacker partition we can still find selected
            # victim hulls, but cannot prove attacker-hull selection or kill-side
            # entity participation for that month.
            if attacker_table:
                attacker_relation = _qualified_rawkm_table(
                    attacker_table[0],
                    attacker_table[1],
                )

                query = f"""
                    WITH candidate AS MATERIALIZED (
                        SELECT km.killmail_id, km.killmail_time
                        FROM {killmail_relation} km
                        WHERE km.victim_ship_type_id = ANY(%s)

                        UNION

                        SELECT shipa.killmail_id, shipa.killmail_time
                        FROM {attacker_relation} shipa
                        WHERE shipa.ship_type_id = ANY(%s)
                    ),
                    entity_matches AS MATERIALIZED (
                        SELECT
                            c.killmail_id,
                            c.killmail_time,
                            ({victim_predicate}) AS is_loss,
                            EXISTS (
                                SELECT 1
                                FROM {attacker_relation} owna
                                WHERE owna.killmail_id = c.killmail_id
                                  AND owna.killmail_time = c.killmail_time
                                  AND {attacker_predicate}
                            ) AS is_kill
                        FROM candidate c
                        JOIN {killmail_relation} km
                          ON km.killmail_id = c.killmail_id
                         AND km.killmail_time = c.killmail_time
                    )
                    SELECT
                        CASE WHEN is_loss THEN 'loss'::text ELSE 'kill'::text END AS side,
                        killmail_id,
                        killmail_time
                    FROM entity_matches
                    WHERE is_loss OR is_kill
                    ORDER BY killmail_time DESC, killmail_id DESC
                    LIMIT %s
                """
                params = (
                    [type_ids, type_ids]
                    + victim_params
                    + attacker_params
                    + [fetch_limit]
                )
            else:
                query = f"""
                    SELECT
                        'loss'::text AS side,
                        km.killmail_id,
                        km.killmail_time
                    FROM {killmail_relation} km
                    WHERE km.victim_ship_type_id = ANY(%s)
                      AND {victim_predicate}
                    ORDER BY km.killmail_time DESC, km.killmail_id DESC
                    LIMIT %s
                """
                params = [type_ids] + victim_params + [fetch_limit]

            cur.execute(query, params)
            month_rows = cur.fetchall()

            for side, killmail_id, killmail_time in month_rows:
                kmid = int(killmail_id)
                if kmid in seen:
                    continue
                seen.add(kmid)
                merged.append((side, kmid, killmail_time))

            # Partitions are scanned newest first. Once enough rows have been
            # collected, no older partition can enter the requested page.
            if len(merged) >= fetch_limit:
                break

    merged.sort(key=lambda row: (row[2], row[1]), reverse=True)
    has_next = len(merged) > offset + per_page
    return merged[offset:offset + per_page], has_next, page, per_page



def _contextual_month_scope(entity_type, entity_id):
    if str(entity_type or "").strip().lower() == "coalition":
        scope_value = _normalize_coalition_killmail_scope(entity_id)
        return {
            "entity_type": "coalition",
            "victim_predicate": _coalition_raw_scope_predicate(
                "km", "victim_corporation_id", "victim_alliance_id", "killmail_time"
            ),
            "victim_params": _coalition_raw_scope_params(scope_value),
            "attacker_predicate": _coalition_raw_scope_predicate(
                "ka", "corporation_id", "alliance_id", "killmail_time"
            ),
            "attacker_params": _coalition_raw_scope_params(scope_value),
            "victim_side": "loss",
            "prefer_kill": False,
        }

    entity_type = normalize_entity_type(entity_type)

    if entity_type == "character":
        return {
            "entity_type": "character",
            "victim_predicate": "km.victim_character_id = %s",
            "victim_params": [int(entity_id)],
            "attacker_predicate": "ka.character_id = %s",
            "attacker_params": [int(entity_id)],
            "victim_side": "loss",
            "prefer_kill": False,
        }

    if entity_type == "corporation":
        return {
            "entity_type": "corporation",
            "victim_predicate": "km.victim_corporation_id = %s",
            "victim_params": [int(entity_id)],
            "attacker_predicate": "ka.corporation_id = %s",
            "attacker_params": [int(entity_id)],
            "victim_side": "loss",
            "prefer_kill": False,
        }

    if entity_type == "alliance":
        alliance_id = int(entity_id)
        return {
            "entity_type": "alliance",
            "victim_predicate": _historical_alliance_scope_predicate(
                "km", "victim_corporation_id", "victim_alliance_id", "killmail_time"
            ),
            "victim_params": [alliance_id, alliance_id],
            "attacker_predicate": _historical_alliance_scope_predicate(
                "ka", "corporation_id", "alliance_id", "killmail_time"
            ),
            "attacker_params": [alliance_id, alliance_id],
            "victim_side": "loss",
            "prefer_kill": False,
        }

    if entity_type == "ship":
        values = (
            sorted({int(value) for value in entity_id if value is not None})
            if isinstance(entity_id, (list, tuple, set))
            else [int(entity_id)]
        )
        if not values:
            raise EntityError("entity_profile_id_invalid")
        use_any = len(values) > 1 or isinstance(entity_id, (list, tuple, set))
        return {
            "entity_type": "ship",
            "victim_predicate": (
                "km.victim_ship_type_id = ANY(%s)"
                if use_any else
                "km.victim_ship_type_id = %s"
            ),
            "victim_params": [values if use_any else values[0]],
            "attacker_predicate": (
                "ka.ship_type_id = ANY(%s)"
                if use_any else
                "ka.ship_type_id = %s"
            ),
            "attacker_params": [values if use_any else values[0]],
            "victim_side": "loss",
            "prefer_kill": True,
        }

    if entity_type == "system":
        values = (
            sorted({int(value) for value in entity_id if value is not None})
            if isinstance(entity_id, (list, tuple, set))
            else [int(entity_id)]
        )
        if not values:
            raise EntityError("entity_profile_id_invalid")
        use_any = len(values) > 1 or isinstance(entity_id, (list, tuple, set))
        return {
            "entity_type": "system",
            "victim_predicate": (
                "km.solar_system_id = ANY(%s)"
                if use_any else
                "km.solar_system_id = %s"
            ),
            "victim_params": [values if use_any else values[0]],
            "attacker_predicate": None,
            "attacker_params": [],
            "victim_side": "kill",
            "prefer_kill": True,
        }

    raise EntityError("entity_type_invalid")


def _progressive_context_pagination(filters, per_page, has_next=False):
    meta = (filters or {}).get("_progressive_meta")
    if not meta:
        return None

    return {
        "page": 1,
        "per_page": per_page,
        "has_prev": False,
        "has_next": bool(meta.get("scan_has_more", has_next)),
        "prev_page": 1,
        "next_page": 1,
        "timed_out": False,
        "progressive": True,
        "scan_has_more": bool(meta.get("scan_has_more")),
        "scan_complete": bool(meta.get("scan_complete")),
        "scan_blocked": bool(meta.get("scan_blocked")),
        "scan_end_label": meta.get("scan_end_label"),
        "scan_before": meta.get("scan_before"),
        "scan_month": meta.get("scan_month"),
        "scan_row": meta.get("scan_row"),
        "scanned_month": meta.get("scanned_month"),
        "scanned_estimate": int(meta.get("scanned_estimate") or 0),
    }


def _filtered_entity_killmail_month_entries(
    conn,
    entity_type,
    entity_id,
    per_page,
    filters,
):
    """Search exactly one rawkm month for a contextual API killboard."""
    per_page = max(1, min(100, int(per_page or 100)))
    scope = _contextual_month_scope(entity_type, entity_id)

    month_map = _rawkm_month_partition_map(conn, "killmails")
    if not month_map:
        return [], {
            "scan_has_more": False,
            "scan_complete": True,
            "scan_blocked": False,
            "scan_end_label": None,
            "scan_before": None,
            "scan_month": None,
            "scan_row": None,
            "scanned_month": None,
            "scanned_estimate": 0,
        }

    requested_month = filters.get("scan_month")
    if requested_month is None:
        requested_month = max(month_map)
    else:
        requested_month = requested_month.replace(day=1)

    if filters.get("date_to"):
        requested_month = min(
            requested_month,
            filters["date_to"].replace(day=1),
        )

    floor_month = (
        filters["date_from"].replace(day=1)
        if filters.get("date_from")
        else min(month_map)
    )

    available = [
        month
        for month in month_map
        if floor_month <= month <= requested_month
    ]
    if not available:
        return [], {
            "scan_has_more": False,
            "scan_complete": True,
            "scan_blocked": False,
            "scan_end_label": requested_month.isoformat(),
            "scan_before": None,
            "scan_month": None,
            "scan_row": None,
            "scanned_month": requested_month.isoformat(),
            "scanned_estimate": 0,
        }

    scan_month = max(available)
    schema_name, table_name = month_map[scan_month]
    killmail_relation = _qualified_rawkm_table(schema_name, table_name)

    attacker_lookup = _attacker_partition_lookup(conn)
    attacker_table = _matching_attacker_table_for_killmail_table(
        attacker_lookup,
        table_name,
    )
    attacker_relation = (
        _qualified_rawkm_table(attacker_table[0], attacker_table[1])
        if attacker_table
        else "rawkm.killmail_attackers"
    )
    item_relation = _monthly_child_table(
        conn,
        "killmail_items",
        table_name,
    )

    base_selects = [
        f"""
        SELECT
            %s::text AS side,
            km.killmail_id,
            km.killmail_time
        FROM {killmail_relation} km
        WHERE {scope["victim_predicate"]}
        """
    ]
    base_params = [scope["victim_side"]] + list(scope["victim_params"])

    if scope["attacker_predicate"]:
        base_selects.append(
            f"""
            SELECT
                'kill'::text AS side,
                ka.killmail_id,
                ka.killmail_time
            FROM {attacker_relation} ka
            WHERE {scope["attacker_predicate"]}
            """
        )
        base_params.extend(scope["attacker_params"])

    preferred_side = "kill" if scope["prefer_kill"] else "loss"

    clauses = []
    params = list(base_params)

    if scope["entity_type"] != "system":
        if filters["participation"] == "kills":
            clauses.append("base.side = 'kill'")
        elif filters["participation"] == "losses":
            clauses.append("base.side = 'loss'")

    if filters.get("date_from"):
        clauses.append("base.killmail_time >= %s::date")
        params.append(filters["date_from"])
    if filters.get("date_to"):
        clauses.append("base.killmail_time < (%s::date + INTERVAL '1 day')")
        params.append(filters["date_to"])

    scan_before = filters.get("scan_before")
    scan_row = filters.get("scan_row")
    if scan_before is not None:
        if scan_row is not None:
            clauses.append(
                "(base.killmail_time, base.killmail_id) < "
                "(%s::timestamptz, %s::bigint)"
            )
            params.extend([scan_before, int(scan_row)])
        else:
            clauses.append("base.killmail_time < %s::timestamptz")
            params.append(scan_before)

    if scope["entity_type"] == "character" and filters["affiliation_corporation_ids"]:
        clauses.append(
            "((base.side = 'loss' AND km.victim_corporation_id = ANY(%s)) "
            f"OR (base.side = 'kill' AND EXISTS ("
            f"SELECT 1 FROM {attacker_relation} owna "
            "WHERE owna.killmail_id = base.killmail_id "
            "AND owna.killmail_time = base.killmail_time "
            "AND owna.character_id = %s "
            "AND owna.corporation_id = ANY(%s))))"
        )
        params.extend([
            filters["affiliation_corporation_ids"],
            int(entity_id),
            filters["affiliation_corporation_ids"],
        ])

    if scope["entity_type"] in {"character", "corporation"} and filters["affiliation_alliance_ids"]:
        own_column = (
            "character_id"
            if scope["entity_type"] == "character"
            else "corporation_id"
        )
        clauses.append(
            "((base.side = 'loss' AND km.victim_alliance_id = ANY(%s)) "
            f"OR (base.side = 'kill' AND EXISTS ("
            f"SELECT 1 FROM {attacker_relation} owna "
            "WHERE owna.killmail_id = base.killmail_id "
            "AND owna.killmail_time = base.killmail_time "
            f"AND owna.{own_column} = %s "
            "AND owna.alliance_id = ANY(%s))))"
        )
        params.extend([
            filters["affiliation_alliance_ids"],
            int(entity_id),
            filters["affiliation_alliance_ids"],
        ])

    def involved_clause(kind, values):
        victim = f"km.victim_{kind}_id = ANY(%s)"
        attacker = (
            f"EXISTS (SELECT 1 FROM {attacker_relation} inva "
            "WHERE inva.killmail_id = base.killmail_id "
            "AND inva.killmail_time = base.killmail_time "
            f"AND inva.{kind}_id = ANY(%s))"
        )
        if filters["involved_role"] == "victim":
            return victim, [values]
        if filters["involved_role"] == "attacker":
            return attacker, [values]
        return f"({victim} OR {attacker})", [values, values]

    for kind, key in (
        ("corporation", "involved_corporation_ids"),
        ("alliance", "involved_alliance_ids"),
    ):
        if filters[key]:
            clause, values = involved_clause(kind, filters[key])
            clauses.append(clause)
            params.extend(values)

    if filters["type_ids"]:
        victim = "km.victim_ship_type_id = ANY(%s)"
        attacker = (
            f"EXISTS (SELECT 1 FROM {attacker_relation} shipa "
            "WHERE shipa.killmail_id = base.killmail_id "
            "AND shipa.killmail_time = base.killmail_time "
            "AND shipa.ship_type_id = ANY(%s))"
        )
        if filters["type_role"] == "victim":
            clauses.append(victim)
            params.append(filters["type_ids"])
        elif filters["type_role"] == "attacker":
            clauses.append(attacker)
            params.append(filters["type_ids"])
        else:
            clauses.append(f"({victim} OR {attacker})")
            params.extend([filters["type_ids"], filters["type_ids"]])

    if filters["module_type_ids"]:
        clauses.append(
            f"EXISTS (SELECT 1 FROM {item_relation} modi "
            "WHERE modi.killmail_id = base.killmail_id "
            "AND modi.killmail_time = base.killmail_time "
            "AND modi.item_type_id = ANY(%s))"
        )
        params.append(filters["module_type_ids"])

    _append_killmail_builder_clauses(
        conn,
        filters,
        clauses,
        params,
        victim_alias="km",
        attacker_table_sql=attacker_relation,
    )

    where_sql = (
        "WHERE " + " AND ".join(clauses)
        if clauses
        else ""
    )

    query = f"""
        WITH base_rows AS (
            {" UNION ALL ".join(base_selects)}
        ),
        base AS (
            SELECT DISTINCT ON (killmail_id)
                side,
                killmail_id,
                killmail_time
            FROM base_rows
            ORDER BY
                killmail_id,
                CASE WHEN side = '{preferred_side}' THEN 0 ELSE 1 END
        )
        SELECT
            base.side,
            base.killmail_id,
            base.killmail_time
        FROM base
        JOIN {killmail_relation} km
          ON km.killmail_id = base.killmail_id
         AND km.killmail_time = base.killmail_time
        {where_sql}
        ORDER BY base.killmail_time DESC, base.killmail_id DESC
        LIMIT %s
    """
    params.append(per_page + 1)

    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '45000ms'")
            cur.execute(query, params)
            rows = cur.fetchall()
    except QueryCanceled:
        conn.rollback()
        return [], {
            "scan_has_more": True,
            "scan_complete": False,
            "scan_blocked": True,
            "scan_end_label": scan_month.isoformat(),
            "scan_before": None,
            "scan_month": scan_month.isoformat(),
            "scan_row": None,
            "scanned_month": scan_month.isoformat(),
            "scanned_estimate": 0,
        }

    has_extra = len(rows) > per_page
    visible = rows[:per_page]

    if has_extra and visible:
        _last_side, last_id, last_time = visible[-1]
        next_month = scan_month
        next_before = last_time
        next_row = int(last_id)
        scanned_estimate = 0
        scan_complete = False
    else:
        next_month = _previous_available_month(
            month_map,
            scan_month,
            floor_month,
        )
        next_before = None
        next_row = None
        scanned_estimate = _rawkm_partition_estimated_rows(
            conn,
            schema_name,
            table_name,
        )
        scan_complete = next_month is None

    return visible, {
        "scan_has_more": not scan_complete,
        "scan_complete": scan_complete,
        "scan_blocked": False,
        "scan_end_label": scan_month.isoformat(),
        "scan_before": (
            next_before.isoformat()
            if next_before is not None
            else None
        ),
        "scan_month": (
            next_month.isoformat()
            if next_month is not None
            else None
        ),
        "scan_row": next_row,
        "scanned_month": scan_month.isoformat(),
        "scanned_estimate": scanned_estimate,
    }

def _filtered_entity_killmail_page_entries(conn, entity_type, entity_id, page, per_page, filters):
    if filters.get("scan_month") is not None:
        rows, meta = _filtered_entity_killmail_month_entries(
            conn,
            entity_type,
            entity_id,
            per_page,
            filters,
        )
        filters["_progressive_meta"] = meta
        return rows, bool(meta.get("scan_has_more")), 1, per_page

    fast_page = _type_only_entity_killmail_page_entries(
        conn,
        entity_type,
        entity_id,
        page,
        per_page,
        filters,
    )
    if fast_page is not None:
        return fast_page

    raw_entity_type = str(entity_type or "").strip().lower()
    if raw_entity_type == "coalition":
        entity_type = "coalition"
        scope_value = _normalize_coalition_killmail_scope(entity_id)
        victim_scope_predicate = _coalition_raw_scope_predicate(
            "km", "victim_corporation_id", "victim_alliance_id", "killmail_time"
        )
        attacker_scope_predicate = _coalition_raw_scope_predicate(
            "ka", "corporation_id", "alliance_id", "killmail_time"
        )
        base_params = _coalition_raw_scope_params(scope_value) + _coalition_raw_scope_params(scope_value)
    else:
        entity_type = normalize_entity_type(entity_type)

    if entity_type == "coalition":
        pass
    elif entity_type == "character":
        victim_column = "victim_character_id"
        attacker_column = "character_id"
        victim_scope_predicate = f"km.{victim_column} = %s"
        attacker_scope_predicate = f"ka.{attacker_column} = %s"
        base_params = [entity_id, entity_id]
    elif entity_type == "corporation":
        victim_column = "victim_corporation_id"
        attacker_column = "corporation_id"
        victim_scope_predicate = f"km.{victim_column} = %s"
        attacker_scope_predicate = f"ka.{attacker_column} = %s"
        base_params = [entity_id, entity_id]
    elif entity_type == "alliance":
        victim_column = "victim_alliance_id"
        attacker_column = "alliance_id"
        victim_scope_predicate = _historical_alliance_scope_predicate(
            "km", "victim_corporation_id", "victim_alliance_id", "killmail_time"
        )
        attacker_scope_predicate = _historical_alliance_scope_predicate(
            "ka", "corporation_id", "alliance_id", "killmail_time"
        )
        base_params = [entity_id, entity_id, entity_id, entity_id]
    elif entity_type == "ship":
        victim_column = "victim_ship_type_id"
        attacker_column = "ship_type_id"
        if isinstance(entity_id, (list, tuple, set)):
            scope_value = sorted({int(value) for value in entity_id if value is not None})
            victim_scope_predicate = f"km.{victim_column} = ANY(%s)"
            attacker_scope_predicate = f"ka.{attacker_column} = ANY(%s)"
            base_params = [scope_value, scope_value]
        else:
            victim_scope_predicate = f"km.{victim_column} = %s"
            attacker_scope_predicate = f"ka.{attacker_column} = %s"
            base_params = [entity_id, entity_id]
    else:
        raise EntityError("entity_type_invalid")

    page = max(1, int(page or 1))
    per_page = max(1, min(100, int(per_page or 100)))
    offset = (page - 1) * per_page
    params = list(base_params)
    clauses = []

    if filters["participation"] == "kills":
        clauses.append("base.side = 'kill'")
    elif filters["participation"] == "losses":
        clauses.append("base.side = 'loss'")

    if filters["date_from"]:
        clauses.append("base.killmail_time >= %s::date")
        params.append(filters["date_from"])
    if filters["date_to"]:
        clauses.append("base.killmail_time < (%s::date + INTERVAL '1 day')")
        params.append(filters["date_to"])
    if filters.get("datetime_from"):
        clauses.append("base.killmail_time >= %s::timestamptz")
        params.append(filters["datetime_from"])
    if filters.get("datetime_to"):
        clauses.append("base.killmail_time < %s::timestamptz")
        params.append(filters["datetime_to"])

    if entity_type == "character" and filters["affiliation_corporation_ids"]:
        clauses.append("((base.side = 'loss' AND km.victim_corporation_id = ANY(%s)) OR (base.side = 'kill' AND EXISTS (SELECT 1 FROM rawkm.killmail_attackers owna WHERE owna.killmail_id = base.killmail_id AND owna.killmail_time = base.killmail_time AND owna.character_id = %s AND owna.corporation_id = ANY(%s))))")
        params.extend([filters["affiliation_corporation_ids"], entity_id, filters["affiliation_corporation_ids"]])
    if entity_type in {"character", "corporation"} and filters["affiliation_alliance_ids"]:
        own_column = "character_id" if entity_type == "character" else "corporation_id"
        clauses.append(f"((base.side = 'loss' AND km.victim_alliance_id = ANY(%s)) OR (base.side = 'kill' AND EXISTS (SELECT 1 FROM rawkm.killmail_attackers owna WHERE owna.killmail_id = base.killmail_id AND owna.killmail_time = base.killmail_time AND owna.{own_column} = %s AND owna.alliance_id = ANY(%s))))")
        params.extend([filters["affiliation_alliance_ids"], entity_id, filters["affiliation_alliance_ids"]])

    def involved_clause(kind, values):
        victim = f"km.victim_{kind}_id = ANY(%s)"
        attacker = f"EXISTS (SELECT 1 FROM rawkm.killmail_attackers inva WHERE inva.killmail_id = base.killmail_id AND inva.killmail_time = base.killmail_time AND inva.{kind}_id = ANY(%s))"
        if filters["involved_role"] == "victim":
            return victim, [values]
        if filters["involved_role"] == "attacker":
            return attacker, [values]
        return f"({victim} OR {attacker})", [values, values]

    for kind, key in (("corporation", "involved_corporation_ids"), ("alliance", "involved_alliance_ids")):
        if filters[key]:
            clause, values = involved_clause(kind, filters[key])
            clauses.append(clause)
            params.extend(values)

    if filters["type_ids"]:
        victim = "km.victim_ship_type_id = ANY(%s)"
        attacker = "EXISTS (SELECT 1 FROM rawkm.killmail_attackers shipa WHERE shipa.killmail_id = base.killmail_id AND shipa.killmail_time = base.killmail_time AND shipa.ship_type_id = ANY(%s))"
        if filters["type_role"] == "victim":
            clauses.append(victim); params.append(filters["type_ids"])
        elif filters["type_role"] == "attacker":
            clauses.append(attacker); params.append(filters["type_ids"])
        else:
            clauses.append(f"({victim} OR {attacker})"); params.extend([filters["type_ids"], filters["type_ids"]])

    if filters["module_type_ids"]:
        clauses.append("EXISTS (SELECT 1 FROM rawkm.killmail_items modi WHERE modi.killmail_id = base.killmail_id AND modi.killmail_time = base.killmail_time AND modi.item_type_id = ANY(%s))")
        params.append(filters["module_type_ids"])

    _append_killmail_builder_clauses(conn, filters, clauses, params, victim_alias="km")

    where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
    preferred_side = "kill" if (
        filters["participation"] == "kills"
        or (filters["participation"] == "both" and entity_type == "ship")
    ) else "loss"
    query = f"""
        WITH base_rows AS (
            SELECT 'loss'::text AS side, km.killmail_id, km.killmail_time
            FROM rawkm.killmails km
            WHERE {victim_scope_predicate}
            UNION ALL
            SELECT 'kill'::text AS side, ka.killmail_id, ka.killmail_time
            FROM rawkm.killmail_attackers ka
            WHERE {attacker_scope_predicate}
        ), base AS (
            SELECT DISTINCT ON (killmail_id) side, killmail_id, killmail_time
            FROM base_rows
            ORDER BY killmail_id, CASE WHEN side = '{preferred_side}' THEN 0 ELSE 1 END
        )
        SELECT base.side, base.killmail_id, base.killmail_time
        FROM base
        JOIN rawkm.killmails km
          ON km.killmail_id = base.killmail_id
         AND km.killmail_time = base.killmail_time
        {where_sql}
        ORDER BY base.killmail_time DESC, base.killmail_id DESC
        LIMIT %s OFFSET %s
    """
    params.extend([per_page + 1, offset])
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '120000ms'")
        cur.execute(query, params)
        rows = cur.fetchall()
    return rows[:per_page], len(rows) > per_page, page, per_page


def get_killmail_filter_affiliations(entity_type, entity_id):
    entity_type = normalize_entity_type(entity_type)
    if entity_type not in {"character", "corporation", "alliance"}:
        raise EntityError("entity_type_invalid")
    entity_id = int(entity_id)
    corporations = {}
    alliances = {}
    with db() as conn:
        with conn.cursor() as cur:
            if entity_type == "character":
                cur.execute("""
                    SELECT corporation_id, corporation_name, corporation_ticker, alliance_id, alliance_name, alliance_ticker
                    FROM (
                        SELECT km.victim_corporation_id AS corporation_id, c.name AS corporation_name, c.ticker AS corporation_ticker,
                               km.victim_alliance_id AS alliance_id, a.name AS alliance_name, a.ticker AS alliance_ticker
                        FROM rawkm.killmails km
                        LEFT JOIN entities.corporations c ON c.corporation_id = km.victim_corporation_id
                        LEFT JOIN entities.alliances a ON a.alliance_id = km.victim_alliance_id
                        WHERE km.victim_character_id = %s
                        UNION
                        SELECT ka.corporation_id, c.name, c.ticker, ka.alliance_id, a.name, a.ticker
                        FROM rawkm.killmail_attackers ka
                        LEFT JOIN entities.corporations c ON c.corporation_id = ka.corporation_id
                        LEFT JOIN entities.alliances a ON a.alliance_id = ka.alliance_id
                        WHERE ka.character_id = %s
                    ) x
                """, (entity_id, entity_id))
            elif entity_type == "corporation":
                cur.execute("""
                    SELECT NULL::bigint, NULL::text, NULL::text, alliance_id, alliance_name, alliance_ticker
                    FROM (
                        SELECT km.victim_alliance_id AS alliance_id, a.name AS alliance_name, a.ticker AS alliance_ticker
                        FROM rawkm.killmails km LEFT JOIN entities.alliances a ON a.alliance_id = km.victim_alliance_id
                        WHERE km.victim_corporation_id = %s
                        UNION
                        SELECT ka.alliance_id, a.name, a.ticker
                        FROM rawkm.killmail_attackers ka LEFT JOIN entities.alliances a ON a.alliance_id = ka.alliance_id
                        WHERE ka.corporation_id = %s
                    ) x
                """, (entity_id, entity_id))
            else:
                return {"corporations": [], "alliances": []}
            for corp_id, corp_name, corp_ticker, alliance_id, alliance_name, alliance_ticker in cur.fetchall():
                if corp_id is not None:
                    corporations[int(corp_id)] = {"entity_id": int(corp_id), "name": corp_name or "Unknown", "ticker": corp_ticker}
                if alliance_id is not None:
                    alliances[int(alliance_id)] = {"entity_id": int(alliance_id), "name": alliance_name or "Unknown", "ticker": alliance_ticker}
    return {
        "corporations": sorted(corporations.values(), key=lambda x: x["name"].lower()),
        "alliances": sorted(alliances.values(), key=lambda x: x["name"].lower()),
    }


def search_killmail_modules(query, limit=20):
    term = str(query or "").strip()
    if len(term) < 2:
        return []
    limit = max(1, min(int(limit or 20), 50))
    with db() as conn:
        table = _sde_table_with_data(conn, ["sde_types", "sde_invtypes", "sde_invTypes"])
        if not table:
            return []
        type_expr = _sde_int_expr("_key", "type_id", "typeID", "typeId")
        query_sql = f"""
            SELECT {type_expr}, COALESCE(data->'name'->>'en', data->>'typeName', data->>'name')
            FROM public.{_quote_ident(table)}
            WHERE lower(COALESCE(data->'name'->>'en', data->>'typeName', data->>'name', '')) LIKE %s
            ORDER BY 2
            LIMIT %s
        """
        with conn.cursor() as cur:
            cur.execute(query_sql, (f"%{term.lower()}%", limit))
            return [{"type_id": int(row[0]), "name": row[1] or f"Type {row[0]}", "image_url": image_url("commodity", row[0], 32)} for row in cur.fetchall()]


def get_killmail_ship_options():
    with db() as conn:
        items = [dict(item) for item in _sde_all_ship_entities(conn)]

    for item in items:
        if item.get("is_pilotable", True):
            item["selection_faction"] = item.get("faction_name") or "Other"
            item["is_structure"] = False
        else:
            item["selection_faction"] = item.get("category_name") or "Structures / Deployables"
            item["is_structure"] = True

    deduped = {int(item["entity_id"]): item for item in items}
    return sorted(
        deduped.values(),
        key=lambda item: (
            (item.get("selection_faction") or "").lower(),
            (item.get("group_name") or "").lower(),
            (item.get("name") or "").lower(),
        ),
    )


def _character_killmails(conn, character_id, page=1, per_page=100, filters=None):
    timing_started = perf_counter()
    timing_marks = {}
    normalized_filters = _normalize_killmail_filters(filters)

    table_check_started = perf_counter()
    if not _table_exists(conn, "rawkm", "killmails") or not _table_exists(conn, "rawkm", "killmail_attackers"):
        timing_marks["table_check_ms"] = round((perf_counter() - table_check_started) * 1000, 1)
        _timing_print(f"ENTITY_PROFILE_TIMING character_killmails character_id={character_id} table_missing total_ms={round((perf_counter() - timing_started) * 1000, 1)} details={timing_marks}")
        return [], None
    timing_marks["table_check_ms"] = round((perf_counter() - table_check_started) * 1000, 1)

    page = max(1, int(page or 1))
    per_page = max(1, min(100, int(per_page or 100)))
    offset = (page - 1) * per_page
    fetch_limit = offset + per_page + 1

    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '8000ms'")

            losses_started = perf_counter()
            cur.execute(
                """
                SELECT
                    'loss'::text AS side,
                    killmail_id,
                    killmail_time
                FROM rawkm.killmails
                WHERE victim_character_id = %s
                ORDER BY killmail_time DESC, killmail_id DESC
                LIMIT %s
                """,
                (character_id, fetch_limit),
            )
            loss_rows = cur.fetchall()
            timing_marks["loss_ids_ms"] = round((perf_counter() - losses_started) * 1000, 1)
            timing_marks["loss_ids_rows"] = len(loss_rows)

            attacks_started = perf_counter()
            cur.execute(
                """
                SELECT
                    'kill'::text AS side,
                    killmail_id,
                    killmail_time
                FROM rawkm.killmail_attackers
                WHERE character_id = %s
                ORDER BY killmail_time DESC, killmail_id DESC
                LIMIT %s
                """,
                (character_id, fetch_limit),
            )
            attack_rows = cur.fetchall()
            timing_marks["attack_ids_ms"] = round((perf_counter() - attacks_started) * 1000, 1)
            timing_marks["attack_ids_rows"] = len(attack_rows)
    except QueryCanceled:
        conn.rollback()
        _timing_print(f"ENTITY_PROFILE_TIMING character_killmails character_id={character_id} timeout_stage=ids total_ms={round((perf_counter() - timing_started) * 1000, 1)} details={timing_marks}")
        return [], {
            "page": page,
            "per_page": per_page,
            "has_prev": page > 1,
            "has_next": False,
            "prev_page": page - 1 if page > 1 else 1,
            "next_page": page,
            "timed_out": True,
        }

    # Merge in Python to avoid a heavy UNION/JOIN over large partitioned tables.
    merge_started = perf_counter()
    merged = []
    seen = set()

    for side, killmail_id, killmail_time in sorted(
        list(loss_rows) + list(attack_rows),
        key=lambda row: (row[2], row[1]),
        reverse=True,
    ):
        kmid = int(killmail_id)
        if kmid in seen:
            continue
        seen.add(kmid)
        merged.append((side, kmid, killmail_time))

    has_next = len(merged) > offset + per_page
    page_entries = merged[offset:offset + per_page]
    if normalized_filters["active"]:
        page_entries, has_next, page, per_page = _filtered_entity_killmail_page_entries(
            conn, "character", character_id, page, per_page, normalized_filters
        )
    timing_marks["merge_ms"] = round((perf_counter() - merge_started) * 1000, 1)
    timing_marks["merged_rows"] = len(merged)
    timing_marks["page_entries"] = len(page_entries)

    if not page_entries:
        _timing_print(f"ENTITY_PROFILE_TIMING character_killmails character_id={character_id} page={page} empty total_ms={round((perf_counter() - timing_started) * 1000, 1)} details={timing_marks}")
        progressive_pagination = _progressive_context_pagination(
            normalized_filters,
            per_page,
            has_next=has_next,
        )
        if progressive_pagination is not None:
            return [], progressive_pagination
        return [], {
            "page": page,
            "per_page": per_page,
            "has_prev": page > 1,
            "has_next": False,
            "prev_page": page - 1 if page > 1 else 1,
            "next_page": page,
            "timed_out": False,
        }

    values_sql = ",".join(["(%s::text, %s::bigint, %s::timestamptz, %s::integer)" for _ in page_entries])
    values_params = []
    for order_index, (side, killmail_id, killmail_time) in enumerate(page_entries):
        values_params.extend([side, killmail_id, killmail_time, order_index])

    # Give PostgreSQL explicit time bounds so it can prune the monthly rawkm
    # partitions while hydrating the 100 killmails on the current page.
    # The exact killmail_time equality against page_ids is not sufficient for
    # reliable partition pruning through the join, especially for location KBs.
    page_times = [entry[2] for entry in page_entries]
    range_first = min(page_times)
    range_last = max(page_times)
    range_start = range_first.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if range_last.month == 12:
        range_end = range_last.replace(
            year=range_last.year + 1, month=1, day=1,
            hour=0, minute=0, second=0, microsecond=0,
        )
    else:
        range_end = range_last.replace(
            month=range_last.month + 1, day=1,
            hour=0, minute=0, second=0, microsecond=0,
        )
    partition_range_params = [range_start, range_end]

    detail_sql = f"""
        WITH page_ids(side, killmail_id, killmail_time, order_index) AS (
            VALUES {values_sql}
        )
        SELECT
            p.side,
            p.order_index,
            km.killmail_id,
            km.killmail_time,
            km.solar_system_id,
            km.victim_ship_type_id,
            km.victim_damage_taken,
            km.victim_character_id,
            vc.name AS victim_character_name,
            km.victim_corporation_id,
            vcorp.name AS victim_corporation_name,
            vcorp.ticker AS victim_corporation_ticker,
            km.victim_alliance_id,
            vall.name AS victim_alliance_name,
            vall.ticker AS victim_alliance_ticker
        FROM page_ids p
        JOIN rawkm.killmails km
          ON km.killmail_id = p.killmail_id
         AND km.killmail_time = p.killmail_time
         AND km.killmail_time >= %s
         AND km.killmail_time < %s
        LEFT JOIN entities.characters vc
          ON vc.character_id = km.victim_character_id
        LEFT JOIN entities.corporations vcorp
          ON vcorp.corporation_id = km.victim_corporation_id
        LEFT JOIN entities.alliances vall
          ON vall.alliance_id = km.victim_alliance_id
        ORDER BY p.order_index ASC
    """

    attackers_sql = f"""
        WITH page_ids(side, killmail_id, killmail_time, order_index) AS (
            VALUES {values_sql}
        )
        SELECT
            ka.killmail_id,
            ka.killmail_time,
            ka.character_id,
            c.name AS character_name,
            ka.corporation_id,
            corp.name AS corporation_name,
            corp.ticker AS corporation_ticker,
            ka.alliance_id,
            alli.name AS alliance_name,
            alli.ticker AS alliance_ticker,
            ka.ship_type_id,
            ka.weapon_type_id,
            COALESCE(ka.damage_done, 0)::integer AS damage_done,
            COALESCE(ka.final_blow, FALSE)::boolean AS final_blow,
            ka.attacker_index
        FROM page_ids p
        JOIN rawkm.killmail_attackers ka
          ON ka.killmail_id = p.killmail_id
         AND ka.killmail_time = p.killmail_time
         AND ka.killmail_time >= %s
         AND ka.killmail_time < %s
        LEFT JOIN entities.characters c
          ON c.character_id = ka.character_id
        LEFT JOIN entities.corporations corp
          ON corp.corporation_id = ka.corporation_id
        LEFT JOIN entities.alliances alli
          ON alli.alliance_id = ka.alliance_id
        WHERE ka.character_id = %s
           OR ka.final_blow IS TRUE
        ORDER BY p.order_index ASC, ka.attacker_index ASC
    """

    counts_sql = f"""
        WITH page_ids(side, killmail_id, killmail_time, order_index) AS (
            VALUES {values_sql}
        )
        SELECT
            ka.killmail_id,
            ka.killmail_time,
            COUNT(*)::integer AS attackers_count
        FROM page_ids p
        JOIN rawkm.killmail_attackers ka
          ON ka.killmail_id = p.killmail_id
         AND ka.killmail_time = p.killmail_time
         AND ka.killmail_time >= %s
         AND ka.killmail_time < %s
        GROUP BY ka.killmail_id, ka.killmail_time
    """

    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '8000ms'")

            detail_started = perf_counter()
            cur.execute(detail_sql, values_params + partition_range_params)
            detail_rows = cur.fetchall()
            timing_marks["detail_killmails_ms"] = round((perf_counter() - detail_started) * 1000, 1)
            timing_marks["detail_killmail_rows"] = len(detail_rows)

            attackers_started = perf_counter()
            cur.execute(attackers_sql, values_params + partition_range_params + [character_id])
            attacker_rows = cur.fetchall()
            timing_marks["detail_attackers_ms"] = round((perf_counter() - attackers_started) * 1000, 1)
            timing_marks["detail_attacker_rows"] = len(attacker_rows)

            counts_started = perf_counter()
            cur.execute(counts_sql, values_params + partition_range_params)
            count_rows = cur.fetchall()
            timing_marks["detail_counts_ms"] = round((perf_counter() - counts_started) * 1000, 1)
            timing_marks["detail_count_rows"] = len(count_rows)
            timing_marks["detail_query_ms"] = round(
                timing_marks["detail_killmails_ms"]
                + timing_marks["detail_attackers_ms"]
                + timing_marks["detail_counts_ms"],
                1,
            )
    except QueryCanceled:
        conn.rollback()
        _timing_print(f"ENTITY_PROFILE_TIMING character_killmails character_id={character_id} timeout_stage=details total_ms={round((perf_counter() - timing_started) * 1000, 1)} details={timing_marks}")
        return [], {
            "page": page,
            "per_page": per_page,
            "has_prev": page > 1,
            "has_next": False,
            "prev_page": page - 1 if page > 1 else 1,
            "next_page": page,
            "timed_out": True,
        }

    counts_by_km = {
        (int(killmail_id), killmail_time): int(attackers_count or 0)
        for killmail_id, killmail_time, attackers_count in count_rows
    }
    self_by_km = {}
    final_by_km = {}
    for attacker_row in attacker_rows:
        (
            attacker_killmail_id,
            attacker_killmail_time,
            attacker_character_id,
            attacker_character_name,
            attacker_corporation_id,
            attacker_corporation_name,
            attacker_corporation_ticker,
            attacker_alliance_id,
            attacker_alliance_name,
            attacker_alliance_ticker,
            attacker_ship_type_id,
            attacker_weapon_type_id,
            attacker_damage_done,
            attacker_final_blow,
            attacker_index,
        ) = attacker_row
        key = (int(attacker_killmail_id), attacker_killmail_time)
        payload = {
            "character_id": attacker_character_id,
            "character_name": attacker_character_name,
            "corporation_id": attacker_corporation_id,
            "corporation_name": attacker_corporation_name,
            "corporation_ticker": attacker_corporation_ticker,
            "alliance_id": attacker_alliance_id,
            "alliance_name": attacker_alliance_name,
            "alliance_ticker": attacker_alliance_ticker,
            "ship_type_id": attacker_ship_type_id,
            "weapon_type_id": attacker_weapon_type_id,
            "damage_done": attacker_damage_done,
            "final_blow": attacker_final_blow,
            "attacker_index": attacker_index,
        }
        if attacker_character_id == character_id:
            self_by_km[key] = payload
        if attacker_final_blow and key not in final_by_km:
            final_by_km[key] = payload

    rows = []
    for detail_row in detail_rows:
        (
            side,
            order_index,
            killmail_id,
            killmail_time,
            solar_system_id,
            victim_ship_type_id,
            victim_damage_taken,
            victim_character_id,
            victim_character_name,
            victim_corporation_id,
            victim_corporation_name,
            victim_corporation_ticker,
            victim_alliance_id,
            victim_alliance_name,
            victim_alliance_ticker,
        ) = detail_row
        key = (int(killmail_id), killmail_time)
        self_attacker = self_by_km.get(key, {})
        final_attacker = final_by_km.get(key, {})
        rows.append((
            side,
            killmail_id,
            killmail_time,
            solar_system_id,
            victim_ship_type_id,
            victim_damage_taken,
            victim_character_id,
            victim_character_name,
            victim_corporation_id,
            victim_corporation_name,
            victim_corporation_ticker,
            victim_alliance_id,
            victim_alliance_name,
            victim_alliance_ticker,
            self_attacker.get("ship_type_id"),
            self_attacker.get("weapon_type_id"),
            self_attacker.get("damage_done", 0),
            self_attacker.get("final_blow", False),
            final_attacker.get("character_id"),
            final_attacker.get("character_name"),
            final_attacker.get("corporation_id"),
            final_attacker.get("corporation_name"),
            final_attacker.get("corporation_ticker"),
            final_attacker.get("alliance_id"),
            final_attacker.get("alliance_name"),
            final_attacker.get("alliance_ticker"),
            final_attacker.get("ship_type_id"),
            final_attacker.get("weapon_type_id"),
            final_attacker.get("damage_done"),
            counts_by_km.get(key, 0),
        ))
    timing_marks["detail_rows"] = len(rows)

    lookup_started = perf_counter()
    type_ids = set()
    system_ids = set()
    for row in rows:
        (
            _side,
            _killmail_id,
            _killmail_time,
            solar_system_id,
            victim_ship_type_id,
            _victim_damage_taken,
            _victim_character_id,
            _victim_character_name,
            _victim_corporation_id,
            _victim_corporation_name,
            _victim_corporation_ticker,
            _victim_alliance_id,
            _victim_alliance_name,
            _victim_alliance_ticker,
            pilot_ship_type_id,
            pilot_weapon_type_id,
            _pilot_damage_done,
            _pilot_final_blow,
            _final_character_id,
            _final_character_name,
            _final_corporation_id,
            _final_corporation_name,
            _final_corporation_ticker,
            _final_alliance_id,
            _final_alliance_name,
            _final_alliance_ticker,
            final_ship_type_id,
            final_weapon_type_id,
            _final_damage_done,
            _attackers_count,
        ) = row
        if solar_system_id is not None:
            system_ids.add(solar_system_id)
        for type_id in (victim_ship_type_id, pilot_ship_type_id, pilot_weapon_type_id, final_ship_type_id, final_weapon_type_id):
            if type_id is not None:
                type_ids.add(type_id)

    type_names = _lookup_type_names(conn, type_ids)
    system_locations = _lookup_system_locations(conn, system_ids)
    timing_marks["sde_lookup_ms"] = round((perf_counter() - lookup_started) * 1000, 1)
    timing_marks["sde_type_names"] = len(type_names)
    timing_marks["sde_system_locations"] = len(system_locations)

    build_started = perf_counter()
    killmails = []
    for row in rows:
        (
            side,
            killmail_id,
            killmail_time,
            solar_system_id,
            victim_ship_type_id,
            victim_damage_taken,
            victim_character_id,
            victim_character_name,
            victim_corporation_id,
            victim_corporation_name,
            victim_corporation_ticker,
            victim_alliance_id,
            victim_alliance_name,
            victim_alliance_ticker,
            pilot_ship_type_id,
            pilot_weapon_type_id,
            pilot_damage_done,
            pilot_final_blow,
            final_character_id,
            final_character_name,
            final_corporation_id,
            final_corporation_name,
            final_corporation_ticker,
            final_alliance_id,
            final_alliance_name,
            final_alliance_ticker,
            final_ship_type_id,
            final_weapon_type_id,
            final_damage_done,
            attackers_count,
        ) = row

        victim_character = _entity_ref("character", victim_character_id, victim_character_name)
        victim_corporation = _entity_ref("corporation", victim_corporation_id, victim_corporation_name, victim_corporation_ticker)
        victim_alliance = _entity_ref("alliance", victim_alliance_id, victim_alliance_name, victim_alliance_ticker)
        final_character = _entity_ref("character", final_character_id, final_character_name)
        final_corporation = _entity_ref("corporation", final_corporation_id, final_corporation_name, final_corporation_ticker)
        final_alliance = _entity_ref("alliance", final_alliance_id, final_alliance_name, final_alliance_ticker)

        pilot_damage = int(pilot_damage_done or 0)

        killmails.append({
            "side": side,
            "is_loss": side == "loss",
            "is_kill": side == "kill",
            "killmail_id": int(killmail_id),
            "killmail_time": killmail_time,
            "date_key": killmail_time.date().isoformat() if killmail_time else "Unknown date",
            "time_label": killmail_time.strftime("%H:%M") if killmail_time else "--:--",
            "solar_system_id": int(solar_system_id) if solar_system_id is not None else None,
            "location": system_locations.get(int(solar_system_id), {
                "system": _location_ref("system", solar_system_id, _format_system_name(solar_system_id)) if solar_system_id is not None else None,
                "constellation": None,
                "region": None,
            }),
            "system_name": (system_locations.get(int(solar_system_id), {}).get("system", {}) or {}).get("name", _format_system_name(solar_system_id)) if solar_system_id is not None else "Unknown",
            "victim_damage_taken": int(victim_damage_taken or 0),
            "victim_ship": {
                "type_id": int(victim_ship_type_id) if victim_ship_type_id is not None else None,
                "name": type_names.get(int(victim_ship_type_id), _format_type_name(victim_ship_type_id)) if victim_ship_type_id is not None else "Unknown",
                "image_url": type_icon_url(victim_ship_type_id, 64),
                "url": profile_url("ship", victim_ship_type_id) if victim_ship_type_id is not None else None,
            },
            "pilot_ship": {
                "type_id": int(pilot_ship_type_id) if pilot_ship_type_id is not None else None,
                "name": type_names.get(int(pilot_ship_type_id), _format_type_name(pilot_ship_type_id)) if pilot_ship_type_id is not None else "Unknown",
                "image_url": type_icon_url(pilot_ship_type_id, 64),
                "url": profile_url("ship", pilot_ship_type_id) if pilot_ship_type_id is not None else None,
            } if pilot_ship_type_id is not None else None,
            "pilot_weapon": {
                "type_id": int(pilot_weapon_type_id) if pilot_weapon_type_id is not None else None,
                "name": type_names.get(int(pilot_weapon_type_id), _format_type_name(pilot_weapon_type_id)) if pilot_weapon_type_id is not None else "Unknown",
                "image_url": type_icon_url(pilot_weapon_type_id, 64),
                "url": profile_url("weapon", pilot_weapon_type_id) if pilot_weapon_type_id is not None else None,
            } if pilot_weapon_type_id is not None else None,
            "pilot_damage_done": pilot_damage,
            "pilot_damage_percent": _damage_percent(pilot_damage, victim_damage_taken),
            "pilot_final_blow": bool(pilot_final_blow),
            "final_ship": {
                "type_id": int(final_ship_type_id) if final_ship_type_id is not None else None,
                "name": type_names.get(int(final_ship_type_id), _format_type_name(final_ship_type_id)) if final_ship_type_id is not None else "Unknown",
                "image_url": type_icon_url(final_ship_type_id, 64),
                "url": profile_url("ship", final_ship_type_id) if final_ship_type_id is not None else None,
            } if final_ship_type_id is not None else None,
            "final_weapon": {
                "type_id": int(final_weapon_type_id) if final_weapon_type_id is not None else None,
                "name": type_names.get(int(final_weapon_type_id), _format_type_name(final_weapon_type_id)) if final_weapon_type_id is not None else "Unknown",
                "image_url": type_icon_url(final_weapon_type_id, 64),
                "url": profile_url("weapon", final_weapon_type_id) if final_weapon_type_id is not None else None,
            } if final_weapon_type_id is not None else None,
            "final_damage_done": int(final_damage_done or 0),
            "attackers_count": int(attackers_count or 0),
            "victim_character": victim_character,
            "victim_corporation": victim_corporation,
            "victim_alliance": victim_alliance,
            "final_character": final_character,
            "final_corporation": final_corporation,
            "final_alliance": final_alliance,
            "zkill_url": f"https://zkillboard.com/kill/{int(killmail_id)}/",
            "value_label": None,
        })

    timing_marks["build_rows_ms"] = round((perf_counter() - build_started) * 1000, 1)
    timing_marks["total_ms"] = round((perf_counter() - timing_started) * 1000, 1)
    _timing_print(f"ENTITY_PROFILE_TIMING character_killmails character_id={character_id} page={page} per_page={per_page} details={timing_marks}")

    pagination = _progressive_context_pagination(
        normalized_filters,
        per_page,
        has_next=has_next,
    )
    if pagination is None:
        pagination = {
            "page": page,
            "per_page": per_page,
            "has_prev": page > 1,
            "has_next": has_next,
            "prev_page": page - 1 if page > 1 else 1,
            "next_page": page + 1 if has_next else page,
            "timed_out": False,
        }

    return killmails, pagination

def _group_killmails_by_date(killmails):
    groups = []
    current_date = None
    current_items = None
    for item in killmails:
        if item["date_key"] != current_date:
            current_date = item["date_key"]
            current_items = []
            groups.append({"date": current_date, "items": current_items})
        current_items.append(item)
    return groups


def get_character_killmails_page(character_id, page=1, per_page=100, filters=None):
    try:
        normalized_id = int(character_id)
    except (TypeError, ValueError) as exc:
        raise EntityError("entity_profile_id_invalid") from exc

    with db() as conn:
        killmails, killmail_pagination = _character_killmails(
            conn,
            normalized_id,
            page=page,
            per_page=per_page,
            filters=filters,
        )

    return {
        "character_id": normalized_id,
        "killmail_participation": _normalize_killmail_filters(filters)["participation"],
        "killmails": killmails,
        "killmail_groups": _group_killmails_by_date(killmails),
        "killmail_pagination": killmail_pagination,
    }



def _global_killmail_page_entries(conn, page=1, per_page=100, filters=None):
    normalized_filters = _normalize_killmail_filters(filters)
    page = max(1, int(page or 1))
    per_page = max(1, min(100, int(per_page or 100)))
    offset = (page - 1) * per_page
    fetch_limit = per_page + 1

    clauses = []
    params = []

    # Fast default/global Killboard path.
    #
    # With no active filter, querying the partitioned rawkm.killmails parent
    # with ORDER BY ... DESC can make PostgreSQL inspect/sort the whole history
    # just to return the newest 100 rows. Instead, walk monthly partitions from
    # newest to oldest and stop as soon as the requested page is filled.
    if not normalized_filters["active"]:
        target_count = offset + fetch_limit
        partitions = _rawkm_partition_tables(conn, "killmails")
        partitions = _rawkm_server_month_tables_first(partitions, "killmails")

        recent_rows = []
        seen = set()

        for schema_name, table_name in partitions:
            remaining = target_count - len(recent_rows)
            if remaining <= 0:
                break

            table_sql = _qualified_rawkm_table(schema_name, table_name)

            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '12000ms'")
                cur.execute(
                    f"""
                    SELECT killmail_id, killmail_time
                    FROM {table_sql}
                    ORDER BY killmail_time DESC, killmail_id DESC
                    LIMIT %s
                    """,
                    (remaining,),
                )
                rows = cur.fetchall()

            for killmail_id, killmail_time in rows:
                key = (int(killmail_id), killmail_time)
                if key in seen:
                    continue
                seen.add(key)
                recent_rows.append(key)
                if len(recent_rows) >= target_count:
                    break

        # Monthly partitions are scanned newest -> oldest. Keep an explicit
        # final sort as a safety net if partition naming/layout ever changes.
        recent_rows.sort(key=lambda row: (row[1], row[0]), reverse=True)

        page_rows = recent_rows[offset:offset + fetch_limit]
        has_next = len(page_rows) > per_page
        entries = [
            ("kill", int(killmail_id), killmail_time)
            for killmail_id, killmail_time in page_rows[:per_page]
        ]
        return entries, has_next, page, per_page

    if normalized_filters["date_from"]:
        clauses.append("km.killmail_time >= %s::date")
        params.append(normalized_filters["date_from"])
    if normalized_filters["date_to"]:
        clauses.append("km.killmail_time < (%s::date + INTERVAL '1 day')")
        params.append(normalized_filters["date_to"])
    if normalized_filters.get("datetime_from"):
        clauses.append("km.killmail_time >= %s::timestamptz")
        params.append(normalized_filters["datetime_from"])
    if normalized_filters.get("datetime_to"):
        clauses.append("km.killmail_time < %s::timestamptz")
        params.append(normalized_filters["datetime_to"])

    def legacy_involved(kind, values):
        victim = f"km.victim_{kind}_id = ANY(%s)"
        attacker = (
            "EXISTS (SELECT 1 FROM rawkm.killmail_attackers gia "
            "WHERE gia.killmail_id = km.killmail_id "
            "AND gia.killmail_time = km.killmail_time "
            f"AND gia.{kind}_id = ANY(%s))"
        )
        role = normalized_filters["involved_role"]
        if role == "victim":
            clauses.append(victim)
            params.append(values)
        elif role == "attacker":
            clauses.append(attacker)
            params.append(values)
        else:
            clauses.append(f"({victim} OR {attacker})")
            params.extend([values, values])

    if normalized_filters["involved_corporation_ids"]:
        legacy_involved("corporation", normalized_filters["involved_corporation_ids"])
    if normalized_filters["involved_alliance_ids"]:
        legacy_involved("alliance", normalized_filters["involved_alliance_ids"])

    if normalized_filters["type_ids"]:
        victim = "km.victim_ship_type_id = ANY(%s)"
        attacker = (
            "EXISTS (SELECT 1 FROM rawkm.killmail_attackers gsa "
            "WHERE gsa.killmail_id = km.killmail_id "
            "AND gsa.killmail_time = km.killmail_time "
            "AND gsa.ship_type_id = ANY(%s))"
        )
        role = normalized_filters["type_role"]
        if role == "victim":
            clauses.append(victim)
            params.append(normalized_filters["type_ids"])
        elif role == "attacker":
            clauses.append(attacker)
            params.append(normalized_filters["type_ids"])
        else:
            clauses.append(f"({victim} OR {attacker})")
            params.extend([normalized_filters["type_ids"], normalized_filters["type_ids"]])

    if normalized_filters["module_type_ids"]:
        clauses.append(
            "EXISTS (SELECT 1 FROM rawkm.killmail_items gmi "
            "WHERE gmi.killmail_id = km.killmail_id "
            "AND gmi.killmail_time = km.killmail_time "
            "AND gmi.item_type_id = ANY(%s))"
        )
        params.append(normalized_filters["module_type_ids"])

    _append_killmail_builder_clauses(
        conn,
        normalized_filters,
        clauses,
        params,
        victim_alias="km",
    )

    where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
    query = f"""
        SELECT km.killmail_id, km.killmail_time
        FROM rawkm.killmails km
        {where_sql}
        ORDER BY km.killmail_time DESC, km.killmail_id DESC
        LIMIT %s OFFSET %s
    """
    params.extend([fetch_limit, offset])

    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '45000ms'")
        cur.execute(query, params)
        rows = cur.fetchall()

    has_next = len(rows) > per_page
    entries = [("kill", int(killmail_id), killmail_time) for killmail_id, killmail_time in rows[:per_page]]
    return entries, has_next, page, per_page



def _rawkm_month_partition_map(conn, parent_table):
    """Map YYYY-MM-01 dates to rawkm child partitions."""
    result = {}
    pattern = re.compile(r"(\d{4})_(\d{2})(?:$|_)")

    for schema_name, table_name in _rawkm_partition_tables(conn, parent_table):
        match = pattern.search(str(table_name))
        if not match:
            continue
        year = int(match.group(1))
        month = int(match.group(2))
        if not (1 <= month <= 12):
            continue
        result[date(year, month, 1)] = (schema_name, table_name)

    return result


def _rawkm_partition_estimated_rows(conn, schema_name, table_name):
    """Cheap PostgreSQL statistics estimate used only for UI progress."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT GREATEST(COALESCE(c.reltuples, 0), 0)::BIGINT
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s
                  AND c.relname = %s
                LIMIT 1
                """,
                (schema_name, table_name),
            )
            row = cur.fetchone()
        return max(0, int(row[0] or 0)) if row else 0
    except Exception:
        return 0


def _previous_available_month(month_map, current_month, floor_month=None):
    candidates = [
        month
        for month in month_map
        if month < current_month
        and (floor_month is None or month >= floor_month)
    ]
    return max(candidates) if candidates else None


def _monthly_child_table(conn, parent_table, killmail_partition_name):
    partitions = {
        table_name: (schema_name, table_name)
        for schema_name, table_name in _rawkm_partition_tables(conn, parent_table)
    }

    if killmail_partition_name == "killmails":
        expected = parent_table
    elif killmail_partition_name.startswith("killmails"):
        expected = parent_table + killmail_partition_name[len("killmails"):]
    else:
        expected = None

    if expected and expected in partitions:
        schema_name, table_name = partitions[expected]
        return _qualified_rawkm_table(schema_name, table_name)

    return f"rawkm.{_quote_ident(parent_table)}"


def _global_killmail_scan_month_entries(conn, per_page=100, filters=None):
    """Scan exactly one rawkm month for the global filtered Killboard.

    The browser calls this repeatedly, one HTTP request per month. This keeps
    rare searches visibly progressing and prevents one request from trying to
    search the complete killmail history.
    """
    normalized_filters = _normalize_killmail_filters(filters)
    per_page = max(1, min(100, int(per_page or 100)))

    month_map = _rawkm_month_partition_map(conn, "killmails")
    if not month_map:
        return [], {
            "scan_complete": True,
            "scan_month": None,
            "next_scan_month": None,
            "next_scan_before": None,
            "next_scan_row": None,
            "month_complete": True,
            "scanned_estimate": 0,
        }

    floor_month = (
        normalized_filters["date_from"].replace(day=1)
        if normalized_filters["date_from"]
        else min(month_map)
    )

    if normalized_filters["scan_month"]:
        requested_month = normalized_filters["scan_month"].replace(day=1)
        available = [
            month for month in month_map
            if month <= requested_month and month >= floor_month
        ]
        scan_month = max(available) if available else None
    elif normalized_filters["date_to"]:
        requested_month = normalized_filters["date_to"].replace(day=1)
        available = [
            month for month in month_map
            if month <= requested_month and month >= floor_month
        ]
        scan_month = max(available) if available else None
    else:
        available = [month for month in month_map if month >= floor_month]
        scan_month = max(available) if available else None

    if scan_month is None:
        return [], {
            "scan_complete": True,
            "scan_month": None,
            "next_scan_month": None,
            "next_scan_before": None,
            "next_scan_row": None,
            "month_complete": True,
            "scanned_estimate": 0,
        }

    schema_name, table_name = month_map[scan_month]
    killmail_table_sql = _qualified_rawkm_table(schema_name, table_name)
    attacker_table_sql = _monthly_child_table(
        conn, "killmail_attackers", table_name
    )
    item_table_sql = _monthly_child_table(
        conn, "killmail_items", table_name
    )

    if scan_month.month == 12:
        month_end = date(scan_month.year + 1, 1, 1)
    else:
        month_end = date(scan_month.year, scan_month.month + 1, 1)

    range_start = scan_month
    range_end = month_end

    if normalized_filters["date_from"]:
        range_start = max(range_start, normalized_filters["date_from"])
    if normalized_filters["date_to"]:
        range_end = min(
            range_end,
            normalized_filters["date_to"] + timedelta(days=1),
        )

    if range_start >= range_end:
        next_month = _previous_available_month(month_map, scan_month, floor_month)
        return [], {
            "scan_complete": next_month is None,
            "scan_month": scan_month,
            "next_scan_month": next_month,
            "next_scan_before": None,
            "next_scan_row": None,
            "month_complete": True,
            "scanned_estimate": 0,
        }

    clauses = [
        "km.killmail_time >= %s::date",
        "km.killmail_time < %s::date",
    ]
    params = [range_start, range_end]

    scan_before = normalized_filters.get("scan_before")
    scan_row = normalized_filters.get("scan_row")
    if scan_before is not None:
        if scan_row is not None:
            clauses.append(
                "(km.killmail_time, km.killmail_id) < (%s::timestamptz, %s::bigint)"
            )
            params.extend([scan_before, int(scan_row)])
        else:
            clauses.append("km.killmail_time < %s::timestamptz")
            params.append(scan_before)

    def legacy_involved(kind, values):
        victim = f"km.victim_{kind}_id = ANY(%s)"
        attacker = (
            f"EXISTS (SELECT 1 FROM {attacker_table_sql} gia "
            "WHERE gia.killmail_id = km.killmail_id "
            "AND gia.killmail_time = km.killmail_time "
            f"AND gia.{kind}_id = ANY(%s))"
        )
        role = normalized_filters["involved_role"]
        if role == "victim":
            clauses.append(victim)
            params.append(values)
        elif role == "attacker":
            clauses.append(attacker)
            params.append(values)
        else:
            clauses.append(f"({victim} OR {attacker})")
            params.extend([values, values])

    if normalized_filters["involved_corporation_ids"]:
        legacy_involved(
            "corporation",
            normalized_filters["involved_corporation_ids"],
        )
    if normalized_filters["involved_alliance_ids"]:
        legacy_involved(
            "alliance",
            normalized_filters["involved_alliance_ids"],
        )

    if normalized_filters["type_ids"]:
        victim = "km.victim_ship_type_id = ANY(%s)"
        attacker = (
            f"EXISTS (SELECT 1 FROM {attacker_table_sql} gsa "
            "WHERE gsa.killmail_id = km.killmail_id "
            "AND gsa.killmail_time = km.killmail_time "
            "AND gsa.ship_type_id = ANY(%s))"
        )
        role = normalized_filters["type_role"]
        if role == "victim":
            clauses.append(victim)
            params.append(normalized_filters["type_ids"])
        elif role == "attacker":
            clauses.append(attacker)
            params.append(normalized_filters["type_ids"])
        else:
            clauses.append(f"({victim} OR {attacker})")
            params.extend([
                normalized_filters["type_ids"],
                normalized_filters["type_ids"],
            ])

    if normalized_filters["module_type_ids"]:
        clauses.append(
            f"EXISTS (SELECT 1 FROM {item_table_sql} gmi "
            "WHERE gmi.killmail_id = km.killmail_id "
            "AND gmi.killmail_time = km.killmail_time "
            "AND gmi.item_type_id = ANY(%s))"
        )
        params.append(normalized_filters["module_type_ids"])

    _append_killmail_builder_clauses(
        conn,
        normalized_filters,
        clauses,
        params,
        victim_alias="km",
        attacker_table_sql=attacker_table_sql,
    )

    query = f"""
        SELECT km.killmail_id, km.killmail_time
        FROM {killmail_table_sql} km
        WHERE {" AND ".join(clauses)}
        ORDER BY km.killmail_time DESC, km.killmail_id DESC
        LIMIT %s
    """
    params.append(per_page + 1)

    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '45000ms'")
        cur.execute(query, params)
        rows = cur.fetchall()

    has_extra = len(rows) > per_page
    visible_rows = rows[:per_page]
    entries = [
        ("kill", int(killmail_id), killmail_time)
        for killmail_id, killmail_time in visible_rows
    ]

    if has_extra and visible_rows:
        # The month is not exhausted: the next visible page resumes inside it.
        last_id, last_time = visible_rows[-1]
        next_month = scan_month
        next_before = last_time
        next_row = int(last_id)
        month_complete = False
        scanned_estimate = 0
        scan_complete = False
    else:
        next_month = _previous_available_month(
            month_map,
            scan_month,
            floor_month,
        )
        next_before = None
        next_row = None
        month_complete = True
        scanned_estimate = _rawkm_partition_estimated_rows(
            conn,
            schema_name,
            table_name,
        )
        scan_complete = next_month is None

    return entries, {
        "scan_complete": scan_complete,
        "scan_month": scan_month,
        "next_scan_month": next_month,
        "next_scan_before": next_before,
        "next_scan_row": next_row,
        "month_complete": month_complete,
        "scanned_estimate": scanned_estimate,
    }


def get_global_killmails_scan_month(per_page=100, filters=None):
    with db() as conn:
        if not _table_exists(conn, "rawkm", "killmails"):
            return {
                "global_context": True,
                "killmail_participation": "both",
                "killmails": [],
                "killmail_groups": [],
                "killmail_pagination": {
                    "progressive": True,
                    "scan_complete": True,
                    "scanned_estimate": 0,
                },
            }

        entries, scan = _global_killmail_scan_month_entries(
            conn,
            per_page=per_page,
            filters=filters,
        )
        killmails = _hydrate_global_killmail_entries(conn, entries)

    pagination = {
        "page": 1,
        "per_page": max(1, min(100, int(per_page or 100))),
        "has_prev": False,
        "has_next": not scan["scan_complete"],
        "prev_page": 1,
        "next_page": 1,
        "timed_out": False,
        "progressive": True,
        **scan,
    }

    return {
        "global_context": True,
        "killmail_participation": "both",
        "killmails": killmails,
        "killmail_groups": _group_killmails_by_date(killmails),
        "killmail_pagination": pagination,
    }

def _hydrate_global_killmail_entries(conn, page_entries):
    if not page_entries:
        return []

    values_sql = ",".join([
        "(%s::text, %s::bigint, %s::timestamptz, %s::integer)"
        for _ in page_entries
    ])
    values_params = []
    for order_index, (side, killmail_id, killmail_time) in enumerate(page_entries):
        values_params.extend([side, killmail_id, killmail_time, order_index])

    page_times = [entry[2] for entry in page_entries]
    range_first = min(page_times)
    range_last = max(page_times)
    range_start = range_first.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if range_last.month == 12:
        range_end = range_last.replace(
            year=range_last.year + 1,
            month=1,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    else:
        range_end = range_last.replace(
            month=range_last.month + 1,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    partition_range_params = [range_start, range_end]

    detail_sql = f"""
        WITH page_ids(side, killmail_id, killmail_time, order_index) AS (
            VALUES {values_sql}
        )
        SELECT
            p.order_index,
            km.killmail_id,
            km.killmail_time,
            km.solar_system_id,
            km.victim_ship_type_id,
            km.victim_damage_taken,
            km.victim_character_id,
            vc.name,
            km.victim_corporation_id,
            vcorp.name,
            vcorp.ticker,
            COALESCE(km.victim_alliance_id, vhist.alliance_id),
            vall.name,
            vall.ticker
        FROM page_ids p
        JOIN rawkm.killmails km
          ON km.killmail_id = p.killmail_id
         AND km.killmail_time = p.killmail_time
         AND km.killmail_time >= %s
         AND km.killmail_time < %s
        LEFT JOIN entities.characters vc
          ON vc.character_id = km.victim_character_id
        LEFT JOIN entities.corporations vcorp
          ON vcorp.corporation_id = km.victim_corporation_id
        LEFT JOIN LATERAL (
            SELECT h.alliance_id
            FROM entities.corporation_alliance_history h
            WHERE km.victim_alliance_id IS NULL
              AND h.corporation_id = km.victim_corporation_id
              AND h.start_date <= km.killmail_time
              AND (h.end_date IS NULL OR km.killmail_time < h.end_date)
            ORDER BY h.start_date DESC, h.record_id DESC
            LIMIT 1
        ) vhist ON TRUE
        LEFT JOIN entities.alliances vall
          ON vall.alliance_id = COALESCE(km.victim_alliance_id, vhist.alliance_id)
        ORDER BY p.order_index ASC
    """

    final_sql = f"""
        WITH page_ids(side, killmail_id, killmail_time, order_index) AS (
            VALUES {values_sql}
        )
        SELECT
            ka.killmail_id,
            ka.killmail_time,
            ka.character_id,
            c.name,
            ka.corporation_id,
            corp.name,
            corp.ticker,
            COALESCE(ka.alliance_id, fhist.alliance_id),
            alli.name,
            alli.ticker,
            ka.ship_type_id,
            ka.weapon_type_id,
            COALESCE(ka.damage_done, 0)::integer,
            ka.attacker_index
        FROM page_ids p
        JOIN rawkm.killmail_attackers ka
          ON ka.killmail_id = p.killmail_id
         AND ka.killmail_time = p.killmail_time
         AND ka.killmail_time >= %s
         AND ka.killmail_time < %s
        LEFT JOIN entities.characters c ON c.character_id = ka.character_id
        LEFT JOIN entities.corporations corp ON corp.corporation_id = ka.corporation_id
        LEFT JOIN LATERAL (
            SELECT h.alliance_id
            FROM entities.corporation_alliance_history h
            WHERE ka.alliance_id IS NULL
              AND h.corporation_id = ka.corporation_id
              AND h.start_date <= ka.killmail_time
              AND (h.end_date IS NULL OR ka.killmail_time < h.end_date)
            ORDER BY h.start_date DESC, h.record_id DESC
            LIMIT 1
        ) fhist ON TRUE
        LEFT JOIN entities.alliances alli
          ON alli.alliance_id = COALESCE(ka.alliance_id, fhist.alliance_id)
        WHERE ka.final_blow IS TRUE
        ORDER BY p.order_index ASC, ka.attacker_index ASC
    """

    counts_sql = f"""
        WITH page_ids(side, killmail_id, killmail_time, order_index) AS (
            VALUES {values_sql}
        )
        SELECT ka.killmail_id, ka.killmail_time, COUNT(*)::integer
        FROM page_ids p
        JOIN rawkm.killmail_attackers ka
          ON ka.killmail_id = p.killmail_id
         AND ka.killmail_time = p.killmail_time
         AND ka.killmail_time >= %s
         AND ka.killmail_time < %s
        GROUP BY ka.killmail_id, ka.killmail_time
    """

    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '45000ms'")
        cur.execute(detail_sql, values_params + partition_range_params)
        detail_rows = cur.fetchall()
        cur.execute(final_sql, values_params + partition_range_params)
        final_rows = cur.fetchall()
        cur.execute(counts_sql, values_params + partition_range_params)
        count_rows = cur.fetchall()

    final_by_km = {}
    for row in final_rows:
        key = (int(row[0]), row[1])
        if key in final_by_km:
            continue
        final_by_km[key] = {
            "character_id": row[2],
            "character_name": row[3],
            "corporation_id": row[4],
            "corporation_name": row[5],
            "corporation_ticker": row[6],
            "alliance_id": row[7],
            "alliance_name": row[8],
            "alliance_ticker": row[9],
            "ship_type_id": row[10],
            "weapon_type_id": row[11],
            "damage_done": int(row[12] or 0),
        }

    counts_by_km = {
        (int(killmail_id), killmail_time): int(count or 0)
        for killmail_id, killmail_time, count in count_rows
    }

    type_ids = set()
    system_ids = set()
    for row in detail_rows:
        if row[3] is not None:
            system_ids.add(int(row[3]))
        if row[4] is not None:
            type_ids.add(int(row[4]))
        final = final_by_km.get((int(row[1]), row[2])) or {}
        for type_id in (final.get("ship_type_id"), final.get("weapon_type_id")):
            if type_id is not None:
                type_ids.add(int(type_id))

    type_names = _lookup_type_names(conn, type_ids)
    system_locations = _lookup_system_locations(conn, system_ids)

    killmails = []
    for row in detail_rows:
        (
            _order_index,
            killmail_id,
            killmail_time,
            solar_system_id,
            victim_ship_type_id,
            victim_damage_taken,
            victim_character_id,
            victim_character_name,
            victim_corporation_id,
            victim_corporation_name,
            victim_corporation_ticker,
            victim_alliance_id,
            victim_alliance_name,
            victim_alliance_ticker,
        ) = row
        key = (int(killmail_id), killmail_time)
        final = final_by_km.get(key) or {}
        final_ship_type_id = final.get("ship_type_id")
        final_weapon_type_id = final.get("weapon_type_id")
        final_damage_done = int(final.get("damage_done") or 0)
        attackers_count = counts_by_km.get(key, 0)

        victim_character = _entity_ref("character", victim_character_id, victim_character_name)
        victim_corporation = _entity_ref(
            "corporation", victim_corporation_id, victim_corporation_name, victim_corporation_ticker
        )
        victim_alliance = _entity_ref(
            "alliance", victim_alliance_id, victim_alliance_name, victim_alliance_ticker
        )
        final_character = _entity_ref("character", final.get("character_id"), final.get("character_name"))
        final_corporation = _entity_ref(
            "corporation",
            final.get("corporation_id"),
            final.get("corporation_name"),
            final.get("corporation_ticker"),
        )
        final_alliance = _entity_ref(
            "alliance",
            final.get("alliance_id"),
            final.get("alliance_name"),
            final.get("alliance_ticker"),
        )

        victim_ship = {
            "type_id": int(victim_ship_type_id) if victim_ship_type_id is not None else None,
            "name": type_names.get(int(victim_ship_type_id), _format_type_name(victim_ship_type_id))
            if victim_ship_type_id is not None else "Unknown",
            "image_url": type_icon_url(victim_ship_type_id, 64),
            "url": profile_url("ship", victim_ship_type_id) if victim_ship_type_id is not None else None,
        }
        final_ship = {
            "type_id": int(final_ship_type_id),
            "name": type_names.get(int(final_ship_type_id), _format_type_name(final_ship_type_id)),
            "image_url": type_icon_url(final_ship_type_id, 64),
            "url": profile_url("ship", final_ship_type_id),
        } if final_ship_type_id is not None else None
        final_weapon = {
            "type_id": int(final_weapon_type_id),
            "name": type_names.get(int(final_weapon_type_id), _format_type_name(final_weapon_type_id)),
            "image_url": type_icon_url(final_weapon_type_id, 64),
            "url": profile_url("weapon", final_weapon_type_id),
        } if final_weapon_type_id is not None else None

        location = system_locations.get(int(solar_system_id), {
            "system": _location_ref("system", solar_system_id, _format_system_name(solar_system_id))
            if solar_system_id is not None else None,
            "constellation": None,
            "region": None,
        }) if solar_system_id is not None else {
            "system": None,
            "constellation": None,
            "region": None,
        }

        killmails.append({
            "side": "kill",
            "is_loss": False,
            "is_kill": True,
            "killmail_id": int(killmail_id),
            "killmail_time": killmail_time,
            "date_key": killmail_time.date().isoformat() if killmail_time else "Unknown date",
            "time_label": killmail_time.strftime("%H:%M") if killmail_time else "--:--",
            "solar_system_id": int(solar_system_id) if solar_system_id is not None else None,
            "location": location,
            "system_name": (location.get("system") or {}).get("name", "Unknown"),
            "victim_damage_taken": int(victim_damage_taken or 0),
            "victim_ship": victim_ship,
            "pilot_ship": final_ship,
            "pilot_weapon": final_weapon,
            "pilot_damage_done": final_damage_done,
            "pilot_damage_percent": _damage_percent(final_damage_done, victim_damage_taken),
            "pilot_final_blow": True if final else False,
            "pilot_count": attackers_count,
            "pilot_count_label": f"{attackers_count} attacker" + ("s" if attackers_count != 1 else ""),
            "entity_context_type": "global",
            "final_ship": final_ship,
            "final_weapon": final_weapon,
            "final_damage_done": final_damage_done,
            "attackers_count": attackers_count,
            "victim_character": victim_character,
            "victim_corporation": victim_corporation,
            "victim_alliance": victim_alliance,
            "final_character": final_character,
            "final_corporation": final_corporation,
            "final_alliance": final_alliance,
            "zkill_url": f"https://zkillboard.com/kill/{int(killmail_id)}/",
            "value_label": None,
        })

    return killmails


def get_global_killmails_page(page=1, per_page=100, filters=None):
    with db() as conn:
        if not _table_exists(conn, "rawkm", "killmails"):
            return {
                "global_context": True,
                "killmail_participation": "both",
                "killmails": [],
                "killmail_groups": [],
                "killmail_pagination": None,
            }

        entries, has_next, normalized_page, normalized_per_page = _global_killmail_page_entries(
            conn,
            page=page,
            per_page=per_page,
            filters=filters,
        )
        killmails = _hydrate_global_killmail_entries(conn, entries)

    pagination = {
        "page": normalized_page,
        "per_page": normalized_per_page,
        "has_prev": normalized_page > 1,
        "has_next": has_next,
        "prev_page": normalized_page - 1 if normalized_page > 1 else 1,
        "next_page": normalized_page + 1 if has_next else normalized_page,
        "timed_out": False,
    }

    return {
        "global_context": True,
        "killmail_participation": "both",
        "killmails": killmails,
        "killmail_groups": _group_killmails_by_date(killmails),
        "killmail_pagination": pagination,
    }

def _entity_killmail_scope(entity_type):
    if str(entity_type or "").strip().lower() == "coalition":
        return {
            "entity_type": "coalition",
            "victim_column": None,
            "attacker_column": None,
            "timing_label": "coalition_killmails",
            "page_key": "coalition_id",
            "kill_priority": False,
        }
    entity_type = normalize_entity_type(entity_type)
    if entity_type == "corporation":
        return {
            "entity_type": "corporation",
            "victim_column": "victim_corporation_id",
            "attacker_column": "corporation_id",
            "timing_label": "corporation_killmails",
            "page_key": "corporation_id",
            "kill_priority": False,
        }
    if entity_type == "alliance":
        return {
            "entity_type": "alliance",
            "victim_column": "victim_alliance_id",
            "attacker_column": "alliance_id",
            "timing_label": "alliance_killmails",
            "page_key": "alliance_id",
            "kill_priority": False,
        }
    if entity_type == "ship":
        return {
            "entity_type": "ship",
            "victim_column": "victim_ship_type_id",
            "attacker_column": "ship_type_id",
            "timing_label": "ship_killmails",
            "page_key": "type_id",
            "kill_priority": True,
        }
    if entity_type == "system":
        return {
            "entity_type": "system",
            "victim_column": "solar_system_id",
            "attacker_column": None,
            "timing_label": "system_killmails",
            "page_key": "system_id",
            "kill_priority": True,
        }
    raise EntityError("entity_type_invalid")


def _attacker_partition_lookup(conn):
    return {
        table_name: (schema_name, table_name)
        for schema_name, table_name in _rawkm_partition_tables(conn, "killmail_attackers")
    }


def _matching_attacker_table_for_killmail_table(attacker_by_name, killmail_table):
    candidates = []

    if killmail_table == "killmails":
        candidates.append("killmail_attackers")
    elif killmail_table.startswith("killmails"):
        candidates.append("killmail_attackers" + killmail_table[len("killmails"):])

    for candidate in candidates:
        attacker_table = attacker_by_name.get(candidate)
        if attacker_table:
            return attacker_table

    return None


def _historical_alliance_scope_predicate(alias, corporation_column, alliance_column, time_column):
    return f"""(
        {alias}.{alliance_column} = %s
        OR (
            {alias}.{alliance_column} IS NULL
            AND {alias}.{corporation_column} IS NOT NULL
            AND EXISTS (
                SELECT 1
                FROM entities.corporation_alliance_history cah
                WHERE cah.corporation_id = {alias}.{corporation_column}
                  AND cah.alliance_id = %s
                  AND cah.start_date <= {alias}.{time_column}
                  AND (cah.end_date IS NULL OR {alias}.{time_column} < cah.end_date)
            )
        )
    )"""


def _normalize_coalition_killmail_scope(scope):
    scope = scope or {}
    alliance_ids = sorted({int(value) for value in (scope.get("alliance_ids") or []) if value is not None})
    corporation_ids = sorted({int(value) for value in (scope.get("corporation_ids") or []) if value is not None})
    return {"alliance_ids": alliance_ids, "corporation_ids": corporation_ids}


def _coalition_raw_scope_predicate(alias, corporation_column, alliance_column, time_column):
    return f"""(
        {alias}.{alliance_column} = ANY(%s)
        OR {alias}.{corporation_column} = ANY(%s)
        OR (
            {alias}.{alliance_column} IS NULL
            AND {alias}.{corporation_column} IS NOT NULL
            AND EXISTS (
                SELECT 1
                FROM entities.corporation_alliance_history cah
                WHERE cah.corporation_id = {alias}.{corporation_column}
                  AND cah.alliance_id = ANY(%s)
                  AND cah.start_date <= {alias}.{time_column}
                  AND (cah.end_date IS NULL OR {alias}.{time_column} < cah.end_date)
            )
        )
    )"""


def _coalition_raw_scope_params(scope):
    scope = _normalize_coalition_killmail_scope(scope)
    return [scope["alliance_ids"], scope["corporation_ids"], scope["alliance_ids"]]


def _coalition_mer_scope_predicate(side):
    return f"(m.{side}_alliance_id = ANY(%s) OR m.{side}_corporation_id = ANY(%s))"


def _coalition_mer_scope_params(scope):
    scope = _normalize_coalition_killmail_scope(scope)
    return [scope["alliance_ids"], scope["corporation_ids"]]


def _entity_killmail_page_entries(conn, entity_type, entity_id, page=1, per_page=100, timing_marks=None):
    scope = _entity_killmail_scope(entity_type)
    victim_column = scope["victim_column"]
    attacker_column = scope["attacker_column"]

    multi_ship_scope = scope["entity_type"] == "ship" and isinstance(entity_id, (list, tuple, set))
    multi_system_scope = scope["entity_type"] == "system" and isinstance(entity_id, (list, tuple, set))
    if multi_ship_scope:
        scope_value = sorted({int(value) for value in entity_id if value is not None})
        if not scope_value:
            page = max(1, int(page or 1))
            per_page = max(1, min(100, int(per_page or 100)))
            return [], False, page, per_page
        victim_predicate = f"km.{victim_column} = ANY(%s)"
        attacker_predicate = f"ka.{attacker_column} = ANY(%s)"
        victim_params = [scope_value]
        attacker_params = [scope_value]
    elif multi_system_scope:
        scope_value = sorted({int(value) for value in entity_id if value is not None})
        if not scope_value:
            page = max(1, int(page or 1))
            per_page = max(1, min(100, int(per_page or 100)))
            return [], False, page, per_page
        victim_predicate = f"km.{victim_column} = ANY(%s)"
        attacker_predicate = None
        victim_params = [scope_value]
        attacker_params = []
    elif scope["entity_type"] == "coalition":
        scope_value = _normalize_coalition_killmail_scope(entity_id)
        if not scope_value["alliance_ids"] and not scope_value["corporation_ids"]:
            page = max(1, int(page or 1))
            per_page = max(1, min(100, int(per_page or 100)))
            return [], False, page, per_page
        victim_predicate = _coalition_raw_scope_predicate(
            "km", "victim_corporation_id", "victim_alliance_id", "killmail_time"
        )
        attacker_predicate = _coalition_raw_scope_predicate(
            "ka", "corporation_id", "alliance_id", "killmail_time"
        )
        victim_params = _coalition_raw_scope_params(scope_value)
        attacker_params = _coalition_raw_scope_params(scope_value)
    elif scope["entity_type"] == "alliance":
        scope_value = int(entity_id)
        # Structures/deployables can have no character and sometimes no alliance_id
        # in the raw killmail. In that case the corporation at kill time is enough
        # to recover the alliance from the local corporation history.
        victim_predicate = _historical_alliance_scope_predicate(
            "km", "victim_corporation_id", "victim_alliance_id", "killmail_time"
        )
        attacker_predicate = _historical_alliance_scope_predicate(
            "ka", "corporation_id", "alliance_id", "killmail_time"
        )
        victim_params = [scope_value, scope_value]
        attacker_params = [scope_value, scope_value]
    elif scope["entity_type"] == "system":
        scope_value = int(entity_id)
        victim_predicate = f"km.{victim_column} = %s"
        attacker_predicate = None
        victim_params = [scope_value]
        attacker_params = []
    else:
        scope_value = entity_id
        victim_predicate = f"km.{victim_column} = %s"
        attacker_predicate = f"ka.{attacker_column} = %s"
        victim_params = [scope_value]
        attacker_params = [scope_value]

    page = max(1, int(page or 1))
    per_page = max(1, min(100, int(per_page or 100)))
    offset = (page - 1) * per_page
    fetch_limit = offset + per_page + 1

    killmail_tables = _rawkm_partition_tables(conn, "killmails")
    if not killmail_tables or not _table_exists(conn, "rawkm", "killmail_attackers"):
        return [], False, page, per_page

    killmail_tables = _rawkm_server_month_tables_first(killmail_tables, "killmails")
    attacker_by_name = _attacker_partition_lookup(conn)

    merged = []
    seen = set()
    scanned_partitions = 0
    loss_rows_count = 0
    attack_rows_count = 0

    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '120000ms'")

        for killmail_schema, killmail_table in killmail_tables:
            if len(merged) >= fetch_limit:
                break

            scanned_partitions += 1
            month_rows = []
            killmail_relation = _qualified_rawkm_table(killmail_schema, killmail_table)

            victim_side = "kill" if scope["entity_type"] == "system" else "loss"
            cur.execute(
                f"""
                SELECT
                    %s::text AS side,
                    km.killmail_id,
                    km.killmail_time
                FROM {killmail_relation} km
                WHERE {victim_predicate}
                ORDER BY km.killmail_time DESC, km.killmail_id DESC
                LIMIT %s
                """,
                [victim_side] + victim_params + [fetch_limit],
            )
            loss_rows = cur.fetchall()
            loss_rows_count += len(loss_rows)
            month_rows.extend(loss_rows)

            attacker_table = _matching_attacker_table_for_killmail_table(attacker_by_name, killmail_table)
            if attacker_table and attacker_predicate:
                attacker_relation = _qualified_rawkm_table(attacker_table[0], attacker_table[1])
                cur.execute(
                    f"""
                    SELECT
                        'kill'::text AS side,
                        ka.killmail_id,
                        ka.killmail_time
                    FROM {attacker_relation} ka
                    WHERE {attacker_predicate}
                    ORDER BY ka.killmail_time DESC, ka.killmail_id DESC
                    LIMIT %s
                    """,
                    attacker_params + [fetch_limit],
                )
                attack_rows = cur.fetchall()
                attack_rows_count += len(attack_rows)
                month_rows.extend(attack_rows)

            prefer_kill = bool(scope.get("kill_priority"))
            for side, killmail_id, killmail_time in sorted(
                month_rows,
                key=lambda row: (
                    row[2],
                    row[1],
                    1 if (row[0] == "kill" if prefer_kill else row[0] == "loss") else 0,
                ),
                reverse=True,
            ):
                kmid = int(killmail_id)
                if kmid in seen:
                    continue
                seen.add(kmid)
                merged.append((side, kmid, killmail_time))
                if len(merged) >= fetch_limit:
                    break

    if timing_marks is not None:
        timing_marks["ids_scanned_partitions"] = scanned_partitions
        timing_marks["loss_ids_rows"] = loss_rows_count
        timing_marks["attack_ids_rows"] = attack_rows_count
        timing_marks["merged_rows"] = len(merged)

    has_next = len(merged) > offset + per_page
    return merged[offset:offset + per_page], has_next, page, per_page


def _group_entity_killmails(conn, entity_type, entity_id, page=1, per_page=100, filters=None):
    timing_started = perf_counter()
    timing_marks = {}
    scope = _entity_killmail_scope(entity_type)
    normalized_filters = _normalize_killmail_filters(filters)
    timing_label = scope["timing_label"]
    attacker_column = scope["attacker_column"]
    multi_ship_scope = scope["entity_type"] == "ship" and isinstance(entity_id, (list, tuple, set))
    multi_system_scope = scope["entity_type"] == "system" and isinstance(entity_id, (list, tuple, set))
    if multi_ship_scope:
        scope_value = sorted({int(value) for value in entity_id if value is not None})
        scope_attacker_predicate = f"ka.{attacker_column} = ANY(%s)"
        scope_attacker_params = [scope_value]
    elif multi_system_scope:
        scope_value = sorted({int(value) for value in entity_id if value is not None})
        scope_attacker_predicate = "TRUE"
        scope_attacker_params = []
    elif scope["entity_type"] == "coalition":
        scope_value = _normalize_coalition_killmail_scope(entity_id)
        scope_attacker_predicate = _coalition_raw_scope_predicate(
            "ka", "corporation_id", "alliance_id", "killmail_time"
        )
        scope_attacker_params = _coalition_raw_scope_params(scope_value)
    elif scope["entity_type"] == "alliance":
        scope_value = int(entity_id)
        scope_attacker_predicate = _historical_alliance_scope_predicate(
            "ka", "corporation_id", "alliance_id", "killmail_time"
        )
        scope_attacker_params = [scope_value, scope_value]
    elif scope["entity_type"] == "system":
        scope_value = int(entity_id)
        # A system killboard has no loss side: every killmail that happened in the
        # system is a kill. Aggregate all attackers for the Pilot column.
        scope_attacker_predicate = "TRUE"
        scope_attacker_params = []
    else:
        scope_value = entity_id
        scope_attacker_predicate = f"ka.{attacker_column} = %s"
        scope_attacker_params = [scope_value]

    table_check_started = perf_counter()
    if not _table_exists(conn, "rawkm", "killmails") or not _table_exists(conn, "rawkm", "killmail_attackers"):
        timing_marks["table_check_ms"] = round((perf_counter() - table_check_started) * 1000, 1)
        _timing_print(f"ENTITY_PROFILE_TIMING {timing_label} entity_id={entity_id} table_missing total_ms={round((perf_counter() - timing_started) * 1000, 1)} details={timing_marks}")
        return [], None
    timing_marks["table_check_ms"] = round((perf_counter() - table_check_started) * 1000, 1)

    try:
        ids_started = perf_counter()
        page_entries, has_next, page, per_page = _entity_killmail_page_entries(
            conn,
            entity_type,
            entity_id,
            page=page,
            per_page=per_page,
            timing_marks=timing_marks,
        )
        if normalized_filters["active"]:
            page_entries, has_next, page, per_page = _filtered_entity_killmail_page_entries(
                conn, entity_type, entity_id, page, per_page, normalized_filters
            )
        timing_marks["ids_ms"] = round((perf_counter() - ids_started) * 1000, 1)
        timing_marks["page_entries"] = len(page_entries)
    except QueryCanceled:
        conn.rollback()
        _timing_print(f"ENTITY_PROFILE_TIMING {timing_label} entity_id={entity_id} timeout_stage=ids total_ms={round((perf_counter() - timing_started) * 1000, 1)} details={timing_marks}")
        return [], {
            "page": max(1, int(page or 1)),
            "per_page": max(1, min(100, int(per_page or 100))),
            "has_prev": int(page or 1) > 1,
            "has_next": False,
            "prev_page": int(page or 1) - 1 if int(page or 1) > 1 else 1,
            "next_page": int(page or 1),
            "timed_out": True,
        }

    if not page_entries:
        _timing_print(f"ENTITY_PROFILE_TIMING {timing_label} entity_id={entity_id} page={page} empty total_ms={round((perf_counter() - timing_started) * 1000, 1)} details={timing_marks}")
        progressive_pagination = _progressive_context_pagination(
            normalized_filters,
            per_page,
            has_next=has_next,
        )
        if progressive_pagination is not None:
            return [], progressive_pagination
        return [], {
            "page": page,
            "per_page": per_page,
            "has_prev": page > 1,
            "has_next": False,
            "prev_page": page - 1 if page > 1 else 1,
            "next_page": page,
            "timed_out": False,
        }

    values_sql = ",".join(["(%s::text, %s::bigint, %s::timestamptz, %s::integer)" for _ in page_entries])
    values_params = []
    for order_index, (side, killmail_id, killmail_time) in enumerate(page_entries):
        values_params.extend([side, killmail_id, killmail_time, order_index])

    # Bound the current page to its actual month span so PostgreSQL can
    # prune rawkm monthly partitions for every detail query below.
    page_times = [entry[2] for entry in page_entries]
    range_first = min(page_times)
    range_last = max(page_times)
    range_start = range_first.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if range_last.month == 12:
        range_end = range_last.replace(
            year=range_last.year + 1, month=1, day=1,
            hour=0, minute=0, second=0, microsecond=0,
        )
    else:
        range_end = range_last.replace(
            month=range_last.month + 1, day=1,
            hour=0, minute=0, second=0, microsecond=0,
        )
    partition_range_params = [range_start, range_end]

    detail_sql = f"""
        WITH page_ids(side, killmail_id, killmail_time, order_index) AS (
            VALUES {values_sql}
        )
        SELECT
            p.side,
            p.order_index,
            km.killmail_id,
            km.killmail_time,
            km.solar_system_id,
            km.victim_ship_type_id,
            km.victim_damage_taken,
            km.victim_character_id,
            vc.name AS victim_character_name,
            km.victim_corporation_id,
            vcorp.name AS victim_corporation_name,
            vcorp.ticker AS victim_corporation_ticker,
            COALESCE(km.victim_alliance_id, vhist.alliance_id) AS victim_alliance_id,
            vall.name AS victim_alliance_name,
            vall.ticker AS victim_alliance_ticker
        FROM page_ids p
        JOIN rawkm.killmails km
          ON km.killmail_id = p.killmail_id
         AND km.killmail_time = p.killmail_time
         AND km.killmail_time >= %s
         AND km.killmail_time < %s
        LEFT JOIN entities.characters vc
          ON vc.character_id = km.victim_character_id
        LEFT JOIN entities.corporations vcorp
          ON vcorp.corporation_id = km.victim_corporation_id
        LEFT JOIN LATERAL (
            SELECT h.alliance_id
            FROM entities.corporation_alliance_history h
            WHERE km.victim_alliance_id IS NULL
              AND h.corporation_id = km.victim_corporation_id
              AND h.start_date <= km.killmail_time
              AND (h.end_date IS NULL OR km.killmail_time < h.end_date)
            ORDER BY h.start_date DESC, h.record_id DESC
            LIMIT 1
        ) vhist ON TRUE
        LEFT JOIN entities.alliances vall
          ON vall.alliance_id = COALESCE(km.victim_alliance_id, vhist.alliance_id)
        ORDER BY p.order_index ASC
    """

    scope_attackers_sql = f"""
        WITH page_ids(side, killmail_id, killmail_time, order_index) AS (
            VALUES {values_sql}
        )
        SELECT
            ka.killmail_id,
            ka.killmail_time,
            COUNT(DISTINCT ka.character_id) FILTER (WHERE ka.character_id IS NOT NULL)::integer AS pilot_count,
            COUNT(*)::integer AS attacker_rows_count,
            COALESCE(SUM(COALESCE(ka.damage_done, 0)), 0)::integer AS damage_done,
            (ARRAY_AGG(ka.ship_type_id ORDER BY COALESCE(ka.damage_done, 0) DESC, ka.attacker_index ASC))[1] AS ship_type_id,
            (ARRAY_AGG(ka.weapon_type_id ORDER BY COALESCE(ka.damage_done, 0) DESC, ka.attacker_index ASC))[1] AS weapon_type_id,
            BOOL_OR(COALESCE(ka.final_blow, FALSE))::boolean AS final_blow
        FROM page_ids p
        JOIN rawkm.killmail_attackers ka
          ON ka.killmail_id = p.killmail_id
         AND ka.killmail_time = p.killmail_time
         AND ka.killmail_time >= %s
         AND ka.killmail_time < %s
        WHERE {scope_attacker_predicate}
        GROUP BY ka.killmail_id, ka.killmail_time
    """

    final_sql = f"""
        WITH page_ids(side, killmail_id, killmail_time, order_index) AS (
            VALUES {values_sql}
        )
        SELECT
            ka.killmail_id,
            ka.killmail_time,
            ka.character_id,
            c.name AS character_name,
            ka.corporation_id,
            corp.name AS corporation_name,
            corp.ticker AS corporation_ticker,
            COALESCE(ka.alliance_id, fhist.alliance_id) AS alliance_id,
            alli.name AS alliance_name,
            alli.ticker AS alliance_ticker,
            ka.ship_type_id,
            ka.weapon_type_id,
            COALESCE(ka.damage_done, 0)::integer AS damage_done,
            ka.attacker_index
        FROM page_ids p
        JOIN rawkm.killmail_attackers ka
          ON ka.killmail_id = p.killmail_id
         AND ka.killmail_time = p.killmail_time
         AND ka.killmail_time >= %s
         AND ka.killmail_time < %s
        LEFT JOIN entities.characters c
          ON c.character_id = ka.character_id
        LEFT JOIN entities.corporations corp
          ON corp.corporation_id = ka.corporation_id
        LEFT JOIN LATERAL (
            SELECT h.alliance_id
            FROM entities.corporation_alliance_history h
            WHERE ka.alliance_id IS NULL
              AND h.corporation_id = ka.corporation_id
              AND h.start_date <= ka.killmail_time
              AND (h.end_date IS NULL OR ka.killmail_time < h.end_date)
            ORDER BY h.start_date DESC, h.record_id DESC
            LIMIT 1
        ) fhist ON TRUE
        LEFT JOIN entities.alliances alli
          ON alli.alliance_id = COALESCE(ka.alliance_id, fhist.alliance_id)
        WHERE ka.final_blow IS TRUE
        ORDER BY p.order_index ASC, ka.attacker_index ASC
    """

    counts_sql = f"""
        WITH page_ids(side, killmail_id, killmail_time, order_index) AS (
            VALUES {values_sql}
        )
        SELECT
            ka.killmail_id,
            ka.killmail_time,
            COUNT(*)::integer AS attackers_count
        FROM page_ids p
        JOIN rawkm.killmail_attackers ka
          ON ka.killmail_id = p.killmail_id
         AND ka.killmail_time = p.killmail_time
         AND ka.killmail_time >= %s
         AND ka.killmail_time < %s
        GROUP BY ka.killmail_id, ka.killmail_time
    """

    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '120000ms'")

            detail_started = perf_counter()
            cur.execute(detail_sql, values_params + partition_range_params)
            detail_rows = cur.fetchall()
            timing_marks["detail_killmails_ms"] = round((perf_counter() - detail_started) * 1000, 1)
            timing_marks["detail_killmail_rows"] = len(detail_rows)

            scope_started = perf_counter()
            cur.execute(scope_attackers_sql, values_params + partition_range_params + scope_attacker_params)
            scope_rows = cur.fetchall()
            timing_marks["detail_scope_attackers_ms"] = round((perf_counter() - scope_started) * 1000, 1)
            timing_marks["detail_scope_attacker_rows"] = len(scope_rows)

            final_started = perf_counter()
            cur.execute(final_sql, values_params + partition_range_params)
            final_rows = cur.fetchall()
            timing_marks["detail_final_ms"] = round((perf_counter() - final_started) * 1000, 1)
            timing_marks["detail_final_rows"] = len(final_rows)

            counts_started = perf_counter()
            cur.execute(counts_sql, values_params + partition_range_params)
            count_rows = cur.fetchall()
            timing_marks["detail_counts_ms"] = round((perf_counter() - counts_started) * 1000, 1)
            timing_marks["detail_count_rows"] = len(count_rows)
    except QueryCanceled:
        conn.rollback()
        _timing_print(f"ENTITY_PROFILE_TIMING {timing_label} entity_id={entity_id} timeout_stage=details total_ms={round((perf_counter() - timing_started) * 1000, 1)} details={timing_marks}")
        return [], {
            "page": page,
            "per_page": per_page,
            "has_prev": page > 1,
            "has_next": False,
            "prev_page": page - 1 if page > 1 else 1,
            "next_page": page,
            "timed_out": True,
        }

    scope_by_km = {}
    for scope_row in scope_rows:
        (
            scope_killmail_id,
            scope_killmail_time,
            pilot_count,
            attacker_rows_count,
            damage_done,
            ship_type_id,
            weapon_type_id,
            final_blow,
        ) = scope_row
        key = (int(scope_killmail_id), scope_killmail_time)
        normalized_pilot_count = int(pilot_count or 0)
        if normalized_pilot_count <= 0:
            normalized_pilot_count = int(attacker_rows_count or 0)
        scope_by_km[key] = {
            "pilot_count": normalized_pilot_count,
            "ship_type_id": ship_type_id,
            "weapon_type_id": weapon_type_id,
            "damage_done": int(damage_done or 0),
            "final_blow": bool(final_blow),
        }

    final_by_km = {}
    for final_row in final_rows:
        (
            final_killmail_id,
            final_killmail_time,
            final_character_id,
            final_character_name,
            final_corporation_id,
            final_corporation_name,
            final_corporation_ticker,
            final_alliance_id,
            final_alliance_name,
            final_alliance_ticker,
            final_ship_type_id,
            final_weapon_type_id,
            final_damage_done,
            final_attacker_index,
        ) = final_row
        key = (int(final_killmail_id), final_killmail_time)
        if key not in final_by_km:
            final_by_km[key] = {
                "character_id": final_character_id,
                "character_name": final_character_name,
                "corporation_id": final_corporation_id,
                "corporation_name": final_corporation_name,
                "corporation_ticker": final_corporation_ticker,
                "alliance_id": final_alliance_id,
                "alliance_name": final_alliance_name,
                "alliance_ticker": final_alliance_ticker,
                "ship_type_id": final_ship_type_id,
                "weapon_type_id": final_weapon_type_id,
                "damage_done": final_damage_done,
                "attacker_index": final_attacker_index,
            }

    counts_by_km = {
        (int(killmail_id), killmail_time): int(attackers_count or 0)
        for killmail_id, killmail_time, attackers_count in count_rows
    }

    rows = []
    for detail_row in detail_rows:
        (
            side,
            order_index,
            killmail_id,
            killmail_time,
            solar_system_id,
            victim_ship_type_id,
            victim_damage_taken,
            victim_character_id,
            victim_character_name,
            victim_corporation_id,
            victim_corporation_name,
            victim_corporation_ticker,
            victim_alliance_id,
            victim_alliance_name,
            victim_alliance_ticker,
        ) = detail_row
        key = (int(killmail_id), killmail_time)
        scope_attacker = scope_by_km.get(key, {})
        final_attacker = final_by_km.get(key, {})
        rows.append((
            side,
            killmail_id,
            killmail_time,
            solar_system_id,
            victim_ship_type_id,
            victim_damage_taken,
            victim_character_id,
            victim_character_name,
            victim_corporation_id,
            victim_corporation_name,
            victim_corporation_ticker,
            victim_alliance_id,
            victim_alliance_name,
            victim_alliance_ticker,
            scope_attacker.get("ship_type_id"),
            scope_attacker.get("weapon_type_id"),
            scope_attacker.get("damage_done", 0),
            scope_attacker.get("final_blow", False),
            scope_attacker.get("pilot_count", 0),
            final_attacker.get("character_id"),
            final_attacker.get("character_name"),
            final_attacker.get("corporation_id"),
            final_attacker.get("corporation_name"),
            final_attacker.get("corporation_ticker"),
            final_attacker.get("alliance_id"),
            final_attacker.get("alliance_name"),
            final_attacker.get("alliance_ticker"),
            final_attacker.get("ship_type_id"),
            final_attacker.get("weapon_type_id"),
            final_attacker.get("damage_done"),
            counts_by_km.get(key, 0),
        ))
    timing_marks["detail_rows"] = len(rows)

    lookup_started = perf_counter()
    type_ids = set()
    system_ids = set()
    for row in rows:
        (
            _side,
            _killmail_id,
            _killmail_time,
            solar_system_id,
            victim_ship_type_id,
            _victim_damage_taken,
            _victim_character_id,
            _victim_character_name,
            _victim_corporation_id,
            _victim_corporation_name,
            _victim_corporation_ticker,
            _victim_alliance_id,
            _victim_alliance_name,
            _victim_alliance_ticker,
            pilot_ship_type_id,
            pilot_weapon_type_id,
            _pilot_damage_done,
            _pilot_final_blow,
            _pilot_count,
            _final_character_id,
            _final_character_name,
            _final_corporation_id,
            _final_corporation_name,
            _final_corporation_ticker,
            _final_alliance_id,
            _final_alliance_name,
            _final_alliance_ticker,
            final_ship_type_id,
            final_weapon_type_id,
            _final_damage_done,
            _attackers_count,
        ) = row
        if solar_system_id is not None:
            system_ids.add(solar_system_id)
        for type_id in (victim_ship_type_id, pilot_ship_type_id, pilot_weapon_type_id, final_ship_type_id, final_weapon_type_id):
            if type_id is not None:
                type_ids.add(type_id)

    type_names = _lookup_type_names(conn, type_ids)
    system_locations = _lookup_system_locations(conn, system_ids)
    timing_marks["sde_lookup_ms"] = round((perf_counter() - lookup_started) * 1000, 1)
    timing_marks["sde_type_names"] = len(type_names)
    timing_marks["sde_system_locations"] = len(system_locations)

    build_started = perf_counter()
    killmails = []
    for row in rows:
        (
            side,
            killmail_id,
            killmail_time,
            solar_system_id,
            victim_ship_type_id,
            victim_damage_taken,
            victim_character_id,
            victim_character_name,
            victim_corporation_id,
            victim_corporation_name,
            victim_corporation_ticker,
            victim_alliance_id,
            victim_alliance_name,
            victim_alliance_ticker,
            pilot_ship_type_id,
            pilot_weapon_type_id,
            pilot_damage_done,
            pilot_final_blow,
            pilot_count,
            final_character_id,
            final_character_name,
            final_corporation_id,
            final_corporation_name,
            final_corporation_ticker,
            final_alliance_id,
            final_alliance_name,
            final_alliance_ticker,
            final_ship_type_id,
            final_weapon_type_id,
            final_damage_done,
            attackers_count,
        ) = row

        victim_character = _entity_ref("character", victim_character_id, victim_character_name)
        victim_corporation = _entity_ref("corporation", victim_corporation_id, victim_corporation_name, victim_corporation_ticker)
        victim_alliance = _entity_ref("alliance", victim_alliance_id, victim_alliance_name, victim_alliance_ticker)
        final_character = _entity_ref("character", final_character_id, final_character_name)
        final_corporation = _entity_ref("corporation", final_corporation_id, final_corporation_name, final_corporation_ticker)
        final_alliance = _entity_ref("alliance", final_alliance_id, final_alliance_name, final_alliance_ticker)

        pilot_damage = int(pilot_damage_done or 0)
        normalized_pilot_count = int(pilot_count or 0)

        killmails.append({
            "side": side,
            "is_loss": side == "loss",
            "is_kill": side == "kill",
            "killmail_id": int(killmail_id),
            "killmail_time": killmail_time,
            "date_key": killmail_time.date().isoformat() if killmail_time else "Unknown date",
            "time_label": killmail_time.strftime("%H:%M") if killmail_time else "--:--",
            "solar_system_id": int(solar_system_id) if solar_system_id is not None else None,
            "location": system_locations.get(int(solar_system_id), {
                "system": _location_ref("system", solar_system_id, _format_system_name(solar_system_id)) if solar_system_id is not None else None,
                "constellation": None,
                "region": None,
            }),
            "system_name": (system_locations.get(int(solar_system_id), {}).get("system", {}) or {}).get("name", _format_system_name(solar_system_id)) if solar_system_id is not None else "Unknown",
            "victim_damage_taken": int(victim_damage_taken or 0),
            "victim_ship": {
                "type_id": int(victim_ship_type_id) if victim_ship_type_id is not None else None,
                "name": type_names.get(int(victim_ship_type_id), _format_type_name(victim_ship_type_id)) if victim_ship_type_id is not None else "Unknown",
                "image_url": type_icon_url(victim_ship_type_id, 64),
                "url": profile_url("ship", victim_ship_type_id) if victim_ship_type_id is not None else None,
            },
            "pilot_ship": {
                "type_id": int(pilot_ship_type_id) if pilot_ship_type_id is not None else None,
                "name": type_names.get(int(pilot_ship_type_id), _format_type_name(pilot_ship_type_id)) if pilot_ship_type_id is not None else "Unknown",
                "image_url": type_icon_url(pilot_ship_type_id, 64),
                "url": profile_url("ship", pilot_ship_type_id) if pilot_ship_type_id is not None else None,
            } if pilot_ship_type_id is not None else None,
            "pilot_weapon": {
                "type_id": int(pilot_weapon_type_id) if pilot_weapon_type_id is not None else None,
                "name": type_names.get(int(pilot_weapon_type_id), _format_type_name(pilot_weapon_type_id)) if pilot_weapon_type_id is not None else "Unknown",
                "image_url": type_icon_url(pilot_weapon_type_id, 64),
                "url": profile_url("weapon", pilot_weapon_type_id) if pilot_weapon_type_id is not None else None,
            } if pilot_weapon_type_id is not None else None,
            "pilot_damage_done": pilot_damage,
            "pilot_damage_percent": _damage_percent(pilot_damage, victim_damage_taken),
            "pilot_final_blow": bool(pilot_final_blow),
            "pilot_count": normalized_pilot_count,
            "pilot_count_label": f"{normalized_pilot_count} pilot" + ("s" if normalized_pilot_count != 1 else "") if normalized_pilot_count > 0 else None,
            "entity_context_type": scope["entity_type"],
            "final_ship": {
                "type_id": int(final_ship_type_id) if final_ship_type_id is not None else None,
                "name": type_names.get(int(final_ship_type_id), _format_type_name(final_ship_type_id)) if final_ship_type_id is not None else "Unknown",
                "image_url": type_icon_url(final_ship_type_id, 64),
                "url": profile_url("ship", final_ship_type_id) if final_ship_type_id is not None else None,
            } if final_ship_type_id is not None else None,
            "final_weapon": {
                "type_id": int(final_weapon_type_id) if final_weapon_type_id is not None else None,
                "name": type_names.get(int(final_weapon_type_id), _format_type_name(final_weapon_type_id)) if final_weapon_type_id is not None else "Unknown",
                "image_url": type_icon_url(final_weapon_type_id, 64),
                "url": profile_url("weapon", final_weapon_type_id) if final_weapon_type_id is not None else None,
            } if final_weapon_type_id is not None else None,
            "final_damage_done": int(final_damage_done or 0),
            "attackers_count": int(attackers_count or 0),
            "victim_character": victim_character,
            "victim_corporation": victim_corporation,
            "victim_alliance": victim_alliance,
            "final_character": final_character,
            "final_corporation": final_corporation,
            "final_alliance": final_alliance,
            "zkill_url": f"https://zkillboard.com/kill/{int(killmail_id)}/",
            "value_label": None,
        })

    timing_marks["build_rows_ms"] = round((perf_counter() - build_started) * 1000, 1)
    timing_marks["total_ms"] = round((perf_counter() - timing_started) * 1000, 1)
    _timing_print(f"ENTITY_PROFILE_TIMING {timing_label} entity_id={entity_id} page={page} per_page={per_page} details={timing_marks}")

    pagination = _progressive_context_pagination(
        normalized_filters,
        per_page,
        has_next=has_next,
    )
    if pagination is None:
        pagination = {
            "page": page,
            "per_page": per_page,
            "has_prev": page > 1,
            "has_next": has_next,
            "prev_page": page - 1 if page > 1 else 1,
            "next_page": page + 1 if has_next else page,
            "timed_out": False,
        }

    return killmails, pagination



def _normalize_group_killmail_mode(mode):
    normalized = str(mode or "api").strip().lower()
    if normalized not in {"api", "total", "hidden"}:
        return "api"
    return normalized


def _mer_group_scope(entity_type):
    if str(entity_type or "").strip().lower() == "coalition":
        return {
            "coalition_scope": True,
            "kill_priority": False,
        }
    entity_type = normalize_entity_type(entity_type)
    if entity_type == "corporation":
        return {
            "victim_column": "victim_corporation_id",
            "killer_column": "killer_corporation_id",
            "kill_priority": False,
        }
    if entity_type == "alliance":
        return {
            "victim_column": "victim_alliance_id",
            "killer_column": "killer_alliance_id",
            "kill_priority": False,
        }
    if entity_type == "ship":
        return {
            "victim_column": "victim_ship_type_id",
            "killer_column": "killer_ship_type_id",
            "kill_priority": True,
        }
    if entity_type == "system":
        return {
            "location_column": "solar_system_id",
            "location_scope": True,
            "kill_priority": True,
        }
    if entity_type == "region":
        return {
            "location_column": "region_id",
            "location_scope": True,
            "kill_priority": True,
        }
    raise EntityError("entity_type_invalid")


def _group_entity_mer_killmails(conn, entity_type, entity_id, page=1, per_page=100, filters=None, hidden_only=False):
    scope = _mer_group_scope(entity_type)
    normalized_filters = _normalize_killmail_filters(filters)
    location_scope = bool(scope.get("location_scope"))
    coalition_scope = bool(scope.get("coalition_scope"))
    multi_scope = isinstance(entity_id, (list, tuple, set))

    if coalition_scope:
        scope_value = _normalize_coalition_killmail_scope(entity_id)
        victim_scope_predicate = _coalition_mer_scope_predicate("victim")
        killer_scope_predicate = _coalition_mer_scope_predicate("killer")
        victim_scope_params = _coalition_mer_scope_params(scope_value)
        killer_scope_params = _coalition_mer_scope_params(scope_value)
        multi_ship_scope = False
    elif location_scope:
        location_column = scope["location_column"]
        if multi_scope:
            scope_value = sorted({int(value) for value in entity_id if value is not None})
            location_scope_predicate = f"m.{location_column} = ANY(%s)"
        else:
            scope_value = int(entity_id)
            location_scope_predicate = f"m.{location_column} = %s"
        location_scope_params = [scope_value]
        victim_scope_predicate = None
        killer_scope_predicate = None
        multi_ship_scope = False
    else:
        victim_column = scope["victim_column"]
        killer_column = scope["killer_column"]
        multi_ship_scope = normalize_entity_type(entity_type) == "ship" and multi_scope
        if multi_ship_scope:
            scope_value = sorted({int(value) for value in entity_id if value is not None})
            victim_scope_predicate = f"m.{victim_column} = ANY(%s)"
            killer_scope_predicate = f"m.{killer_column} = ANY(%s)"
        else:
            scope_value = entity_id
            victim_scope_predicate = f"m.{victim_column} = %s"
            killer_scope_predicate = f"m.{killer_column} = %s"
        victim_scope_params = [scope_value]
        killer_scope_params = [scope_value]

    page = max(1, int(page or 1))
    per_page = max(1, min(100, int(per_page or 100)))
    offset = (page - 1) * per_page

    if not _table_exists(conn, "mer", "killmails"):
        return [], {
            "page": page,
            "per_page": per_page,
            "has_prev": page > 1,
            "has_next": False,
            "prev_page": page - 1 if page > 1 else 1,
            "next_page": page,
            "timed_out": False,
        }

    # MER ne contient pas les modules. Un filtre module ne peut donc pas être
    # validé sur une ligne MER sans inventer de donnée : aucune ligne n'est renvoyée.
    if normalized_filters["module_type_ids"]:
        return [], {
            "page": page,
            "per_page": per_page,
            "has_prev": page > 1,
            "has_next": False,
            "prev_page": page - 1 if page > 1 else 1,
            "next_page": page,
            "timed_out": False,
        }

    if (multi_scope and not scope_value) or (coalition_scope and not scope_value["alliance_ids"] and not scope_value["corporation_ids"]):
        return [], {
            "page": page,
            "per_page": per_page,
            "has_prev": page > 1,
            "has_next": False,
            "prev_page": page - 1 if page > 1 else 1,
            "next_page": page,
            "timed_out": False,
        }

    participation = normalized_filters["participation"]
    if location_scope:
        # A location is where the fight happened, not one side of it. Every row is
        # intentionally rendered green in system/constellation/region killboards.
        side_sql = "'kill'::text"
        clauses = [location_scope_predicate]
        params = list(location_scope_params)
    elif participation == "kills":
        side_sql = "'kill'::text"
        clauses = [killer_scope_predicate]
        params = list(killer_scope_params)
    elif participation == "losses":
        side_sql = "'loss'::text"
        clauses = [victim_scope_predicate]
        params = list(victim_scope_params)
    else:
        side_sql = (
            f"CASE WHEN {killer_scope_predicate} THEN 'kill'::text ELSE 'loss'::text END"
            if scope.get("kill_priority")
            else f"CASE WHEN {victim_scope_predicate} THEN 'loss'::text ELSE 'kill'::text END"
        )
        clauses = [f"({victim_scope_predicate} OR {killer_scope_predicate})"]
        if scope.get("kill_priority"):
            params = list(killer_scope_params) + list(victim_scope_params) + list(killer_scope_params)
        else:
            params = list(victim_scope_params) + list(victim_scope_params) + list(killer_scope_params)

    if hidden_only:
        clauses.append("m.resolved_km IS NULL")

    if normalized_filters["date_from"]:
        clauses.append("m.kill_datetime >= %s::date")
        params.append(normalized_filters["date_from"])
    if normalized_filters["date_to"]:
        clauses.append("m.kill_datetime < (%s::date + INTERVAL '1 day')")
        params.append(normalized_filters["date_to"])
    if normalized_filters.get("datetime_from"):
        clauses.append("m.kill_datetime >= %s::timestamptz")
        params.append(normalized_filters["datetime_from"])
    if normalized_filters.get("datetime_to"):
        clauses.append("m.kill_datetime < %s::timestamptz")
        params.append(normalized_filters["datetime_to"])

    if entity_type == "corporation" and normalized_filters["affiliation_alliance_ids"]:
        clauses.append(
            "((m.victim_corporation_id = %s AND m.victim_alliance_id = ANY(%s)) "
            "OR (m.killer_corporation_id = %s AND m.killer_alliance_id = ANY(%s)))"
        )
        params.extend([
            entity_id,
            normalized_filters["affiliation_alliance_ids"],
            entity_id,
            normalized_filters["affiliation_alliance_ids"],
        ])

    def add_involved(kind, values):
        victim = f"m.victim_{kind}_id = ANY(%s)"
        killer = f"m.killer_{kind}_id = ANY(%s)"
        role = normalized_filters["involved_role"]
        if role == "victim":
            clauses.append(victim)
            params.append(values)
        elif role == "attacker":
            clauses.append(killer)
            params.append(values)
        else:
            clauses.append(f"({victim} OR {killer})")
            params.extend([values, values])

    if normalized_filters["involved_corporation_ids"]:
        add_involved("corporation", normalized_filters["involved_corporation_ids"])
    if normalized_filters["involved_alliance_ids"]:
        add_involved("alliance", normalized_filters["involved_alliance_ids"])

    if normalized_filters["type_ids"]:
        victim = "m.victim_ship_type_id = ANY(%s)"
        killer = "m.killer_ship_type_id = ANY(%s)"
        role = normalized_filters["type_role"]
        if role == "victim":
            clauses.append(victim)
            params.append(normalized_filters["type_ids"])
        elif role == "attacker":
            clauses.append(killer)
            params.append(normalized_filters["type_ids"])
        else:
            clauses.append(f"({victim} OR {killer})")
            params.extend([normalized_filters["type_ids"], normalized_filters["type_ids"]])

    def mer_ship_term(term):
        role = term["role"]
        type_id = int(term["type_id"])
        victim_sql = "m.victim_ship_type_id = %s"
        attacker_sql = "m.killer_ship_type_id = %s"
        if role == "victim":
            return victim_sql, [type_id]
        if role == "attacker":
            return attacker_sql, [type_id]
        return f"({victim_sql} OR {attacker_sql})", [type_id, type_id]

    def mer_entity_term(term):
        role = term["role"]
        kind = term["entity_type"]
        entity_id = int(term["entity_id"])
        if kind == "character":
            # MER does not contain character IDs. Do not invent a match.
            return "FALSE", []
        victim_sql = f"m.victim_{kind}_id = %s"
        attacker_sql = f"m.killer_{kind}_id = %s"
        if role == "victim":
            return victim_sql, [entity_id]
        if role == "attacker":
            return attacker_sql, [entity_id]
        return f"({victim_sql} OR {attacker_sql})", [entity_id, entity_id]

    builder_ship_include = normalized_filters.get("builder_ship_include") or []
    if builder_ship_include:
        by_role = {"attacker": [], "victim": [], "both": []}
        for term in builder_ship_include:
            by_role[term["role"]].append(term)
        for role_terms in by_role.values():
            if not role_terms:
                continue
            parts = []
            values = []
            for term in role_terms:
                sql, sql_params = mer_ship_term(term)
                parts.append(sql)
                values.extend(sql_params)
            clauses.append("(" + " OR ".join(parts) + ")")
            params.extend(values)

    for term in normalized_filters.get("builder_ship_exclude") or []:
        sql, sql_params = mer_ship_term(term)
        clauses.append(f"NOT ({sql})")
        params.extend(sql_params)

    builder_entity_include = normalized_filters.get("builder_entity_include") or []
    if builder_entity_include:
        by_role = {"attacker": [], "victim": [], "both": []}
        for term in builder_entity_include:
            by_role[term["role"]].append(term)
        for role_terms in by_role.values():
            if not role_terms:
                continue
            parts = []
            values = []
            for term in role_terms:
                sql, sql_params = mer_entity_term(term)
                parts.append(sql)
                values.extend(sql_params)
            clauses.append("(" + " OR ".join(parts) + ")")
            params.extend(values)

    for term in normalized_filters.get("builder_entity_exclude") or []:
        if term.get("entity_type") == "character":
            # A negative character condition cannot be proven from MER-only data.
            clauses.append("FALSE")
            continue
        sql, sql_params = mer_entity_term(term)
        clauses.append(f"NOT ({sql})")
        params.extend(sql_params)

    builder_zone_include = normalized_filters.get("builder_zone_include") or []
    if builder_zone_include:
        zone_ids = _killmail_builder_zone_system_ids(conn, builder_zone_include)
        if zone_ids:
            clauses.append("m.solar_system_id = ANY(%s)")
            params.append(zone_ids)
        else:
            clauses.append("FALSE")

    builder_zone_exclude = normalized_filters.get("builder_zone_exclude") or []
    if builder_zone_exclude:
        zone_ids = _killmail_builder_zone_system_ids(conn, builder_zone_exclude)
        if zone_ids:
            clauses.append("NOT (m.solar_system_id = ANY(%s))")
            params.append(zone_ids)

    select_sql = f"""
        SELECT
            {side_sql} AS side,
            m.source_month,
            m.source_row,
            m.kill_datetime,
            m.solar_system_id,
            m.solar_system_name,
            m.region_id,
            m.region_name,
            m.victim_ship_type_id,
            m.victim_ship_type_name,
            m.victim_ship_group_name,
            m.victim_corporation_id,
            m.victim_corporation_name,
            m.victim_alliance_id,
            m.victim_alliance_name,
            m.killer_ship_type_id,
            m.killer_corporation_id,
            m.killer_corporation_name,
            m.killer_alliance_id,
            m.killer_alliance_name,
            m.resolved_km,
            m.resolved_km_ambiguous,
            m.ccp_isk_lost,
            m.ccp_isk_destroyed,
            m.zkb_isk_lost,
            m.zkb_isk_destroyed
        FROM mer.killmails m
    """

    progressive_pagination = None

    if hidden_only:
        # Hidden rows can be extremely sparse (for example Supercap losses).
        # Scan explicit time windows so each HTTP request returns whatever was
        # found before nginx's timeout, then resume from the returned cursor.
        base_clauses = list(clauses)
        base_params = list(params)
        scan_before = normalized_filters.get("scan_before")
        resume_month = normalized_filters.get("scan_month")
        resume_row = normalized_filters.get("scan_row")

        if scan_before is None:
            if normalized_filters.get("datetime_to"):
                scan_before = normalized_filters["datetime_to"]
            elif normalized_filters["date_to"]:
                scan_before = datetime.combine(
                    normalized_filters["date_to"] + timedelta(days=1),
                    datetime.min.time(),
                    tzinfo=timezone.utc,
                )
            else:
                scan_before = datetime.now(timezone.utc) + timedelta(seconds=1)

        if normalized_filters.get("datetime_from"):
            scan_floor = normalized_filters["datetime_from"]
        elif normalized_filters["date_from"]:
            scan_floor = datetime.combine(
                normalized_filters["date_from"],
                datetime.min.time(),
                tzinfo=timezone.utc,
            )
        else:
            # MER data cannot predate EVE itself; this also gives the progressive
            # scanner a deterministic completion point without a MIN() full scan.
            scan_floor = datetime(2003, 1, 1, tzinfo=timezone.utc)

        fetched = []
        scan_upper = scan_before
        scanned_to = scan_before
        scan_complete = scan_upper <= scan_floor
        scan_blocked = False
        window_days = 31
        deadline = perf_counter() + 20.0

        while len(fetched) <= per_page and not scan_complete and perf_counter() < deadline:
            scan_lower = max(scan_floor, scan_upper - timedelta(days=window_days))
            chunk_clauses = list(base_clauses)
            chunk_params = list(base_params)
            chunk_clauses.append("m.kill_datetime >= %s")
            chunk_params.append(scan_lower)

            if resume_month is not None and resume_row is not None:
                chunk_clauses.append("m.kill_datetime <= %s")
                chunk_params.append(scan_upper)
                chunk_clauses.append(
                    "(m.kill_datetime, m.source_month, m.source_row) < (%s, %s, %s)"
                )
                chunk_params.extend([scan_upper, resume_month, resume_row])
            else:
                chunk_clauses.append("m.kill_datetime < %s")
                chunk_params.append(scan_upper)

            remaining = (per_page + 1) - len(fetched)
            chunk_query = select_sql + f"""
                WHERE {' AND '.join(chunk_clauses)}
                ORDER BY m.kill_datetime DESC, m.source_month DESC, m.source_row DESC
                LIMIT %s
            """
            chunk_params.append(remaining)

            try:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL statement_timeout = '12000ms'")
                    cur.execute(chunk_query, chunk_params)
                    rows = cur.fetchall()
            except QueryCanceled:
                conn.rollback()
                if window_days > 1:
                    window_days = max(1, window_days // 4)
                    continue
                scan_blocked = True
                break

            fetched.extend(rows)

            if len(rows) >= remaining:
                # We filled the visible page inside this time window. Resume
                # strictly after the last displayed MER row on the next request.
                break

            # This whole window has been searched successfully.
            scan_upper = scan_lower
            scanned_to = scan_lower
            resume_month = None
            resume_row = None
            if scan_upper <= scan_floor:
                scan_complete = True

        has_extra_row = len(fetched) > per_page
        fetched = fetched[:per_page]

        if has_extra_row and fetched:
            last_row = fetched[-1]
            next_before = last_row[3]
            next_month = last_row[1]
            next_row = int(last_row[2])
            scan_has_more = True
            scanned_to = next_before
        else:
            next_before = scanned_to
            next_month = None
            next_row = None
            scan_has_more = not scan_complete

        progressive_pagination = {
            "page": 1,
            "per_page": per_page,
            "has_prev": False,
            "has_next": scan_has_more,
            "prev_page": 1,
            "next_page": 1,
            "timed_out": False,
            "progressive": True,
            "scan_has_more": scan_has_more,
            "scan_complete": scan_complete,
            "scan_blocked": scan_blocked,
            "scan_end_label": scanned_to.strftime("%Y-%m-%d") if scanned_to else None,
            "scan_before": next_before.isoformat() if next_before else None,
            "scan_month": next_month.isoformat() if next_month else None,
            "scan_row": next_row,
        }
        has_next = scan_has_more

    else:
        where_sql = " AND ".join(clauses)
        query = select_sql + f"""
            WHERE {where_sql}
            ORDER BY m.kill_datetime DESC, m.source_month DESC, m.source_row DESC
            LIMIT %s OFFSET %s
        """
        params.extend([per_page + 1, offset])

        try:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '120000ms'")
                cur.execute(query, params)
                fetched = cur.fetchall()
        except QueryCanceled:
            conn.rollback()
            return [], {
                "page": page,
                "per_page": per_page,
                "has_prev": page > 1,
                "has_next": False,
                "prev_page": page - 1 if page > 1 else 1,
                "next_page": page,
                "timed_out": True,
            }

        has_next = len(fetched) > per_page
        fetched = fetched[:per_page]

    system_ids = {int(row[4]) for row in fetched if row[4] is not None}
    type_ids = set()
    for row in fetched:
        if row[8] is not None:
            type_ids.add(int(row[8]))
        if row[15] is not None:
            type_ids.add(int(row[15]))

    type_names = _lookup_type_names(conn, type_ids)
    system_locations = _lookup_system_locations(conn, system_ids)

    corporation_ids = set()
    alliance_ids = set()
    for row in fetched:
        for value in (row[11], row[16]):
            if value is not None:
                corporation_ids.add(int(value))
        for value in (row[13], row[18]):
            if value is not None:
                alliance_ids.add(int(value))

    corporation_entities = _lookup_entity_names(conn, "corporation", corporation_ids)
    alliance_entities = _lookup_entity_names(conn, "alliance", alliance_ids)

    killmails = []
    for row in fetched:
        (
            side,
            source_month,
            source_row,
            kill_datetime,
            solar_system_id,
            solar_system_name,
            region_id,
            region_name,
            victim_ship_type_id,
            victim_ship_type_name,
            victim_ship_group_name,
            victim_corporation_id,
            victim_corporation_name,
            victim_alliance_id,
            victim_alliance_name,
            killer_ship_type_id,
            killer_corporation_id,
            killer_corporation_name,
            killer_alliance_id,
            killer_alliance_name,
            resolved_km,
            resolved_km_ambiguous,
            ccp_isk_lost,
            ccp_isk_destroyed,
            zkb_isk_lost,
            zkb_isk_destroyed,
        ) = row

        candidate_ids = [int(value) for value in (resolved_km or [])]
        kill_datetime_query = quote(kill_datetime.isoformat(), safe="") if kill_datetime else None
        if resolved_km is None:
            match_status = "hidden"
            source_tag = "HID"
            hidden_url = f"/kill/mer/{source_month.isoformat()}/{int(source_row)}/hidden"
            if kill_datetime_query:
                hidden_url += f"?kill_datetime={kill_datetime_query}"
            tag_url = hidden_url
            detail_url = hidden_url
            resolved_killmail_id = None
        elif bool(resolved_km_ambiguous) or len(candidate_ids) != 1:
            match_status = "ambiguous"
            source_tag = "AMB"
            ambiguous_url = f"/kill/mer/{source_month.isoformat()}/{int(source_row)}"
            if kill_datetime_query:
                ambiguous_url += f"?kill_datetime={kill_datetime_query}"
            tag_url = ambiguous_url
            detail_url = ambiguous_url
            resolved_killmail_id = None
        else:
            match_status = "api"
            source_tag = "API"
            resolved_killmail_id = candidate_ids[0]
            tag_url = f"/kill/{resolved_killmail_id}"
            detail_url = tag_url

        victim_ship_name = victim_ship_type_name
        if not victim_ship_name and victim_ship_type_id is not None:
            victim_ship_name = type_names.get(int(victim_ship_type_id), _format_type_name(victim_ship_type_id))
        victim_ship = {
            "type_id": int(victim_ship_type_id) if victim_ship_type_id is not None else None,
            "name": victim_ship_name or "Unknown",
            "image_url": type_icon_url(victim_ship_type_id, 64),
            "url": profile_url("ship", victim_ship_type_id) if victim_ship_type_id is not None else None,
        }

        killer_ship = None
        if killer_ship_type_id is not None:
            killer_ship = {
                "type_id": int(killer_ship_type_id),
                "name": type_names.get(int(killer_ship_type_id), _format_type_name(killer_ship_type_id)),
                "image_url": type_icon_url(killer_ship_type_id, 64),
                "url": profile_url("ship", killer_ship_type_id),
            }

        location = system_locations.get(int(solar_system_id), {}) if solar_system_id is not None else {}
        if not location:
            location = {
                "system": _location_ref("system", solar_system_id, solar_system_name) if solar_system_id is not None else None,
                "constellation": None,
                "region": _location_ref("region", region_id, region_name) if region_id is not None else None,
            }

        value = ccp_isk_lost if ccp_isk_lost is not None else zkb_isk_lost

        killmails.append({
            "source_kind": "mer",
            "match_status": match_status,
            "source_tag": source_tag,
            "tag_url": tag_url,
            "detail_url": detail_url,
            "resolved_killmail_id": resolved_killmail_id,
            "candidate_killmail_ids": candidate_ids,
            "side": side,
            "is_loss": side == "loss",
            "is_kill": side == "kill",
            "killmail_id": resolved_killmail_id,
            "killmail_time": kill_datetime,
            "date_key": kill_datetime.date().isoformat() if kill_datetime else "Unknown date",
            "time_label": kill_datetime.strftime("%H:%M") if kill_datetime else "--:--",
            "source_month": source_month,
            "source_row": int(source_row),
            "solar_system_id": int(solar_system_id) if solar_system_id is not None else None,
            "location": location,
            "system_name": solar_system_name or _format_system_name(solar_system_id),
            "victim_damage_taken": 0,
            "victim_ship": victim_ship,
            "victim_ship_group_name": victim_ship_group_name,
            "pilot_ship": killer_ship if side == "kill" else None,
            "pilot_weapon": None,
            "pilot_damage_done": 0,
            "pilot_damage_percent": None,
            "pilot_final_blow": side == "kill",
            "pilot_count": 0,
            "pilot_count_label": None,
            "entity_context_type": entity_type,
            "final_ship": killer_ship,
            "final_weapon": None,
            "final_damage_done": 0,
            "attackers_count": 0,
            "victim_character": None,
            "victim_corporation": _resolved_entity_ref(
                "corporation", victim_corporation_id, victim_corporation_name, corporation_entities
            ),
            "victim_alliance": _resolved_entity_ref(
                "alliance", victim_alliance_id, victim_alliance_name, alliance_entities
            ),
            "final_character": None,
            "final_corporation": _resolved_entity_ref(
                "corporation", killer_corporation_id, killer_corporation_name, corporation_entities
            ),
            "final_alliance": _resolved_entity_ref(
                "alliance", killer_alliance_id, killer_alliance_name, alliance_entities
            ),
            "zkill_url": None,
            "value_label": _format_isk(value),
            "ccp_isk_lost": ccp_isk_lost,
            "ccp_isk_destroyed": ccp_isk_destroyed,
            "zkb_isk_lost": zkb_isk_lost,
            "zkb_isk_destroyed": zkb_isk_destroyed,
        })

    if progressive_pagination is not None:
        pagination = progressive_pagination
    else:
        pagination = {
            "page": page,
            "per_page": per_page,
            "has_prev": page > 1,
            "has_next": has_next,
            "prev_page": page - 1 if page > 1 else 1,
            "next_page": page + 1 if has_next else page,
            "timed_out": False,
        }
    return killmails, pagination


def get_mer_killmail_match_detail(source_month, source_row, kill_datetime_value=None):
    try:
        normalized_month = datetime.strptime(str(source_month), "%Y-%m-%d").date()
        normalized_row = int(source_row)
        normalized_kill_datetime = (
            datetime.fromisoformat(str(kill_datetime_value).replace("Z", "+00:00"))
            if kill_datetime_value
            else None
        )
    except (TypeError, ValueError) as exc:
        raise EntityError("mer_killmail_id_invalid") from exc

    with db() as conn:
        if not _table_exists(conn, "mer", "killmails"):
            raise EntityError("mer.killmails_missing")

        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '30000ms'")
            if normalized_kill_datetime is not None:
                cur.execute(
                    """
                    SELECT *
                    FROM mer.killmails
                    WHERE kill_datetime = %s
                      AND source_month = %s
                      AND source_row = %s
                    LIMIT 2
                    """,
                    (normalized_kill_datetime, normalized_month, normalized_row),
                )
            else:
                cur.execute(
                    """
                    SELECT *
                    FROM mer.killmails
                    WHERE source_month = %s
                      AND source_row = %s
                    ORDER BY kill_datetime ASC
                    LIMIT 2
                    """,
                    (normalized_month, normalized_row),
                )
            columns = [item.name for item in cur.description]
            rows = cur.fetchall()

        if not rows:
            raise EntityError("mer_killmail_not_found")
        if len(rows) > 1:
            raise EntityError("mer_killmail_not_unique")

        raw = dict(zip(columns, rows[0]))

        type_ids = {
            int(value)
            for value in (raw.get("victim_ship_type_id"), raw.get("killer_ship_type_id"))
            if value is not None
        }
        type_names = _lookup_type_names(conn, type_ids)

        solar_system_id = raw.get("solar_system_id")
        location = {}
        if solar_system_id is not None:
            location = _lookup_system_locations(conn, [solar_system_id]).get(int(solar_system_id), {})

        corporation_entities = _lookup_entity_names(
            conn,
            "corporation",
            [raw.get("victim_corporation_id"), raw.get("killer_corporation_id")],
        )
        alliance_entities = _lookup_entity_names(
            conn,
            "alliance",
            [raw.get("victim_alliance_id"), raw.get("killer_alliance_id")],
        )

    if not location:
        location = {
            "system": _location_ref("system", solar_system_id, raw.get("solar_system_name")) if solar_system_id is not None else None,
            "constellation": None,
            "region": _location_ref("region", raw.get("region_id"), raw.get("region_name")) if raw.get("region_id") is not None else None,
        }

    victim_ship_type_id = raw.get("victim_ship_type_id")
    victim_ship_name = raw.get("victim_ship_type_name")
    if not victim_ship_name and victim_ship_type_id is not None:
        victim_ship_name = type_names.get(int(victim_ship_type_id), _format_type_name(victim_ship_type_id))

    victim_ship = _killmail_type_ref(
        victim_ship_type_id,
        victim_ship_name or "Unknown",
        "ship",
        128,
    ) if victim_ship_type_id is not None else None

    killer_ship_type_id = raw.get("killer_ship_type_id")
    killer_ship = _killmail_type_ref(
        killer_ship_type_id,
        type_names.get(int(killer_ship_type_id), _format_type_name(killer_ship_type_id)),
        "ship",
        64,
    ) if killer_ship_type_id is not None else None

    victim = {
        "character": None,
        "corporation": _resolved_entity_ref(
            "corporation", raw.get("victim_corporation_id"), raw.get("victim_corporation_name"), corporation_entities
        ),
        "alliance": _resolved_entity_ref(
            "alliance", raw.get("victim_alliance_id"), raw.get("victim_alliance_name"), alliance_entities
        ),
    }
    final_blow = {
        "character": None,
        "corporation": _resolved_entity_ref(
            "corporation", raw.get("killer_corporation_id"), raw.get("killer_corporation_name"), corporation_entities
        ),
        "alliance": _resolved_entity_ref(
            "alliance", raw.get("killer_alliance_id"), raw.get("killer_alliance_name"), alliance_entities
        ),
        "ship": killer_ship,
        "weapon": None,
        "damage_done": None,
        "damage_percent": None,
        "final_blow": True,
    }

    raw_kill_datetime = raw.get("kill_datetime")
    mer_killmail = {
        "source_kind": "mer",
        "match_status": "ambiguous",
        "source_tag": "AMB",
        "source_month": raw.get("source_month") or normalized_month,
        "source_row": int(raw.get("source_row") or normalized_row),
        "killmail_time": raw_kill_datetime,
        "date_label": raw_kill_datetime.strftime("%Y-%m-%d %H:%M:%S UTC") if raw_kill_datetime else "Unknown date",
        "solar_system_id": int(solar_system_id) if solar_system_id is not None else None,
        "location": location,
        "victim": victim,
        "victim_ship": victim_ship,
        "victim_ship_group_name": raw.get("victim_ship_group_name"),
        "final_blow": final_blow,
        "ccp_isk_lost": raw.get("ccp_isk_lost"),
        "ccp_isk_destroyed": raw.get("ccp_isk_destroyed"),
        "zkb_isk_lost": raw.get("zkb_isk_lost"),
        "zkb_isk_destroyed": raw.get("zkb_isk_destroyed"),
        "isk_lost_label": _format_isk(raw.get("ccp_isk_lost") if raw.get("ccp_isk_lost") is not None else raw.get("zkb_isk_lost")),
        "isk_destroyed_label": _format_isk(raw.get("ccp_isk_destroyed") if raw.get("ccp_isk_destroyed") is not None else raw.get("zkb_isk_destroyed")),
        "resolved_km_ambiguous": bool(raw.get("resolved_km_ambiguous")),
    }

    candidate_ids = [int(value) for value in (raw.get("resolved_km") or [])]
    candidates = []
    for candidate_id in candidate_ids:
        try:
            detail = get_killmail_detail(candidate_id)
            candidates.append({
                "killmail_id": candidate_id,
                "url": f"/kill/{candidate_id}",
                "error": None,
                "detail": detail,
            })
        except EntityError as exc:
            candidates.append({
                "killmail_id": candidate_id,
                "url": f"/kill/{candidate_id}",
                "error": str(exc),
                "detail": None,
            })

    return {
        "source_month": normalized_month,
        "source_row": normalized_row,
        "raw": raw,
        "mer_killmail": mer_killmail,
        "is_ambiguous": bool(raw.get("resolved_km_ambiguous")) or len(candidate_ids) > 1,
        "candidate_ids": candidate_ids,
        "candidates": candidates,
    }


def get_mer_hidden_killmail_detail(source_month, source_row, kill_datetime_value=None):
    try:
        normalized_month = datetime.strptime(str(source_month), "%Y-%m-%d").date()
        normalized_row = int(source_row)
        normalized_kill_datetime = (
            datetime.fromisoformat(str(kill_datetime_value).replace("Z", "+00:00"))
            if kill_datetime_value
            else None
        )
    except (TypeError, ValueError) as exc:
        raise EntityError("mer_killmail_id_invalid") from exc

    with db() as conn:
        if not _table_exists(conn, "mer", "killmails"):
            raise EntityError("mer.killmails_missing")

        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '30000ms'")
            select_sql = """
                SELECT
                    source_month,
                    source_row,
                    kill_datetime,
                    solar_system_id,
                    solar_system_name,
                    region_id,
                    region_name,
                    victim_ship_type_id,
                    victim_ship_type_name,
                    victim_ship_group_name,
                    victim_corporation_id,
                    victim_corporation_name,
                    victim_alliance_id,
                    victim_alliance_name,
                    killer_ship_type_id,
                    killer_corporation_id,
                    killer_corporation_name,
                    killer_alliance_id,
                    killer_alliance_name,
                    resolved_km,
                    resolved_km_ambiguous,
                    ccp_isk_lost,
                    ccp_isk_destroyed,
                    zkb_isk_lost,
                    zkb_isk_destroyed
                FROM mer.killmails
            """
            if normalized_kill_datetime is not None:
                cur.execute(
                    select_sql + """
                    WHERE kill_datetime = %s
                      AND source_month = %s
                      AND source_row = %s
                    LIMIT 2
                    """,
                    (normalized_kill_datetime, normalized_month, normalized_row),
                )
            else:
                cur.execute(
                    select_sql + """
                    WHERE source_month = %s
                      AND source_row = %s
                    ORDER BY kill_datetime ASC
                    LIMIT 2
                    """,
                    (normalized_month, normalized_row),
                )
            rows = cur.fetchall()

        if not rows:
            raise EntityError("mer_killmail_not_found")
        if len(rows) > 1:
            raise EntityError("mer_killmail_not_unique")

        (
            row_source_month,
            row_source_row,
            kill_datetime,
            solar_system_id,
            solar_system_name,
            region_id,
            region_name,
            victim_ship_type_id,
            victim_ship_type_name,
            victim_ship_group_name,
            victim_corporation_id,
            victim_corporation_name,
            victim_alliance_id,
            victim_alliance_name,
            killer_ship_type_id,
            killer_corporation_id,
            killer_corporation_name,
            killer_alliance_id,
            killer_alliance_name,
            resolved_km,
            resolved_km_ambiguous,
            ccp_isk_lost,
            ccp_isk_destroyed,
            zkb_isk_lost,
            zkb_isk_destroyed,
        ) = rows[0]

        if resolved_km is not None:
            raise EntityError("mer_killmail_not_hidden")

        type_ids = {
            int(value)
            for value in (victim_ship_type_id, killer_ship_type_id)
            if value is not None
        }
        type_names = _lookup_type_names(conn, type_ids)

        location = {}
        if solar_system_id is not None:
            location = _lookup_system_locations(conn, [solar_system_id]).get(int(solar_system_id), {})

        corporation_entities = _lookup_entity_names(
            conn,
            "corporation",
            [victim_corporation_id, killer_corporation_id],
        )
        alliance_entities = _lookup_entity_names(
            conn,
            "alliance",
            [victim_alliance_id, killer_alliance_id],
        )

    if not location:
        location = {
            "system": _location_ref("system", solar_system_id, solar_system_name) if solar_system_id is not None else None,
            "constellation": None,
            "region": _location_ref("region", region_id, region_name) if region_id is not None else None,
        }

    victim_ship_name = victim_ship_type_name
    if not victim_ship_name and victim_ship_type_id is not None:
        victim_ship_name = type_names.get(int(victim_ship_type_id), _format_type_name(victim_ship_type_id))

    victim_ship = _killmail_type_ref(
        victim_ship_type_id,
        victim_ship_name or "Unknown",
        "ship",
        128,
    ) if victim_ship_type_id is not None else None

    killer_ship = _killmail_type_ref(
        killer_ship_type_id,
        type_names.get(int(killer_ship_type_id), _format_type_name(killer_ship_type_id)),
        "ship",
        64,
    ) if killer_ship_type_id is not None else None

    victim = {
        "character": None,
        "corporation": _resolved_entity_ref(
            "corporation", victim_corporation_id, victim_corporation_name, corporation_entities
        ),
        "alliance": _resolved_entity_ref(
            "alliance", victim_alliance_id, victim_alliance_name, alliance_entities
        ),
    }
    final_blow = {
        "character": None,
        "corporation": _resolved_entity_ref(
            "corporation", killer_corporation_id, killer_corporation_name, corporation_entities
        ),
        "alliance": _resolved_entity_ref(
            "alliance", killer_alliance_id, killer_alliance_name, alliance_entities
        ),
        "ship": killer_ship,
        "weapon": None,
        "damage_done": None,
        "damage_percent": None,
        "final_blow": True,
    }

    return {
        "source_kind": "mer",
        "match_status": "hidden",
        "source_tag": "HID",
        "source_month": row_source_month,
        "source_row": int(row_source_row),
        "killmail_time": kill_datetime,
        "date_label": kill_datetime.strftime("%Y-%m-%d %H:%M:%S UTC") if kill_datetime else "Unknown date",
        "solar_system_id": int(solar_system_id) if solar_system_id is not None else None,
        "location": location,
        "victim": victim,
        "victim_ship": victim_ship,
        "victim_ship_group_name": victim_ship_group_name,
        "final_blow": final_blow,
        "ccp_isk_lost": ccp_isk_lost,
        "ccp_isk_destroyed": ccp_isk_destroyed,
        "zkb_isk_lost": zkb_isk_lost,
        "zkb_isk_destroyed": zkb_isk_destroyed,
        "isk_lost_label": _format_isk(ccp_isk_lost if ccp_isk_lost is not None else zkb_isk_lost),
        "isk_destroyed_label": _format_isk(ccp_isk_destroyed if ccp_isk_destroyed is not None else zkb_isk_destroyed),
        "resolved_km_ambiguous": bool(resolved_km_ambiguous),
    }


def _location_system_ids(conn, entity_type, entity_id):
    entity_type = normalize_entity_type(entity_type)
    entity_id = int(entity_id)

    if entity_type == "system":
        return [entity_id]

    if entity_type == "constellation":
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data->'solarSystemIDs' FROM public.sde_mapconstellations WHERE sde_key = %s LIMIT 1",
                (str(entity_id),),
            )
            row = cur.fetchone()
        values = (row[0] if row else None) or []
        return sorted({int(value) for value in values if value is not None})

    if entity_type == "region":
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(NULLIF(data->>'_key',''), NULLIF(sde_key,''))::BIGINT
                FROM public.sde_mapsolarsystems
                WHERE NULLIF(data->>'regionID','')::BIGINT = %s
                ORDER BY 1
                """,
                (entity_id,),
            )
            return [int(row[0]) for row in cur.fetchall()]

    raise EntityError("entity_type_invalid")


def get_location_killmails_page(entity_type, entity_id, page=1, per_page=100, mode="api", filters=None):
    entity_type = normalize_entity_type(entity_type)
    if entity_type not in LOCATION_ENTITY_TYPES:
        raise EntityError("entity_type_invalid")
    try:
        normalized_id = int(entity_id)
    except (TypeError, ValueError) as exc:
        raise EntityError("entity_profile_id_invalid") from exc

    mode = _normalize_group_killmail_mode(mode)
    with db() as conn:
        system_ids = _location_system_ids(conn, entity_type, normalized_id)
        if mode == "api":
            scope_value = system_ids[0] if entity_type == "system" and system_ids else system_ids
            killmails, killmail_pagination = _group_entity_killmails(
                conn,
                "system",
                scope_value,
                page=page,
                per_page=per_page,
                filters=filters,
            )
        else:
            # MER recent (2025-11+) no longer provides region_id.
            # Use solar_system_id for all location scopes (system/constellation/region).
            mer_scope_type = "system"
            mer_scope_value = system_ids[0] if entity_type == "system" and system_ids else system_ids
            killmails, killmail_pagination = _group_entity_mer_killmails(
                conn,
                mer_scope_type,
                mer_scope_value,
                page=page,
                per_page=per_page,
                filters=filters,
                hidden_only=(mode == "hidden"),
            )

    for killmail in killmails:
        killmail["side"] = "kill"
        killmail["is_loss"] = False
        killmail["is_kill"] = True

    return {
        "location_context": True,
        "location_entity_type": entity_type,
        "location_entity_id": normalized_id,
        "system_id": normalized_id if entity_type == "system" else None,
        "killmail_mode": mode,
        "killmail_participation": "kills",
        "killmails": killmails,
        "killmail_groups": _group_killmails_by_date(killmails),
        "killmail_pagination": killmail_pagination,
    }


def get_system_killmails_page(system_id, page=1, per_page=100, mode="api", filters=None):
    return get_location_killmails_page(
        "system",
        system_id,
        page=page,
        per_page=per_page,
        mode=mode,
        filters=filters,
    )


def get_corporation_killmails_page(corporation_id, page=1, per_page=100, filters=None, mode="api"):
    try:
        normalized_id = int(corporation_id)
    except (TypeError, ValueError) as exc:
        raise EntityError("entity_profile_id_invalid") from exc

    mode = _normalize_group_killmail_mode(mode)
    with db() as conn:
        if mode == "api":
            killmails, killmail_pagination = _group_entity_killmails(
                conn,
                "corporation",
                normalized_id,
                page=page,
                per_page=per_page,
                filters=filters,
            )
        else:
            killmails, killmail_pagination = _group_entity_mer_killmails(
                conn,
                "corporation",
                normalized_id,
                page=page,
                per_page=per_page,
                filters=filters,
                hidden_only=(mode == "hidden"),
            )

    return {
        "corporation_id": normalized_id,
        "killmail_mode": mode,
        "killmail_participation": _normalize_killmail_filters(filters)["participation"],
        "killmails": killmails,
        "killmail_groups": _group_killmails_by_date(killmails),
        "killmail_pagination": killmail_pagination,
    }


def get_coalition_killmails_page(alliance_ids, corporation_ids, page=1, per_page=100, filters=None, mode="api"):
    scope_value = _normalize_coalition_killmail_scope({
        "alliance_ids": alliance_ids,
        "corporation_ids": corporation_ids,
    })

    mode = _normalize_group_killmail_mode(mode)
    with db() as conn:
        if mode == "api":
            killmails, killmail_pagination = _group_entity_killmails(
                conn,
                "coalition",
                scope_value,
                page=page,
                per_page=per_page,
                filters=filters,
            )
        else:
            killmails, killmail_pagination = _group_entity_mer_killmails(
                conn,
                "coalition",
                scope_value,
                page=page,
                per_page=per_page,
                filters=filters,
                hidden_only=(mode == "hidden"),
            )

    return {
        "coalition_context": True,
        "killmail_mode": mode,
        "killmail_participation": _normalize_killmail_filters(filters)["participation"],
        "killmails": killmails,
        "killmail_groups": _group_killmails_by_date(killmails),
        "killmail_pagination": killmail_pagination,
    }


def get_alliance_killmails_page(alliance_id, page=1, per_page=100, filters=None, mode="api"):
    try:
        normalized_id = int(alliance_id)
    except (TypeError, ValueError) as exc:
        raise EntityError("entity_profile_id_invalid") from exc

    mode = _normalize_group_killmail_mode(mode)
    with db() as conn:
        if mode == "api":
            killmails, killmail_pagination = _group_entity_killmails(
                conn,
                "alliance",
                normalized_id,
                page=page,
                per_page=per_page,
                filters=filters,
            )
        else:
            killmails, killmail_pagination = _group_entity_mer_killmails(
                conn,
                "alliance",
                normalized_id,
                page=page,
                per_page=per_page,
                filters=filters,
                hidden_only=(mode == "hidden"),
            )

    return {
        "alliance_id": normalized_id,
        "killmail_mode": mode,
        "killmail_participation": _normalize_killmail_filters(filters)["participation"],
        "killmails": killmails,
        "killmail_groups": _group_killmails_by_date(killmails),
        "killmail_pagination": killmail_pagination,
    }


def get_ship_killmails_page(type_id, page=1, per_page=100, mode="api", filters=None):
    try:
        normalized_id = int(type_id)
    except (TypeError, ValueError) as exc:
        raise EntityError("entity_profile_id_invalid") from exc

    mode = _normalize_group_killmail_mode(mode)
    with db() as conn:
        if mode == "api":
            killmails, killmail_pagination = _group_entity_killmails(
                conn,
                "ship",
                normalized_id,
                page=page,
                per_page=per_page,
                filters=filters,
            )
        else:
            killmails, killmail_pagination = _group_entity_mer_killmails(
                conn,
                "ship",
                normalized_id,
                page=page,
                per_page=per_page,
                filters=filters,
                hidden_only=(mode == "hidden"),
            )

    return {
        "ship_type_id": normalized_id,
        "killmail_mode": mode,
        "killmail_participation": _normalize_killmail_filters(filters)["participation"],
        "killmails": killmails,
        "killmail_groups": _group_killmails_by_date(killmails),
        "killmail_pagination": killmail_pagination,
    }



def get_ship_selection_killmails_page(ship_ids, page=1, per_page=100, mode="api", filters=None):
    normalized_ids = sorted({int(value) for value in (ship_ids or []) if value is not None})
    mode = _normalize_group_killmail_mode(mode)

    if not normalized_ids:
        pagination = {
            "page": max(1, int(page or 1)),
            "per_page": max(1, min(100, int(per_page or 100))),
            "has_prev": int(page or 1) > 1,
            "has_next": False,
            "prev_page": max(1, int(page or 1) - 1),
            "next_page": max(1, int(page or 1)),
            "timed_out": False,
        }
        killmails = []
    else:
        with db() as conn:
            if mode == "api":
                killmails, pagination = _group_entity_killmails(
                    conn,
                    "ship",
                    normalized_ids,
                    page=page,
                    per_page=per_page,
                    filters=filters,
                )
            else:
                killmails, pagination = _group_entity_mer_killmails(
                    conn,
                    "ship",
                    normalized_ids,
                    page=page,
                    per_page=per_page,
                    filters=filters,
                    hidden_only=(mode == "hidden"),
                )

    return {
        "ship_type_ids": normalized_ids,
        "killmail_mode": mode,
        "killmail_participation": _normalize_killmail_filters(filters)["participation"],
        "killmails": killmails,
        "killmail_groups": _group_killmails_by_date(killmails),
        "killmail_pagination": pagination,
    }



def _type_entity_profile(conn, entity_type, type_id):
    type_id = int(type_id)
    names = _lookup_type_names(conn, [type_id])
    name = names.get(type_id) or _format_type_name(type_id)

    if entity_type == "ship":
        subtitle = "Ship profile"
    elif entity_type == "weapon":
        subtitle = "Weapon profile"
    elif entity_type == "skill":
        subtitle = "Skill profile"
    else:
        subtitle = "Commodity profile"

    return {
        "name": name,
        "subtitle": subtitle,
        "ticker": None,
        "image_url": type_icon_url(type_id, 128),
    }


def _recent_rows_from_sql(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '30000ms'")
        cur.execute(sql, params or ())
        return cur.fetchall()


def _qualified_rawkm_table(schema_name, table_name):
    return f"{_quote_ident(schema_name)}.{_quote_ident(table_name)}"


def _rawkm_partition_tables(conn, parent_table):
    if not _table_exists(conn, "rawkm", parent_table):
        return []

    parent_regclass = f"rawkm.{parent_table}"
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT child_ns.nspname, child.relname
            FROM pg_inherits i
            JOIN pg_class parent ON parent.oid = i.inhparent
            JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
            JOIN pg_class child ON child.oid = i.inhrelid
            JOIN pg_namespace child_ns ON child_ns.oid = child.relnamespace
            WHERE parent.oid = %s::regclass
            ORDER BY child.relname DESC
            """,
            (parent_regclass,),
        )
        rows = cur.fetchall()

    if rows:
        return [(row[0], row[1]) for row in rows]

    return [("rawkm", parent_table)]


def _matching_attackers_tables_for_killmail_tables(conn, killmail_tables):
    attacker_partitions = _rawkm_partition_tables(conn, "killmail_attackers")
    attacker_by_name = {
        table_name: (schema_name, table_name)
        for schema_name, table_name in attacker_partitions
    }

    matched = []
    used = set()

    for killmail_schema, killmail_table in killmail_tables:
        candidates = []

        if killmail_table == "killmails":
            candidates.append("killmail_attackers")
        elif killmail_table.startswith("killmails"):
            candidates.append("killmail_attackers" + killmail_table[len("killmails"):])

        for candidate in candidates:
            attacker_table = attacker_by_name.get(candidate)
            if attacker_table and attacker_table not in used:
                used.add(attacker_table)
                matched.append(attacker_table)

    for attacker_table in attacker_partitions:
        if attacker_table not in used:
            matched.append(attacker_table)

    return matched


def _rawkm_server_month_tables_first(partitions, parent_table):
    today = datetime.now().date()
    current_key = today.strftime("%Y_%m")
    previous_month = today.month - 1
    previous_year = today.year
    if previous_month == 0:
        previous_month = 12
        previous_year -= 1
    previous_key = f"{previous_year:04d}_{previous_month:02d}"

    current = []
    previous = []
    others = []

    for schema_name, table_name in partitions:
        normalized = table_name.lower()
        if normalized == parent_table.lower():
            others.append((schema_name, table_name))
        elif current_key in normalized:
            current.append((schema_name, table_name))
        elif previous_key in normalized:
            previous.append((schema_name, table_name))
        else:
            others.append((schema_name, table_name))

    return (
        sorted(current, key=lambda item: item[1], reverse=True)
        + sorted(previous, key=lambda item: item[1], reverse=True)
        + sorted(others, key=lambda item: item[1], reverse=True)
    )


def _recent_killmail_rows(conn, limit=100):
    limit = max(1, min(100, int(limit or 100)))
    killmail_tables = _rawkm_partition_tables(conn, "killmails")
    if not killmail_tables:
        return []

    killmail_tables = _rawkm_server_month_tables_first(killmail_tables, "killmails")

    result = []
    seen = set()

    for schema_name, table_name in killmail_tables:
        remaining = limit - len(result)
        if remaining <= 0:
            break

        rows = _recent_rows_from_sql(
            conn,
            f"""
            SELECT
                killmail_id,
                killmail_time,
                victim_character_id,
                victim_corporation_id,
                victim_alliance_id,
                victim_ship_type_id,
                solar_system_id
            FROM {_qualified_rawkm_table(schema_name, table_name)}
            ORDER BY killmail_time DESC, killmail_id DESC
            LIMIT %s
            """,
            (remaining,),
        )

        for row in rows:
            killmail_id = int(row[0])
            if killmail_id in seen:
                continue
            seen.add(killmail_id)
            result.append(row)
            if len(result) >= limit:
                return result

    return result


def _recent_attacker_weapon_rows(conn, killmail_rows):
    killmail_ids = sorted({int(row[0]) for row in killmail_rows if row and row[0] is not None})
    if not killmail_ids:
        return []

    killmail_tables = _rawkm_partition_tables(conn, "killmails")
    attacker_tables = _matching_attackers_tables_for_killmail_tables(conn, killmail_tables)
    if not attacker_tables:
        return []

    result = []
    seen = set()

    for schema_name, table_name in attacker_tables:
        rows = _recent_rows_from_sql(
            conn,
            f"""
            SELECT
                killmail_id,
                killmail_time,
                weapon_type_id
            FROM {_qualified_rawkm_table(schema_name, table_name)}
            WHERE killmail_id = ANY(%s)
              AND weapon_type_id IS NOT NULL
            ORDER BY killmail_time DESC, killmail_id DESC, attacker_index ASC
            """,
            (killmail_ids,),
        )

        for row in rows:
            key = (int(row[0]), int(row[2]))
            if key in seen:
                continue
            seen.add(key)
            result.append(row)

    return result


def _dedupe_recent_pairs(pairs, limit=100):
    result = []
    seen = set()

    for entity_id, killmail_time in pairs:
        if entity_id is None:
            continue

        try:
            normalized_id = int(entity_id)
        except (TypeError, ValueError):
            continue

        if normalized_id <= 0 or normalized_id in seen:
            continue

        seen.add(normalized_id)
        result.append((normalized_id, killmail_time))

        if len(result) >= limit:
            break

    return result


def _lookup_entity_names(conn, entity_type, entity_ids):
    ids = sorted({int(value) for value in entity_ids if value is not None})
    if not ids:
        return {}

    if entity_type == "character":
        if not _table_exists(conn, "entities", "characters"):
            return {}
        query = """
            SELECT character_id::bigint, COALESCE(NULLIF(name, ''), 'Unknown') AS name, NULL::text AS ticker
            FROM entities.characters
            WHERE character_id = ANY(%s)
        """
    elif entity_type == "corporation":
        if not _table_exists(conn, "entities", "corporations"):
            return {}
        query = """
            SELECT corporation_id::bigint, COALESCE(NULLIF(name, ''), 'Unknown') AS name, ticker
            FROM entities.corporations
            WHERE corporation_id = ANY(%s)
        """
    elif entity_type == "alliance":
        if not _table_exists(conn, "entities", "alliances"):
            return {}
        query = """
            SELECT alliance_id::bigint, COALESCE(NULLIF(name, ''), 'Unknown') AS name, ticker
            FROM entities.alliances
            WHERE alliance_id = ANY(%s)
        """
    else:
        return {}

    with conn.cursor() as cur:
        cur.execute(query, (ids,))
        return {
            int(row[0]): {
                "name": row[1] or "Unknown",
                "ticker": row[2],
                "group_name": None,
                "region_name": None,
            }
            for row in cur.fetchall()
        }


def _sde_table_with_data(conn, preferred_names):
    lowered = [name.lower() for name in preferred_names]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.table_name
            FROM information_schema.columns c
            WHERE c.table_schema = 'public'
              AND c.column_name = 'data'
              AND lower(c.table_name) = ANY(%s)
            LIMIT 1
            """,
            (lowered,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _sde_groups_lookup(conn, group_ids):
    ids = sorted({int(value) for value in group_ids if value is not None})
    if not ids:
        return {}

    table = _sde_table_with_data(conn, ["sde_groups", "sde_invgroups", "sde_invGroups"])
    if not table:
        return {}

    group_expr = _sde_int_expr("_key", "group_id", "groupID", "groupId")
    category_expr = _sde_int_expr("category_id", "categoryID", "categoryId")
    query = f"""
        SELECT
            {group_expr} AS group_id,
            COALESCE(
                data->'name'->>'en',
                data->>'groupName',
                data->>'name',
                'Group ' || ({group_expr})::TEXT
            ) AS group_name,
            {category_expr} AS category_id
        FROM public.{_quote_ident(table)}
        WHERE {group_expr} = ANY(%s)
    """

    with conn.cursor() as cur:
        cur.execute(query, (ids,))
        return {
            int(row[0]): {
                "group_id": int(row[0]),
                "group_name": row[1],
                "category_id": int(row[2]) if row[2] is not None else None,
            }
            for row in cur.fetchall()
        }


def _sde_categories_lookup(conn, category_ids):
    ids = sorted({int(value) for value in category_ids if value is not None})
    if not ids:
        return {}

    table = _sde_table_with_data(conn, ["sde_categories", "sde_invcategories", "sde_invCategories"])
    if not table:
        return {}

    category_expr = _sde_int_expr("_key", "category_id", "categoryID", "categoryId")
    query = f"""
        SELECT
            {category_expr} AS category_id,
            COALESCE(
                data->'name'->>'en',
                data->>'categoryName',
                data->>'name',
                'Category ' || ({category_expr})::TEXT
            ) AS category_name
        FROM public.{_quote_ident(table)}
        WHERE {category_expr} = ANY(%s)
    """

    with conn.cursor() as cur:
        cur.execute(query, (ids,))
        return {
            int(row[0]): {
                "category_id": int(row[0]),
                "category_name": row[1],
            }
            for row in cur.fetchall()
        }


def _lookup_type_details(conn, type_ids):
    ids = sorted({int(value) for value in type_ids if value is not None})
    if not ids:
        return {}

    table = _sde_table_with_data(conn, ["sde_types", "sde_invtypes", "sde_invTypes"])
    if not table:
        return {}

    type_expr = _sde_int_expr("_key", "type_id", "typeID", "typeId")
    group_expr = _sde_int_expr("group_id", "groupID", "groupId")
    query = f"""
        SELECT
            {type_expr} AS type_id,
            COALESCE(
                data->'name'->>'en',
                data->>'typeName',
                data->>'name',
                'Type ' || ({type_expr})::TEXT
            ) AS type_name,
            {group_expr} AS group_id
        FROM public.{_quote_ident(table)}
        WHERE {type_expr} = ANY(%s)
    """

    with conn.cursor() as cur:
        cur.execute(query, (ids,))
        rows = cur.fetchall()

    type_ids = {int(row[0]) for row in rows if row[0] is not None}
    typedogma = _sde_typedogma_lookup(conn, type_ids)

    group_ids = {int(row[2]) for row in rows if row[2] is not None}
    groups = _sde_groups_lookup(conn, group_ids)
    category_ids = {group["category_id"] for group in groups.values() if group.get("category_id") is not None}
    categories = _sde_categories_lookup(conn, category_ids)

    result = {}
    for type_id, type_name, group_id in rows:
        group = groups.get(int(group_id)) if group_id is not None else None
        category = categories.get(group.get("category_id")) if group and group.get("category_id") is not None else None
        result[int(type_id)] = {
            "name": type_name or f"Type {int(type_id)}",
            "ticker": None,
            "group_name": group.get("group_name") if group else None,
            "category_name": category.get("category_name") if category else None,
        }

    return result


def _recent_type_rows_from_pairs(conn, entity_type, pairs, limit=100):
    pairs = _dedupe_recent_pairs(pairs, limit=limit)
    ids = [entity_id for entity_id, _killmail_time in pairs]
    details = _lookup_type_details(conn, ids)

    result = []
    for entity_id, killmail_time in pairs:
        payload = details.get(int(entity_id), {})
        result.append({
            "entity_type": entity_type,
            "entity_id": int(entity_id),
            "name": payload.get("name") or _format_type_name(entity_id),
            "ticker": None,
            "group_name": payload.get("group_name"),
            "category_name": payload.get("category_name"),
            "region_name": None,
            "url": profile_url(entity_type, entity_id),
            "image_url": image_url(entity_type, entity_id, 32),
            "last_killmail_time": killmail_time,
        })

    return result


def _recent_standard_rows_from_pairs(conn, entity_type, pairs, limit=100):
    pairs = _dedupe_recent_pairs(pairs, limit=limit)
    ids = [entity_id for entity_id, _killmail_time in pairs]

    if entity_type in {"character", "corporation", "alliance"}:
        names = _lookup_entity_names(conn, entity_type, ids)
    elif entity_type == "system":
        locations = _lookup_system_locations(conn, ids)
        names = {}
        for entity_id in ids:
            location = locations.get(int(entity_id), {}) or {}
            system = location.get("system")
            constellation = location.get("constellation")
            region = location.get("region")
            names[int(entity_id)] = {
                "name": (system or {}).get("name") or _format_system_name(entity_id),
                "ticker": None,
                "group_name": (constellation or {}).get("name"),
                "region_name": (region or {}).get("name"),
            }
    else:
        names = {}

    result = []
    for entity_id, killmail_time in pairs:
        payload = names.get(int(entity_id), {})
        result.append({
            "entity_type": entity_type,
            "entity_id": int(entity_id),
            "name": payload.get("name") or "Unknown",
            "ticker": payload.get("ticker"),
            "group_name": payload.get("group_name"),
            "category_name": None,
            "region_name": payload.get("region_name"),
            "url": profile_url(entity_type, entity_id),
            "image_url": image_url(entity_type, entity_id, 32),
            "last_killmail_time": killmail_time,
        })

    return result


SHIP_SIZE_BY_GROUP_NAME = {
    "corvette": ("Corvette", 10),
    "rookie ship": ("Corvette", 10),

    "frigate": ("Frigate", 20),
    "assault frigate": ("Frigate", 20),
    "covert ops": ("Frigate", 20),
    "interceptor": ("Frigate", 20),
    "electronic attack ship": ("Frigate", 20),
    "logistics frigate": ("Frigate", 20),
    "stealth bomber": ("Frigate", 20),
    "expedition frigate": ("Mining", 80),

    "destroyer": ("Destroyer", 30),
    "interdictor": ("Destroyer", 30),
    "command destroyer": ("Destroyer", 30),
    "tactical destroyer": ("Destroyer", 30),

    "cruiser": ("Cruiser", 40),
    "combat recon ship": ("Cruiser", 40),
    "force recon ship": ("Cruiser", 40),
    "heavy assault cruiser": ("Cruiser", 40),
    "heavy interdiction cruiser": ("Cruiser", 40),
    "logistics": ("Cruiser", 40),
    "strategic cruiser": ("Cruiser", 40),

    "attack battlecruiser": ("Battlecruiser", 50),
    "combat battlecruiser": ("Battlecruiser", 50),
    "battlecruiser": ("Battlecruiser", 50),
    "command ship": ("Battlecruiser", 50),

    "battleship": ("Battleship", 60),
    "black ops": ("Battleship", 60),
    "marauder": ("Battleship", 60),

    "hauler": ("Industrial", 70),

    "industrial": ("Industrial", 70),
    "blockade runner": ("Industrial", 70),
    "deep space transport": ("Industrial", 70),
    "transport ship": ("Industrial", 70),
    "freighter": ("Capital Industrial", 95),
    "jump freighter": ("Capital Industrial", 95),
    "industrial command ship": ("Industrial", 70),

    "mining frigate": ("Mining", 80),
    "mining barge": ("Mining", 80),
    "exhumer": ("Mining", 80),

    "carrier": ("Capital", 90),
    "dreadnought": ("Capital", 90),
    "force auxiliary": ("Capital", 90),
    "lancer dreadnought": ("Capital", 90),
    "capital industrial ship": ("Capital", 90),

    "supercarrier": ("Supercapital", 100),
    "titan": ("Supercapital", 100),

    "shuttle": ("Shuttle", 120),
}


RACE_LABELS = {
    1: "Caldari",
    2: "Minmatar",
    4: "Amarr",
    8: "Gallente",
}


RACE_LOGO_URLS = {
    1: "https://images.evetech.net/corporations/500001/logo?size=32",
    2: "https://images.evetech.net/corporations/500002/logo?size=32",
    4: "https://images.evetech.net/corporations/500003/logo?size=32",
    8: "https://images.evetech.net/corporations/500004/logo?size=32",
}


FACTION_FALLBACK_LOGO_URLS = {
    500001: "https://images.evetech.net/corporations/500001/logo?size=32",
    500002: "https://images.evetech.net/corporations/500002/logo?size=32",
    500003: "https://images.evetech.net/corporations/500003/logo?size=32",
    500004: "https://images.evetech.net/corporations/500004/logo?size=32",
    500010: "https://images.evetech.net/corporations/500010/logo?size=32",
    500011: "https://images.evetech.net/corporations/500011/logo?size=32",
    500012: "https://images.evetech.net/corporations/500012/logo?size=32",
    500014: "https://images.evetech.net/corporations/500014/logo?size=32",
    500016: "https://images.evetech.net/corporations/500016/logo?size=32",
    500017: "https://images.evetech.net/corporations/500017/logo?size=32",
    500018: "https://images.evetech.net/corporations/500018/logo?size=32",
    500019: "https://images.evetech.net/corporations/500019/logo?size=32",
    500020: "https://images.evetech.net/corporations/500020/logo?size=32",
    500026: "https://images.evetech.net/corporations/500026/logo?size=32",
    500027: "https://images.evetech.net/corporations/500027/logo?size=32",
}


def _ship_group_key(group_name):
    return (group_name or "").strip().lower()


def _classify_ship_size_from_group(group_name):
    return SHIP_SIZE_BY_GROUP_NAME.get(_ship_group_key(group_name), ("Special", 900))


def _ship_tier_from_meta_group(meta_group_name):
    normalized = (meta_group_name or "").strip().lower()

    if normalized in {"tech ii", "t2"}:
        return "T2", 30
    if normalized in {"tech iii", "t3"}:
        return "T3", 40
    if normalized == "faction":
        return "Navy / Faction", 20
    if normalized == "storyline":
        return "Storyline", 25
    if normalized == "officer":
        return "Officer", 60
    if normalized == "deadspace":
        return "Deadspace", 55

    return "T1", 10


def _lookup_sde_names(conn, table_name, ids):
    normalized_ids = sorted({int(value) for value in ids if value is not None})
    if not normalized_ids:
        return {}

    if not _table_exists(conn, "public", table_name):
        return {}

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                sde_key::bigint AS entity_id,
                COALESCE(
                    data->'name'->>'en',
                    data->>'name',
                    sde_key
                ) AS entity_name,
                NULLIF(data->>'iconID', '')::bigint AS icon_id,
                NULLIF(data->>'corporationID', '')::bigint AS corporation_id
            FROM public.{_quote_ident(table_name)}
            WHERE sde_key::bigint = ANY(%s)
            """,
            (normalized_ids,),
        )
        return {
            int(row[0]): {
                "name": row[1],
                "icon_id": int(row[2]) if row[2] is not None else None,
                "corporation_id": int(row[3]) if row[3] is not None else None,
            }
            for row in cur.fetchall()
        }


def _sde_marketgroups_lookup(conn, market_group_ids):
    normalized_ids = sorted({int(value) for value in market_group_ids if value is not None})
    if not normalized_ids:
        return {}

    if not _table_exists(conn, "public", "sde_marketgroups"):
        return {}

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                sde_key::bigint AS market_group_id,
                COALESCE(
                    data->'name'->>'en',
                    data->>'name',
                    sde_key
                ) AS market_group_name,
                NULLIF(data->>'parentGroupID', '')::bigint AS parent_group_id,
                COALESCE((data->>'hasTypes')::boolean, FALSE) AS has_types
            FROM public.sde_marketgroups
            WHERE sde_key::bigint = ANY(%s)
            """,
            (normalized_ids,),
        )

        return {
            int(row[0]): {
                "name": row[1],
                "parent_group_id": int(row[2]) if row[2] is not None else None,
                "has_types": bool(row[3]),
            }
            for row in cur.fetchall()
        }


def _sde_marketgroups_tree_lookup(conn, starting_ids):
    found = {}
    pending = {int(value) for value in starting_ids if value is not None}

    while pending:
        current = _sde_marketgroups_lookup(conn, pending)
        found.update(current)

        pending = {
            payload["parent_group_id"]
            for payload in current.values()
            if payload.get("parent_group_id") is not None
            and payload["parent_group_id"] not in found
        }

    return found


def _ship_market_path(market_group_id, marketgroups):
    path = []
    current_id = int(market_group_id) if market_group_id is not None else None
    seen = set()

    while current_id is not None and current_id not in seen:
        seen.add(current_id)
        payload = marketgroups.get(current_id)
        if not payload:
            break

        path.append({
            "market_group_id": current_id,
            "name": payload.get("name"),
            "has_types": payload.get("has_types"),
        })

        current_id = payload.get("parent_group_id")

    path.reverse()
    return path


def _ship_sde_band_from_market_path(market_path):
    names = [item.get("name") for item in market_path if item.get("name")]

    for name in names:
        if name != "Ships":
            return name

    return "Other"


def _ship_sde_band_order_from_market_path(market_path):
    names = [item.get("name") for item in market_path if item.get("name")]
    order = {
        "Standard Frigates": 10,
        "Shuttles": 15,
        "Faction Frigates": 20,
        "Advanced Frigates": 30,
        "Standard Destroyers": 40,
        "Advanced Destroyers": 50,
        "Standard Cruisers": 60,
        "Faction Cruisers": 70,
        "Advanced Cruisers": 80,
        "Standard Battlecruisers": 90,
        "Faction Battlecruisers": 100,
        "Advanced Battlecruisers": 110,
        "Standard Battleships": 120,
        "Faction Battleships": 130,
        "Advanced Battleships": 140,
        "Industrial Ships": 150,
        "Mining Barges": 160,
        "Freighters": 170,
        "Capital Ships": 180,
        "Supercapital Ships": 190,
    }

    for name in names:
        if name in order:
            return order[name]

    return 900


def _ship_display_size_label_from_rig_size(rig_size, ship_size=None, group_name=None, type_name=None):
    if rig_size == 1:
        return "S"
    if rig_size == 2:
        return "M"
    if rig_size == 3:
        return "L"
    if rig_size == 4:
        return "XL"

    normalized_ship_size = (ship_size or "").strip().lower()
    normalized_group_name = (group_name or "").strip().lower()
    normalized_type_name = (type_name or "").strip().lower()

    if normalized_ship_size in {"capsule", "corvette", "shuttle"}:
        return "S"

    if normalized_group_name in {"capsule", "corvette", "shuttle"}:
        return "S"

    if normalized_type_name == "zephyr":
        return "S"

    if normalized_type_name == "primae":
        return "M"

    return "UNKNOWN SIZE"


def _ship_rig_size_from_typedogma(type_dogma):
    if not type_dogma:
        return None

    for attr in type_dogma.get("dogmaAttributes", []):
        if int(attr.get("attributeID", 0)) == 1547:
            value = attr.get("value")
            if value is None:
                return None
            return int(float(value))

    return None


def _sde_typedogma_lookup(conn, type_ids):
    ids = sorted({str(int(value)) for value in type_ids if value is not None})
    if not ids:
        return {}

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT sde_key, data
            FROM public.sde_typedogma
            WHERE sde_key = ANY(%s)
            """,
            (ids,),
        )
        rows = cur.fetchall()

    return {str(row[0]): row[1] for row in rows}


def _ship_tree_row(ship_size):
    normalized = (ship_size or "").strip().lower()
    if normalized in {"industrial", "capital industrial", "mining"}:
        return "industrial"
    return "combat"




def _faction_logo_url(faction_id, faction_payload):
    if faction_id is None:
        return None

    return f"https://images.evetech.net/corporations/{int(faction_id)}/logo?size=64"


def _race_label(race_id):
    if race_id is None:
        return "Other"
    return RACE_LABELS.get(int(race_id), "Other")


def _race_logo_url(race_id):
    if race_id is None:
        return None
    return RACE_LOGO_URLS.get(int(race_id))


def _ship_faction_order(label):
    orders = {
        "Amarr Empire": 10,
        "Caldari State": 20,
        "Gallente Federation": 30,
        "Minmatar Republic": 40,
        "Amarr": 10,
        "Caldari": 20,
        "Gallente": 30,
        "Minmatar": 40,
        "Outer Ring Excavations": 50,
        "ORE": 50,
        "Angel Cartel": 60,
        "Blood Raider Covenant": 70,
        "Guristas Pirates": 80,
        "Sansha's Nation": 90,
        "Serpentis": 100,
        "Mordu's Legion Command": 110,
        "Sisters of EVE": 120,
        "The Society of Conscious Thought": 130,
        "Triglavian Collective": 140,
        "EDENCOM": 150,
        "Other": 900,
    }
    return orders.get(label or "Other", 500)


KILLMAIL_NONSHIP_CATEGORY_NAMES = {
    "structure",
    "structures",
    "deployable",
    "deployables",
    "starbase",
    "starbases",
    "sovereignty structure",
    "sovereignty structures",
    "orbital",
    "orbitals",
    "station",
    "stations",
    "cargo container",
    "cargo containers",
}


def _sde_killmail_nonship_entities(conn):
    type_table = _sde_table_with_data(conn, ["sde_types", "sde_invtypes", "sde_invTypes"])
    group_table = _sde_table_with_data(conn, ["sde_groups", "sde_invgroups", "sde_invGroups"])
    category_table = _sde_table_with_data(conn, ["sde_categories", "sde_invcategories", "sde_invCategories"])
    if not type_table or not group_table or not category_table:
        return []

    type_id_expr = _sde_int_expr("_key", "type_id", "typeID", "typeId").replace("data", "t.data")
    type_group_expr = _sde_int_expr("group_id", "groupID", "groupId").replace("data", "t.data")
    group_id_expr = _sde_int_expr("_key", "group_id", "groupID", "groupId").replace("data", "g.data")
    group_category_expr = _sde_int_expr("category_id", "categoryID", "categoryId").replace("data", "g.data")
    category_id_expr = _sde_int_expr("_key", "category_id", "categoryID", "categoryId").replace("data", "c.data")

    category_name_expr = "COALESCE(c.data->'name'->>'en', c.data->>'categoryName', c.data->>'name', '')"
    type_name_expr = "COALESCE(t.data->'name'->>'en', t.data->>'typeName', t.data->>'name')"
    group_name_expr = "COALESCE(g.data->'name'->>'en', g.data->>'groupName', g.data->>'name')"

    query = f"""
        SELECT
            {type_id_expr} AS type_id,
            {type_name_expr} AS type_name,
            {group_name_expr} AS group_name,
            {category_name_expr} AS category_name
        FROM public.{_quote_ident(type_table)} t
        JOIN public.{_quote_ident(group_table)} g
          ON {type_group_expr} = {group_id_expr}
        JOIN public.{_quote_ident(category_table)} c
          ON {group_category_expr} = {category_id_expr}
        WHERE lower({category_name_expr}) = ANY(%s)
          AND {type_name_expr} IS NOT NULL
        ORDER BY lower({category_name_expr}), lower({group_name_expr}), lower({type_name_expr}), {type_id_expr}
    """

    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '30000ms'")
        cur.execute(query, (sorted(KILLMAIL_NONSHIP_CATEGORY_NAMES),))
        rows = cur.fetchall()

    result = []
    for type_id, name, group_name, category_name in rows:
        type_id = int(type_id)
        category_name = category_name or "Structure"
        group_name = group_name or category_name
        result.append({
            "entity_type": "ship",
            "entity_id": type_id,
            "name": name or f"Type {type_id}",
            "ticker": None,
            "group_name": group_name,
            "category_name": category_name,
            "region_name": None,
            "faction_name": category_name,
            "faction_logo_url": None,
            "faction_order": 800,
            "ship_size": category_name,
            "ship_size_order": 800,
            "ship_sde_band": group_name,
            "ship_sde_band_order": 800,
            "ship_display_size": "UNKNOWN SIZE",
            "ship_tree_row": "killmail_object",
            "ship_tier": category_name,
            "ship_tier_order": 800,
            "url": profile_url("ship", type_id),
            "image_url": type_icon_url(type_id, 64),
            "last_killmail_time": None,
            "is_pilotable": False,
            "is_structure": True,
            "selection_faction": category_name,
        })
    return result


def _sde_all_ship_entities(conn):
    if not _table_exists(conn, "sde_work", "ship_entities"):
        raise EntityError("sde_work.ship_entities_missing")

    query = """
        SELECT
            entity_id, name, group_name, category_name,
            faction_name, faction_logo_url, faction_order,
            ship_size, ship_size_order,
            ship_sde_band, ship_sde_band_order,
            ship_display_size, ship_tree_row,
            ship_tier, ship_tier_order,
            image_url, url
        FROM sde_work.ship_entities
        ORDER BY
            faction_order,
            CASE ship_display_size
                WHEN 'S' THEN 10
                WHEN 'M' THEN 20
                WHEN 'L' THEN 30
                WHEN 'XL' THEN 40
                WHEN 'UNKNOWN SIZE' THEN 90
                ELSE 900
            END,
            ship_size_order,
            ship_sde_band_order,
            ship_tier_order,
            lower(COALESCE(group_name, '')),
            lower(COALESCE(name, '')),
            entity_id
    """

    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '30000ms'")
        cur.execute(query)
        rows = cur.fetchall()

    ships = [
        {
            "entity_type": "ship",
            "entity_id": int(row[0]),
            "name": row[1] or f"Type {int(row[0])}",
            "ticker": None,
            "group_name": row[2],
            "category_name": row[3],
            "region_name": None,
            "faction_name": row[4] or "Other",
            "faction_logo_url": row[5],
            "faction_order": int(row[6]) if row[6] is not None else 500,
            "ship_size": row[7],
            "ship_size_order": int(row[8]) if row[8] is not None else 900,
            "ship_sde_band": row[9],
            "ship_sde_band_order": int(row[10]) if row[10] is not None else 900,
            "ship_display_size": row[11],
            "ship_tree_row": row[12],
            "ship_tier": row[13],
            "ship_tier_order": int(row[14]) if row[14] is not None else 900,
            "url": profile_url("ship", row[0]),
            "image_url": row[15] or image_url("ship", row[0], 32),
            "last_killmail_time": None,
            "is_pilotable": True,
            "is_structure": False,
            "selection_faction": row[4] or "Other",
        }
        for row in rows
    ]

    # Keep Supercarriers and Titans in the Supercapital category even if
    # the precomputed sde_work.ship_entities row still carries an older
    # Capital classification.
    for ship in ships:
        if _ship_group_key(ship.get("group_name")) in {"supercarrier", "titan"}:
            ship["ship_size"] = "Supercapital"
            ship["ship_size_order"] = 100
            ship["ship_sde_band"] = "Supercapital Ships"
            ship["ship_sde_band_order"] = 190

    combined = ships + _sde_killmail_nonship_entities(conn)
    deduped = {int(item["entity_id"]): item for item in combined}
    return sorted(
        deduped.values(),
        key=lambda item: (
            int(item.get("faction_order") or 500),
            int(item.get("ship_size_order") or 900),
            int(item.get("ship_sde_band_order") or 900),
            int(item.get("ship_tier_order") or 900),
            (item.get("group_name") or "").lower(),
            (item.get("name") or "").lower(),
            int(item["entity_id"]),
        ),
    )


def _sde_all_regions(conn):
    table = _sde_table_with_data(conn, ["sde_mapregions", "sde_mapRegions", "sde_regions"])
    if not table:
        return []

    id_expr = _sde_int_expr("_key", "region_id", "regionID", "regionId")
    query = f"""
        SELECT
            {id_expr} AS region_id,
            COALESCE(
                data->'name'->>'en',
                data->>'regionName',
                data->>'name',
                'Region ' || ({id_expr})::TEXT
            ) AS region_name
        FROM public.{_quote_ident(table)}
        ORDER BY lower(COALESCE(
            data->'name'->>'en',
            data->>'regionName',
            data->>'name',
            'Region ' || ({id_expr})::TEXT
        ))
    """

    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '30000ms'")
        cur.execute(query)
        rows = cur.fetchall()

    return [
        {
            "entity_type": "region",
            "entity_id": int(region_id),
            "name": region_name or f"Region {int(region_id)}",
            "ticker": None,
            "group_name": None,
            "category_name": None,
            "region_name": None,
            "url": profile_url("region", region_id),
            "image_url": image_url("region", region_id, 32),
            "last_killmail_time": None,
        }
        for region_id, region_name in rows
    ]


def _sde_all_constellations(conn):
    table = _sde_table_with_data(conn, ["sde_mapconstellations", "sde_mapConstellations", "sde_constellations"])
    if not table:
        return []

    id_expr = _sde_int_expr("_key", "constellation_id", "constellationID", "constellationId")
    region_expr = _sde_int_expr("region_id", "regionID", "regionId")
    query = f"""
        SELECT
            {id_expr} AS constellation_id,
            COALESCE(
                data->'name'->>'en',
                data->>'constellationName',
                data->>'name',
                'Constellation ' || ({id_expr})::TEXT
            ) AS constellation_name,
            {region_expr} AS region_id
        FROM public.{_quote_ident(table)}
    """

    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '30000ms'")
        cur.execute(query)
        rows = cur.fetchall()

    region_ids = {int(row[2]) for row in rows if row[2] is not None}
    regions = _lookup_regions(conn, region_ids)

    result = []
    for constellation_id, constellation_name, region_id in rows:
        region = regions.get(int(region_id)) if region_id is not None else None
        result.append({
            "entity_type": "constellation",
            "entity_id": int(constellation_id),
            "name": constellation_name or f"Constellation {int(constellation_id)}",
            "ticker": None,
            "group_name": None,
            "category_name": None,
            "region_name": region.get("name") if region else None,
            "url": profile_url("constellation", constellation_id),
            "image_url": image_url("constellation", constellation_id, 32),
            "last_killmail_time": None,
        })

    return sorted(
        result,
        key=lambda item: (
            (item.get("region_name") or "").lower(),
            (item.get("name") or "").lower(),
            item["entity_id"],
        ),
    )


def get_recent_entities_for_index(entity_type, limit=100):
    entity_type = normalize_entity_type(entity_type)
    limit = max(1, min(100, int(limit or 100)))

    with db() as conn:
        if entity_type == "ship":
            return _sde_all_ship_entities(conn)

        if entity_type == "region":
            return _sde_all_regions(conn)

        if entity_type == "constellation":
            return _sde_all_constellations(conn)

        if entity_type in {"skill", "commodity"}:
            return []

        killmail_rows = _recent_killmail_rows(conn, limit=100)

        if entity_type == "character":
            return _recent_standard_rows_from_pairs(
                conn,
                "character",
                [(row[2], row[1]) for row in killmail_rows],
                limit=limit,
            )

        if entity_type == "corporation":
            pairs = []
            for row in killmail_rows:
                killmail_time = row[1]
                for value in (row[3],):
                    pairs.append((value, killmail_time))
            return _recent_standard_rows_from_pairs(conn, "corporation", pairs, limit=limit)

        if entity_type == "alliance":
            return _recent_standard_rows_from_pairs(
                conn,
                "alliance",
                [(row[4], row[1]) for row in killmail_rows],
                limit=limit,
            )

        if entity_type == "system":
            return _recent_standard_rows_from_pairs(
                conn,
                "system",
                [(row[6], row[1]) for row in killmail_rows],
                limit=limit,
            )

        if entity_type == "weapon":
            weapon_rows = _recent_attacker_weapon_rows(conn, killmail_rows)
            return _recent_type_rows_from_pairs(
                conn,
                "weapon",
                [(row[2], row[1]) for row in weapon_rows],
                limit=limit,
            )

    return []

def get_entity_profile(entity_type, entity_id):
    timing_started = perf_counter()
    timing_marks = {}
    entity_type = normalize_entity_type(entity_type)

    try:
        normalized_id = int(entity_id)
    except (TypeError, ValueError) as exc:
        raise EntityError("entity_profile_id_invalid") from exc

    current_corporation = None
    current_alliance = None
    current_constellation = None
    current_region = None
    image = image_url(entity_type, normalized_id, 128)

    with db() as conn:
        if entity_type in ITEM_ENTITY_TYPES:
            lookup_started = perf_counter()
            payload = _type_entity_profile(conn, entity_type, normalized_id)
            timing_marks["entity_lookup_ms"] = round((perf_counter() - lookup_started) * 1000, 1)
            name = payload.get("name") or f"Unknown {entity_type.title()}"
            subtitle = payload.get("subtitle") or f"{entity_type.title()} profile"
            ticker = payload.get("ticker")
            image = payload.get("image_url")
        elif entity_type in LOCATION_ENTITY_TYPES:
            lookup_started = perf_counter()
            payload = _location_profile_lookup(conn, entity_type, normalized_id)
            timing_marks["entity_lookup_ms"] = round((perf_counter() - lookup_started) * 1000, 1)
            name = payload.get("name") or f"Unknown {entity_type.title()}"
            subtitle = payload.get("subtitle") or f"{entity_type.title()} profile"
            ticker = payload.get("ticker")
            current_constellation = payload.get("current_constellation")
            current_region = payload.get("current_region")
            image = payload.get("image_url")
        else:
            lookup_started = perf_counter()
            name, subtitle, ticker = _entity_table_lookup(conn, entity_type, normalized_id)
            timing_marks["entity_lookup_ms"] = round((perf_counter() - lookup_started) * 1000, 1)

            if entity_type == "character":
                affiliation_started = perf_counter()
                current_corporation, current_alliance = _current_character_profile(conn, normalized_id)
                timing_marks["current_affiliation_ms"] = round((perf_counter() - affiliation_started) * 1000, 1)
            elif entity_type == "corporation":
                affiliation_started = perf_counter()
                current_alliance = _current_corporation_profile(conn, normalized_id)
                timing_marks["current_affiliation_ms"] = round((perf_counter() - affiliation_started) * 1000, 1)

    if not name:
        name = f"Unknown {entity_type.title()}"

    timing_marks["total_ms"] = round((perf_counter() - timing_started) * 1000, 1)
    _timing_print(
        f"ENTITY_PROFILE_TIMING get_entity_profile entity_type={entity_type} "
        f"entity_id={normalized_id} details={timing_marks}"
    )

    return {
        "entity_type": entity_type,
        "entity_id": normalized_id,
        "name": name,
        "ticker": ticker,
        "subtitle": subtitle,
        "image_url": image,
        "current_corporation": current_corporation,
        "current_alliance": current_alliance,
        "current_constellation": current_constellation,
        "current_region": current_region,
    }


def _mask_bit_is_set(mask_bytes, bit_index):
    if mask_bytes is None:
        return False
    bit_index = int(bit_index)
    byte_index = bit_index // 8
    bit_offset = bit_index % 8
    if byte_index < 0 or byte_index >= len(mask_bytes):
        return False
    return bool(mask_bytes[byte_index] & (1 << bit_offset))


def get_character_pilotable_ships(character_id):
    try:
        normalized_id = int(character_id)
    except (TypeError, ValueError) as exc:
        raise EntityError("entity_profile_id_invalid") from exc

    with db() as conn:
        if not _table_exists(conn, "entities", "character_pilotable_ships"):
            return {
                "character_id": normalized_id,
                "ships": [],
                "ready": False,
                "error": "character_pilotable_ships_missing",
            }
        if not _table_exists(conn, "entities", "pilotable_ship_index"):
            return {
                "character_id": normalized_id,
                "ships": [],
                "ready": False,
                "error": "pilotable_ship_index_missing",
            }
        if not _table_exists(conn, "sde_work", "ship_entities"):
            return {
                "character_id": normalized_id,
                "ships": [],
                "ready": False,
                "error": "sde_work.ship_entities_missing",
            }

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ship_mask
                FROM entities.character_pilotable_ships
                WHERE character_id = %s
                LIMIT 1
                """,
                (normalized_id,),
            )
            row = cur.fetchone()

        if not row:
            return {
                "character_id": normalized_id,
                "ships": [],
                "ready": True,
                "error": None,
            }

        mask_bytes = bytes(row[0] or b"")

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ship_type_id, bit_index
                FROM entities.pilotable_ship_index
                WHERE enabled IS TRUE
                ORDER BY bit_index ASC
                """
            )
            index_rows = cur.fetchall()

        ship_ids = [
            int(ship_type_id)
            for ship_type_id, bit_index in index_rows
            if _mask_bit_is_set(mask_bytes, bit_index)
        ]

        if not ship_ids:
            return {
                "character_id": normalized_id,
                "ships": [],
                "ready": True,
                "error": None,
            }

        query = """
            SELECT
                entity_id, name, group_name, category_name,
                faction_name, faction_logo_url, faction_order,
                ship_size, ship_size_order,
                ship_sde_band, ship_sde_band_order,
                ship_display_size, ship_tree_row,
                ship_tier, ship_tier_order,
                image_url, url
            FROM sde_work.ship_entities
            WHERE entity_id::BIGINT = ANY(%s)
            ORDER BY
                faction_order,
                CASE ship_display_size
                    WHEN 'S' THEN 10
                    WHEN 'M' THEN 20
                    WHEN 'L' THEN 30
                    WHEN 'XL' THEN 40
                    WHEN 'UNKNOWN SIZE' THEN 90
                    ELSE 900
                END,
                ship_size_order,
                ship_sde_band_order,
                ship_tier_order,
                lower(COALESCE(group_name, '')),
                lower(COALESCE(name, '')),
                entity_id
        """

        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '8000ms'")
            cur.execute(query, (ship_ids,))
            rows = cur.fetchall()

    ships = [
        {
            "entity_type": "ship",
            "entity_id": int(row[0]),
            "name": row[1] or f"Type {int(row[0])}",
            "ticker": None,
            "group_name": row[2],
            "category_name": row[3],
            "region_name": None,
            "faction_name": row[4] or "Other",
            "faction_logo_url": row[5],
            "faction_order": int(row[6]) if row[6] is not None else 500,
            "ship_size": row[7],
            "ship_size_order": int(row[8]) if row[8] is not None else 900,
            "ship_sde_band": row[9],
            "ship_sde_band_order": int(row[10]) if row[10] is not None else 900,
            "ship_display_size": row[11],
            "ship_tree_row": row[12],
            "ship_tier": row[13],
            "ship_tier_order": int(row[14]) if row[14] is not None else 900,
            "url": profile_url("ship", row[0]),
            "image_url": row[15] or image_url("ship", row[0], 32),
            "last_killmail_time": None,
        }
        for row in rows
    ]

    return {
        "character_id": normalized_id,
        "ships": ships,
        "ready": True,
        "error": None,
    }


def _ship_payloads_from_ids(conn, ship_ids, pilot_counts=None):
    normalized_ids = sorted({int(value) for value in ship_ids if value is not None})
    if not normalized_ids:
        return []

    pilot_counts = pilot_counts or {}
    query = """
        SELECT
            entity_id, name, group_name, category_name,
            faction_name, faction_logo_url, faction_order,
            ship_size, ship_size_order,
            ship_sde_band, ship_sde_band_order,
            ship_display_size, ship_tree_row,
            ship_tier, ship_tier_order,
            image_url, url
        FROM sde_work.ship_entities
        WHERE entity_id::BIGINT = ANY(%s)
        ORDER BY
            faction_order,
            CASE ship_display_size
                WHEN 'S' THEN 10
                WHEN 'M' THEN 20
                WHEN 'L' THEN 30
                WHEN 'XL' THEN 40
                WHEN 'UNKNOWN SIZE' THEN 90
                ELSE 900
            END,
            ship_size_order,
            ship_sde_band_order,
            ship_tier_order,
            lower(COALESCE(group_name, '')),
            lower(COALESCE(name, '')),
            entity_id
    """

    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '8000ms'")
        cur.execute(query, (normalized_ids,))
        rows = cur.fetchall()

    ships = []
    for row in rows:
        ship_type_id = int(row[0])
        payload = {
            "entity_type": "ship",
            "entity_id": ship_type_id,
            "name": row[1] or f"Type {ship_type_id}",
            "ticker": None,
            "group_name": row[2],
            "category_name": row[3],
            "region_name": None,
            "faction_name": row[4] or "Other",
            "faction_logo_url": row[5],
            "faction_order": int(row[6]) if row[6] is not None else 500,
            "ship_size": row[7],
            "ship_size_order": int(row[8]) if row[8] is not None else 900,
            "ship_sde_band": row[9],
            "ship_sde_band_order": int(row[10]) if row[10] is not None else 900,
            "ship_display_size": row[11],
            "ship_tree_row": row[12],
            "ship_tier": row[13],
            "ship_tier_order": int(row[14]) if row[14] is not None else 900,
            "url": profile_url("ship", row[0]),
            "image_url": row[15] or image_url("ship", row[0], 32),
            "last_killmail_time": None,
        }
        if ship_type_id in pilot_counts:
            payload["pilot_count"] = int(pilot_counts.get(ship_type_id) or 0)
        ships.append(payload)

    return ships



def _entity_pilotable_members_sql(entity_type):
    entity_type = normalize_entity_type(entity_type)
    if entity_type == "corporation":
        return """
            SELECT DISTINCT cch.character_id::BIGINT AS character_id
            FROM entities.character_corporation_history cch
            WHERE cch.corporation_id = %s
              AND cch.end_date IS NULL
              AND COALESCE(cch.is_deleted, FALSE) IS FALSE
        """
    if entity_type == "alliance":
        return """
            SELECT DISTINCT cch.character_id::BIGINT AS character_id
            FROM entities.character_corporation_history cch
            JOIN entities.corporation_alliance_history cah
              ON cah.corporation_id = cch.corporation_id
             AND cah.end_date IS NULL
             AND COALESCE(cah.is_deleted, FALSE) IS FALSE
            WHERE cah.alliance_id = %s
              AND cch.end_date IS NULL
              AND COALESCE(cch.is_deleted, FALSE) IS FALSE
        """
    raise EntityError("entity_type_invalid")


def _pilotable_ship_index_rows(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ship_type_id, bit_index
            FROM entities.pilotable_ship_index
            WHERE enabled IS TRUE
            ORDER BY bit_index ASC
            """
        )
        return [(int(row[0]), int(row[1])) for row in cur.fetchall()]


def _ship_counts_to_payload(conn, ship_counts):
    normalized_counts = {}
    for key, value in (ship_counts or {}).items():
        try:
            ship_type_id = int(key)
            count = int(value or 0)
        except (TypeError, ValueError):
            continue
        if count > 0:
            normalized_counts[ship_type_id] = count

    return _ship_payloads_from_ids(conn, normalized_counts.keys(), pilot_counts=normalized_counts)


def _pilotable_ship_metadata_by_id(conn, ship_ids):
    normalized_ids = sorted({int(value) for value in ship_ids if value is not None})
    if not normalized_ids:
        return {}

    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '8000ms'")
        cur.execute(
            """
            SELECT
                entity_id::BIGINT,
                COALESCE(NULLIF(faction_name, ''), 'Other') AS faction_name,
                ship_tree_row,
                ship_display_size,
                ship_size,
                ship_sde_band,
                ship_tier,
                group_name
            FROM sde_work.ship_entities
            WHERE entity_id::BIGINT = ANY(%s)
            """,
            (normalized_ids,),
        )
        rows = cur.fetchall()

    return {
        int(row[0]): {
            "faction": row[1] or "Other",
            "ship_tree_row": row[2],
            "ship_display_size": row[3],
            "ship_size": row[4],
            "ship_sde_band": row[5],
            "ship_tier": row[6],
            "group_name": row[7],
        }
        for row in rows
    }


def _category_count_key(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _add_category_seen(seen, scope, value):
    key = _category_count_key(value)
    if key is not None:
        seen.add((scope, key))


def _empty_pilotable_category_counts():
    return {
        "faction": {},
        "ship_tree_row": {},
        "ship_display_size": {},
        "ship_size": {},
        "ship_sde_band": {},
        "ship_tier": {},
        "group_name": {},
        "filter_group": {},
    }


def _same_pilotable_text(left, right):
    return str(left or "").strip().lower() == str(right or "").strip().lower()


def _pilotable_metadata_matches_filter(metadata, tier=None, group=None, size=None, faction=None):
    if tier:
        return _same_pilotable_text(metadata.get("ship_tier"), tier)
    if group:
        return (
            _same_pilotable_text(metadata.get("ship_size"), group)
            or _same_pilotable_text(metadata.get("group_name"), group)
            or _same_pilotable_text(metadata.get("ship_sde_band"), group)
        )
    if size:
        return _same_pilotable_text(metadata.get("ship_display_size"), size)
    if faction:
        return _same_pilotable_text(metadata.get("faction"), faction)
    return True


def _count_group_pilotable_ships_live(conn, entity_type, entity_id):
    entity_type = normalize_entity_type(entity_type)
    if entity_type not in {"corporation", "alliance"}:
        raise EntityError("entity_type_invalid")

    members_sql = _entity_pilotable_members_sql(entity_type)
    index_rows = _pilotable_ship_index_rows(conn)

    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '120000ms'")
        cur.execute(
            f"""
            WITH members AS (
                {members_sql}
            )
            SELECT m.character_id, cps.ship_mask
            FROM members m
            LEFT JOIN entities.character_pilotable_ships cps
              ON cps.character_id = m.character_id
            ORDER BY m.character_id ASC
            """,
            (entity_id,),
        )
        rows = cur.fetchall()

    ship_counts = {}

    for _character_id, mask_value in rows:
        if mask_value is None:
            continue

        mask_bytes = bytes(mask_value or b"")

        for ship_type_id, bit_index in index_rows:
            if not _mask_bit_is_set(mask_bytes, bit_index):
                continue

            ship_counts[ship_type_id] = int(ship_counts.get(ship_type_id, 0) or 0) + 1

    return ship_counts, len(rows)


def get_group_pilotable_category_counts(entity_type, entity_id, visible_ship_ids):
    entity_type = normalize_entity_type(entity_type)
    if entity_type not in {"corporation", "alliance"}:
        raise EntityError("entity_type_invalid")

    try:
        normalized_id = int(entity_id)
    except (TypeError, ValueError) as exc:
        raise EntityError("entity_profile_id_invalid") from exc

    normalized_ship_ids = sorted({int(value) for value in (visible_ship_ids or []) if value is not None})
    if not normalized_ship_ids:
        counts = _empty_pilotable_category_counts()
        counts["_visible_total"] = 0
        return counts

    with db() as conn:
        members_sql = _entity_pilotable_members_sql(entity_type)
        metadata_by_ship_id = _pilotable_ship_metadata_by_id(conn, normalized_ship_ids)

        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '120000ms'")
            cur.execute(
                """
                SELECT ship_type_id, bit_index
                FROM entities.pilotable_ship_index
                WHERE enabled IS TRUE
                  AND ship_type_id = ANY(%s)
                ORDER BY bit_index ASC
                """,
                (normalized_ship_ids,),
            )
            index_rows = [(int(row[0]), int(row[1])) for row in cur.fetchall()]

            cur.execute(
                f"""
                WITH members AS (
                    {members_sql}
                )
                SELECT m.character_id, cps.ship_mask
                FROM members m
                JOIN entities.character_pilotable_ships cps
                  ON cps.character_id = m.character_id
                ORDER BY m.character_id ASC
                """,
                (normalized_id,),
            )
            rows = cur.fetchall()

    category_counts = _empty_pilotable_category_counts()
    visible_total = 0

    for _character_id, mask_value in rows:
        if mask_value is None:
            continue

        mask_bytes = bytes(mask_value or b"")
        seen_categories = set()
        has_visible_ship = False

        for ship_type_id, bit_index in index_rows:
            if not _mask_bit_is_set(mask_bytes, bit_index):
                continue

            metadata = metadata_by_ship_id.get(ship_type_id, {})
            has_visible_ship = True

            _add_category_seen(seen_categories, "faction", metadata.get("faction"))
            _add_category_seen(seen_categories, "ship_tree_row", metadata.get("ship_tree_row"))
            _add_category_seen(seen_categories, "ship_display_size", metadata.get("ship_display_size"))
            _add_category_seen(seen_categories, "ship_size", metadata.get("ship_size"))
            _add_category_seen(seen_categories, "ship_sde_band", metadata.get("ship_sde_band"))
            _add_category_seen(seen_categories, "ship_tier", metadata.get("ship_tier"))
            _add_category_seen(seen_categories, "group_name", metadata.get("group_name"))

            _add_category_seen(seen_categories, "filter_group", metadata.get("ship_size"))
            _add_category_seen(seen_categories, "filter_group", metadata.get("ship_sde_band"))
            _add_category_seen(seen_categories, "filter_group", metadata.get("group_name"))

        if has_visible_ship:
            visible_total += 1

        for scope, value in seen_categories:
            category_counts[scope][value] = int(category_counts[scope].get(value, 0) or 0) + 1

    category_counts["_visible_total"] = visible_total
    return category_counts


def get_group_pilotable_ships(entity_type, entity_id, batch_size=2500):
    entity_type = normalize_entity_type(entity_type)
    if entity_type not in {"corporation", "alliance"}:
        raise EntityError("entity_type_invalid")

    try:
        normalized_id = int(entity_id)
    except (TypeError, ValueError) as exc:
        raise EntityError("entity_profile_id_invalid") from exc

    with db() as conn:
        if not _table_exists(conn, "entities", "character_pilotable_ships"):
            return {
                "entity_type": entity_type,
                "entity_id": normalized_id,
                "ships": [],
                "ready": False,
                "error": "character_pilotable_ships_missing",
            }
        if not _table_exists(conn, "entities", "pilotable_ship_index"):
            return {
                "entity_type": entity_type,
                "entity_id": normalized_id,
                "ships": [],
                "ready": False,
                "error": "pilotable_ship_index_missing",
            }
        if not _table_exists(conn, "sde_work", "ship_entities"):
            return {
                "entity_type": entity_type,
                "entity_id": normalized_id,
                "ships": [],
                "ready": False,
                "error": "sde_work.ship_entities_missing",
            }

        ship_counts, total_pilots = _count_group_pilotable_ships_live(conn, entity_type, normalized_id)
        ships = _ship_counts_to_payload(conn, ship_counts)

    return {
        "entity_type": entity_type,
        "entity_id": normalized_id,
        "ships": ships,
        "ready": True,
        "error": None,
        "status": "done",
        "total_pilots": total_pilots,
        "processed_pilots": total_pilots,
        "progress_percent": 100.0,
        "updated_at": None,
        "finished_at": None,
        "category_pilot_counts": {},
    }


# ---------------------------------------------------------------------------
# Coalition pilotable ships
# ---------------------------------------------------------------------------

_COALITION_PILOTABLE_MEMBER_CACHE = OrderedDict()
_COALITION_PILOTABLE_MEMBER_CACHE_LOCK = Lock()
_COALITION_PILOTABLE_MEMBER_CACHE_TTL_SECONDS = 300
_COALITION_PILOTABLE_MEMBER_CACHE_MAX = 8


def _coalition_pilotable_cache_get(key):
    now = monotonic()
    with _COALITION_PILOTABLE_MEMBER_CACHE_LOCK:
        cached = _COALITION_PILOTABLE_MEMBER_CACHE.get(key)
        if cached is None:
            return None
        stored_at, value = cached
        if now - stored_at > _COALITION_PILOTABLE_MEMBER_CACHE_TTL_SECONDS:
            _COALITION_PILOTABLE_MEMBER_CACHE.pop(key, None)
            return None
        _COALITION_PILOTABLE_MEMBER_CACHE.move_to_end(key)
        return value


def _coalition_pilotable_cache_put(key, value):
    with _COALITION_PILOTABLE_MEMBER_CACHE_LOCK:
        _COALITION_PILOTABLE_MEMBER_CACHE[key] = (monotonic(), value)
        _COALITION_PILOTABLE_MEMBER_CACHE.move_to_end(key)
        while len(_COALITION_PILOTABLE_MEMBER_CACHE) > _COALITION_PILOTABLE_MEMBER_CACHE_MAX:
            _COALITION_PILOTABLE_MEMBER_CACHE.popitem(last=False)


def _normalized_id_list(values):
    normalized = set()
    for value in values or []:
        try:
            normalized.add(int(value))
        except (TypeError, ValueError):
            continue
    return sorted(normalized)


def _coalition_pilotable_corporation_ids(conn, alliance_ids, corporation_ids):
    alliance_ids = _normalized_id_list(alliance_ids)
    resolved = set(_normalized_id_list(corporation_ids))

    if alliance_ids:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '8000ms'")
            cur.execute(
                """
                SELECT DISTINCT corporation_id::BIGINT
                FROM entities.corporation_alliance_history
                WHERE alliance_id = ANY(%s)
                  AND end_date IS NULL
                  AND COALESCE(is_deleted, FALSE) IS FALSE
                """,
                (alliance_ids,),
            )
            resolved.update(int(row[0]) for row in cur.fetchall() if row[0] is not None)

    return sorted(resolved)


def _query_coalition_pilotable_mask_chunk(conn, corporation_ids, retry_single=False):
    corporation_ids = _normalized_id_list(corporation_ids)
    if not corporation_ids:
        return []

    timeout_ms = 30000 if retry_single else 12000
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = '{timeout_ms}ms'")
            cur.execute(
                """
                SELECT DISTINCT ON (cch.character_id)
                    cch.character_id::BIGINT,
                    cps.ship_mask
                FROM entities.character_corporation_history cch
                LEFT JOIN entities.character_pilotable_ships cps
                  ON cps.character_id = cch.character_id
                WHERE cch.corporation_id = ANY(%s)
                  AND cch.end_date IS NULL
                  AND COALESCE(cch.is_deleted, FALSE) IS FALSE
                ORDER BY cch.character_id, cch.start_date DESC
                """,
                (corporation_ids,),
            )
            return cur.fetchall()
    except QueryCanceled:
        conn.rollback()
        if len(corporation_ids) > 1:
            midpoint = max(1, len(corporation_ids) // 2)
            return (
                _query_coalition_pilotable_mask_chunk(conn, corporation_ids[:midpoint])
                + _query_coalition_pilotable_mask_chunk(conn, corporation_ids[midpoint:])
            )
        if not retry_single:
            return _query_coalition_pilotable_mask_chunk(conn, corporation_ids, retry_single=True)
        raise


def _coalition_pilotable_member_masks(conn, alliance_ids, corporation_ids):
    member_corporation_ids = _coalition_pilotable_corporation_ids(
        conn, alliance_ids, corporation_ids
    )
    cache_key = ("coalition_pilotable_masks", tuple(member_corporation_ids))
    cached = _coalition_pilotable_cache_get(cache_key)
    if cached is not None:
        return member_corporation_ids, cached

    rows_by_character = {}
    chunk_size = 150
    for offset in range(0, len(member_corporation_ids), chunk_size):
        chunk = member_corporation_ids[offset:offset + chunk_size]
        for character_id, mask_value in _query_coalition_pilotable_mask_chunk(conn, chunk):
            cid = int(character_id)
            rows_by_character[cid] = (
                bytes(mask_value) if mask_value is not None else None
            )

    rows = sorted(rows_by_character.items())
    _coalition_pilotable_cache_put(cache_key, rows)
    return member_corporation_ids, rows


def _iter_mask_set_bits(mask_bytes):
    for byte_index, byte_value in enumerate(mask_bytes or b""):
        value = int(byte_value)
        while value:
            least_bit = value & -value
            yield (byte_index * 8) + (least_bit.bit_length() - 1)
            value &= value - 1


def get_coalition_pilotable_ships(coalition_id, alliance_ids, corporation_ids):
    try:
        normalized_coalition_id = int(coalition_id)
    except (TypeError, ValueError) as exc:
        raise EntityError("entity_profile_id_invalid") from exc

    with db() as conn:
        if not _table_exists(conn, "entities", "character_pilotable_ships"):
            return {
                "entity_type": "coalition",
                "entity_id": normalized_coalition_id,
                "ships": [],
                "ready": False,
                "error": "character_pilotable_ships_missing",
            }
        if not _table_exists(conn, "entities", "pilotable_ship_index"):
            return {
                "entity_type": "coalition",
                "entity_id": normalized_coalition_id,
                "ships": [],
                "ready": False,
                "error": "pilotable_ship_index_missing",
            }
        if not _table_exists(conn, "sde_work", "ship_entities"):
            return {
                "entity_type": "coalition",
                "entity_id": normalized_coalition_id,
                "ships": [],
                "ready": False,
                "error": "sde_work.ship_entities_missing",
            }

        try:
            member_corporation_ids, rows = _coalition_pilotable_member_masks(
                conn, alliance_ids, corporation_ids
            )
        except QueryCanceled as exc:
            conn.rollback()
            raise EntityError("pilotable_ships_timeout") from exc
        index_rows = _pilotable_ship_index_rows(conn)
        ship_by_bit = {int(bit_index): int(ship_type_id) for ship_type_id, bit_index in index_rows}
        ship_counts = {}

        for _character_id, mask_bytes in rows:
            if not mask_bytes:
                continue
            for bit_index in _iter_mask_set_bits(mask_bytes):
                ship_type_id = ship_by_bit.get(bit_index)
                if ship_type_id is None:
                    continue
                ship_counts[ship_type_id] = int(ship_counts.get(ship_type_id, 0) or 0) + 1

        ships = _ship_counts_to_payload(conn, ship_counts)

    return {
        "entity_type": "coalition",
        "entity_id": normalized_coalition_id,
        "ships": ships,
        "ready": True,
        "error": None,
        "status": "done",
        "total_pilots": len(rows),
        "processed_pilots": len(rows),
        "progress_percent": 100.0,
        "updated_at": None,
        "finished_at": None,
        "category_pilot_counts": {},
        "scope_alliance_ids": _normalized_id_list(alliance_ids),
        "scope_corporation_ids": _normalized_id_list(corporation_ids),
        "member_corporation_ids": member_corporation_ids,
    }


def get_coalition_pilotable_category_counts(coalition_id, alliance_ids, corporation_ids, visible_ship_ids):
    try:
        int(coalition_id)
    except (TypeError, ValueError) as exc:
        raise EntityError("entity_profile_id_invalid") from exc

    normalized_ship_ids = _normalized_id_list(visible_ship_ids)
    if not normalized_ship_ids:
        counts = _empty_pilotable_category_counts()
        counts["_visible_total"] = 0
        return counts

    with db() as conn:
        try:
            _member_corporation_ids, rows = _coalition_pilotable_member_masks(
                conn, alliance_ids, corporation_ids
            )
        except QueryCanceled as exc:
            conn.rollback()
            raise EntityError("pilotable_ships_timeout") from exc
        metadata_by_ship_id = _pilotable_ship_metadata_by_id(conn, normalized_ship_ids)

        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '8000ms'")
            cur.execute(
                """
                SELECT ship_type_id, bit_index
                FROM entities.pilotable_ship_index
                WHERE enabled IS TRUE
                  AND ship_type_id = ANY(%s)
                ORDER BY bit_index ASC
                """,
                (normalized_ship_ids,),
            )
            index_rows = [(int(row[0]), int(row[1])) for row in cur.fetchall()]

    category_counts = _empty_pilotable_category_counts()
    visible_total = 0

    for _character_id, mask_bytes in rows:
        if not mask_bytes:
            continue
        seen_categories = set()
        has_visible_ship = False
        for ship_type_id, bit_index in index_rows:
            if not _mask_bit_is_set(mask_bytes, bit_index):
                continue
            metadata = metadata_by_ship_id.get(ship_type_id, {})
            has_visible_ship = True
            _add_category_seen(seen_categories, "faction", metadata.get("faction"))
            _add_category_seen(seen_categories, "ship_tree_row", metadata.get("ship_tree_row"))
            _add_category_seen(seen_categories, "ship_display_size", metadata.get("ship_display_size"))
            _add_category_seen(seen_categories, "ship_size", metadata.get("ship_size"))
            _add_category_seen(seen_categories, "ship_sde_band", metadata.get("ship_sde_band"))
            _add_category_seen(seen_categories, "ship_tier", metadata.get("ship_tier"))
            _add_category_seen(seen_categories, "group_name", metadata.get("group_name"))
            _add_category_seen(seen_categories, "filter_group", metadata.get("ship_size"))
            _add_category_seen(seen_categories, "filter_group", metadata.get("ship_sde_band"))
            _add_category_seen(seen_categories, "filter_group", metadata.get("group_name"))

        if has_visible_ship:
            visible_total += 1
        for scope, value in seen_categories:
            category_counts[scope][value] = int(category_counts[scope].get(value, 0) or 0) + 1

    category_counts["_visible_total"] = visible_total
    return category_counts


def _coalition_profile_for_pilotable(conn, coalition_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT name, short_name
            FROM entities.coalitions
            WHERE coalition_id = %s
            """,
            (int(coalition_id),),
        )
        row = cur.fetchone()
    if not row:
        raise EntityError("coalition_not_found")
    return {
        "entity_type": "coalition",
        "entity_id": int(coalition_id),
        "name": row[0] or f"Coalition {int(coalition_id)}",
        "subtitle": "Coalition",
        "ticker": row[1],
        "image_url": None,
    }


def get_coalition_pilotable_ship_pilots(
    coalition_id,
    alliance_ids,
    corporation_ids,
    ship_type_id,
    page=1,
    per_page=100,
    q="",
):
    try:
        normalized_coalition_id = int(coalition_id)
        normalized_ship_type_id = int(ship_type_id)
    except (TypeError, ValueError) as exc:
        raise EntityError("entity_profile_id_invalid") from exc

    page = _normalize_positive_int(page, 1, minimum=1)
    per_page = _normalize_positive_int(per_page, 100, minimum=1, maximum=250)
    query = str(q or "").strip()

    with db() as conn:
        profile = _coalition_profile_for_pilotable(conn, normalized_coalition_id)

        def empty_result(error=None, ship=None, ready=True):
            return {
                "entity_type": "coalition",
                "entity_id": normalized_coalition_id,
                "ship_type_id": normalized_ship_type_id,
                "profile": profile,
                "ship": ship,
                "pilots": [],
                "total_members": 0,
                "matching_pilots": 0,
                "query": query,
                "pagination": {
                    "page": 1,
                    "per_page": per_page,
                    "total_rows": 0,
                    "total_pages": 1,
                    "has_prev": False,
                    "has_next": False,
                    "prev_page": None,
                    "next_page": None,
                },
                "ready": ready,
                "error": error,
            }

        if not _table_exists(conn, "entities", "character_pilotable_ships"):
            return empty_result(error="character_pilotable_ships_missing", ready=False)
        if not _table_exists(conn, "entities", "pilotable_ship_index"):
            return empty_result(error="pilotable_ship_index_missing", ready=False)
        if not _table_exists(conn, "sde_work", "ship_entities"):
            return empty_result(error="sde_work.ship_entities_missing", ready=False)

        ship_rows = _ship_payloads_from_ids(conn, [normalized_ship_type_id])
        ship = ship_rows[0] if ship_rows else None
        if not ship:
            return empty_result(error="ship_not_found", ship=None, ready=True)

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT bit_index
                FROM entities.pilotable_ship_index
                WHERE ship_type_id = %s
                  AND enabled IS TRUE
                LIMIT 1
                """,
                (normalized_ship_type_id,),
            )
            bit_row = cur.fetchone()
        if not bit_row:
            return empty_result(error=None, ship=ship, ready=True)

        try:
            member_corporation_ids, member_mask_rows = _coalition_pilotable_member_masks(
                conn, alliance_ids, corporation_ids
            )
        except QueryCanceled as exc:
            conn.rollback()
            raise EntityError("pilotable_ship_pilots_timeout") from exc
        if not member_corporation_ids:
            return empty_result(error=None, ship=ship, ready=True)
        total_members = len(member_mask_rows)

        bit_index = int(bit_row[0])
        byte_index = bit_index // 8
        bit_mask = 1 << (bit_index % 8)

        members_sql = """
            SELECT DISTINCT ON (cch.character_id)
                cch.character_id::BIGINT AS character_id,
                cch.corporation_id::BIGINT AS corporation_id,
                corp.name AS corporation_name,
                corp.ticker AS corporation_ticker,
                cah.alliance_id::BIGINT AS alliance_id,
                alli.name AS alliance_name,
                alli.ticker AS alliance_ticker
            FROM entities.character_corporation_history cch
            LEFT JOIN entities.corporations corp
              ON corp.corporation_id = cch.corporation_id
            LEFT JOIN entities.corporation_alliance_history cah
              ON cah.corporation_id = cch.corporation_id
             AND cah.end_date IS NULL
             AND COALESCE(cah.is_deleted, FALSE) IS FALSE
            LEFT JOIN entities.alliances alli
              ON alli.alliance_id = cah.alliance_id
            WHERE cch.corporation_id = ANY(%s)
              AND cch.end_date IS NULL
              AND COALESCE(cch.is_deleted, FALSE) IS FALSE
            ORDER BY cch.character_id, cch.start_date DESC, cah.start_date DESC NULLS LAST
        """

        search_sql = ""
        search_params = []
        if query:
            search_like = f"%{query.lower()}%"
            search_sql = """
                AND (
                    lower(COALESCE(c.name, '')) LIKE %s
                    OR lower(COALESCE(m.corporation_name, '')) LIKE %s
                    OR lower(COALESCE(m.corporation_ticker, '')) LIKE %s
                    OR lower(COALESCE(m.alliance_name, '')) LIKE %s
                    OR lower(COALESCE(m.alliance_ticker, '')) LIKE %s
                )
            """
            search_params = [search_like] * 5

        base_sql = f"""
            WITH members AS (
                {members_sql}
            ), eligible AS (
                SELECT
                    m.character_id,
                    c.name AS character_name,
                    cps.updated_at,
                    m.corporation_id,
                    m.corporation_name,
                    m.corporation_ticker,
                    m.alliance_id,
                    m.alliance_name,
                    m.alliance_ticker
                FROM members m
                JOIN entities.character_pilotable_ships cps
                  ON cps.character_id = m.character_id
                LEFT JOIN entities.characters c
                  ON c.character_id = m.character_id
                WHERE octet_length(cps.ship_mask) > %s
                  AND (get_byte(cps.ship_mask, %s)::INT & %s) <> 0
                  {search_sql}
            )
        """

        try:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '30000ms'")
                base_params = [member_corporation_ids, byte_index, byte_index, bit_mask] + search_params
                offset = (page - 1) * per_page
                cur.execute(
                    base_sql + """
                    SELECT
                        character_id,
                        character_name,
                        updated_at,
                        corporation_id,
                        corporation_name,
                        corporation_ticker,
                        alliance_id,
                        alliance_name,
                        alliance_ticker,
                        COUNT(*) OVER() AS total_rows
                    FROM eligible
                    ORDER BY lower(COALESCE(character_name, '')), character_id
                    LIMIT %s OFFSET %s
                    """,
                    tuple(base_params + [per_page, offset]),
                )
                rows = cur.fetchall()
                matching_pilots = int(rows[0][9] or 0) if rows else 0

                if not rows and page > 1:
                    cur.execute(base_sql + "SELECT COUNT(*) FROM eligible", tuple(base_params))
                    matching_pilots = int(cur.fetchone()[0] or 0)
                    total_pages = max(1, (matching_pilots + per_page - 1) // per_page)
                    page = min(page, total_pages)
                    offset = (page - 1) * per_page
                    if matching_pilots:
                        cur.execute(
                            base_sql + """
                            SELECT
                                character_id,
                                character_name,
                                updated_at,
                                corporation_id,
                                corporation_name,
                                corporation_ticker,
                                alliance_id,
                                alliance_name,
                                alliance_ticker,
                                COUNT(*) OVER() AS total_rows
                            FROM eligible
                            ORDER BY lower(COALESCE(character_name, '')), character_id
                            LIMIT %s OFFSET %s
                            """,
                            tuple(base_params + [per_page, offset]),
                        )
                        rows = cur.fetchall()

                total_pages = max(1, (matching_pilots + per_page - 1) // per_page)
        except QueryCanceled:
            conn.rollback()
            raise EntityError("pilotable_ship_pilots_timeout")

    pilots = []
    for row in rows:
        character_id = int(row[0])
        corporation = None
        alliance = None
        if row[3] is not None:
            corporation_id = int(row[3])
            corporation = {
                "entity_type": "corporation",
                "entity_id": corporation_id,
                "name": row[4] or "Unknown",
                "ticker": row[5],
                "url": profile_url("corporation", corporation_id),
                "image_url": image_url("corporation", corporation_id, 32),
            }
        if row[6] is not None:
            alliance_id = int(row[6])
            alliance = {
                "entity_type": "alliance",
                "entity_id": alliance_id,
                "name": row[7] or "Unknown",
                "ticker": row[8],
                "url": profile_url("alliance", alliance_id),
                "image_url": image_url("alliance", alliance_id, 32),
            }

        pilots.append({
            "entity_type": "character",
            "entity_id": character_id,
            "name": row[1] or f"Character {character_id}",
            "url": profile_url("character", character_id),
            "image_url": image_url("character", character_id, 32),
            "updated_at": row[2],
            "corporation": corporation,
            "alliance": alliance,
        })

    return {
        "entity_type": "coalition",
        "entity_id": normalized_coalition_id,
        "ship_type_id": normalized_ship_type_id,
        "profile": profile,
        "ship": ship,
        "pilots": pilots,
        "total_members": total_members,
        "matching_pilots": matching_pilots,
        "query": query,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total_rows": matching_pilots,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
            "prev_page": page - 1 if page > 1 else None,
            "next_page": page + 1 if page < total_pages else None,
        },
        "ready": True,
        "error": None,
    }

def _normalize_positive_int(value, default, minimum=1, maximum=None):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default

    if number < minimum:
        number = minimum
    if maximum is not None and number > maximum:
        number = maximum
    return number


def get_group_pilotable_ship_pilots(entity_type, entity_id, ship_type_id, page=1, per_page=100, q=""):
    entity_type = normalize_entity_type(entity_type)
    if entity_type not in {"corporation", "alliance"}:
        raise EntityError("entity_type_invalid")

    try:
        normalized_entity_id = int(entity_id)
        normalized_ship_type_id = int(ship_type_id)
    except (TypeError, ValueError) as exc:
        raise EntityError("entity_profile_id_invalid") from exc

    page = _normalize_positive_int(page, 1, minimum=1)
    per_page = _normalize_positive_int(per_page, 100, minimum=1, maximum=250)
    query = str(q or "").strip()

    profile = get_entity_profile(entity_type, normalized_entity_id)

    def empty_result(error=None, ship=None, ready=True):
        return {
            "entity_type": entity_type,
            "entity_id": normalized_entity_id,
            "ship_type_id": normalized_ship_type_id,
            "profile": profile,
            "ship": ship,
            "pilots": [],
            "total_members": 0,
            "matching_pilots": 0,
            "query": query,
            "pagination": {
                "page": 1,
                "per_page": per_page,
                "total_rows": 0,
                "total_pages": 1,
                "has_prev": False,
                "has_next": False,
                "prev_page": None,
                "next_page": None,
            },
            "ready": ready,
            "error": error,
        }

    with db() as conn:
        if not _table_exists(conn, "entities", "character_pilotable_ships"):
            return empty_result(error="character_pilotable_ships_missing", ready=False)
        if not _table_exists(conn, "entities", "pilotable_ship_index"):
            return empty_result(error="pilotable_ship_index_missing", ready=False)
        if not _table_exists(conn, "sde_work", "ship_entities"):
            return empty_result(error="sde_work.ship_entities_missing", ready=False)

        ship_rows = _ship_payloads_from_ids(conn, [normalized_ship_type_id])
        ship = ship_rows[0] if ship_rows else None
        if not ship:
            return empty_result(error="ship_not_found", ship=None, ready=True)

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT bit_index
                FROM entities.pilotable_ship_index
                WHERE ship_type_id = %s
                  AND enabled IS TRUE
                LIMIT 1
                """,
                (normalized_ship_type_id,),
            )
            bit_row = cur.fetchone()

        if not bit_row:
            return empty_result(error=None, ship=ship, ready=True)

        bit_index = int(bit_row[0])
        byte_index = bit_index // 8
        bit_mask = 1 << (bit_index % 8)

        if entity_type == "corporation":
            members_sql = """
                SELECT DISTINCT
                    cch.character_id::BIGINT AS character_id,
                    NULL::BIGINT AS corporation_id,
                    NULL::TEXT AS corporation_name,
                    NULL::TEXT AS corporation_ticker
                FROM entities.character_corporation_history cch
                WHERE cch.corporation_id = %s
                  AND cch.end_date IS NULL
                  AND COALESCE(cch.is_deleted, FALSE) IS FALSE
            """
        else:
            members_sql = """
                SELECT DISTINCT
                    cch.character_id::BIGINT AS character_id,
                    cch.corporation_id::BIGINT AS corporation_id,
                    corp.name AS corporation_name,
                    corp.ticker AS corporation_ticker
                FROM entities.character_corporation_history cch
                JOIN entities.corporation_alliance_history cah
                  ON cah.corporation_id = cch.corporation_id
                 AND cah.end_date IS NULL
                 AND COALESCE(cah.is_deleted, FALSE) IS FALSE
                LEFT JOIN entities.corporations corp
                  ON corp.corporation_id = cch.corporation_id
                WHERE cah.alliance_id = %s
                  AND cch.end_date IS NULL
                  AND COALESCE(cch.is_deleted, FALSE) IS FALSE
            """

        search_sql = ""
        search_params = []
        if query:
            search_like = f"%{query.lower()}%"
            search_sql = """
                AND (
                    lower(COALESCE(c.name, '')) LIKE %s
                    OR lower(COALESCE(m.corporation_name, '')) LIKE %s
                    OR lower(COALESCE(m.corporation_ticker, '')) LIKE %s
                )
            """
            search_params = [search_like, search_like, search_like]

        base_sql = f"""
            WITH members AS (
                {members_sql}
            ), eligible AS (
                SELECT
                    m.character_id,
                    c.name AS character_name,
                    cps.updated_at,
                    m.corporation_id,
                    m.corporation_name,
                    m.corporation_ticker
                FROM members m
                JOIN entities.character_pilotable_ships cps
                  ON cps.character_id = m.character_id
                LEFT JOIN entities.characters c
                  ON c.character_id = m.character_id
                WHERE octet_length(cps.ship_mask) > %s
                  AND (get_byte(cps.ship_mask, %s)::INT & %s) <> 0
                  {search_sql}
            )
        """

        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '120000ms'")
            cur.execute(
                f"""
                WITH members AS (
                    {members_sql}
                )
                SELECT COUNT(*)
                FROM members
                """,
                (normalized_entity_id,),
            )
            total_members = int(cur.fetchone()[0] or 0)

            base_params = [normalized_entity_id, byte_index, byte_index, bit_mask] + search_params
            cur.execute(
                base_sql + "SELECT COUNT(*) FROM eligible",
                tuple(base_params),
            )
            matching_pilots = int(cur.fetchone()[0] or 0)

            total_pages = max(1, (matching_pilots + per_page - 1) // per_page)
            if page > total_pages:
                page = total_pages
            offset = (page - 1) * per_page

            cur.execute(
                base_sql + """
                SELECT
                    character_id,
                    character_name,
                    updated_at,
                    corporation_id,
                    corporation_name,
                    corporation_ticker
                FROM eligible
                ORDER BY lower(COALESCE(character_name, '')), character_id
                LIMIT %s OFFSET %s
                """,
                tuple(base_params + [per_page, offset]),
            )
            rows = cur.fetchall()

    pilots = []
    for row in rows:
        character_id = int(row[0])
        corporation = None
        if entity_type == "alliance" and row[3] is not None:
            corporation_id = int(row[3])
            corporation = {
                "entity_type": "corporation",
                "entity_id": corporation_id,
                "name": row[4] or "Unknown",
                "ticker": row[5],
                "url": profile_url("corporation", corporation_id),
                "image_url": image_url("corporation", corporation_id, 32),
            }

        pilots.append({
            "entity_type": "character",
            "entity_id": character_id,
            "name": row[1] or f"Character {character_id}",
            "url": profile_url("character", character_id),
            "image_url": image_url("character", character_id, 32),
            "updated_at": row[2],
            "corporation": corporation,
        })

    return {
        "entity_type": entity_type,
        "entity_id": normalized_entity_id,
        "ship_type_id": normalized_ship_type_id,
        "profile": profile,
        "ship": ship,
        "pilots": pilots,
        "total_members": total_members,
        "matching_pilots": matching_pilots,
        "query": query,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total_rows": matching_pilots,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
            "prev_page": page - 1 if page > 1 else None,
            "next_page": page + 1 if page < total_pages else None,
        },
        "ready": True,
        "error": None,
    }


def _ship_entity_rows_to_payload(rows):
    ships = []
    for row in rows:
        ship_type_id = int(row[0])
        ships.append({
            "entity_type": "ship",
            "entity_id": ship_type_id,
            "name": row[1] or f"Type {ship_type_id}",
            "ticker": None,
            "group_name": row[2],
            "category_name": row[3],
            "region_name": None,
            "faction_name": row[4] or "Other",
            "faction_logo_url": row[5],
            "faction_order": int(row[6]) if row[6] is not None else 500,
            "ship_size": row[7],
            "ship_size_order": int(row[8]) if row[8] is not None else 900,
            "ship_sde_band": row[9],
            "ship_sde_band_order": int(row[10]) if row[10] is not None else 900,
            "ship_display_size": row[11],
            "ship_tree_row": row[12],
            "ship_tier": row[13],
            "ship_tier_order": int(row[14]) if row[14] is not None else 900,
            "url": profile_url("ship", ship_type_id),
            "image_url": row[15] or image_url("ship", ship_type_id, 32),
            "last_killmail_time": None,
        })
    return ships


def _ship_selection_query_conditions(*, type_id=None, tier=None, group=None, size=None, faction=None):
    conditions = []
    params = []

    if type_id is not None:
        conditions.append("entity_id::BIGINT = %s")
        params.append(int(type_id))

    if tier:
        conditions.append("lower(COALESCE(ship_tier, '')) = lower(%s)")
        params.append(str(tier).strip())

    if group:
        conditions.append("(lower(COALESCE(ship_size, '')) = lower(%s) OR lower(COALESCE(group_name, '')) = lower(%s) OR lower(COALESCE(ship_sde_band, '')) = lower(%s))")
        value = str(group).strip()
        params.extend([value, value, value])

    if size:
        conditions.append("lower(COALESCE(ship_display_size, '')) = lower(%s)")
        params.append(str(size).strip())

    if faction:
        conditions.append("lower(COALESCE(faction_name, 'Other')) = lower(%s)")
        params.append(str(faction).strip())

    if not conditions:
        conditions.append("TRUE")

    return " AND ".join(conditions), params


def _fetch_ship_selection_ships(conn, *, type_id=None, tier=None, group=None, size=None, faction=None):
    items = _sde_all_ship_entities(conn)

    def same(left, right):
        return str(left or "").strip().lower() == str(right or "").strip().lower()

    if type_id is not None:
        wanted = int(type_id)
        items = [item for item in items if int(item["entity_id"]) == wanted]
    elif tier:
        items = [item for item in items if same(item.get("ship_tier"), tier)]
    elif group:
        items = [
            item for item in items
            if same(item.get("ship_size"), group)
            or same(item.get("group_name"), group)
            or same(item.get("ship_sde_band"), group)
        ]
    elif size:
        items = [item for item in items if same(item.get("ship_display_size"), size)]
    elif faction:
        items = [item for item in items if same(item.get("faction_name"), faction)]

    return [dict(item) for item in items]


def get_ship_selection_profile(type_id=None, tier=None, group=None, size=None, faction=None):
    with db() as conn:
        ships = _fetch_ship_selection_ships(
            conn,
            type_id=type_id,
            tier=tier,
            group=group,
            size=size,
            faction=faction,
        )

    if type_id is not None and not ships:
        raise EntityError("ship_not_found")

    if type_id is not None:
        ship = ships[0]
        title = ship["name"]
        subtitle = ship.get("group_name") or "Ship"
        selection_type = "ship"
        image = ship.get("image_url") or image_url("ship", ship["entity_id"], 128)
        active_filter = {"type_id": int(type_id)}
    else:
        selection_type = "category"
        active_filter = {}
        if faction:
            label = str(faction).strip()
            title = f"{label} ships"
            subtitle = "Ship faction selection"
            active_filter["faction"] = label
        elif group:
            label = str(group).strip()
            title = f"{label} ships"
            subtitle = "Ship group selection"
            active_filter["group"] = label
        elif size:
            label = str(size).strip()
            title = f"{label} ships"
            subtitle = "Ship size selection"
            active_filter["size"] = label
        elif tier:
            label = str(tier).strip()
            title = f"{label} ships"
            subtitle = "Ship tier selection"
            active_filter["tier"] = label
        else:
            title = "Ships"
            subtitle = "Ship selection"
        image = None
        if faction:
            for ship in ships:
                if ship.get("faction_logo_url"):
                    image = ship.get("faction_logo_url")
                    break

    ranking_ship_ids = [
        int(ship["entity_id"])
        for ship in ships
        if ship.get("is_pilotable", True)
    ]
    non_ship_count = sum(1 for ship in ships if not ship.get("is_pilotable", True))

    if ships and non_ship_count == len(ships):
        if type_id is not None:
            subtitle = ships[0].get("group_name") or ships[0].get("category_name") or "Killable object"
        elif faction:
            title = str(faction).strip()
            subtitle = "Killable object selection"
        elif group:
            title = str(group).strip()
            subtitle = "Killable object group selection"
        elif tier:
            title = str(tier).strip()
            subtitle = "Killable object selection"

    return {
        "selection_type": selection_type,
        "title": title,
        "subtitle": subtitle,
        "image_url": image,
        "ships": ships,
        "ship_ids": [int(ship["entity_id"]) for ship in ships],
        "ranking_ship_ids": ranking_ship_ids,
        "has_ranking": bool(ranking_ship_ids),
        "non_ship_count": non_ship_count,
        "ships_count": len(ships),
        "active_filter": active_filter,
    }


def _pilotable_bit_expression(bit_indices):
    parts = []
    for bit_index in sorted({int(value) for value in bit_indices}):
        byte_index = bit_index // 8
        bit_value = 1 << (bit_index % 8)
        parts.append(f"(octet_length(cps.ship_mask) > {byte_index} AND (get_byte(cps.ship_mask, {byte_index}) & {bit_value}) <> 0)")
    return " OR ".join(parts) if parts else "FALSE"


def _ship_selection_bit_indices(conn, ship_ids):
    normalized_ids = sorted({int(value) for value in ship_ids if value is not None})
    if not normalized_ids:
        return []
    if not _table_exists(conn, "entities", "pilotable_ship_index"):
        raise EntityError("pilotable_ship_index_missing")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT bit_index
            FROM entities.pilotable_ship_index
            WHERE enabled IS TRUE
              AND ship_type_id = ANY(%s)
            ORDER BY bit_index
            """,
            (normalized_ids,),
        )
        return [int(row[0]) for row in cur.fetchall()]


NPC_CORPORATION_RANKING_EXCLUDES = [
    ("viziam", "VI"),
    ("ministry of war", "MW"),
    ("state war academy", "SWA"),
    ("imperial academy", "IAC"),
    ("the scope", "TS"),
    ("caldari provisions", "CP"),
    ("federal navy academy", "FNA"),
    ("royal amarr institute", "RIN"),
    ("deep core mining inc.", "DCMI"),
    ("imperial shipment", "IS"),
    ("perkone", "P"),
    ("center for advanced studies", "CAS"),
    ("school of applied knowledge", "SAK"),
    ("republic military school", "RMS"),
    ("science and trade institute", "STI"),
    ("hedion university", "HU"),
    ("aliastra", "A"),
    ("sebiestor tribe", "S"),
    ("brutor tribe", "B"),
    ("university of caille", "UC"),
    ("garoun investment bank", "GIB"),
    ("pator tech school", "PTS"),
    ("republic university", "RUN"),
    ("native freshfood", "NF"),
    ("24th imperial crusade", "IC24"),
    ("state protectorate", "SPROT"),
    ("federal defense union", "FEDEF"),
    ("tribal liberation force", "TLIB"),
]


def get_ship_selection_ranking(ship_ids, ranking_type="alliance", page=1, per_page=10, q="", offset=None, hide_npc_corps=True):
    ranking_type = (ranking_type or "alliance").strip().lower()
    if ranking_type not in {"alliance", "corporation"}:
        ranking_type = "alliance"

    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    if page < 1:
        page = 1

    try:
        per_page = int(per_page)
    except (TypeError, ValueError):
        per_page = 10
    per_page = max(1, min(per_page, 100))

    if offset is None:
        offset_value = (page - 1) * per_page
    else:
        try:
            offset_value = int(offset)
        except (TypeError, ValueError):
            offset_value = 0
        if offset_value < 0:
            offset_value = 0

    query_text = (q or "").strip()

    with db() as conn:
        if not _table_exists(conn, "entities", "character_pilotable_ships"):
            return {"items": [], "page": page, "per_page": per_page, "offset": offset_value, "has_next": False, "next_offset": None, "total_rows": 0, "error": "character_pilotable_ships_missing"}
        bit_indices = _ship_selection_bit_indices(conn, ship_ids)
        if not bit_indices:
            return {"items": [], "page": page, "per_page": per_page, "offset": offset_value, "has_next": False, "next_offset": None, "total_rows": 0, "error": None}

        bit_sql = _pilotable_bit_expression(bit_indices)
        search_sql = ""
        params = []

        if ranking_type == "corporation":
            npc_filter_sql = "TRUE"
            if hide_npc_corps:
                npc_checks = []
                for npc_name, npc_ticker in NPC_CORPORATION_RANKING_EXCLUDES:
                    npc_checks.append("(lower(COALESCE(c.name, '')) = %s AND upper(COALESCE(c.ticker, '')) = %s)")
                    params.extend([npc_name, npc_ticker])
                if npc_checks:
                    npc_filter_sql = "NOT (" + " OR ".join(npc_checks) + ")"

            grouped_sql = f"""
                WITH matched AS (
                    SELECT cps.character_id
                    FROM entities.character_pilotable_ships cps
                    WHERE {bit_sql}
                ), grouped AS (
                    SELECT
                        cc.corporation_id AS entity_id,
                        COUNT(*)::BIGINT AS pilot_count
                    FROM matched m
                    JOIN entities.character_corporation_history cc
                      ON cc.character_id = m.character_id
                     AND cc.end_date IS NULL
                     AND cc.is_deleted IS FALSE
                    WHERE cc.corporation_id IS NOT NULL
                    GROUP BY cc.corporation_id
                ), enriched AS (
                    SELECT
                        g.entity_id,
                        COALESCE(NULLIF(c.name, ''), 'Unknown') AS name,
                        c.ticker,
                        g.pilot_count
                    FROM grouped g
                    LEFT JOIN entities.corporations c ON c.corporation_id = g.entity_id
                    WHERE {npc_filter_sql}
                ), ranked AS (
                    SELECT
                        ROW_NUMBER() OVER (ORDER BY pilot_count DESC, lower(COALESCE(name, '')), entity_id) AS rank,
                        entity_id,
                        name,
                        ticker,
                        pilot_count
                    FROM enriched
                )
            """
        else:
            grouped_sql = f"""
                WITH matched AS (
                    SELECT cps.character_id
                    FROM entities.character_pilotable_ships cps
                    WHERE {bit_sql}
                ), grouped AS (
                    SELECT
                        ca.alliance_id AS entity_id,
                        COUNT(*)::BIGINT AS pilot_count
                    FROM matched m
                    JOIN entities.character_corporation_history cc
                      ON cc.character_id = m.character_id
                     AND cc.end_date IS NULL
                     AND cc.is_deleted IS FALSE
                    JOIN entities.corporation_alliance_history ca
                      ON ca.corporation_id = cc.corporation_id
                     AND ca.end_date IS NULL
                     AND ca.is_deleted IS FALSE
                    WHERE ca.alliance_id IS NOT NULL
                    GROUP BY ca.alliance_id
                ), ranked AS (
                    SELECT
                        ROW_NUMBER() OVER (ORDER BY g.pilot_count DESC, lower(COALESCE(a.name, '')), g.entity_id) AS rank,
                        g.entity_id,
                        COALESCE(NULLIF(a.name, ''), 'Unknown') AS name,
                        a.ticker,
                        g.pilot_count
                    FROM grouped g
                    LEFT JOIN entities.alliances a ON a.alliance_id = g.entity_id
                )
            """

        if query_text:
            search_sql = "WHERE lower(name) LIKE lower(%s) OR lower(COALESCE(ticker, '')) LIKE lower(%s) OR entity_id::TEXT = %s"
            like_value = f"%{query_text}%"
            params.extend([like_value, like_value, query_text])

        sql = grouped_sql + f"""
            SELECT COUNT(*) OVER() AS total_rows, rank, entity_id, name, ticker, pilot_count
            FROM ranked
            {search_sql}
            ORDER BY rank
            LIMIT %s OFFSET %s
        """
        params.extend([per_page + 1, offset_value])

        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '120000ms'")
            cur.execute(sql, params)
            rows = cur.fetchall()

    has_next = len(rows) > per_page
    visible_rows = rows[:per_page]
    total_rows = int(rows[0][0]) if rows else 0
    items = []
    for _, rank, entity_id, name, ticker, pilot_count in visible_rows:
        entity_type = "corporation" if ranking_type == "corporation" else "alliance"
        items.append({
            "rank": int(rank),
            "entity_type": entity_type,
            "entity_id": int(entity_id),
            "name": name or "Unknown",
            "ticker": ticker,
            "pilot_count": int(pilot_count or 0),
            "url": profile_url(entity_type, entity_id),
            "image_url": image_url(entity_type, entity_id, 32),
        })

    return {
        "items": items,
        "page": page,
        "per_page": per_page,
        "offset": offset_value,
        "has_next": has_next,
        "next_offset": offset_value + per_page if has_next else None,
        "next_page": page + 1 if has_next else None,
        "total_rows": total_rows,
        "q": query_text,
        "ranking_type": ranking_type,
        "hide_npc_corps": bool(hide_npc_corps),
        "error": None,
    }


KILLMAIL_FITTING_FLAGS = {
    "high": set(range(27, 35)),
    "mid": set(range(19, 27)),
    "low": set(range(11, 19)),
    "rig": set(range(92, 100)),
    "subsystem": set(range(125, 133)),
    "drone": {87},
    "cargo": {5, 86, 89, 90, 94, 133, 134, 135, 136, 137, 138, 139},
}

KILLMAIL_FITTING_GROUP_LABELS = {
    "high": "High slots",
    "mid": "Mid slots",
    "low": "Low slots",
    "rig": "Rigs",
    "subsystem": "Subsystems",
    "drone": "Drones",
    "cargo": "Cargo / other",
}

KILLMAIL_FITTING_GROUP_ORDER = {
    "high": 10,
    "mid": 20,
    "low": 30,
    "rig": 40,
    "subsystem": 50,
    "drone": 60,
    "cargo": 70,
}


def _killmail_item_group(flag):
    try:
        normalized_flag = int(flag)
    except (TypeError, ValueError):
        return "cargo"

    for group_key, flags in KILLMAIL_FITTING_FLAGS.items():
        if normalized_flag in flags:
            return group_key

    return "cargo"


def _killmail_item_flag_label(flag):
    try:
        normalized_flag = int(flag)
    except (TypeError, ValueError):
        return "Unknown"

    group_key = _killmail_item_group(normalized_flag)
    if group_key == "high":
        return f"High slot {normalized_flag - 26}"
    if group_key == "mid":
        return f"Mid slot {normalized_flag - 18}"
    if group_key == "low":
        return f"Low slot {normalized_flag - 10}"
    if group_key == "rig":
        return f"Rig slot {normalized_flag - 91}"
    if group_key == "subsystem":
        return f"Subsystem {normalized_flag - 124}"
    if group_key == "drone":
        return "Drone bay"
    if normalized_flag == 5:
        return "Cargo"
    return f"Flag {normalized_flag}"


def _killmail_type_ref(type_id, name, entity_type="commodity", size=64):
    if type_id is None:
        return None

    type_id = int(type_id)
    if entity_type == "ship":
        url = profile_url("ship", type_id)
    elif entity_type == "weapon":
        url = profile_url("weapon", type_id)
    else:
        url = profile_url("commodity", type_id)

    return {
        "entity_type": entity_type,
        "entity_id": type_id,
        "type_id": type_id,
        "name": name or _format_type_name(type_id),
        "ticker": None,
        "url": url,
        "image_url": type_icon_url(type_id, size),
    }


def _killmail_items_from_rows(rows, type_names):
    items = []

    for row in rows:
        (
            item_index,
            item_type_id,
            flag,
            quantity_destroyed,
            quantity_dropped,
            singleton,
        ) = row

        if item_type_id is None:
            continue

        item_type_id = int(item_type_id)
        destroyed_qty = int(quantity_destroyed or 0)
        dropped_qty = int(quantity_dropped or 0)
        group_key = _killmail_item_group(flag)

        items.append({
            "item_index": int(item_index or 0),
            "type_id": item_type_id,
            "item": _killmail_type_ref(
                item_type_id,
                type_names.get(item_type_id, _format_type_name(item_type_id)),
                "commodity",
                32,
            ),
            "flag": int(flag) if flag is not None else None,
            "flag_label": _killmail_item_flag_label(flag),
            "group_key": group_key,
            "group_label": KILLMAIL_FITTING_GROUP_LABELS.get(group_key, "Cargo / other"),
            "quantity_destroyed": destroyed_qty,
            "quantity_dropped": dropped_qty,
            "singleton": singleton,
            "is_destroyed": destroyed_qty > 0,
            "is_dropped": dropped_qty > 0,
            "total_quantity": destroyed_qty + dropped_qty,
        })

    return sorted(
        items,
        key=lambda item: (
            KILLMAIL_FITTING_GROUP_ORDER.get(item["group_key"], 900),
            item["flag"] if item["flag"] is not None else 9999,
            (item["item"] or {}).get("name", ""),
            item["item_index"],
        ),
    )


def _killmail_fitting_sections(items):
    grouped = {
        key: {
            "key": key,
            "label": KILLMAIL_FITTING_GROUP_LABELS[key],
            "items": [],
        }
        for key in ("high", "mid", "low", "rig", "subsystem", "drone", "cargo")
    }

    for item in items:
        group_key = item.get("group_key") or "cargo"
        grouped.setdefault(group_key, {
            "key": group_key,
            "label": KILLMAIL_FITTING_GROUP_LABELS.get(group_key, group_key.title()),
            "items": [],
        })
        grouped[group_key]["items"].append(item)

    return [
        grouped[key]
        for key in ("high", "mid", "low", "rig", "subsystem", "drone", "cargo")
        if grouped.get(key) and grouped[key]["items"]
    ]


def _killmail_attacker_coalition_map(conn, attacker_rows, killmail_time):
    """Resolve each attacker to its effective coalition on the killmail date.

    Coalition rules are evaluated recursively with INCLUDE/EXCLUDE semantics.
    When a nested coalition and its parent both resolve to the same attacker,
    the top-level matching coalition is used. If multiple unrelated top-level
    coalitions match, they are represented as one combined group so damage is
    never double-counted.
    """
    if not attacker_rows or killmail_time is None:
        return {}
    if not _table_exists(conn, "entities", "coalitions") or not _table_exists(conn, "entities", "coalition_memberships"):
        return {}

    kill_day = killmail_time.date()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                c.coalition_id,
                c.name,
                c.short_name,
                m.operation,
                m.member_type,
                m.member_id
            FROM entities.coalitions c
            LEFT JOIN entities.coalition_memberships m
              ON m.coalition_id = c.coalition_id
             AND (m.valid_from IS NULL OR m.valid_from <= %s)
             AND (m.valid_to IS NULL OR m.valid_to >= %s)
            ORDER BY c.coalition_id, m.id
            """,
            (kill_day, kill_day),
        )
        rows = cur.fetchall()

    coalition_meta = {}
    rules_by_coalition = {}
    for coalition_id, name, short_name, operation, member_type, member_id in rows:
        coalition_id = int(coalition_id)
        coalition_meta[coalition_id] = {
            "coalition_id": coalition_id,
            "name": name or f"Coalition {coalition_id}",
            "short_name": short_name,
            "url": f"/coalition/{coalition_id}",
            "image_url": None,
        }
        if operation is None or member_type is None or member_id is None:
            continue
        rules_by_coalition.setdefault(coalition_id, []).append({
            "operation": str(operation).strip().lower(),
            "member_type": str(member_type).strip().lower(),
            "member_id": int(member_id),
        })

    resolved_cache = {}

    def resolve(coalition_id, stack=frozenset()):
        coalition_id = int(coalition_id)
        if coalition_id in resolved_cache:
            return resolved_cache[coalition_id]
        if coalition_id in stack:
            return frozenset()

        includes = set()
        excludes = set()
        next_stack = stack | {coalition_id}
        for rule in rules_by_coalition.get(coalition_id, []):
            if rule["member_type"] == "coalition":
                target = set(resolve(rule["member_id"], next_stack))
            elif rule["member_type"] in {"alliance", "corporation"}:
                target = {(rule["member_type"], rule["member_id"])}
            else:
                continue

            if rule["operation"] == "exclude":
                excludes.update(target)
            else:
                includes.update(target)

        result = frozenset(includes - excludes)
        resolved_cache[coalition_id] = result
        return result

    resolved_by_coalition = {
        coalition_id: resolve(coalition_id)
        for coalition_id in coalition_meta
    }

    direct_child_state = {}
    for parent_id, rules in rules_by_coalition.items():
        for rule in rules:
            if rule["member_type"] != "coalition":
                continue
            key = (int(parent_id), int(rule["member_id"]))
            if rule["operation"] == "exclude":
                direct_child_state[key] = False
            elif key not in direct_child_state:
                direct_child_state[key] = True

    active_parent_links = {
        key for key, included in direct_child_state.items() if included
    }

    result = {}
    for row in attacker_rows:
        attacker_index = int(row[0] or 0)
        corporation_id = int(row[3]) if row[3] is not None else None
        alliance_id = int(row[6]) if row[6] is not None else None
        entity_keys = set()
        if alliance_id is not None:
            entity_keys.add(("alliance", alliance_id))
        if corporation_id is not None:
            entity_keys.add(("corporation", corporation_id))

        matching = {
            coalition_id
            for coalition_id, resolved in resolved_by_coalition.items()
            if entity_keys.intersection(resolved)
        }
        if not matching:
            continue

        roots = {
            coalition_id
            for coalition_id in matching
            if not any(
                parent_id in matching and (parent_id, coalition_id) in active_parent_links
                for parent_id in matching
                if parent_id != coalition_id
            )
        }
        selected = sorted(
            roots or matching,
            key=lambda coalition_id: (
                (coalition_meta[coalition_id]["name"] or "").casefold(),
                coalition_id,
            ),
        )

        if len(selected) == 1:
            result[attacker_index] = dict(coalition_meta[selected[0]])
        else:
            names = [coalition_meta[coalition_id]["name"] for coalition_id in selected]
            result[attacker_index] = {
                "coalition_id": "+".join(str(coalition_id) for coalition_id in selected),
                "name": " / ".join(names),
                "short_name": None,
                "url": None,
                "image_url": None,
            }

    return result


def get_killmail_detail(killmail_id):
    try:
        normalized_id = int(killmail_id)
    except (TypeError, ValueError) as exc:
        raise EntityError("killmail_id_invalid") from exc

    with db() as conn:
        if not _table_exists(conn, "rawkm", "killmails"):
            raise EntityError("rawkm.killmails_missing")

        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '30000ms'")
            cur.execute(
                """
                SELECT
                    km.killmail_id,
                    km.killmail_hash,
                    km.killmail_time,
                    km.solar_system_id,
                    km.victim_character_id,
                    vc.name AS victim_character_name,
                    km.victim_corporation_id,
                    vcorp.name AS victim_corporation_name,
                    vcorp.ticker AS victim_corporation_ticker,
                    km.victim_alliance_id,
                    vall.name AS victim_alliance_name,
                    vall.ticker AS victim_alliance_ticker,
                    km.victim_ship_type_id,
                    km.victim_damage_taken
                FROM rawkm.killmails km
                LEFT JOIN entities.characters vc
                  ON vc.character_id = km.victim_character_id
                LEFT JOIN entities.corporations vcorp
                  ON vcorp.corporation_id = km.victim_corporation_id
                LEFT JOIN entities.alliances vall
                  ON vall.alliance_id = km.victim_alliance_id
                WHERE km.killmail_id = %s
                LIMIT 1
                """,
                (normalized_id,),
            )
            killmail_row = cur.fetchone()

        if not killmail_row:
            raise EntityError("killmail_not_found")

        (
            row_killmail_id,
            killmail_hash,
            killmail_time,
            solar_system_id,
            victim_character_id,
            victim_character_name,
            victim_corporation_id,
            victim_corporation_name,
            victim_corporation_ticker,
            victim_alliance_id,
            victim_alliance_name,
            victim_alliance_ticker,
            victim_ship_type_id,
            victim_damage_taken,
        ) = killmail_row

        source_url = None

        attacker_rows = []
        if _table_exists(conn, "rawkm", "killmail_attackers"):
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '30000ms'")
                cur.execute(
                    """
                    SELECT
                        ka.attacker_index,
                        ka.character_id,
                        c.name AS character_name,
                        ka.corporation_id,
                        corp.name AS corporation_name,
                        corp.ticker AS corporation_ticker,
                        ka.alliance_id,
                        alli.name AS alliance_name,
                        alli.ticker AS alliance_ticker,
                        ka.ship_type_id,
                        ka.weapon_type_id,
                        COALESCE(ka.damage_done, 0)::INTEGER AS damage_done,
                        COALESCE(ka.final_blow, FALSE)::BOOLEAN AS final_blow,
                        ka.security_status
                    FROM rawkm.killmail_attackers ka
                    LEFT JOIN entities.characters c
                      ON c.character_id = ka.character_id
                    LEFT JOIN entities.corporations corp
                      ON corp.corporation_id = ka.corporation_id
                    LEFT JOIN entities.alliances alli
                      ON alli.alliance_id = ka.alliance_id
                    WHERE ka.killmail_id = %s
                    ORDER BY
                        COALESCE(ka.final_blow, FALSE) DESC,
                        COALESCE(ka.damage_done, 0) DESC,
                        ka.attacker_index ASC
                    """,
                    (normalized_id,),
                )
                attacker_rows = cur.fetchall()

        item_rows = []
        if _table_exists(conn, "rawkm", "killmail_items"):
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '30000ms'")
                cur.execute(
                    """
                    SELECT
                        item_index,
                        item_type_id,
                        flag,
                        quantity_destroyed,
                        quantity_dropped,
                        singleton
                    FROM rawkm.killmail_items
                    WHERE killmail_id = %s
                    ORDER BY item_index ASC
                    """,
                    (normalized_id,),
                )
                item_rows = cur.fetchall()

        type_ids = set()
        if victim_ship_type_id is not None:
            type_ids.add(int(victim_ship_type_id))

        for row in attacker_rows:
            if row[9] is not None:
                type_ids.add(int(row[9]))
            if row[10] is not None:
                type_ids.add(int(row[10]))

        for row in item_rows:
            if row[1] is not None:
                type_ids.add(int(row[1]))

        type_names = _lookup_type_names(conn, type_ids)
        location = {}
        if solar_system_id is not None:
            location = _lookup_system_locations(conn, [solar_system_id]).get(int(solar_system_id), {})
        attacker_coalitions = _killmail_attacker_coalition_map(conn, attacker_rows, killmail_time)

    victim_damage = int(victim_damage_taken or 0)
    victim_character = _entity_ref("character", victim_character_id, victim_character_name)
    victim_corporation = _entity_ref("corporation", victim_corporation_id, victim_corporation_name, victim_corporation_ticker)
    victim_alliance = _entity_ref("alliance", victim_alliance_id, victim_alliance_name, victim_alliance_ticker)
    victim_ship = _killmail_type_ref(
        victim_ship_type_id,
        type_names.get(int(victim_ship_type_id), _format_type_name(victim_ship_type_id)) if victim_ship_type_id is not None else "Unknown",
        "ship",
        128,
    ) if victim_ship_type_id is not None else None

    attackers = []
    total_attacker_damage = 0

    for row in attacker_rows:
        (
            attacker_index,
            character_id,
            character_name,
            corporation_id,
            corporation_name,
            corporation_ticker,
            alliance_id,
            alliance_name,
            alliance_ticker,
            ship_type_id,
            weapon_type_id,
            damage_done,
            final_blow,
            security_status,
        ) = row

        damage = int(damage_done or 0)
        damage_percent_value = (damage / victim_damage) * 100.0 if victim_damage > 0 else 0.0
        normalized_attacker_index = int(attacker_index or 0)
        total_attacker_damage += damage

        attackers.append({
            "attacker_index": normalized_attacker_index,
            "character": _entity_ref("character", character_id, character_name),
            "corporation": _entity_ref("corporation", corporation_id, corporation_name, corporation_ticker),
            "alliance": _entity_ref("alliance", alliance_id, alliance_name, alliance_ticker),
            "coalition": attacker_coalitions.get(normalized_attacker_index),
            "ship": _killmail_type_ref(
                ship_type_id,
                type_names.get(int(ship_type_id), _format_type_name(ship_type_id)) if ship_type_id is not None else "Unknown",
                "ship",
                64,
            ) if ship_type_id is not None else None,
            "weapon": _killmail_type_ref(
                weapon_type_id,
                type_names.get(int(weapon_type_id), _format_type_name(weapon_type_id)) if weapon_type_id is not None else "Unknown",
                "weapon",
                64,
            ) if weapon_type_id is not None else None,
            "damage_done": damage,
            "damage_percent": _damage_percent(damage, victim_damage),
            "damage_percent_value": damage_percent_value,
            "final_blow": bool(final_blow),
            "security_status": security_status,
        })

    final_blow = next((attacker for attacker in attackers if attacker.get("final_blow")), None)
    top_damage = attackers[0] if attackers else None
    if attackers:
        top_damage = sorted(
            attackers,
            key=lambda item: (-int(item.get("damage_done") or 0), item.get("attacker_index") or 0),
        )[0]

    items = _killmail_items_from_rows(item_rows, type_names)
    fitting_sections = _killmail_fitting_sections(items)
    dropped_items = [item for item in items if item.get("is_dropped")]
    destroyed_items = [item for item in items if item.get("is_destroyed")]

    return {
        "killmail_id": int(row_killmail_id),
        "killmail_hash": killmail_hash,
        "killmail_time": killmail_time,
        "date_label": killmail_time.strftime("%Y-%m-%d %H:%M:%S UTC") if killmail_time else "Unknown date",
        "solar_system_id": int(solar_system_id) if solar_system_id is not None else None,
        "location": location or {
            "system": _location_ref("system", solar_system_id, _format_system_name(solar_system_id)) if solar_system_id is not None else None,
            "constellation": None,
            "region": None,
        },
        "victim": {
            "character": victim_character,
            "corporation": victim_corporation,
            "alliance": victim_alliance,
            "damage_taken": victim_damage,
        },
        "victim_ship": victim_ship,
        "attackers": attackers,
        "attackers_count": len(attackers),
        "total_attacker_damage": total_attacker_damage,
        "final_blow": final_blow,
        "top_damage": top_damage,
        "items": items,
        "fitting_sections": fitting_sections,
        "dropped_items": dropped_items,
        "destroyed_items": destroyed_items,
        "source_url": source_url,
        "zkill_url": f"https://zkillboard.com/kill/{int(row_killmail_id)}/",
        "value_label": None,
    }

