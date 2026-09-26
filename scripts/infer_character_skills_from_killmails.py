#!/usr/bin/env python3
import argparse
import json
import logging
import sys
import time
from pathlib import Path

import psycopg2


CONFIG_PATH = Path.home() / "eveosint" / "config" / "db.json"
BASE_DIR = Path.home() / "eveosint"
LOG_DIR = BASE_DIR / "data" / "logs"
LOG_FILE = LOG_DIR / "character_skill_inference.log"

RAWKM_SCHEMA = "rawkm"
ENTITIES_SCHEMA = "entities"

SKILLS_TABLE = "character_inferred_skills"
DONE_TABLE = "character_skill_inference_killmails_done"


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


logger = logging.getLogger(__name__)


def load_db_config() -> dict:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"Missing DB config file: {CONFIG_PATH}")

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def db_connect():
    cfg = load_db_config()
    return psycopg2.connect(
        dbname=cfg["db_name"],
        user=cfg["db_user"],
        password=cfg["db_password"],
        host=cfg["db_host"],
        port=cfg["db_port"],
    )


def table_exists(conn, schema: str, table: str) -> bool:
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


def ensure_tables(conn) -> None:
    if not table_exists(conn, "sde_work", "type_required_skills"):
        raise RuntimeError("Missing required table sde_work.type_required_skills")

    if not table_exists(conn, "sde_work", "killmail_item_flags"):
        raise RuntimeError("Missing required table sde_work.killmail_item_flags")

    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {ENTITIES_SCHEMA};")

        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {ENTITIES_SCHEMA}.{SKILLS_TABLE} (
            character_id bigint NOT NULL,
            skill_type_id bigint NOT NULL,
            inferred_level integer NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (character_id, skill_type_id)
        );
        """)

        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {ENTITIES_SCHEMA}.{DONE_TABLE} (
            killmail_id bigint PRIMARY KEY,
            processed_at timestamptz NOT NULL DEFAULT now()
        );
        """)

        cur.execute(f"""
        CREATE INDEX IF NOT EXISTS {SKILLS_TABLE}_skill_idx
        ON {ENTITIES_SCHEMA}.{SKILLS_TABLE} (skill_type_id);
        """)

        cur.execute(f"""
        CREATE INDEX IF NOT EXISTS {DONE_TABLE}_processed_at_idx
        ON {ENTITIES_SCHEMA}.{DONE_TABLE} (processed_at);
        """)

    conn.commit()


def rawkm_partitions(conn, base_table: str) -> list[str]:
    prefix = f"{base_table}_"
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = %s
              AND tablename LIKE %s
            ORDER BY tablename
            """,
            (RAWKM_SCHEMA, prefix + "%"),
        )
        return [row[0] for row in cur.fetchall()]


def month_suffix_from_table(table_name: str, base_table: str) -> str | None:
    prefix = f"{base_table}_"
    if not table_name.startswith(prefix):
        return None

    suffix = table_name[len(prefix):]
    parts = suffix.split("_")
    if len(parts) != 2:
        return None

    year, month = parts
    if not (year.isdigit() and month.isdigit() and len(year) == 4 and len(month) == 2):
        return None

    return suffix


def available_months(conn) -> list[str]:
    killmail_tables = rawkm_partitions(conn, "killmails")
    attacker_tables = set(rawkm_partitions(conn, "killmail_attackers"))
    item_tables = set(rawkm_partitions(conn, "killmail_items"))

    months = []
    for table in killmail_tables:
        suffix = month_suffix_from_table(table, "killmails")
        if suffix is None:
            continue

        if f"killmail_attackers_{suffix}" not in attacker_tables:
            continue
        if f"killmail_items_{suffix}" not in item_tables:
            continue

        months.append(suffix)

    return months


def process_month(conn, suffix: str, progress_batch_size: int, limit: int | None) -> int:
    killmails_table = f"{RAWKM_SCHEMA}.killmails_{suffix}"
    attackers_table = f"{RAWKM_SCHEMA}.killmail_attackers_{suffix}"
    items_table = f"{RAWKM_SCHEMA}.killmail_items_{suffix}"

    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH todo AS (
                SELECT k.killmail_id
                FROM {killmails_table} k
                LEFT JOIN {ENTITIES_SCHEMA}.{DONE_TABLE} d
                  ON d.killmail_id = k.killmail_id
                WHERE d.killmail_id IS NULL
                ORDER BY k.killmail_id
                LIMIT %s
            ),
            kill_context AS (
                SELECT
                    k.killmail_id,
                    k.victim_character_id,
                    k.victim_ship_type_id,
                    vc.data->'name'->>'en' AS victim_category_name
                FROM todo td
                JOIN {killmails_table} k
                  ON k.killmail_id = td.killmail_id
                LEFT JOIN public.sde_types vt
                  ON vt.sde_key = k.victim_ship_type_id::text
                LEFT JOIN public.sde_groups vg
                  ON vg.sde_key = vt.data->>'groupID'
                LEFT JOIN public.sde_categories vc
                  ON vc.sde_key = vg.data->>'categoryID'
            ),
            victim_ship_sources AS (
                SELECT
                    kc.killmail_id,
                    kc.victim_character_id AS character_id,
                    kc.victim_ship_type_id AS type_id
                FROM kill_context kc
                WHERE kc.victim_category_name = 'Ship'
                  AND kc.victim_character_id IS NOT NULL
                  AND kc.victim_ship_type_id IS NOT NULL
            ),
            attacker_sources AS (
                SELECT
                    a.killmail_id,
                    a.character_id,
                    src.type_id
                FROM todo td
                JOIN {attackers_table} a
                  ON a.killmail_id = td.killmail_id
                LEFT JOIN public.sde_types st
                  ON st.sde_key = a.ship_type_id::text
                LEFT JOIN public.sde_groups sg
                  ON sg.sde_key = st.data->>'groupID'
                LEFT JOIN public.sde_categories sc
                  ON sc.sde_key = sg.data->>'categoryID'
                CROSS JOIN LATERAL (
                    VALUES
                        (a.ship_type_id),
                        (a.weapon_type_id)
                ) AS src(type_id)
                WHERE sc.data->'name'->>'en' = 'Ship'
                  AND a.character_id IS NOT NULL
                  AND src.type_id IS NOT NULL
            ),
            victim_item_sources AS (
                SELECT
                    kc.killmail_id,
                    kc.victim_character_id AS character_id,
                    i.item_type_id AS type_id
                FROM kill_context kc
                JOIN {items_table} i
                  ON i.killmail_id = kc.killmail_id
                JOIN sde_work.killmail_item_flags f
                  ON f.flag_id = i.flag
                 AND f.include_for_skill_inference IS TRUE
                WHERE kc.victim_category_name = 'Ship'
                  AND kc.victim_character_id IS NOT NULL
                  AND i.item_type_id IS NOT NULL
            ),
            all_sources AS (
                SELECT * FROM victim_ship_sources
                UNION ALL
                SELECT * FROM attacker_sources
                UNION ALL
                SELECT * FROM victim_item_sources
            ),
            inferred AS (
                SELECT
                    s.character_id,
                    trs.skill_type_id,
                    MAX(trs.required_level)::integer AS inferred_level
                FROM all_sources s
                JOIN sde_work.type_required_skills trs
                  ON trs.type_id = s.type_id
                GROUP BY s.character_id, trs.skill_type_id
            ),
            upserted AS (
                INSERT INTO {ENTITIES_SCHEMA}.{SKILLS_TABLE} (
                    character_id,
                    skill_type_id,
                    inferred_level,
                    updated_at
                )
                SELECT
                    character_id,
                    skill_type_id,
                    inferred_level,
                    now()
                FROM inferred
                ON CONFLICT (character_id, skill_type_id)
                DO UPDATE SET
                    inferred_level = GREATEST(
                        {ENTITIES_SCHEMA}.{SKILLS_TABLE}.inferred_level,
                        EXCLUDED.inferred_level
                    ),
                    updated_at = CASE
                        WHEN EXCLUDED.inferred_level > {ENTITIES_SCHEMA}.{SKILLS_TABLE}.inferred_level
                            THEN now()
                        ELSE {ENTITIES_SCHEMA}.{SKILLS_TABLE}.updated_at
                    END
                RETURNING 1
            ),
            marked_done AS (
                INSERT INTO {ENTITIES_SCHEMA}.{DONE_TABLE} (
                    killmail_id,
                    processed_at
                )
                SELECT killmail_id, now()
                FROM todo
                ON CONFLICT (killmail_id) DO NOTHING
                RETURNING killmail_id
            )
            SELECT
                COUNT(*)::bigint AS processed_count,
                COALESCE(MAX(killmail_id), 0)::bigint AS last_killmail_id
            FROM marked_done;
            """,
            (progress_batch_size,),
        )
        processed_count, last_killmail_id = cur.fetchone()

    conn.commit()
    processed_count = int(processed_count or 0)

    if processed_count > 0:
        logger.info(
            "MONTH %s batch_done processed=%s last_killmail_id=%s",
            suffix,
            processed_count,
            int(last_killmail_id or 0),
        )

    return processed_count


def main() -> None:
    configure_logging()

    parser = argparse.ArgumentParser(
        description="Infer minimum character skills from all unprocessed killmails."
    )
    parser.add_argument(
        "--progress-batch-size",
        type=int,
        default=1000,
        help="Number of killmails processed per progress log SQL chunk.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional max killmails to process for this run.",
    )

    args = parser.parse_args()

    if args.progress_batch_size < 1:
        raise RuntimeError("--progress-batch-size must be greater than 0")
    if args.progress_batch_size > 100000:
        raise RuntimeError("--progress-batch-size must be <= 100000")
    if args.limit is not None and args.limit < 1:
        raise RuntimeError("--limit must be greater than 0")

    logger.info(
        "START character skill inference progress_batch_size=%s limit=%s",
        args.progress_batch_size,
        args.limit,
    )

    started_total = time.monotonic()
    total_processed = 0
    conn = db_connect()

    try:
        ensure_tables(conn)
        months = available_months(conn)

        logger.info("MONTHS available=%s", len(months))

        for suffix in months:
            month_started = time.monotonic()
            month_processed = 0

            logger.info("MONTH %s start", suffix)

            while True:
                remaining_limit = None
                if args.limit is not None:
                    remaining_limit = args.limit - total_processed
                    if remaining_limit <= 0:
                        logger.info("LIMIT reached total_processed=%s", total_processed)
                        break

                current_batch_size = args.progress_batch_size
                if remaining_limit is not None:
                    current_batch_size = min(current_batch_size, remaining_limit)

                batch_started = time.monotonic()
                processed = process_month(conn, suffix, current_batch_size, args.limit)
                batch_elapsed = time.monotonic() - batch_started

                if processed <= 0:
                    break

                month_processed += processed
                total_processed += processed

                logger.info(
                    "PROGRESS month=%s processed_batch=%s elapsed_batch_sec=%.3f total_processed=%s",
                    suffix,
                    processed,
                    batch_elapsed,
                    total_processed,
                )

            logger.info(
                "MONTH %s done processed=%s elapsed_sec=%.3f",
                suffix,
                month_processed,
                time.monotonic() - month_started,
            )

            if args.limit is not None and total_processed >= args.limit:
                break

        logger.info(
            "DONE character skill inference total_processed=%s elapsed_sec=%.3f",
            total_processed,
            time.monotonic() - started_total,
        )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
