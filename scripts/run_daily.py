#!/usr/bin/env python3
"""Run ingestion then reminders (convenience wrapper for daily cron)."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tracker.env import load_env

load_env()

from tracker.db import init_db
from tracker.ingestion.run import run_ingestion
from tracker.engine.reminder import run_reminders


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily pipeline: ingest then remind")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--today", type=str)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    init_db()
    ingest_stats = run_ingestion(dry_run=args.dry_run)
    logging.info("Ingestion: ingested=%d flagged=%d", ingest_stats.ingested, ingest_stats.flagged_for_review)

    today = date.fromisoformat(args.today) if args.today else None
    reminder_stats = run_reminders(today=today, dry_run=args.dry_run)
    logging.info("Reminders: actions=%d", len(reminder_stats.actions_taken))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
