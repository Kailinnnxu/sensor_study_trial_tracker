#!/usr/bin/env python3
"""One-time Gmail OAuth setup for klx5505@gmail.com."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tracker.ingestion.gmail_client import get_gmail_service


def main() -> int:
    print("Starting Gmail OAuth flow...")
    print("A browser window will open. Sign in and grant read-only Gmail access.")
    print("The token will be saved to the path configured by GMAIL_TOKEN_PATH.")
    print("If the saved refresh token is expired, a new browser login will start.")
    service = get_gmail_service()
    profile = service.users().getProfile(userId="me").execute()
    print(f"Authenticated as: {profile.get('emailAddress')}")
    print("Setup complete.")
    print("If you deploy on Railway, re-run: python scripts/export_railway_secrets.py")
    print("Then update GMAIL_TOKEN_B64 (and credentials if needed) in Railway Variables.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
