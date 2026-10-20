#!/usr/bin/env python3
"""Explicit, additive setup. Never runs automatically at web startup."""
import argparse
import json
from pathlib import Path
import psycopg2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db-config', required=True, type=Path)
    args = parser.parse_args()
    cfg = json.loads(args.db_config.read_text())
    with psycopg2.connect(dbname=cfg['db_name'], user=cfg['db_user'],
                          password=cfg['db_password'], host=cfg['db_host'],
                          port=cfg['db_port'], connect_timeout=5,
                          options='-c statement_timeout=15000 -c lock_timeout=5000') as conn:
        with conn.cursor() as cur:
            cur.execute((Path(__file__).resolve().parents[1] / 'web/app/killmail_generator_schema.sql').read_text())
        conn.commit()
    conn.close()
    print('KillmailGenerator collector tables ready.')


if __name__ == '__main__':
    main()
