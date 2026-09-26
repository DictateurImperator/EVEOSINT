#!/usr/bin/env python3
import argparse
import logging
import subprocess
import sys
from pathlib import Path


BASE_DIR = Path.home() / "eveosint"
SCRIPT_DIR = BASE_DIR / "scripts"
LOG_DIR = BASE_DIR / "data" / "logs"
LOG_FILE = LOG_DIR / "recent_kill_pilots_affiliation_refresh.log"

BUILD_RECENT_SCRIPT = SCRIPT_DIR / "build_recent_killmail_pilots.py"
ESI_REFRESH_SCRIPT = SCRIPT_DIR / "esi_affiliation_refresh.py"

SOURCE_TABLE = "entities.recent_killmail_pilots"
SOURCE_COLUMN = "character_id"


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


def validate_args(args) -> None:
    if args.days <= 0:
        raise RuntimeError("--days must be greater than 0")
    if args.workers <= 0:
        raise RuntimeError("--workers must be greater than 0")
    if args.workers > 16:
        raise RuntimeError("--workers must be <= 16")
    if args.batch_size <= 0 or args.batch_size > 999:
        raise RuntimeError("--batch-size must be between 1 and 999")
    if args.process_batch_size <= 0:
        raise RuntimeError("--process-batch-size must be greater than 0")
    if args.esi_max_calls_per_minute <= 0 or args.esi_max_calls_per_minute > 300:
        raise RuntimeError("--esi-max-calls-per-minute must be between 1 and 300")
    if args.limit is not None and args.limit <= 0:
        raise RuntimeError("--limit must be greater than 0")


def run_command(command: list[str], label: str) -> None:
    logger.info("START %s command=%s", label, " ".join(command))
    subprocess.run(command, check=True)
    logger.info("DONE %s", label)


def main() -> None:
    configure_logging()

    parser = argparse.ArgumentParser(
        description="Refresh ESI affiliations for pilots involved in killmails since X days."
    )
    parser.add_argument("--days", type=int, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=999)
    parser.add_argument("--process-batch-size", type=int, default=2000)
    parser.add_argument("--esi-max-calls-per-minute", type=int, default=280)
    parser.add_argument("--limit", type=int)

    args = parser.parse_args()
    validate_args(args)

    if not BUILD_RECENT_SCRIPT.is_file():
        raise RuntimeError(f"Missing script: {BUILD_RECENT_SCRIPT}")
    if not ESI_REFRESH_SCRIPT.is_file():
        raise RuntimeError(f"Missing script: {ESI_REFRESH_SCRIPT}")

    python_bin = sys.executable

    logger.info(
        "START recent kill pilots affiliation refresh days=%s workers=%s",
        args.days,
        args.workers,
    )

    run_command(
        [
            python_bin,
            str(BUILD_RECENT_SCRIPT),
            "--days",
            str(args.days),
        ],
        "build_recent_killmail_pilots",
    )

    refresh_command = [
        python_bin,
        str(ESI_REFRESH_SCRIPT),
        "--source-table",
        SOURCE_TABLE,
        "--source-column",
        SOURCE_COLUMN,
        "--workers",
        str(args.workers),
        "--batch-size",
        str(args.batch_size),
        "--process-batch-size",
        str(args.process_batch_size),
        "--esi-max-calls-per-minute",
        str(args.esi_max_calls_per_minute),
    ]

    if args.limit is not None:
        refresh_command.extend(["--limit", str(args.limit)])

    run_command(refresh_command, "esi_affiliation_refresh")

    logger.info("DONE recent kill pilots affiliation refresh")


if __name__ == "__main__":
    main()
