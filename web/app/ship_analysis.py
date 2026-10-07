from collections import defaultdict
from datetime import date, datetime, timedelta
import logging

from psycopg2.errors import QueryCanceled

from .db import db
from .entities import (
    EntityError,
    _killmail_builder_zone_system_ids,
    _qualified_rawkm_table,
    _rawkm_month_partition_map,
    _table_exists,
    search_global_entities,
)


logger = logging.getLogger(__name__)


ANALYSIS_ROLES = {"attacker", "victim", "both"}
ANALYSIS_METRICS = {"unique", "unique_percent", "uses", "uses_percent"}
ANALYSIS_ZONE_TYPES = {"security", "system", "constellation", "region"}
ANALYSIS_SECURITY_ZONES = {"highsec", "lowsec", "nullsec", "pochven", "wormhole"}
RESTRICTED_SUPER_GROUPS = {"supercarrier", "titan"}
MAX_ANALYSIS_SERIES = 12
MAX_ACTIVITY_DAYS = 36500
ANALYSIS_STATEMENT_TIMEOUT_MS = 45000


def _as_date(value, name):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(str(value or ""), "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise EntityError(f"ship_analysis_invalid:{name}") from exc


def _as_sign(value):
    try:
        return 1 if int(value) >= 0 else -1
    except (TypeError, ValueError):
        return 1


def _normalize_id_terms(raw_terms, *, allow_none=False):
    result = []
    seen = set()
    for raw in raw_terms or []:
        if not isinstance(raw, dict):
            continue
        raw_id = raw.get("entity_id")
        if allow_none and str(raw_id or "").strip().lower() in {"none", "no_alliance", "no-alliance"}:
            entity_id = "none"
        else:
            try:
                entity_id = int(raw_id)
            except (TypeError, ValueError):
                continue
        sign = _as_sign(raw.get("sign", 1))
        key = (sign, entity_id)
        if key in seen:
            continue
        seen.add(key)
        result.append({"sign": sign, "entity_id": entity_id})
    return result


def _normalize_zone_terms(raw_terms):
    result = []
    seen = set()
    for raw in raw_terms or []:
        if not isinstance(raw, dict):
            continue
        entity_type = str(raw.get("entity_type") or "").strip().lower()
        if entity_type not in ANALYSIS_ZONE_TYPES:
            continue
        raw_id = raw.get("entity_id")
        if entity_type == "security":
            entity_id = str(raw_id or "").strip().lower()
            if entity_id not in ANALYSIS_SECURITY_ZONES:
                continue
        else:
            try:
                entity_id = int(raw_id)
            except (TypeError, ValueError):
                continue
        sign = _as_sign(raw.get("sign", 1))
        key = (sign, entity_type, entity_id)
        if key in seen:
            continue
        seen.add(key)
        result.append({"sign": sign, "entity_type": entity_type, "entity_id": entity_id})
    return result


def _normalize_ship_terms(raw_terms):
    result = []
    seen = set()

    for raw in raw_terms or []:
        if not isinstance(raw, dict):
            continue

        entity_type = str(raw.get("entity_type") or "ship").strip().lower()
        sign = _as_sign(raw.get("sign", 1))

        if entity_type == "ship_group":
            group_name = str(
                raw.get("group_name")
                or raw.get("name")
                or raw.get("label")
                or raw.get("entity_id")
                or ""
            ).strip()
            if not group_name:
                continue
            key = (sign, "ship_group", group_name.lower())
            if key in seen:
                continue
            seen.add(key)
            result.append({
                "sign": sign,
                "entity_type": "ship_group",
                "entity_id": group_name,
            })
            continue

        try:
            entity_id = int(raw.get("entity_id"))
        except (TypeError, ValueError):
            continue

        key = (sign, "ship", entity_id)
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "sign": sign,
            "entity_type": "ship",
            "entity_id": entity_id,
        })

    return result


def _normalize_series(raw, index):
    if not isinstance(raw, dict):
        raise EntityError("ship_analysis_invalid:series")

    role = str(raw.get("role") or "both").strip().lower()
    if role not in ANALYSIS_ROLES:
        raise EntityError("ship_analysis_invalid:role")

    metric = str(raw.get("metric") or "unique").strip().lower()
    if metric not in ANALYSIS_METRICS:
        raise EntityError("ship_analysis_invalid:metric")

    raw_days = raw.get("activity_days")
    if raw_days in (None, "", "infinite", "inf"):
        activity_days = None
    else:
        try:
            activity_days = int(raw_days)
        except (TypeError, ValueError) as exc:
            raise EntityError("ship_analysis_invalid:activity_days") from exc
        if activity_days < 1 or activity_days > MAX_ACTIVITY_DAYS:
            raise EntityError("ship_analysis_invalid:activity_days")

    uid = str(raw.get("uid") or f"series-{index + 1}").strip()[:120]
    if not uid:
        uid = f"series-{index + 1}"

    name = str(raw.get("name") or f"Series {index + 1}").strip()[:120]
    if not name:
        name = f"Series {index + 1}"

    return {
        "uid": uid,
        "name": name,
        "role": role,
        "metric": metric,
        "activity_days": activity_days,
        "ships": _normalize_ship_terms(raw.get("ships")),
        "alliances": _normalize_id_terms(raw.get("alliances"), allow_none=True),
        "corporations": _normalize_id_terms(raw.get("corporations"), allow_none=False),
        "zones": _normalize_zone_terms(raw.get("zones")),
    }


def normalize_ship_analysis_request(payload):
    if not isinstance(payload, dict):
        raise EntityError("ship_analysis_invalid:payload")

    date_from = _as_date(payload.get("date_from"), "date_from")
    date_to = _as_date(payload.get("date_to"), "date_to")
    if date_from > date_to:
        raise EntityError("ship_analysis_invalid:date_range")

    raw_series = payload.get("series") or []
    if not isinstance(raw_series, list) or not raw_series:
        raise EntityError("ship_analysis_invalid:series")
    if len(raw_series) > MAX_ANALYSIS_SERIES:
        raise EntityError("ship_analysis_too_many_series")

    series = [_normalize_series(item, index) for index, item in enumerate(raw_series)]
    return {
        "date_from": date_from,
        "date_to": date_to,
        "series": series,
    }


def _first_of_month(value):
    return date(value.year, value.month, 1)


def _next_month(value):
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _iter_months(start, end):
    current = _first_of_month(start)
    final = _first_of_month(end)
    while current <= final:
        yield current
        current = _next_month(current)


def _mid_date(start, end):
    return start + timedelta(days=(end - start).days // 2)


def _split_terms(terms):
    plus = []
    minus = []
    for term in terms or []:
        target = plus if term.get("sign", 1) > 0 else minus
        target.append(term.get("entity_id"))
    return plus, minus


def _resolve_ship_terms(conn, series):
    terms = series.get("ships") or []
    if not terms:
        return []

    resolved = []
    group_terms = []

    for term in terms:
        if term.get("entity_type") == "ship_group":
            group_terms.append(term)
        else:
            resolved.append({
                "sign": term.get("sign", 1),
                "entity_id": int(term["entity_id"]),
            })

    if group_terms:
        if not _table_exists(conn, "sde_work", "ship_entities"):
            raise EntityError("sde_work.ship_entities_missing")

        group_names = sorted({
            str(term.get("entity_id") or "").strip().lower()
            for term in group_terms
            if str(term.get("entity_id") or "").strip()
        })

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT entity_id::bigint, lower(group_name)
                FROM sde_work.ship_entities
                WHERE group_name IS NOT NULL
                  AND lower(group_name) = ANY(%s)
                """,
                (group_names,),
            )
            rows = cur.fetchall()

        ids_by_group = defaultdict(list)
        for entity_id, group_name in rows:
            ids_by_group[str(group_name)].append(int(entity_id))

        for term in group_terms:
            group_name = str(term.get("entity_id") or "").strip()
            ids = ids_by_group.get(group_name.lower(), [])
            if not ids:
                raise EntityError(f"ship_analysis_ship_group_not_found:{group_name}")
            for entity_id in ids:
                resolved.append({
                    "sign": term.get("sign", 1),
                    "entity_id": entity_id,
                })

    deduped = []
    seen = set()
    for term in resolved:
        key = (_as_sign(term.get("sign", 1)), int(term["entity_id"]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append({"sign": key[0], "entity_id": key[1]})

    return deduped


def _restricted_super_titan_catalog(conn):
    if not _table_exists(conn, "sde_work", "ship_entities"):
        raise EntityError("sde_work.ship_entities_missing")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                entity_id::bigint,
                lower(COALESCE(group_name, ''))
            FROM sde_work.ship_entities
            WHERE
                lower(COALESCE(group_name, '')) = ANY(%s)
                OR lower(COALESCE(ship_size, '')) = 'supercapital'
                OR lower(COALESCE(ship_sde_band, '')) = 'supercapital ships'
            """,
            (sorted(RESTRICTED_SUPER_GROUPS),),
        )
        rows = cur.fetchall()

    ship_ids = {int(entity_id) for entity_id, _group_name in rows}
    group_names = {
        str(group_name or "").strip().lower()
        for _entity_id, group_name in rows
        if str(group_name or "").strip()
    }
    return ship_ids, group_names


def enforce_ship_analysis_super_rights(normalized_request, allow_super_titan=False):
    if allow_super_titan:
        return normalized_request

    with db() as conn:
        restricted_ids, _restricted_groups = _restricted_super_titan_catalog(conn)

        protected = dict(normalized_request)
        protected_series = []

        for series in normalized_request.get("series") or []:
            # Resolve groups server-side before authorizing. This blocks both
            # direct ship IDs and forged ship-group payloads.
            resolved_terms = _resolve_ship_terms(conn, series)
            if any(int(term["entity_id"]) in restricted_ids for term in resolved_terms):
                raise EntityError("permission_denied:superintel.view")

            secured_series = dict(series)
            # Hard exclusion also applies to Universe and percentage denominators,
            # so restricted ships cannot be inferred through aggregate analysis.
            secured_series["_forbidden_ship_ids"] = sorted(restricted_ids)
            protected_series.append(secured_series)

        protected["series"] = protected_series
        return protected


def _ship_filter_sql(column, series):
    plus, minus = _split_terms(series.get("ships"))
    plus = [int(value) for value in plus if value is not None]
    minus = [int(value) for value in minus if value is not None]

    clauses = [f"{column} IS NOT NULL"]
    params = []
    if plus:
        clauses.append(f"{column} = ANY(%s)")
        params.append(plus)
    if minus:
        clauses.append(f"NOT ({column} = ANY(%s))")
        params.append(minus)
    return "(" + " AND ".join(clauses) + ")", params


def _participant_filter_sql(alias, series, *, victim=False):
    clauses = []
    params = []

    corporation_column = (
        f"{alias}.victim_corporation_id"
        if victim
        else f"{alias}.corporation_id"
    )
    alliance_column = (
        f"{alias}.victim_alliance_id"
        if victim
        else f"{alias}.alliance_id"
    )

    corp_plus, corp_minus = _split_terms(series.get("corporations"))
    corp_plus = [int(value) for value in corp_plus if value is not None]
    corp_minus = [int(value) for value in corp_minus if value is not None]

    if corp_plus:
        clauses.append(f"{corporation_column} = ANY(%s)")
        params.append(corp_plus)
    if corp_minus:
        clauses.append(f"({corporation_column} IS NULL OR NOT ({corporation_column} = ANY(%s)))")
        params.append(corp_minus)

    alliance_plus, alliance_minus = _split_terms(series.get("alliances"))
    plus_none = "none" in alliance_plus
    minus_none = "none" in alliance_minus
    alliance_plus_ids = [int(value) for value in alliance_plus if value != "none"]
    alliance_minus_ids = [int(value) for value in alliance_minus if value != "none"]

    if alliance_plus_ids or plus_none:
        parts = []
        if alliance_plus_ids:
            parts.append(f"{alliance_column} = ANY(%s)")
            params.append(alliance_plus_ids)
        if plus_none:
            parts.append(f"{alliance_column} IS NULL")
        clauses.append("(" + " OR ".join(parts) + ")")

    if alliance_minus_ids or minus_none:
        parts = []
        if alliance_minus_ids:
            parts.append(f"{alliance_column} = ANY(%s)")
            params.append(alliance_minus_ids)
        if minus_none:
            parts.append(f"{alliance_column} IS NULL")
        excluded = "(" + " OR ".join(parts) + ")"
        if minus_none:
            clauses.append(f"NOT {excluded}")
        else:
            clauses.append(f"({alliance_column} IS NULL OR NOT {excluded})")

    return clauses, params


def _resolve_zone_sets(conn, series):
    plus_terms = []
    minus_terms = []
    for term in series.get("zones") or []:
        normalized = {
            "entity_type": term["entity_type"],
            "entity_id": term["entity_id"],
        }
        if term.get("sign", 1) > 0:
            plus_terms.append(normalized)
        else:
            minus_terms.append(normalized)
    return (
        _killmail_builder_zone_system_ids(conn, plus_terms) if plus_terms else None,
        _killmail_builder_zone_system_ids(conn, minus_terms) if minus_terms else [],
    )


def _zone_filter_sql(column, zone_plus, zone_minus):
    clauses = []
    params = []
    if zone_plus is not None:
        if zone_plus:
            clauses.append(f"{column} = ANY(%s)")
            params.append(zone_plus)
        else:
            clauses.append("FALSE")
    if zone_minus:
        clauses.append(f"NOT ({column} = ANY(%s))")
        params.append(zone_minus)
    return clauses, params


def _event_branch_sql(side, relation, kill_relation, period_start, period_end, series, zone_plus, zone_minus, filter_selected):
    if side == "victim":
        alias = "km"
        from_sql = f"FROM {relation} {alias}"
        time_column = f"{alias}.killmail_time"
        character_column = f"{alias}.victim_character_id"
        ship_column = f"{alias}.victim_ship_type_id"
        participant_alias = alias
        zone_column = f"{alias}.solar_system_id"
    else:
        alias = "ka"
        time_column = f"{alias}.killmail_time"
        character_column = f"{alias}.character_id"
        ship_column = f"{alias}.ship_type_id"
        participant_alias = alias
        if zone_plus is not None or zone_minus:
            if not kill_relation:
                return None, []
            from_sql = (
                f"FROM {relation} {alias} "
                f"JOIN {kill_relation} loc "
                f"ON loc.killmail_id = {alias}.killmail_id "
                f"AND loc.killmail_time = {alias}.killmail_time"
            )
            zone_column = "loc.solar_system_id"
        else:
            from_sql = f"FROM {relation} {alias}"
            zone_column = None

    clauses = [
        f"{time_column} >= %s::date",
        f"{time_column} < %s::date",
        f"{character_column} IS NOT NULL",
        f"{ship_column} IS NOT NULL",
    ]
    params = [period_start, period_end + timedelta(days=1)]

    participant_clauses, participant_params = _participant_filter_sql(
        participant_alias,
        series,
        victim=(side == "victim"),
    )
    clauses.extend(participant_clauses)
    params.extend(participant_params)

    forbidden_ship_ids = [
        int(value)
        for value in (series.get("_forbidden_ship_ids") or [])
        if value is not None
    ]
    if forbidden_ship_ids:
        clauses.append(f"NOT ({ship_column} = ANY(%s))")
        params.append(forbidden_ship_ids)

    if zone_column:
        zone_clauses, zone_params = _zone_filter_sql(zone_column, zone_plus, zone_minus)
        clauses.extend(zone_clauses)
        params.extend(zone_params)

    if filter_selected:
        ship_sql, ship_params = _ship_filter_sql(ship_column, series)
        clauses.append(ship_sql)
        params.extend(ship_params)

    sql = f"""
        SELECT
            {time_column}::date AS event_day,
            {character_column}::bigint AS character_id,
            {ship_column}::bigint AS ship_type_id
        {from_sql}
        WHERE {' AND '.join(clauses)}
    """
    return sql, params


def _period_event_sql(kill_relation, attacker_relation, period_start, period_end, series, zone_plus, zone_minus, filter_selected):
    branches = []
    params = []
    role = series["role"]

    if role in {"victim", "both"} and kill_relation:
        sql, values = _event_branch_sql(
            "victim",
            kill_relation,
            kill_relation,
            period_start,
            period_end,
            series,
            zone_plus,
            zone_minus,
            filter_selected,
        )
        if sql:
            branches.append(sql)
            params.extend(values)

    if role in {"attacker", "both"} and attacker_relation:
        sql, values = _event_branch_sql(
            "attacker",
            attacker_relation,
            kill_relation,
            period_start,
            period_end,
            series,
            zone_plus,
            zone_minus,
            filter_selected,
        )
        if sql:
            branches.append(sql)
            params.extend(values)

    if not branches:
        return None, []
    return " UNION ALL ".join(branches), params


def _selected_outer_sql(series):
    return _ship_filter_sql("ship_type_id", series)


def _unique_period_query(kill_relation, attacker_relation, period_start, period_end, series, zone_plus, zone_minus):
    percent = series["metric"] == "unique_percent"
    filter_selected = not percent
    event_sql, params = _period_event_sql(
        kill_relation,
        attacker_relation,
        period_start,
        period_end,
        series,
        zone_plus,
        zone_minus,
        filter_selected,
    )
    if not event_sql:
        return None, []

    if percent:
        selected_sql, selected_params = _selected_outer_sql(series)
    else:
        selected_sql, selected_params = "TRUE", []

    params = params + selected_params
    activity_days = series["activity_days"]

    if activity_days is None:
        sql = f"""
            WITH events AS (
                {event_sql}
            ), daily_presence AS (
                SELECT
                    event_day,
                    character_id,
                    BOOL_OR({selected_sql}) AS selected
                FROM events
                GROUP BY event_day, character_id
            )
            SELECT
                character_id,
                MIN(event_day) AS base_first_day,
                MIN(event_day) FILTER (WHERE selected) AS selected_first_day
            FROM daily_presence
            GROUP BY character_id
            ORDER BY character_id
        """
        return sql, params

    sql = f"""
        WITH events AS (
            {event_sql}
        ), daily_presence AS (
            SELECT
                event_day,
                character_id,
                BOOL_OR({selected_sql}) AS selected
            FROM events
            GROUP BY event_day, character_id
        ), presence AS (
            SELECT 'selected'::text AS metric_key, event_day, character_id
            FROM daily_presence
            WHERE selected
            {"UNION ALL SELECT 'base'::text AS metric_key, event_day, character_id FROM daily_presence" if percent else ""}
        ), lagged AS (
            SELECT
                metric_key,
                character_id,
                event_day,
                LAG(event_day) OVER (
                    PARTITION BY metric_key, character_id
                    ORDER BY event_day
                ) AS previous_day
            FROM presence
        ), marked AS (
            SELECT
                metric_key,
                character_id,
                event_day,
                CASE
                    WHEN previous_day IS NULL OR (event_day - previous_day) > %s
                    THEN 1 ELSE 0
                END AS new_group
            FROM lagged
        ), grouped AS (
            SELECT
                metric_key,
                character_id,
                event_day,
                SUM(new_group) OVER (
                    PARTITION BY metric_key, character_id
                    ORDER BY event_day
                    ROWS UNBOUNDED PRECEDING
                ) AS grp
            FROM marked
        )
        SELECT
            metric_key,
            character_id,
            MIN(event_day) AS first_day,
            MAX(event_day) AS last_event_day
        FROM grouped
        GROUP BY metric_key, character_id, grp
        ORDER BY first_day, character_id
    """
    params.append(int(activity_days))
    return sql, params


def _uses_period_query(kill_relation, attacker_relation, period_start, period_end, series, zone_plus, zone_minus):
    percent = series["metric"] == "uses_percent"
    filter_selected = not percent
    event_sql, params = _period_event_sql(
        kill_relation,
        attacker_relation,
        period_start,
        period_end,
        series,
        zone_plus,
        zone_minus,
        filter_selected,
    )
    if not event_sql:
        return None, []

    if percent:
        selected_sql, selected_params = _selected_outer_sql(series)
        params.extend(selected_params)
        sql = f"""
            WITH events AS (
                {event_sql}
            )
            SELECT
                event_day,
                COUNT(*) FILTER (WHERE {selected_sql})::bigint AS selected_uses,
                COUNT(*)::bigint AS base_uses
            FROM events
            GROUP BY event_day
            ORDER BY event_day
        """
    else:
        sql = f"""
            WITH events AS (
                {event_sql}
            )
            SELECT
                event_day,
                COUNT(*)::bigint AS selected_uses,
                NULL::bigint AS base_uses
            FROM events
            GROUP BY event_day
            ORDER BY event_day
        """
    return sql, params


def _relation_for_month(month_map, parent_table, month, partitioned):
    item = month_map.get(month)
    if item:
        return _qualified_rawkm_table(item[0], item[1])
    # Parent fallback keeps DEFAULT/unexpected partitions visible. PostgreSQL
    # can still prune normal child partitions using the period predicate.
    return f"rawkm.{parent_table}"


def _query_rows(conn, sql, params):
    with conn.cursor() as cur:
        cur.execute(f"SET LOCAL statement_timeout = '{ANALYSIS_STATEMENT_TIMEOUT_MS}ms'")
        cur.execute(sql, params)
        return cur.fetchall()


def _apply_interval(delta, open_end, character_id, start_day, end_day):
    previous_end = open_end.get(character_id)
    if previous_end is not None and start_day <= previous_end + timedelta(days=1):
        if end_day <= previous_end:
            return
        # Cancel the previously scheduled expiry and move it to the new end.
        delta[previous_end + timedelta(days=1)] += 1
        delta[end_day + timedelta(days=1)] -= 1
        open_end[character_id] = end_day
        return

    delta[start_day] += 1
    delta[end_day + timedelta(days=1)] -= 1
    open_end[character_id] = end_day


def _render_unique_points(series, scan_from, date_from, date_to, selected_delta, base_delta):
    selected_value = 0
    base_value = 0
    points = []
    percent = series["metric"] == "unique_percent"

    day = scan_from
    while day <= date_to:
        selected_value += selected_delta.get(day, 0)
        if percent:
            base_value += base_delta.get(day, 0)
        if day >= date_from:
            if percent:
                value = (selected_value / base_value * 100.0) if base_value else None
                denominator = base_value
            else:
                value = selected_value
                denominator = None
            points.append({
                "date": day.isoformat(),
                "value": round(value, 6) if isinstance(value, float) else value,
                "numerator": selected_value,
                "denominator": denominator,
            })
        day += timedelta(days=1)
    return points


def _render_uses_points(series, scan_from, date_from, date_to, selected_daily, base_daily):
    selected_value = 0
    base_value = 0
    points = []
    percent = series["metric"] == "uses_percent"
    activity_days = series["activity_days"]

    day = scan_from
    while day <= date_to:
        selected_value += int(selected_daily.get(day, 0) or 0)
        if percent:
            base_value += int(base_daily.get(day, 0) or 0)

        if activity_days is not None:
            expired_day = day - timedelta(days=int(activity_days))
            if expired_day >= scan_from:
                selected_value -= int(selected_daily.get(expired_day, 0) or 0)
                if percent:
                    base_value -= int(base_daily.get(expired_day, 0) or 0)

        if day >= date_from:
            if percent:
                value = (selected_value / base_value * 100.0) if base_value else None
                denominator = base_value
            else:
                value = selected_value
                denominator = None
            points.append({
                "date": day.isoformat(),
                "value": round(value, 6) if isinstance(value, float) else value,
                "numerator": selected_value,
                "denominator": denominator,
            })
        day += timedelta(days=1)
    return points


def _series_label(series):
    metric_labels = {
        "unique": "Unique pilots",
        "unique_percent": "Unique pilots %",
        "uses": "Uses",
        "uses_percent": "Uses %",
    }
    activity = "Infinite" if series["activity_days"] is None else f"{series['activity_days']}d"
    role = {"attacker": "Attacker", "victim": "Victim", "both": "Attacker + Victim"}[series["role"]]
    return f"{metric_labels[series['metric']]} · {role} · {activity}"


def _series_scan_from(series, requested_from, earliest_month):
    # The graph start is also the activity-counting start.
    # Never warm up a rolling or Infinite series with events before it.
    return requested_from


def _process_unique_rows(series, rows, selected_delta, base_delta, selected_open, base_open, selected_seen, base_seen):
    percent = series["metric"] == "unique_percent"
    activity_days = series["activity_days"]

    if activity_days is None:
        for character_id, base_first_day, selected_first_day in rows:
            character_id = int(character_id)
            if base_first_day is not None and percent and character_id not in base_seen:
                base_seen.add(character_id)
                base_delta[base_first_day] += 1
            if selected_first_day is not None and character_id not in selected_seen:
                selected_seen.add(character_id)
                selected_delta[selected_first_day] += 1
        return

    for metric_key, character_id, first_day, last_event_day in rows:
        character_id = int(character_id)
        end_day = last_event_day + timedelta(days=int(activity_days) - 1)
        if metric_key == "base":
            _apply_interval(base_delta, base_open, character_id, first_day, end_day)
        else:
            _apply_interval(selected_delta, selected_open, character_id, first_day, end_day)


def iter_ship_analysis_stream(normalized_request):
    date_from = normalized_request["date_from"]
    date_to = normalized_request["date_to"]
    series_list = normalized_request["series"]

    yield {
        "type": "start",
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "series_count": len(series_list),
    }

    for series_index, series in enumerate(series_list):
        try:
            with db() as conn:
                if not _table_exists(conn, "rawkm", "killmails"):
                    raise EntityError("rawkm.killmails_missing")

                kill_map = _rawkm_month_partition_map(conn, "killmails")
                has_attackers = _table_exists(conn, "rawkm", "killmail_attackers")
                attacker_map = _rawkm_month_partition_map(conn, "killmail_attackers") if has_attackers else {}
                kill_partitioned = bool(kill_map)
                attacker_partitioned = bool(attacker_map)
                earliest_candidates = list(kill_map.keys()) + list(attacker_map.keys())
                earliest_month = min(earliest_candidates) if earliest_candidates else None

                query_series = dict(series)
                query_series["ships"] = _resolve_ship_terms(conn, series)

                scan_from = _series_scan_from(series, date_from, earliest_month)
                if scan_from > date_to:
                    scan_from = date_from

                zone_plus, zone_minus = _resolve_zone_sets(conn, series)
                selected_delta = defaultdict(int)
                base_delta = defaultdict(int)
                selected_open = {}
                base_open = {}
                selected_seen = set()
                base_seen = set()
                selected_daily = defaultdict(int)
                base_daily = defaultdict(int)
                total_rows = 0

                yield {
                    "type": "progress",
                    "series_index": series_index,
                    "uid": series["uid"],
                    "name": series["name"],
                    "message": f"Starting {series['name']}",
                    "period": None,
                    "rows": 0,
                }

                for month in _iter_months(scan_from, date_to):
                    month_end = _next_month(month) - timedelta(days=1)
                    period_start = max(scan_from, month)
                    period_end = min(date_to, month_end)
                    kill_relation = _relation_for_month(kill_map, "killmails", month, kill_partitioned)
                    attacker_relation = (
                        _relation_for_month(attacker_map, "killmail_attackers", month, attacker_partitioned)
                        if has_attackers else None
                    )

                    pending = [(period_start, period_end)]
                    while pending:
                        batch_start, batch_end = pending.pop(0)
                        yield {
                            "type": "progress",
                            "series_index": series_index,
                            "uid": series["uid"],
                            "name": series["name"],
                            "message": f"Scanning {batch_start.isoformat()} → {batch_end.isoformat()}",
                            "period": month.isoformat(),
                            "rows": total_rows,
                        }

                        if series["metric"].startswith("unique"):
                            sql, params = _unique_period_query(
                                kill_relation,
                                attacker_relation,
                                batch_start,
                                batch_end,
                                query_series,
                                zone_plus,
                                zone_minus,
                            )
                        else:
                            sql, params = _uses_period_query(
                                kill_relation,
                                attacker_relation,
                                batch_start,
                                batch_end,
                                query_series,
                                zone_plus,
                                zone_minus,
                            )

                        if not sql:
                            continue

                        try:
                            rows = _query_rows(conn, sql, params)
                        except QueryCanceled:
                            conn.rollback()
                            if batch_start >= batch_end:
                                raise EntityError(f"ship_analysis_day_timeout:{batch_start.isoformat()}")
                            mid = _mid_date(batch_start, batch_end)
                            left = (batch_start, mid)
                            right = (mid + timedelta(days=1), batch_end)
                            pending = [left, right] + pending
                            yield {
                                "type": "progress",
                                "series_index": series_index,
                                "uid": series["uid"],
                                "name": series["name"],
                                "message": "Heavy period detected; splitting the scan automatically.",
                                "period": month.isoformat(),
                                "rows": total_rows,
                            }
                            continue

                        total_rows += len(rows)
                        if series["metric"].startswith("unique"):
                            _process_unique_rows(
                                series,
                                rows,
                                selected_delta,
                                base_delta,
                                selected_open,
                                base_open,
                                selected_seen,
                                base_seen,
                            )
                        else:
                            for event_day, selected_uses, base_uses in rows:
                                selected_daily[event_day] += int(selected_uses or 0)
                                if series["metric"] == "uses_percent":
                                    base_daily[event_day] += int(base_uses or 0)

                if series["metric"].startswith("unique"):
                    points = _render_unique_points(
                        series,
                        scan_from,
                        date_from,
                        date_to,
                        selected_delta,
                        base_delta,
                    )
                else:
                    points = _render_uses_points(
                        series,
                        scan_from,
                        date_from,
                        date_to,
                        selected_daily,
                        base_daily,
                    )

                yield {
                    "type": "series",
                    "series_index": series_index,
                    "uid": series["uid"],
                    "name": series["name"],
                    "metric": series["metric"],
                    "role": series["role"],
                    "activity_days": series["activity_days"],
                    "label": _series_label(series),
                    "points": points,
                    "rows_processed": total_rows,
                }

        except Exception:
            logger.exception(
                "Ship analysis series failed series_index=%s uid=%s name=%s",
                series_index,
                series.get("uid"),
                series.get("name"),
            )
            yield {
                "type": "error",
                "series_index": series_index,
                "uid": series.get("uid"),
                "name": series.get("name"),
                "error": "Erreur 500",
            }

    yield {"type": "done"}


def search_ship_analysis_entities(entity_type, query, limit=15, allow_super_titan=True):
    entity_type = str(entity_type or "").strip().lower()
    if entity_type not in {"ship", "corporation", "alliance"}:
        raise EntityError("ship_analysis_invalid:entity_type")

    term = str(query or "").strip()
    if len(term) < 2:
        return []

    try:
        limit = max(1, min(int(limit or 15), 30))
    except (TypeError, ValueError):
        limit = 15

    if entity_type == "ship":
        candidates = search_global_entities(term, limit=limit)
        restricted_ids = set()
        restricted_groups = set()
        if not allow_super_titan:
            with db() as conn:
                restricted_ids, restricted_groups = _restricted_super_titan_catalog(conn)

        results = []
        for item in candidates:
            item_type = str(item.get("entity_type") or "").strip().lower()
            if item_type not in {"ship", "ship_group"}:
                continue

            normalized = dict(item)
            if item_type == "ship_group":
                group_name = str(item.get("name") or item.get("label") or "").strip()
                if not group_name:
                    continue
                if not allow_super_titan and group_name.lower() in restricted_groups:
                    continue
                normalized["entity_id"] = group_name
                normalized["name"] = group_name
                normalized["label"] = group_name
                normalized["subtitle"] = "Ship type"
            else:
                try:
                    ship_id = int(item.get("entity_id"))
                except (TypeError, ValueError):
                    continue
                if not allow_super_titan and ship_id in restricted_ids:
                    continue

            results.append(normalized)
            if len(results) >= limit:
                break
        return results

    if entity_type == "corporation":
        table = "entities.corporations"
        id_column = "corporation_id"
    else:
        table = "entities.alliances"
        id_column = "alliance_id"

    numeric_id = int(term) if term.isdigit() else None
    needle = term.lower()
    contains = f"%{needle}%"
    prefix = f"{needle}%"

    sql = f"""
        SELECT
            {id_column}::bigint,
            COALESCE(NULLIF(name,''), 'Unknown') AS name,
            ticker
        FROM {table}
        WHERE
            {id_column} = %s
            OR lower(COALESCE(name,'')) LIKE %s
            OR lower(COALESCE(ticker,'')) LIKE %s
        ORDER BY
            CASE
                WHEN {id_column} = %s THEN 0
                WHEN lower(COALESCE(ticker,'')) = %s THEN 1
                WHEN lower(COALESCE(name,'')) = %s THEN 2
                WHEN lower(COALESCE(ticker,'')) LIKE %s THEN 3
                ELSE 4
            END,
            lower(COALESCE(name,'')) ASC
        LIMIT %s
    """
    params = (numeric_id, contains, contains, numeric_id, needle, needle, prefix, limit)

    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '1500ms'")
                cur.execute(sql, params)
                rows = cur.fetchall()
    except QueryCanceled:
        return []

    results = []
    for entity_id, name, ticker in rows:
        entity_id = int(entity_id)
        label = f"{name} [{ticker}]" if ticker else name
        image_kind = "corporations" if entity_type == "corporation" else "alliances"
        results.append({
            "entity_type": entity_type,
            "entity_id": entity_id,
            "name": name,
            "ticker": ticker,
            "label": label,
            "subtitle": entity_type.title(),
            "image_url": f"https://images.evetech.net/{image_kind}/{entity_id}/logo?size=32",
        })
    return results
