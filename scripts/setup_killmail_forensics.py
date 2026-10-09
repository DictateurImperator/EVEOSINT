#!/usr/bin/env python3
"""One-off, repeatable migration. Run from Admin Jobs with the application DB role."""

import json
from pathlib import Path

import psycopg2


def main():
    config = json.loads((Path.home() / "eveosint/config/db.json").read_text())
    with psycopg2.connect(
        dbname=config["db_name"],
        user=config["db_user"],
        password=config["db_password"],
        host=config["db_host"],
        port=config["db_port"],
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '5s'")
            cur.execute("SET LOCAL statement_timeout = '30s'")
            cur.execute("SELECT pg_advisory_xact_lock(472119044)")
            cur.execute(
                (
                    Path(__file__).resolve().parents[1] / "web/app/forensics_schema.sql"
                ).read_text()
            )
    print(
        "Killmail Forensics tables are ready. Existing investigations were preserved.",
        flush=True,
    )


if __name__ == "__main__":
    main()
