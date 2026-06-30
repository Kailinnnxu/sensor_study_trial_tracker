#!/usr/bin/env python3
"""Export base64 Gmail secrets for Railway environment variables."""

from __future__ import annotations

import argparse
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
    parser = argparse.ArgumentParser(description="Export Gmail B64 secrets for Railway")
    parser.add_argument(
        "--out",
        type=Path,
        help="Write variables to a text file (easier than copying from terminal)",
    )
    args = parser.parse_args()

    creds = gmail_credentials_path()
    token = gmail_token_path()
    creds_b64 = _b64(creds)
    token_b64 = _b64(token)

    lines = [
        f"GMAIL_CREDENTIALS_B64={creds_b64}",
        f"GMAIL_TOKEN_B64={token_b64}",
    ]

    if args.out:
        args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"Wrote {args.out}")
        print(f"  GMAIL_CREDENTIALS_B64 length: {len(creds_b64)}")
        print(f"  GMAIL_TOKEN_B64 length: {len(token_b64)}")
        print("Open the file and copy each FULL line into Railway Variables.")
        return 0

    print("Paste these into Railway → Variables (copy the FULL line after =):\n")
    print(lines[0])
    print()
    print(lines[1])
    print()
    print(f"Lengths: credentials={len(creds_b64)}, token={len(token_b64)}")
    print()
    print("Tip: use --out railway_gmail_vars.txt to avoid terminal truncation.")
    print()
    print("Also set (shared by web + cron services):")
    print("  TRACKER_DATABASE_PATH=/data/tracker.db")
    print("  GMAIL_TOKEN_PATH=/data/gmail_token.json")
    print("  GMAIL_CREDENTIALS_PATH=/data/gmail_credentials.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
