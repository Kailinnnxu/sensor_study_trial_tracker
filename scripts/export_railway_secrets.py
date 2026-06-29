#!/usr/bin/env python3
"""Print base64 Gmail secrets for pasting into Railway environment variables."""

from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tracker.config import gmail_credentials_path, gmail_token_path


def _b64(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Missing: {path}")
    return base64.b64encode(path.read_bytes()).decode("ascii")


def main() -> int:
    creds = gmail_credentials_path()
    token = gmail_token_path()

    print("Paste these into Railway → your service → Variables:\n")
    print(f"GMAIL_CREDENTIALS_B64={_b64(creds)}")
    print()
    print(f"GMAIL_TOKEN_B64={_b64(token)}")
    print()
    print("Also set (shared by web + cron services):")
    print("  TRACKER_DATABASE_PATH=/data/tracker.db")
    print("  GMAIL_TOKEN_PATH=/data/gmail_token.json")
    print("  GMAIL_CREDENTIALS_PATH=/data/gmail_credentials.json")
    print("  KAILIN_EMAIL=...")
    print("  SMTP_USER=...")
    print("  SMTP_PASSWORD=...")
    print("  FLASK_SECRET_KEY=...")
    print("  APP_URL=https://your-app.up.railway.app")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
