#!/usr/bin/env python3
"""Admin job: estimate every unmatched MER loss; never calls CCP."""

import argparse
import logging
import signal
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    parser.add_argument("--refresh-only", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))
    from app.forensics_batch import run_analysis

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    interrupted = False

    def stop(_signal, _frame):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    run_analysis(
        args.user_id,
        args.date_from,
        args.date_to,
        lambda: interrupted,
        refresh_only=args.refresh_only,
    )


if __name__ == "__main__":
    main()
