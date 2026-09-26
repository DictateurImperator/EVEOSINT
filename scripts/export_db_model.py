#!/usr/bin/env python3
import argparse
import json
from pathlib import Path

import psycopg2


DEFAULT_CONFIG = Path.home() / "eveosint" / "config" / "db.json"
DEFAULT_OUTPUT = Path("database_model.md")


def load_config(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def connect_db(cfg):
    conn = psycopg2.connect(
        dbname=cfg["db_name"],
        user=cfg["db_user"],
        password=cfg["db_password"],
        host=cfg["db_host"],
        port=cfg["db_port"],
    )
    conn.set_session(readonly=True, autocommit=True)
    return conn


def fetchall(conn, sql, params=None):
    with conn.cursor() as cur:
        if params is None:
            cur.execute(sql)
        else:
            cur.execute(sql, params)
        return cur.fetchall()


def md_escape(value):
    if value is None:
        return ""
    return str(value).replace("|", r"\|").replace("\n", " ")


def write_table(out, headers, rows):
    out.append("| " + " | ".join(headers) + " |")
    out.append("| " + " | ".join(["---"] * len(headers)) + " |")
    for row in rows:
        out.append("| " + " | ".join(md_escape(v) for v in row) + " |")
    out.append("")


def main():
    parser = argparse.ArgumentParser(
        description="Export PostgreSQL database structure to Markdown (read-only)."
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help=f"DB config JSON path (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help=f"Output Markdown file (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()

    config_path = Path(args.config).expanduser()
    output_path = Path(args.output).expanduser()

    cfg = load_config(config_path)

    out = []
    out.append("# PostgreSQL database model")
    out.append("")
    out.append("> Structure-only export. No business rows and no database password are included.")
    out.append("")

    conn = connect_db(cfg)

    try:
        db_info = fetchall(
            conn,
            """
            SELECT
                current_database(),
                current_user,
                version()
            """
        )[0]

        out.append("## Database")
        out.append("")
        write_table(
            out,
            ["Database", "Connected user", "PostgreSQL version"],
            [db_info],
        )

        schemas = fetchall(
            conn,
            """
            SELECT nspname
            FROM pg_namespace
            WHERE nspname NOT IN ('pg_catalog', 'information_schema')
              AND nspname NOT LIKE 'pg_toast%'
              AND nspname NOT LIKE 'pg_temp_%'
            ORDER BY nspname
            """
        )

        out.append("## Schemas")
        out.append("")
        write_table(out, ["Schema"], schemas)

        tables = fetchall(
            conn,
            """
            SELECT
                n.nspname AS schema_name,
                c.relname AS table_name,
                CASE c.relkind
                    WHEN 'r' THEN 'table'
                    WHEN 'p' THEN 'partitioned table'
                    WHEN 'f' THEN 'foreign table'
                    ELSE c.relkind::text
                END AS object_type,
                c.reltuples::bigint AS estimated_rows,
                pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p', 'f')
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname NOT LIKE 'pg_toast%'
              AND n.nspname NOT LIKE 'pg_temp_%'
            ORDER BY n.nspname, c.relname
            """
        )

        out.append("## Tables")
        out.append("")
        write_table(
            out,
            ["Schema", "Table", "Type", "Estimated rows", "Total size"],
            tables,
        )

        columns = fetchall(
            conn,
            """
            SELECT
                c.table_schema,
                c.table_name,
                c.ordinal_position,
                c.column_name,
                c.data_type,
                c.udt_name,
                c.character_maximum_length,
                c.numeric_precision,
                c.numeric_scale,
                c.is_nullable,
                c.column_default
            FROM information_schema.columns c
            WHERE c.table_schema NOT IN ('pg_catalog', 'information_schema')
              AND c.table_schema NOT LIKE 'pg_toast%'
              AND c.table_schema NOT LIKE 'pg_temp_%'
            ORDER BY c.table_schema, c.table_name, c.ordinal_position
            """
        )

        out.append("## Columns")
        out.append("")
        write_table(
            out,
            [
                "Schema",
                "Table",
                "#",
                "Column",
                "Data type",
                "UDT",
                "Max length",
                "Precision",
                "Scale",
                "Nullable",
                "Default",
            ],
            columns,
        )

        constraints = fetchall(
            conn,
            """
            SELECT
                ns.nspname AS schema_name,
                tbl.relname AS table_name,
                con.conname AS constraint_name,
                CASE con.contype
                    WHEN 'p' THEN 'PRIMARY KEY'
                    WHEN 'f' THEN 'FOREIGN KEY'
                    WHEN 'u' THEN 'UNIQUE'
                    WHEN 'c' THEN 'CHECK'
                    WHEN 'x' THEN 'EXCLUDE'
                    ELSE con.contype::text
                END AS constraint_type,
                pg_get_constraintdef(con.oid, true) AS definition
            FROM pg_constraint con
            JOIN pg_class tbl ON tbl.oid = con.conrelid
            JOIN pg_namespace ns ON ns.oid = tbl.relnamespace
            WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
              AND ns.nspname NOT LIKE 'pg_toast%'
              AND ns.nspname NOT LIKE 'pg_temp_%'
            ORDER BY ns.nspname, tbl.relname, constraint_type, con.conname
            """
        )

        out.append("## Constraints")
        out.append("")
        write_table(
            out,
            ["Schema", "Table", "Constraint", "Type", "Definition"],
            constraints,
        )

        foreign_keys = fetchall(
            conn,
            """
            SELECT
                src_ns.nspname AS source_schema,
                src.relname AS source_table,
                con.conname AS fk_name,
                pg_get_constraintdef(con.oid, true) AS definition,
                dst_ns.nspname AS target_schema,
                dst.relname AS target_table
            FROM pg_constraint con
            JOIN pg_class src ON src.oid = con.conrelid
            JOIN pg_namespace src_ns ON src_ns.oid = src.relnamespace
            JOIN pg_class dst ON dst.oid = con.confrelid
            JOIN pg_namespace dst_ns ON dst_ns.oid = dst.relnamespace
            WHERE con.contype = 'f'
              AND src_ns.nspname NOT IN ('pg_catalog', 'information_schema')
            ORDER BY src_ns.nspname, src.relname, con.conname
            """
        )

        out.append("## Foreign-key relationships")
        out.append("")
        write_table(
            out,
            ["Source schema", "Source table", "FK", "Definition", "Target schema", "Target table"],
            foreign_keys,
        )

        indexes = fetchall(
            conn,
            """
            SELECT
                schemaname,
                tablename,
                indexname,
                indexdef
            FROM pg_indexes
            WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
              AND schemaname NOT LIKE 'pg_toast%'
            ORDER BY schemaname, tablename, indexname
            """
        )

        out.append("## Indexes")
        out.append("")
        write_table(
            out,
            ["Schema", "Table", "Index", "Definition"],
            indexes,
        )

        partitions = fetchall(
            conn,
            """
            SELECT
                parent_ns.nspname AS parent_schema,
                parent.relname AS parent_table,
                child_ns.nspname AS child_schema,
                child.relname AS child_table,
                pg_get_expr(child.relpartbound, child.oid, true) AS partition_bound
            FROM pg_inherits i
            JOIN pg_class parent ON parent.oid = i.inhparent
            JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
            JOIN pg_class child ON child.oid = i.inhrelid
            JOIN pg_namespace child_ns ON child_ns.oid = child.relnamespace
            WHERE parent_ns.nspname NOT IN ('pg_catalog', 'information_schema')
            ORDER BY parent_ns.nspname, parent.relname, child.relname
            """
        )

        out.append("## Partitions / inheritance")
        out.append("")
        write_table(
            out,
            ["Parent schema", "Parent table", "Child schema", "Child table", "Partition bound"],
            partitions,
        )

        views = fetchall(
            conn,
            """
            SELECT
                schemaname,
                viewname,
                definition
            FROM pg_views
            WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
            ORDER BY schemaname, viewname
            """
        )

        out.append("## Views")
        out.append("")
        write_table(
            out,
            ["Schema", "View", "Definition"],
            views,
        )

        matviews = fetchall(
            conn,
            """
            SELECT
                schemaname,
                matviewname,
                definition
            FROM pg_matviews
            WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
            ORDER BY schemaname, matviewname
            """
        )

        out.append("## Materialized views")
        out.append("")
        write_table(
            out,
            ["Schema", "Materialized view", "Definition"],
            matviews,
        )

        sequences = fetchall(
            conn,
            """
            SELECT
                sequence_schema,
                sequence_name,
                data_type,
                start_value,
                minimum_value,
                maximum_value,
                increment
            FROM information_schema.sequences
            WHERE sequence_schema NOT IN ('pg_catalog', 'information_schema')
            ORDER BY sequence_schema, sequence_name
            """
        )

        out.append("## Sequences")
        out.append("")
        write_table(
            out,
            [
                "Schema",
                "Sequence",
                "Data type",
                "Start",
                "Min",
                "Max",
                "Increment",
            ],
            sequences,
        )

        routines = fetchall(
            conn,
            """
            SELECT
                n.nspname AS schema_name,
                p.proname AS routine_name,
                pg_get_function_identity_arguments(p.oid) AS arguments,
                pg_get_function_result(p.oid) AS result_type,
                CASE p.prokind
                    WHEN 'f' THEN 'function'
                    WHEN 'p' THEN 'procedure'
                    WHEN 'a' THEN 'aggregate'
                    WHEN 'w' THEN 'window'
                    ELSE p.prokind::text
                END AS routine_type,
                l.lanname AS language
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            JOIN pg_language l ON l.oid = p.prolang
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname NOT LIKE 'pg_toast%'
              AND n.nspname NOT LIKE 'pg_temp_%'
            ORDER BY n.nspname, p.proname, arguments
            """
        )

        out.append("## Functions and procedures")
        out.append("")
        out.append("> Signatures only. Function/procedure source code is intentionally excluded.")
        out.append("")
        write_table(
            out,
            ["Schema", "Name", "Arguments", "Returns", "Type", "Language"],
            routines,
        )

        triggers = fetchall(
            conn,
            """
            SELECT
                n.nspname AS schema_name,
                c.relname AS table_name,
                t.tgname AS trigger_name,
                pg_get_triggerdef(t.oid, true) AS definition
            FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE NOT t.tgisinternal
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
            ORDER BY n.nspname, c.relname, t.tgname
            """
        )

        out.append("## Triggers")
        out.append("")
        write_table(
            out,
            ["Schema", "Table", "Trigger", "Definition"],
            triggers,
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(out), encoding="utf-8")

        print(f"OK: {output_path.resolve()}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
