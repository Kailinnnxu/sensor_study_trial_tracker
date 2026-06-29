#!/usr/bin/env python3
"""Run email ingestion (schedule daily, before reminder engine)."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow running from scripts/ without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tracker.env import load_env

load_env()

from tracker.db import init_db
from tracker.ingestion.run import run_ingestion


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest anchor events from Gmail")
    parser.add_argument("--dry-run", action="store_true", help="Parse only; do not write")
    parser.add_argument("--max-results", type=int, default=100)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    init_db()
    stats = run_ingestion(dry_run=args.dry_run, max_results=args.max_results)
    logging.info(
        "Ingestion complete: ingested=%d flagged=%d skipped=%d processed=%d",
        stats.ingested,
        stats.flagged_for_review,
        stats.skipped_already_processed,
        stats.processed,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
