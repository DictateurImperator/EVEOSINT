import csv
import io
import re
from datetime import date

from .db import db
from .superintel import (
    DAILY_TABLE_TEMPLATES,
    DETAILED_SHIP_COLUMNS,
    ENTITY_CONFIG,
    REPORT_SCHEMA,
    SuperIntelError,
    same_day_previous_month,
)


EVOLUTION_ENTITY_TYPES = {"alliance", "corporation", "player"}
EVOLUTION_TERM_KINDS = EVOLUTION_ENTITY_TYPES | {"universe", "no_alliance"}
EVOLUTION_MAX_SERIES = 24
EVOLUTION_MAX_TERMS_PER_SERIES = 64
EVOLUTION_TABLE_BATCH_SIZE = 24

_DAILY_PATTERNS = {
    entity_type: re.compile(
        "^"
        + re.escape(template.split("{date_key}", 1)[0])
        + r"(\d{4}_\d{2}_\d{2})$"
    )
    for entity_type, template in DAILY_TABLE_TEMPLATES.items()
}


def _safe_report_table(table_name):
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", table_name or ""):
        raise SuperIntelError(f"unsafe_identifier:{table_name}")
    return f"{REPORT_SCHEMA}.{table_name}"


def _parse_day(value, fallback=None):
    if isinstance(value, date):
        return value
    value = (value or "").strip()
    if not value:
        return fallback
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SuperIntelError(f"invalid_date:{value}") from exc


def _available_daily_tables(conn):
    """Return {day: {entity_type: table_name}} for every daily snapshot."""
    prefixes = [
        template.split("{date_key}", 1)[0]
        for template in DAILY_TABLE_TEMPLATES.values()
    ]

    clauses = " OR ".join(["table_name LIKE %s"] * len(prefixes))
    params = [prefix + "%" for prefix in prefixes]

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s
              AND ({clauses})
            """,
            [REPORT_SCHEMA] + params,
        )
        names = [row[0] for row in cur.fetchall()]

    result = {}
    for table_name in names:
        for entity_type, pattern in _DAILY_PATTERNS.items():
            match = pattern.fullmatch(table_name)
            if not match:
                continue
            try:
                day = date.fromisoformat(match.group(1).replace("_", "-"))
            except ValueError:
                continue
            result.setdefault(day, {})[entity_type] = table_name
            break
    return result


def _table_columns(conn, table_names):
    table_names = sorted(set(table_names))
    if not table_names:
        return {}

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = ANY(%s)
            """,
            (REPORT_SCHEMA, table_names),
        )
        rows = cur.fetchall()

    result = {table_name: set() for table_name in table_names}
    for table_name, column_name in rows:
        result.setdefault(table_name, set()).add(column_name)
    return result


def _normalise_hulls(raw_hulls):
    allowed = {ship["key"] for ship in DETAILED_SHIP_COLUMNS}
    if not isinstance(raw_hulls, list):
        return [ship["key"] for ship in DETAILED_SHIP_COLUMNS]

    selected = []
    seen = set()
    for value in raw_hulls:
        key = str(value or "").strip().lower()
        if key in allowed and key not in seen:
            seen.add(key)
            selected.append(key)

    if not selected:
        return [ship["key"] for ship in DETAILED_SHIP_COLUMNS]
    return selected


def _normalise_term(raw):
    if not isinstance(raw, dict):
        raise SuperIntelError("invalid_evolution_term")

    kind = str(raw.get("kind") or "").strip().lower()
    if kind == "character":
        kind = "player"
    if kind not in EVOLUTION_TERM_KINDS:
        raise SuperIntelError(f"invalid_evolution_term_kind:{kind}")

    raw_sign = raw.get("sign", 1)
    sign = -1 if str(raw_sign).strip() in {"-1", "-", "subtract"} else 1

    entity_id = raw.get("entity_id")
    if kind in EVOLUTION_ENTITY_TYPES:
        try:
            entity_id = int(entity_id)
        except (TypeError, ValueError) as exc:
            raise SuperIntelError(f"invalid_evolution_entity_id:{entity_id}") from exc
    else:
        entity_id = None

    if kind == "universe":
        default_name = "UNIVERSE"
    elif kind == "no_alliance":
        default_name = "NO_ALLIANCE"
    else:
        default_name = str(entity_id)

    name = str(raw.get("name") or default_name).strip()[:160] or default_name
    ticker = str(raw.get("ticker") or "").strip()[:40]

    return {
        "kind": kind,
        "entity_id": entity_id,
        "name": name,
        "ticker": ticker,
        "sign": sign,
    }


def _term_expression_name(term):
    if term["kind"] == "universe":
        return "UNIVERSE"
    if term["kind"] == "no_alliance":
        return "NO_ALLIANCE"
    if term.get("ticker"):
        return f'{term["name"]} [{term["ticker"]}]'
    return term["name"]


def _normalise_series(raw_series, fallback_hulls):
    if not isinstance(raw_series, list):
        raw_series = []

    result = []
    used_names = {}

    for index, raw in enumerate(raw_series[:EVOLUTION_MAX_SERIES]):
        if not isinstance(raw, dict):
            continue

        raw_terms = raw.get("terms") or []
        if not isinstance(raw_terms, list):
            continue

        terms = [
            _normalise_term(term)
            for term in raw_terms[:EVOLUTION_MAX_TERMS_PER_SERIES]
        ]
        if not terms:
            continue

        raw_hulls = raw.get("hulls")
        if isinstance(raw_hulls, list):
            series_hulls = _normalise_hulls(raw_hulls)
        else:
            series_hulls = list(fallback_hulls)

        requested_name = str(raw.get("name") or "").strip()[:160]
        if not requested_name:
            requested_name = " ".join(
                (
                    ("+" if term["sign"] > 0 else "-")
                    + " "
                    + _term_expression_name(term)
                )
                for term in terms
            ).lstrip("+ ").strip()
        if not requested_name:
            requested_name = f"Series {index + 1}"

        count = used_names.get(requested_name, 0) + 1
        used_names[requested_name] = count
        display_name = requested_name if count == 1 else f"{requested_name} ({count})"

        result.append({
            "name": display_name,
            "terms": terms,
            "hulls": series_hulls,
        })

    if not result:
        result = [{
            "name": "UNIVERSE",
            "terms": [{
                "kind": "universe",
                "entity_id": None,
                "name": "UNIVERSE",
                "ticker": "",
                "sign": 1,
            }],
            "hulls": list(fallback_hulls),
        }]

    return result


def _sum_expression(columns, hull_keys):
    parts = [
        f'COALESCE("{key}", 0)::BIGINT'
        for key in hull_keys
        if key in columns
    ]
    if not parts:
        return "0::BIGINT"
    return "(" + " + ".join(parts) + ")"


def _no_alliance_predicate(columns):
    parts = []
    if "alliance_id" in columns:
        parts.append('COALESCE("alliance_id", 0) = 0')
    if "alliance_name" in columns:
        parts.append(
            "UPPER(COALESCE(\"alliance_name\", '')) IN "
            "('NO_ALLIANCE', 'NONE', 'UNKNOWN', '')"
        )
    if not parts:
        return None
    return "(" + " OR ".join(parts) + ")"


def _needed_entity_types(series):
    needed = set()
    for item in series:
        for term in item["terms"]:
            if term["kind"] in {"universe", "no_alliance", "player"}:
                needed.add("player")
            elif term["kind"] in {"corporation", "alliance"}:
                needed.add(term["kind"])
    return needed or {"player"}


def _specific_ids(series, kind):
    return sorted({
        int(term["entity_id"])
        for item in series
        for term in item["terms"]
        if term["kind"] == kind and term.get("entity_id") is not None
    })


def _chunks(items, size=EVOLUTION_TABLE_BATCH_SIZE):
    for index in range(0, len(items), size):
        yield items[index:index + size]


def _special_term_usage(series):
    kinds = {
        term["kind"]
        for item in series
        for term in item["terms"]
        if term["kind"] in {"universe", "no_alliance"}
    }
    return "universe" in kinds, "no_alliance" in kinds


def _series_hull_signatures(series):
    """Return distinct hull selections in stable order."""
    result = []
    seen = set()

    for item in series:
        signature = tuple(item.get("hulls") or [])
        if not signature or signature in seen:
            continue
        seen.add(signature)
        result.append(signature)

    return result


def _fetch_evolution_values(
    conn,
    days,
    table_map,
    columns_by_table,
    hull_keys,
    series,
):
    """Fetch one scalar per distinct hull selection instead of raw hull columns.

    This preserves per-series ship selections while keeping the old fast query
    shape.  Example: if five series all use "Titans", PostgreSQL computes the
    Titans expression once per entity/snapshot, rather than returning every
    individual ship column and making Python recompute it afterwards.
    """
    values_by_day = {day: {} for day in days}
    unavailable_by_day = {day: set() for day in days}
    need_universe, need_no_alliance = _special_term_usage(series)

    hull_signatures = _series_hull_signatures(series)
    if not hull_signatures:
        hull_signatures = [tuple(hull_keys)]

    zero_values = {signature: 0 for signature in hull_signatures}

    for kind in ("alliance", "corporation", "player"):
        ids = _specific_ids(series, kind)
        needs_special_player = (
            kind == "player" and (need_universe or need_no_alliance)
        )
        if not ids and not needs_special_player:
            continue

        if ids:
            id_column = ENTITY_CONFIG[kind]["id_column"]
            query_items = []

            for day in days:
                table_name = table_map.get(day, {}).get(kind)
                if not table_name:
                    unavailable_by_day[day].add(kind)
                    continue

                columns = columns_by_table.get(table_name, set())
                if id_column not in columns:
                    unavailable_by_day[day].add(kind)
                    continue

                for entity_id in ids:
                    values_by_day[day][(kind, entity_id)] = dict(zero_values)

                query_items.append((day, table_name, columns))

            for batch in _chunks(query_items):
                selects = []
                params = []

                for day, table_name, columns in batch:
                    relation = _safe_report_table(table_name)
                    value_selects = [
                        f"{_sum_expression(columns, signature)}::BIGINT AS value_{index}"
                        for index, signature in enumerate(hull_signatures)
                    ]

                    selects.append(
                        f"""SELECT %s::date AS snapshot_day,
                                  "{id_column}"::BIGINT AS entity_id,
                                  {", ".join(value_selects)}
                           FROM {relation}
                           WHERE "{id_column}" = ANY(%s)"""
                    )
                    params.extend([day, ids])

                with conn.cursor() as cur:
                    cur.execute(" UNION ALL ".join(selects), params)
                    for row in cur.fetchall():
                        snapshot_day = row[0]
                        entity_id = int(row[1])
                        values_by_day[snapshot_day][(kind, entity_id)] = {
                            signature: int(row[2 + index] or 0)
                            for index, signature in enumerate(hull_signatures)
                        }

        if not needs_special_player:
            continue

        special_items = []
        for day in days:
            table_name = table_map.get(day, {}).get("player")
            if not table_name:
                unavailable_by_day[day].add("player")
                continue

            columns = columns_by_table.get(table_name, set())
            predicate = _no_alliance_predicate(columns)

            if need_no_alliance and predicate is None:
                values_by_day[day][("no_alliance", None)] = None
                if not need_universe:
                    continue

            special_items.append((day, table_name, columns, predicate))

        for batch in _chunks(special_items):
            selects = []
            params = []

            for day, table_name, columns, predicate in batch:
                relation = _safe_report_table(table_name)
                universe_selects = []
                no_alliance_selects = []

                for signature in hull_signatures:
                    expression = _sum_expression(columns, signature)

                    universe_selects.append(
                        f"COALESCE(SUM({expression}), 0)::BIGINT"
                        if need_universe
                        else "NULL::BIGINT"
                    )
                    no_alliance_selects.append(
                        (
                            f"COALESCE(SUM({expression}) FILTER (WHERE {predicate}), 0)::BIGINT"
                            if need_no_alliance and predicate is not None
                            else "NULL::BIGINT"
                        )
                    )

                selects.append(
                    f"""SELECT %s::date AS snapshot_day,
                               {", ".join(universe_selects + no_alliance_selects)}
                        FROM {relation}"""
                )
                params.append(day)

            with conn.cursor() as cur:
                cur.execute(" UNION ALL ".join(selects), params)
                for row in cur.fetchall():
                    snapshot_day = row[0]
                    width = len(hull_signatures)

                    if need_universe:
                        universe_values = row[1:1 + width]
                        values_by_day[snapshot_day][("universe", None)] = {
                            signature: int(universe_values[index] or 0)
                            for index, signature in enumerate(hull_signatures)
                        }

                    if need_no_alliance:
                        no_values = row[1 + width:1 + (2 * width)]
                        if not no_values or all(value is None for value in no_values):
                            values_by_day[snapshot_day][("no_alliance", None)] = None
                        else:
                            values_by_day[snapshot_day][("no_alliance", None)] = {
                                signature: int(no_values[index] or 0)
                                for index, signature in enumerate(hull_signatures)
                            }

    return values_by_day, unavailable_by_day


def _series_value(item, values, unavailable_kinds):
    total = 0
    signature = tuple(item.get("hulls") or [])

    for term in item["terms"]:
        kind = term["kind"]
        snapshot_kind = "player" if kind in {"universe", "no_alliance", "player"} else kind

        if snapshot_kind in unavailable_kinds:
            return None

        key = (
            (kind, None)
            if kind in {"universe", "no_alliance"}
            else (kind, int(term["entity_id"]))
        )
        hull_values = values.get(key)

        if hull_values is None:
            return None

        value = int(hull_values.get(signature, 0) or 0)
        total += int(term["sign"]) * value

    return total


def _available_bounds(table_map):
    days = sorted(table_map)
    if not days:
        raise SuperIntelError("superintel_no_daily_snapshots")
    return days[0], days[-1]


def get_evolution_page_context():
    conn = db()
    try:
        conn.autocommit = True
        table_map = _available_daily_tables(conn)
        earliest, latest = _available_bounds(table_map)
    finally:
        conn.close()

    default_from = same_day_previous_month(latest)
    if default_from < earliest:
        default_from = earliest

    return {
        "ship_columns": DETAILED_SHIP_COLUMNS,
        "available_from": earliest,
        "available_to": latest,
        "default_from": default_from,
        "default_to": latest,
        "default_hulls": [ship["key"] for ship in DETAILED_SHIP_COLUMNS],
        "default_series": [{
            "name": "UNIVERSE",
            "terms": [{
                "kind": "universe",
                "entity_id": None,
                "name": "UNIVERSE",
                "ticker": "",
                "sign": 1,
            }],
            "hulls": [ship["key"] for ship in DETAILED_SHIP_COLUMNS],
        }],
    }


def build_evolution_data(payload):
    if not isinstance(payload, dict):
        raise SuperIntelError("invalid_evolution_payload")

    hull_keys = _normalise_hulls(payload.get("hulls"))
    series = _normalise_series(payload.get("series"), hull_keys)
    needed_hull_keys = [
        ship["key"]
        for ship in DETAILED_SHIP_COLUMNS
        if any(ship["key"] in item.get("hulls", []) for item in series)
    ]

    conn = db()
    try:
        conn.autocommit = True
        table_map = _available_daily_tables(conn)
        earliest, latest = _available_bounds(table_map)

        date_to = _parse_day(payload.get("date_to"), latest)
        date_from = _parse_day(payload.get("date_from"), same_day_previous_month(date_to))

        if date_to < date_from:
            raise SuperIntelError("invalid_date_range")
        if date_to < earliest or date_from > latest:
            raise SuperIntelError("evolution_range_outside_snapshots")

        # Clamp only to real report availability; never fabricate history.
        effective_from = max(date_from, earliest)
        effective_to = min(date_to, latest)

        needed_types = _needed_entity_types(series)

        all_days = [
            day
            for day in sorted(table_map)
            if effective_from <= day <= effective_to
            and any(kind in table_map[day] for kind in needed_types)
        ]
        if not all_days:
            raise SuperIntelError("superintel_no_daily_snapshots_in_range")

        # Progressive mode is opt-in so the existing CSV/backend behavior
        # remains compatible. The browser requests small chronological chunks
        # and renders each chunk immediately; no snapshot day is discarded.
        progressive = "cursor" in payload or "chunk_size" in payload
        if progressive:
            try:
                cursor = int(payload.get("cursor", 0))
                chunk_size = int(payload.get("chunk_size", 120))
            except (TypeError, ValueError) as exc:
                raise SuperIntelError("invalid_evolution_cursor") from exc

            if cursor < 0 or cursor >= len(all_days):
                raise SuperIntelError("invalid_evolution_cursor")
            chunk_size = max(1, min(chunk_size, 240))
            days = all_days[cursor:cursor + chunk_size]
        else:
            cursor = 0
            days = all_days

        next_cursor = cursor + len(days)
        done = next_cursor >= len(all_days)

        relevant_tables = [
            table_map[day][kind]
            for day in days
            for kind in needed_types
            if kind in table_map[day]
        ]
        columns_by_table = _table_columns(conn, relevant_tables)

        values_by_day, unavailable_by_day = _fetch_evolution_values(
            conn,
            days,
            table_map,
            columns_by_table,
            needed_hull_keys,
            series,
        )

        output_rows = []
        incomplete_days = []

        for day in days:
            values = values_by_day[day]
            unavailable = unavailable_by_day[day]

            row = {"date": day.isoformat()}
            has_missing = False
            for index, item in enumerate(series):
                value = _series_value(item, values, unavailable)
                row[f"s{index}"] = value
                if value is None:
                    has_missing = True

            if has_missing:
                incomplete_days.append(day.isoformat())

            output_rows.append(row)
    finally:
        conn.close()

    selected_ship_rows = [
        ship
        for ship in DETAILED_SHIP_COLUMNS
        if ship["key"] in hull_keys
    ]

    return {
        "date_from": effective_from.isoformat(),
        "date_to": effective_to.isoformat(),
        "requested_date_from": date_from.isoformat(),
        "requested_date_to": date_to.isoformat(),
        "available_from": earliest.isoformat(),
        "available_to": latest.isoformat(),
        "hulls": hull_keys,
        "hull_labels": [ship["label"] for ship in selected_ship_rows],
        "series": [
            {
                "key": f"s{index}",
                "name": item["name"],
                "terms": item["terms"],
                "hulls": item["hulls"],
                "hull_labels": [
                    ship["label"]
                    for ship in DETAILED_SHIP_COLUMNS
                    if ship["key"] in item["hulls"]
                ],
            }
            for index, item in enumerate(series)
        ],
        "rows": output_rows,
        "incomplete_days": incomplete_days,
        "cursor": cursor,
        "next_cursor": next_cursor,
        "done": done,
        "total_snapshots": len(all_days),
    }


def _csv_safe(value):
    if value is None:
        return ""

    text = str(value)
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text

    return text


def build_evolution_csv(payload):
    data = build_evolution_data(payload)

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [_csv_safe("date")]
        + [_csv_safe(item["name"]) for item in data["series"]]
    )

    for row in data["rows"]:
        writer.writerow(
            [_csv_safe(row["date"])]
            + [
                _csv_safe("" if row.get(item["key"]) is None else row.get(item["key"]))
                for item in data["series"]
            ]
        )

    filename = (
        f"superintel_evolution_"
        f"{data['date_from']}_to_{data['date_to']}.csv"
    )
    return filename, output.getvalue().encode("utf-8-sig")
