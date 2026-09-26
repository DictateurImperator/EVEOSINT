#!/usr/bin/env python3
import argparse
import json
import logging
import logging.handlers
import multiprocessing
import shutil
import tarfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

import psycopg2
import requests


# ============================================================
# CONFIG
# ============================================================

CONFIG_PATH = Path.home() / "eveosint" / "config" / "db.json"

SCHEMA = "rawkm"

BASE_DIR = Path.home() / "eveosint"

DOWNLOAD_DIR = BASE_DIR / "data" / "killmails" / "downloads"
ARCHIVE_DIR = BASE_DIR / "data" / "killmails" / "archive"
TMP_DIR = BASE_DIR / "data" / "killmails" / "tmp"
PROOF_DIR = BASE_DIR / "data" / "killmails" / "proof"

LOG_DIR = BASE_DIR / "data" / "logs"
LOG_FILE = LOG_DIR / "killmail_import.log"

USER_AGENT = "DataArcheology/1.0.0"

COMMIT_BATCH_SIZE = 1000
DEFAULT_WORKERS = 4


# ============================================================
# LOGGER
# ============================================================

log_queue = multiprocessing.Queue()


def configure_listener_logger():
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s | %(processName)s | %(levelname)s | %(message)s"
    )

    file_handler = logging.FileHandler(
        LOG_FILE,
        encoding="utf-8"
    )

    file_handler.setFormatter(formatter)

    listener = logging.handlers.QueueListener(
        log_queue,
        file_handler,
    )

    listener.start()

    return listener


def configure_worker_logger():
    queue_handler = logging.handlers.QueueHandler(log_queue)

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.handlers = []
    logger.addHandler(queue_handler)

    return logger


logger = configure_worker_logger()


# ============================================================
# CONFIG LOAD
# ============================================================

def load_db_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


DB_CONFIG = load_db_config()


# ============================================================
# DB
# ============================================================

def t(name):
    return f"{SCHEMA}.{name}" if SCHEMA else name


def db():
    return psycopg2.connect(
        dbname=DB_CONFIG["db_name"],
        user=DB_CONFIG["db_user"],
        password=DB_CONFIG["db_password"],
        host=DB_CONFIG["db_host"],
        port=DB_CONFIG["db_port"],
    )


def ensure_dirs():
    for p in [
        DOWNLOAD_DIR,
        ARCHIVE_DIR,
        TMP_DIR,
        PROOF_DIR,
        LOG_DIR,
    ]:
        p.mkdir(parents=True, exist_ok=True)


def month_start(d):
    return date(d.year, d.month, 1)


def next_month(d):
    if d.month == 12:
        return date(d.year + 1, 1, 1)

    return date(d.year, d.month + 1, 1)


def months_between(start, end):
    current = month_start(start)

    while current <= end:
        yield current
        current = next_month(current)


def partition_name(base_name, month):
    return f"{base_name}_{month.year}_{month.month:02d}"


def ensure_tables(conn):
    with conn.cursor() as cur:
        if SCHEMA:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA};")

        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {t('killmails')} (
            killmail_id BIGINT NOT NULL,
            killmail_hash TEXT,
            killmail_time TIMESTAMPTZ NOT NULL,
            solar_system_id BIGINT NOT NULL,
            victim_character_id BIGINT,
            victim_corporation_id BIGINT,
            victim_alliance_id BIGINT,
            victim_ship_type_id BIGINT,
            victim_damage_taken INTEGER,
            http_last_modified TIMESTAMPTZ,
            imported_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (killmail_id, killmail_time)
        )
        PARTITION BY RANGE (killmail_time);
        """)

        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {t('killmail_attackers')} (
            killmail_id BIGINT NOT NULL,
            killmail_time TIMESTAMPTZ NOT NULL,
            attacker_index INTEGER NOT NULL,
            character_id BIGINT,
            corporation_id BIGINT,
            alliance_id BIGINT,
            faction_id BIGINT,
            ship_type_id BIGINT,
            weapon_type_id BIGINT,
            damage_done INTEGER,
            final_blow BOOLEAN,
            security_status DOUBLE PRECISION,
            PRIMARY KEY (killmail_id, killmail_time, attacker_index)
        )
        PARTITION BY RANGE (killmail_time);
        """)

        cur.execute(f"""
        ALTER TABLE {t('killmail_attackers')}
        ADD COLUMN IF NOT EXISTS faction_id BIGINT;
        """)

        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {t('killmail_items')} (
            killmail_id BIGINT NOT NULL,
            killmail_time TIMESTAMPTZ NOT NULL,
            item_index INTEGER NOT NULL,
            item_type_id BIGINT,
            flag INTEGER,
            quantity_destroyed BIGINT,
            quantity_dropped BIGINT,
            singleton INTEGER,
            PRIMARY KEY (killmail_id, killmail_time, item_index)
        )
        PARTITION BY RANGE (killmail_time);
        """)

        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {t('killmail_import_days')} (
            day DATE PRIMARY KEY,
            status TEXT,
            files_count INTEGER,
            archive_count INTEGER,
            error TEXT
        );
        """)

    conn.commit()


def ensure_month_partitions(conn, start, end):
    with conn.cursor() as cur:
        for m in months_between(start, end):
            m_next = next_month(m)

            km_part = partition_name("killmails", m)
            atk_part = partition_name("killmail_attackers", m)
            item_part = partition_name("killmail_items", m)

            cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {t(km_part)}
            PARTITION OF {t('killmails')}
            FOR VALUES FROM (%s) TO (%s);
            """, (m.isoformat(), m_next.isoformat()))

            cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {t(atk_part)}
            PARTITION OF {t('killmail_attackers')}
            FOR VALUES FROM (%s) TO (%s);
            """, (m.isoformat(), m_next.isoformat()))

            cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {t(item_part)}
            PARTITION OF {t('killmail_items')}
            FOR VALUES FROM (%s) TO (%s);
            """, (m.isoformat(), m_next.isoformat()))

            cur.execute(f"""
            CREATE INDEX IF NOT EXISTS {km_part}_system_time_idx
            ON {t(km_part)} (solar_system_id, killmail_time);
            """)

            cur.execute(f"""
            CREATE INDEX IF NOT EXISTS {km_part}_victim_char_idx
            ON {t(km_part)} (victim_character_id);
            """)

            cur.execute(f"""
            CREATE INDEX IF NOT EXISTS {km_part}_victim_corp_idx
            ON {t(km_part)} (victim_corporation_id);
            """)

            cur.execute(f"""
            CREATE INDEX IF NOT EXISTS {km_part}_victim_alliance_idx
            ON {t(km_part)} (victim_alliance_id);
            """)

            cur.execute(f"""
            CREATE INDEX IF NOT EXISTS {km_part}_victim_ship_idx
            ON {t(km_part)} (victim_ship_type_id);
            """)

            cur.execute(f"""
            CREATE INDEX IF NOT EXISTS {atk_part}_char_idx
            ON {t(atk_part)} (character_id);
            """)

            cur.execute(f"""
            CREATE INDEX IF NOT EXISTS {atk_part}_corp_idx
            ON {t(atk_part)} (corporation_id);
            """)

            cur.execute(f"""
            CREATE INDEX IF NOT EXISTS {atk_part}_alliance_idx
            ON {t(atk_part)} (alliance_id);
            """)

            cur.execute(f"""
            CREATE INDEX IF NOT EXISTS {atk_part}_ship_idx
            ON {t(atk_part)} (ship_type_id);
            """)

            cur.execute(f"""
            CREATE INDEX IF NOT EXISTS {item_part}_type_idx
            ON {t(item_part)} (item_type_id);
            """)

    conn.commit()


def is_day_success(conn, d):
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT status
            FROM {t('killmail_import_days')}
            WHERE day=%s
            """,
            (d,)
        )

        row = cur.fetchone()

    return row is not None and row[0] == "success"


# ============================================================
# UTILS
# ============================================================

def parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def days_between(start, end):
    d = start

    while d <= end:
        yield d
        d += timedelta(days=1)


def day_url(day):
    return (
        f"https://data.everef.net/killmails/"
        f"{day.year}/killmails-{day}.tar.bz2"
    )


def archive_tar_path(day):
    archive_dir = ARCHIVE_DIR / str(day.year)
    archive_dir.mkdir(parents=True, exist_ok=True)
    return archive_dir / f"killmails-{day}.tar.bz2"


def day_tmp_dir(day):
    return TMP_DIR / f"{day}"


def day_download_path(day):
    return DOWNLOAD_DIR / f"{day}.tar.bz2"


def mark_day(
    conn,
    d,
    status,
    files_count=None,
    archive_count=None,
    error=None,
):
    with conn.cursor() as cur:
        cur.execute(f"""
        INSERT INTO {t('killmail_import_days')} (
            day,
            status,
            files_count,
            archive_count,
            error
        )
        VALUES (%s, %s, %s, %s, %s)

        ON CONFLICT (day)
        DO UPDATE SET
            status = EXCLUDED.status,
            files_count = EXCLUDED.files_count,
            archive_count = EXCLUDED.archive_count,
            error = EXCLUDED.error
        """, (
            d,
            status,
            files_count,
            archive_count,
            error,
        ))

    conn.commit()


# ============================================================
# ITEMS
# ============================================================

def insert_item_recursive(
    cur,
    kmid,
    killmail_time,
    item,
    item_index,
    parent_flag=None,
):
    cur.execute(f"""
    INSERT INTO {t('killmail_items')} (
        killmail_id,
        killmail_time,
        item_index,
        item_type_id,
        flag,
        quantity_destroyed,
        quantity_dropped,
        singleton
    )
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)

    ON CONFLICT
    (killmail_id, killmail_time, item_index)
    DO NOTHING
    """, (
        kmid,
        killmail_time,
        item_index,
        item.get("item_type_id"),
        item.get("flag", parent_flag),
        item.get("quantity_destroyed"),
        item.get("quantity_dropped"),
        item.get("singleton"),
    ))

    next_index = item_index + 1

    for child in item.get("items", []):
        next_index = insert_item_recursive(
            cur,
            kmid,
            killmail_time,
            child,
            next_index,
            item.get("flag"),
        )

    return next_index


# ============================================================
# IMPORT
# ============================================================

def insert_killmail(
    conn,
    payload,
):
    kmid = payload["killmail_id"]
    killmail_time = payload["killmail_time"]

    victim = payload.get(
        "victim",
        {}
    )

    with conn.cursor() as cur:
        cur.execute(f"""
        INSERT INTO {t('killmails')} (
            killmail_id,
            killmail_hash,
            killmail_time,
            solar_system_id,
            victim_character_id,
            victim_corporation_id,
            victim_alliance_id,
            victim_ship_type_id,
            victim_damage_taken,
            http_last_modified
        )
        VALUES (
            %s,%s,%s,%s,%s,%s,%s,
            %s,%s,%s
        )

        ON CONFLICT (killmail_id, killmail_time)
        DO NOTHING
        """, (
            kmid,
            payload.get("killmail_hash"),
            killmail_time,
            payload["solar_system_id"],
            victim.get("character_id"),
            victim.get("corporation_id"),
            victim.get("alliance_id"),
            victim.get("ship_type_id"),
            victim.get("damage_taken"),
            payload.get("http_last_modified"),
        ))

        cur.execute(
            f"""
            DELETE FROM
            {t('killmail_attackers')}
            WHERE killmail_id=%s
            AND killmail_time=%s
            """,
            (kmid, killmail_time)
        )

        for i, a in enumerate(
            payload.get("attackers", [])
        ):
            cur.execute(f"""
            INSERT INTO {t('killmail_attackers')} (
                killmail_id,
                killmail_time,
                attacker_index,
                character_id,
                corporation_id,
                alliance_id,
                faction_id,
                ship_type_id,
                weapon_type_id,
                damage_done,
                final_blow,
                security_status
            )
            VALUES (
                %s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s
            )

            ON CONFLICT
            (killmail_id, killmail_time, attacker_index)
            DO NOTHING
            """, (
                kmid,
                killmail_time,
                i,
                a.get("character_id"),
                a.get("corporation_id"),
                a.get("alliance_id"),
                a.get("faction_id"),
                a.get("ship_type_id"),
                a.get("weapon_type_id"),
                a.get("damage_done"),
                a.get("final_blow"),
                a.get("security_status"),
            ))

        cur.execute(
            f"""
            DELETE FROM
            {t('killmail_items')}
            WHERE killmail_id=%s
            AND killmail_time=%s
            """,
            (kmid, killmail_time)
        )

        item_index = 0

        for item in victim.get(
            "items",
            []
        ):
            item_index = insert_item_recursive(
                cur,
                kmid,
                killmail_time,
                item,
                item_index,
            )


# ============================================================
# WORKER
# ============================================================

def process_day(day_iso):
    configure_worker_logger()

    d = parse_date(day_iso)

    url = day_url(d)

    bz2_path = day_download_path(d)
    archive_path = archive_tar_path(d)
    out_dir = day_tmp_dir(d)

    logger.info(f"START {d}")

    conn = db()

    try:
        if is_day_success(conn, d):
            logger.info(f"SKIP {d} already success")
            return

        mark_day(
            conn,
            d,
            "running"
        )

        if not archive_path.exists():
            logger.info(f"{d} downloading")

            r = requests.get(
                url,
                headers={
                    "User-Agent": USER_AGENT
                },
                stream=True,
                timeout=(10, 300),
            )

            if r.status_code == 404:
                logger.warning(f"{d} missing")

                mark_day(
                    conn,
                    d,
                    "missing",
                    0,
                    0,
                    "404",
                )

                return

            r.raise_for_status()

            with open(
                bz2_path,
                "wb"
            ) as f:
                for chunk in r.iter_content(
                    1024 * 1024
                ):
                    if chunk:
                        f.write(chunk)

            shutil.move(
                str(bz2_path),
                str(archive_path),
            )

        else:
            logger.info(f"{d} archive exists")

        if out_dir.exists():
            shutil.rmtree(out_dir)

        out_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        logger.info(f"{d} extracting")

        with tarfile.open(
            archive_path,
            "r:bz2"
        ) as tar:
            tar.extractall(out_dir, filter="data")

        json_files = list(
            out_dir.rglob("*.json")
        )

        logger.info(
            f"{d} files={len(json_files)}"
        )

        imported = 0

        for jf in json_files:
            with open(
                jf,
                encoding="utf-8"
            ) as f:
                payload = json.load(f)

            insert_killmail(
                conn,
                payload,
            )

            imported += 1

            if (
                imported %
                COMMIT_BATCH_SIZE
                == 0
            ):
                conn.commit()

                logger.info(
                    f"{d} committed "
                    f"{imported}/"
                    f"{len(json_files)}"
                )

        conn.commit()

        logger.info(f"{d} final commit")

        proof_file = PROOF_DIR / f"{d}.done.txt"

        proof_file.write_text(
            (
                f"OK {d} | "
                f"files={len(json_files)} | "
                f"archive={archive_path} | "
                f"source={url}\n"
            ),
            encoding="utf-8",
        )

        mark_day(
            conn,
            d,
            "success",
            len(json_files),
            1,
        )

        logger.info(f"SUCCESS {d}")

    except Exception as e:
        conn.rollback()

        logger.exception(f"FAILED {d}: {e}")

        try:
            mark_day(
                conn,
                d,
                "failed",
                None,
                None,
                str(e),
            )

        except Exception:
            pass

    finally:
        try:
            conn.close()

        except Exception:
            pass

        if out_dir.exists():
            shutil.rmtree(out_dir)

        if bz2_path.exists():
            bz2_path.unlink()


# ============================================================
# MAIN
# ============================================================

def main():
    listener = configure_listener_logger()

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--from",
        dest="from_date",
        default="2007-12-05",
    )

    parser.add_argument(
        "--to",
        dest="to_date",
        default=date.today().isoformat(),
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
    )

    args = parser.parse_args()

    ensure_dirs()

    start = parse_date(
        args.from_date
    )

    end = parse_date(
        args.to_date
    )

    days = [
        d.isoformat()
        for d in days_between(
            start,
            end
        )
    ]

    with db() as conn:
        ensure_tables(conn)
        ensure_month_partitions(conn, start, end)

    logger.info(
        f"START IMPORT "
        f"days={len(days)} "
        f"workers={args.workers}"
    )

    with ProcessPoolExecutor(
        max_workers=args.workers
    ) as executor:
        futures = [
            executor.submit(
                process_day,
                d
            )
            for d in days
        ]

        for future in as_completed(
            futures
        ):
            try:
                future.result()

            except Exception as e:
                logger.exception(
                    f"FUTURE FAILED: {e}"
                )

    logger.info("DONE")

    listener.stop()


if __name__ == "__main__":
    main()