#!/usr/bin/env python3
"""Manually remove generated first-loss study data; retain all source data."""

import argparse
import json
import logging
import os
from datetime import datetime, UTC
from pathlib import Path

import psycopg2
from psycopg2 import sql

LOCK = 79130421  # Shared with analyze_first_losses.py, including direct CLI launches.
TABLES = (
    "first_loss_players",
    "first_loss_group_days",
    "first_loss_months",
    "first_loss_runs",
)
LOG = logging.getLogger("first_loss_purge")


def purge(conn, reset_progress=lambda: None):
    """Release table/index storage transactionally; never cascade to other tables."""
    acquired = False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (LOCK,))
            acquired = cur.fetchone()[0]
            if not acquired:
                raise RuntimeError(
                    "The first-loss study is running. Stop it and wait before purging."
                )
            cur.execute(
                "SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='60s'"
            )
            existing = []
            size = 0
            for table in TABLES:
                cur.execute("SELECT to_regclass(%s)", ("research." + table,))
                if cur.fetchone()[0]:
                    existing.append(table)
                    cur.execute(
                        "SELECT pg_total_relation_size(%s::regclass)",
                        ("research." + table,),
                    )
                    size += cur.fetchone()[0]
            if existing:
                cur.execute(
                    sql.SQL("TRUNCATE TABLE {} RESTRICT").format(
                        sql.SQL(", ").join(
                            sql.Identifier("research", table) for table in existing
                        )
                    )
                )
        conn.commit()
        # Retain the advisory lock until the checkpoint display is reset.
        try:
            reset_progress()
        except Exception as exc:
            raise RuntimeError(
                "Study data was deleted, but its progress display could not be reset: "
                + str(exc)
            ) from exc
        return {"tables": len(existing), "previous_bytes": size}
    except Exception:
        conn.rollback()
        raise
    finally:
        if acquired:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK,))
            conn.commit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-config", type=Path, default=Path.home() / "eveosint/config/db.json"
    )
    parser.add_argument(
        "--progress",
        type=Path,
        default=Path.home() / "eveosint/data/research/first_loss_purge_progress.json",
    )
    parser.add_argument(
        "--study-progress",
        type=Path,
        default=Path.home() / "eveosint/data/research/first_loss_progress.json",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    def write(path, state):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "updated_at": datetime.now(UTC).isoformat(),
                    **state,
                }
            )
        )
        tmp.replace(path)

    write(args.progress, {"phase": "running"})
    try:
        cfg = json.loads(args.db_config.read_text())
        conn = psycopg2.connect(
            dbname=cfg["db_name"],
            user=cfg["db_user"],
            password=cfg["db_password"],
            host=cfg["db_host"],
            port=cfg.get("db_port", 5432),
            application_name="first_loss_purge",
        )
        try:
            result = purge(
                conn,
                lambda: write(
                    args.study_progress,
                    {
                        "phase": "purged",
                        "message": "Study data deleted. Start a new analysis when needed.",
                    },
                ),
            )
        finally:
            conn.close()
        write(args.progress, {"phase": "completed", **result})
        LOG.info("PURGE_DONE %s", json.dumps(result))
        return 0
    except Exception as exc:
        LOG.exception("First-loss purge failed")
        write(args.progress, {"phase": "failed", "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
