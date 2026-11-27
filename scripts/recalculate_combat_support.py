#!/usr/bin/env python3
"""Reconcile recorded healing/capacitor separately from collection. Writes devencour only."""
import argparse
from datetime import datetime
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
from app.combat_support import recalculate
from app.combat_killmail_links import refresh


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date-from',type=datetime.fromisoformat)
    parser.add_argument('--date-to',type=datetime.fromisoformat)
    args=parser.parse_args()
    if any(v is not None and v.tzinfo is None for v in (args.date_from,args.date_to)):
        parser.error('Use UTC timestamps with +00:00 or Z')
    if not refresh(force=True):
        parser.error('Killmail matching is already running')
    print(recalculate(args.date_from,args.date_to))


if __name__=='__main__':main()
