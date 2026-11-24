#!/usr/bin/env python3
"""Initialize the isolated combat prototype; optionally remove legacy SQLite."""
import argparse
import re
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
from app import combat_timeline as store


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--owner',type=int)
    p.add_argument('--token-sha256')
    p.add_argument('--purge-legacy-sqlite',action='store_true')
    args=p.parse_args()
    if bool(args.owner)!=bool(args.token_sha256) or (args.token_sha256 and not re.fullmatch(r'[0-9a-f]{64}',args.token_sha256)):
        p.error('Supply a valid owner and token SHA256 together')
    store.setup()
    if args.owner:
        with store.connection() as conn,conn.cursor() as cur:
            cur.execute('''INSERT INTO devencour.collector_tokens(owner,digest) VALUES(%s,%s)
              ON CONFLICT(owner) DO UPDATE SET digest=excluded.digest,created_at=clock_timestamp()''',(args.owner,args.token_sha256))
    if args.purge_legacy_sqlite:
        # Fixed local legacy files only. PostgreSQL setup/activation must succeed first.
        for name in ('timeline.sqlite3','timeline.sqlite3-wal','timeline.sqlite3-shm','timeline.sqlite3-journal'):
            path=store.ROOT/name
            if path.is_symlink(): raise RuntimeError('Refusing a symlink: '+str(path))
        for name in ('timeline.sqlite3','timeline.sqlite3-wal','timeline.sqlite3-shm','timeline.sqlite3-journal'):
            (store.ROOT/name).unlink(missing_ok=True)
        print('Legacy SQLite removed completely.')
    print('devencour ready. No legacy observations imported.')

if __name__=='__main__': main()
