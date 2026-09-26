#!/usr/bin/env python3
import argparse
import json
import logging
import sys
from pathlib import Path

import psycopg2


CONFIG_PATH = Path.home() / "eveosint" / "config" / "db.json"
BASE_DIR = Path.home() / "eveosint"
LOG_DIR = BASE_DIR / "data" / "logs"
LOG_FILE = LOG_DIR / "recent_killmail_pilots.log"

SOURCE_SCHEMA = "rawkm"
ENTITIES_SCHEMA = "entities"
TARGET_TABLE = "recent_killmail_pilots"


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


def rebuild_recent_killmail_pilots(conn, days: int) -> int:
    if days <= 0:
        raise RuntimeError("--days must be greater than 0")

    if not table_exists(conn, SOURCE_SCHEMA, "killmails"):
        raise RuntimeError("Missing source table rawkm.killmails")

    if not table_exists(conn, SOURCE_SCHEMA, "killmail_attackers"):
        raise RuntimeError("Missing source table rawkm.killmail_attackers")

    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {ENTITIES_SCHEMA};")
        cur.execute(f"DROP TABLE IF EXISTS {ENTITIES_SCHEMA}.{TARGET_TABLE}_tmp;")
        cur.execute(f"DROP INDEX IF EXISTS {ENTITIES_SCHEMA}.{TARGET_TABLE}_tmp_last_killmail_time_idx;")

        cur.execute(
            f"""
            CREATE TABLE {ENTITIES_SCHEMA}.{TARGET_TABLE}_tmp AS
            WITH params AS (
                SELECT
                    NOW() AS generated_at,
                    (NOW() - (%s::int * INTERVAL '1 day')) AS since_at,
                    %s::int AS days
            ),
            involved AS (
                SELECT
                    k.victim_character_id::bigint AS character_id,
                    k.killmail_id,
                    k.killmail_time,
                    1::bigint AS kills_as_victim,
                    0::bigint AS kills_as_attacker
                FROM {SOURCE_SCHEMA}.killmails k
                CROSS JOIN params p
                WHERE k.killmail_time >= p.since_at
                  AND k.victim_character_id IS NOT NULL

                UNION ALL

                SELECT
                    a.character_id::bigint AS character_id,
                    k.killmail_id,
                    k.killmail_time,
                    0::bigint AS kills_as_victim,
                    1::bigint AS kills_as_attacker
                FROM {SOURCE_SCHEMA}.killmail_attackers a
                JOIN {SOURCE_SCHEMA}.killmails k
                  ON k.killmail_id = a.killmail_id
                CROSS JOIN params p
                WHERE k.killmail_time >= p.since_at
                  AND a.character_id IS NOT NULL
            )
            SELECT
                i.character_id,
                MIN(i.killmail_time) AS first_killmail_time,
                MAX(i.killmail_time) AS last_killmail_time,
                COUNT(DISTINCT i.killmail_id)::bigint AS killmail_count,
                SUM(i.kills_as_victim)::bigint AS kills_as_victim,
                SUM(i.kills_as_attacker)::bigint AS kills_as_attacker,
                p.days,
                p.since_at,
                p.generated_at
            FROM involved i
            CROSS JOIN params p
            GROUP BY
                i.character_id,
                p.days,
                p.since_at,
                p.generated_at
            """,
            (days, days),
        )

        cur.execute(f"""
            ALTER TABLE {ENTITIES_SCHEMA}.{TARGET_TABLE}_tmp
            ADD PRIMARY KEY (character_id);
        """)

        cur.execute(f"""
            CREATE INDEX {TARGET_TABLE}_tmp_last_killmail_time_idx
            ON {ENTITIES_SCHEMA}.{TARGET_TABLE}_tmp (last_killmail_time DESC);
        """)

        cur.execute(f"DROP TABLE IF EXISTS {ENTITIES_SCHEMA}.{TARGET_TABLE}_old;")
        cur.execute(f"""
            ALTER TABLE IF EXISTS {ENTITIES_SCHEMA}.{TARGET_TABLE}
            RENAME TO {TARGET_TABLE}_old;
        """)
        cur.execute(f"""
            ALTER TABLE {ENTITIES_SCHEMA}.{TARGET_TABLE}_tmp
            RENAME TO {TARGET_TABLE};
        """)
        cur.execute(f"DROP TABLE IF EXISTS {ENTITIES_SCHEMA}.{TARGET_TABLE}_old;")

        cur.execute(f"SELECT COUNT(*) FROM {ENTITIES_SCHEMA}.{TARGET_TABLE};")
        rows = int(cur.fetchone()[0])

    conn.commit()
    return rows


def main() -> None:
    configure_logging()

    parser = argparse.ArgumentParser(
        description="Build entities.recent_killmail_pilots from raw killmails."
    )
    parser.add_argument(
        "--days",
        type=int,
        required=True,
        help="Number of recent days to scan from rawkm.killmails.",
    )

    args = parser.parse_args()

    if args.days <= 0:
        raise RuntimeError("--days must be greater than 0")

    logger.info("START rebuild recent killmail pilots days=%s", args.days)

    conn = db_connect()
    try:
        rows = rebuild_recent_killmail_pilots(conn, args.days)
        logger.info(
            "DONE rebuild recent killmail pilots days=%s rows=%s target=%s.%s",
            args.days,
            rows,
            ENTITIES_SCHEMA,
            TARGET_TABLE,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
