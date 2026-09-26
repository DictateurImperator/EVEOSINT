#!/usr/bin/env python3
import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values


CONFIG_PATH = Path.home() / "eveosint" / "config" / "db.json"
SCHEMA = "entities"
SHIP_INDEX_TABLE = "pilotable_ship_index"
PILOTABLE_TABLE = "character_pilotable_ships"
DEFAULT_BATCH_SIZE = 5000


log = logging.getLogger("pilotable_ships")


class PilotableShipsError(RuntimeError):
    pass


def load_db_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def db():
    cfg = load_db_config()
    return psycopg2.connect(
        dbname=cfg["db_name"],
        user=cfg["db_user"],
        password=cfg["db_password"],
        host=cfg["db_host"],
        port=cfg["db_port"],
    )


def quote_ident(value):
    value = str(value)
    if not value:
        raise PilotableShipsError("identifier_empty")
    return '"' + value.replace('"', '""') + '"'


def qualified_name(schema, table):
    return f"{quote_ident(schema)}.{quote_ident(table)}"


def parse_table_name(raw_value):
    value = (raw_value or "").strip()
    if not value:
        raise PilotableShipsError("source_table_missing")

    parts = value.split(".")
    if len(parts) == 1:
        return None, parts[0]
    if len(parts) == 2:
        return parts[0], parts[1]
    raise PilotableShipsError("source_table_invalid")


def parse_column_name(raw_value):
    value = (raw_value or "").strip()
    if not value:
        raise PilotableShipsError("source_column_missing")
    if "." in value:
        raise PilotableShipsError("source_column_invalid")
    return value


def relation_sql(raw_table):
    schema, table = parse_table_name(raw_table)
    if schema:
        return qualified_name(schema, table)
    return quote_ident(table)


def ensure_tables(conn):
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {quote_ident(SCHEMA)};")

        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {qualified_name(SCHEMA, SHIP_INDEX_TABLE)} (
            ship_type_id BIGINT PRIMARY KEY,
            bit_index INTEGER UNIQUE NOT NULL,
            ship_name TEXT,
            group_name TEXT,
            faction_name TEXT,
            enabled BOOLEAN NOT NULL DEFAULT TRUE
        );
        """)

        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {qualified_name(SCHEMA, PILOTABLE_TABLE)} (
            character_id BIGINT PRIMARY KEY,
            ship_mask BYTEA NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)

        cur.execute(f"""
        CREATE INDEX IF NOT EXISTS character_pilotable_ships_updated_at_idx
        ON {qualified_name(SCHEMA, PILOTABLE_TABLE)} (updated_at DESC);
        """)

    conn.commit()


def table_exists(conn, schema, table):
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
            (schema, table),
        )
        return bool(cur.fetchone()[0])


def require_source_tables(conn):
    missing = []
    for schema, table in [
        ("sde_work", "ship_entities"),
        ("sde_work", "type_required_skills"),
        ("entities", "character_inferred_skills"),
    ]:
        if not table_exists(conn, schema, table):
            missing.append(f"{schema}.{table}")

    if missing:
        raise PilotableShipsError("missing_table:" + ",".join(missing))


def sync_ship_index(conn):
    """
    Append-only ship_type_id -> bit_index mapping.
    Existing bit indexes are never changed and never reused.
    """
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COALESCE(MAX(bit_index), -1)
            FROM {qualified_name(SCHEMA, SHIP_INDEX_TABLE)}
        """)
        next_bit_index = int(cur.fetchone()[0]) + 1

        cur.execute(f"""
            SELECT se.entity_id::BIGINT, se.name, se.group_name, se.faction_name
            FROM sde_work.ship_entities se
            LEFT JOIN {qualified_name(SCHEMA, SHIP_INDEX_TABLE)} idx
              ON idx.ship_type_id = se.entity_id::BIGINT
            WHERE idx.ship_type_id IS NULL
            ORDER BY se.entity_id::BIGINT
        """)
        new_ships = cur.fetchall()

        if new_ships:
            values = []
            for offset, row in enumerate(new_ships):
                values.append((
                    int(row[0]),
                    next_bit_index + offset,
                    row[1],
                    row[2],
                    row[3],
                    True,
                ))

            execute_values(
                cur,
                f"""
                INSERT INTO {qualified_name(SCHEMA, SHIP_INDEX_TABLE)}
                    (ship_type_id, bit_index, ship_name, group_name, faction_name, enabled)
                VALUES %s
                ON CONFLICT (ship_type_id) DO NOTHING
                """,
                values,
                page_size=1000,
            )

        cur.execute(f"""
            UPDATE {qualified_name(SCHEMA, SHIP_INDEX_TABLE)} idx
            SET
                ship_name = se.name,
                group_name = se.group_name,
                faction_name = se.faction_name,
                enabled = TRUE
            FROM sde_work.ship_entities se
            WHERE idx.ship_type_id = se.entity_id::BIGINT
        """)
        refreshed = cur.rowcount

        cur.execute(f"""
            UPDATE {qualified_name(SCHEMA, SHIP_INDEX_TABLE)} idx
            SET enabled = FALSE
            WHERE NOT EXISTS (
                SELECT 1
                FROM sde_work.ship_entities se
                WHERE se.entity_id::BIGINT = idx.ship_type_id
            )
        """)
        disabled = cur.rowcount

    conn.commit()
    log.info("ship_index synced new=%s refreshed=%s disabled=%s", len(new_ships), refreshed, disabled)


def load_ship_bits(conn):
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT ship_type_id, bit_index
            FROM {qualified_name(SCHEMA, SHIP_INDEX_TABLE)}
            WHERE enabled IS TRUE
            ORDER BY bit_index ASC
        """)
        rows = cur.fetchall()

    ship_bits = {int(ship_type_id): int(bit_index) for ship_type_id, bit_index in rows}
    max_bit = max(ship_bits.values(), default=-1)
    mask_len = (max_bit + 8) // 8 if max_bit >= 0 else 0
    return ship_bits, mask_len


def source_character_count(conn, mode, source_table=None, source_column=None):
    if mode == "init":
        sql = """
            SELECT COUNT(DISTINCT character_id)
            FROM entities.character_inferred_skills
            WHERE character_id IS NOT NULL
        """
        params = ()
    else:
        sql = f"""
            SELECT COUNT(DISTINCT {quote_ident(source_column)})
            FROM {relation_sql(source_table)}
            WHERE {quote_ident(source_column)} IS NOT NULL
        """
        params = ()

    with conn.cursor() as cur:
        cur.execute(sql, params)
        return int(cur.fetchone()[0] or 0)


def iter_character_batches(conn, mode, batch_size, source_table=None, source_column=None):
    last_character_id = -1

    while True:
        if mode == "init":
            sql = """
                SELECT character_id
                FROM (
                    SELECT DISTINCT character_id::BIGINT AS character_id
                    FROM entities.character_inferred_skills
                    WHERE character_id IS NOT NULL
                      AND character_id::BIGINT > %s
                ) src
                ORDER BY character_id
                LIMIT %s
            """
            params = (last_character_id, batch_size)
        else:
            column_sql = quote_ident(source_column)
            sql = f"""
                SELECT character_id
                FROM (
                    SELECT DISTINCT {column_sql}::BIGINT AS character_id
                    FROM {relation_sql(source_table)}
                    WHERE {column_sql} IS NOT NULL
                      AND {column_sql}::BIGINT > %s
                ) src
                ORDER BY character_id
                LIMIT %s
            """
            params = (last_character_id, batch_size)

        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        if not rows:
            break

        character_ids = [int(row[0]) for row in rows if row[0] is not None]
        if not character_ids:
            break

        last_character_id = character_ids[-1]
        yield character_ids


def build_masks_for_batch(conn, character_ids, ship_bits, mask_len):
    if not character_ids:
        return []

    masks = {character_id: bytearray(mask_len) for character_id in character_ids}

    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH req AS (
                SELECT tr.type_id::BIGINT AS ship_type_id, COUNT(*)::INTEGER AS req_count
                FROM sde_work.type_required_skills tr
                JOIN {qualified_name(SCHEMA, SHIP_INDEX_TABLE)} idx
                  ON idx.ship_type_id = tr.type_id::BIGINT
                 AND idx.enabled IS TRUE
                GROUP BY tr.type_id::BIGINT
            ),
            matched AS (
                SELECT
                    cis.character_id::BIGINT AS character_id,
                    tr.type_id::BIGINT AS ship_type_id,
                    COUNT(*)::INTEGER AS matched_count
                FROM sde_work.type_required_skills tr
                JOIN entities.character_inferred_skills cis
                  ON cis.skill_type_id = tr.skill_type_id
                 AND cis.inferred_level >= tr.required_level
                JOIN {qualified_name(SCHEMA, SHIP_INDEX_TABLE)} idx
                  ON idx.ship_type_id = tr.type_id::BIGINT
                 AND idx.enabled IS TRUE
                WHERE cis.character_id = ANY(%s)
                GROUP BY cis.character_id::BIGINT, tr.type_id::BIGINT
            )
            SELECT m.character_id, m.ship_type_id
            FROM matched m
            JOIN req r
              ON r.ship_type_id = m.ship_type_id
             AND r.req_count = m.matched_count
            """,
            (character_ids,),
        )
        rows = cur.fetchall()

    for character_id, ship_type_id in rows:
        bit_index = ship_bits.get(int(ship_type_id))
        if bit_index is None:
            continue
        mask = masks.get(int(character_id))
        if mask is None:
            continue
        byte_index = bit_index // 8
        bit_offset = bit_index % 8
        if byte_index >= len(mask):
            continue
        mask[byte_index] |= 1 << bit_offset

    now = datetime.now(timezone.utc)
    return [
        (character_id, psycopg2.Binary(bytes(mask)), now)
        for character_id, mask in masks.items()
    ]


def upsert_masks(conn, rows):
    if not rows:
        return 0

    with conn.cursor() as cur:
        execute_values(
            cur,
            f"""
            INSERT INTO {qualified_name(SCHEMA, PILOTABLE_TABLE)}
                (character_id, ship_mask, updated_at)
            VALUES %s
            ON CONFLICT (character_id)
            DO UPDATE SET
                ship_mask = EXCLUDED.ship_mask,
                updated_at = EXCLUDED.updated_at
            """,
            rows,
            page_size=1000,
        )

    conn.commit()
    return len(rows)


def run(mode, batch_size, source_table=None, source_column=None):
    if mode == "update":
        if not source_table:
            raise PilotableShipsError("source_table_required_for_update")
        if not source_column:
            raise PilotableShipsError("source_column_required_for_update")
        source_column = parse_column_name(source_column)

    with db() as conn:
        ensure_tables(conn)
        require_source_tables(conn)
        sync_ship_index(conn)
        ship_bits, mask_len = load_ship_bits(conn)

        if not ship_bits:
            raise PilotableShipsError("pilotable_ship_index_empty")

        total = source_character_count(
            conn,
            mode,
            source_table=source_table,
            source_column=source_column,
        )
        log.info("START mode=%s characters=%s ships=%s mask_len=%s batch_size=%s", mode, total, len(ship_bits), mask_len, batch_size)

        processed = 0
        upserted = 0
        for character_ids in iter_character_batches(
            conn,
            mode,
            batch_size,
            source_table=source_table,
            source_column=source_column,
        ):
            rows = build_masks_for_batch(conn, character_ids, ship_bits, mask_len)
            upserted += upsert_masks(conn, rows)
            processed += len(character_ids)
            log.info("progress processed=%s/%s upserted=%s", processed, total, upserted)

        log.info("DONE mode=%s processed=%s upserted=%s", mode, processed, upserted)


def main():
    parser = argparse.ArgumentParser(description="Build pilotable ship masks for EVEOSINT characters.")
    parser.add_argument("--mode", choices=["init", "update"], required=True)
    parser.add_argument("--source-table", help="Update mode only. Table containing character IDs, optionally schema-qualified.")
    parser.add_argument("--source-column", help="Update mode only. Column containing character IDs.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    if args.batch_size < 1:
        raise SystemExit("batch_size_invalid")

    try:
        run(
            mode=args.mode,
            batch_size=args.batch_size,
            source_table=args.source_table,
            source_column=args.source_column,
        )
    except PilotableShipsError as exc:
        log.error(str(exc))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
