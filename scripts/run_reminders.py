#!/usr/bin/env python3
"""Run reminder engine (schedule daily, after ingestion)."""

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
from tracker.engine.reminder import run_reminders


def main() -> int:
    parser = argparse.ArgumentParser(description="Run touchpoint reminder engine")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate only; do not send")
    parser.add_argument("--today", type=str, help="Override today's date (YYYY-MM-DD)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    init_db()
    today = date.fromisoformat(args.today) if args.today else None
    stats = run_reminders(today=today, dry_run=args.dry_run)
    logging.info(
        "Reminder run complete: checked=%d actions=%d",
        stats.participants_checked,
        len(stats.actions_taken),
    )
    for action in stats.actions_taken:
        logging.info(
            "  %s %s offset %d (%s)",
            action.study_id,
            action.touchpoint_key,
            action.offset,
            action.action_type,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
