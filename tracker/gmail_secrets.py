"""Bootstrap Gmail credential files from env vars (for Railway / cloud deploy)."""

from __future__ import annotations

import base64
import json
import os

from tracker.config import gmail_credentials_path, gmail_token_path


def _decode_secret(value: str) -> str:
    value = value.strip()
    if value.startswith("{"):
        return value
    return base64.b64decode(value).decode("utf-8")


def ensure_gmail_files() -> None:
    """Write credential/token files from env if they are not already on disk."""
    creds_env = os.environ.get("GMAIL_CREDENTIALS_JSON") or os.environ.get(
        "GMAIL_CREDENTIALS_B64"
    )
    token_env = os.environ.get("GMAIL_TOKEN_JSON") or os.environ.get("GMAIL_TOKEN_B64")

    if creds_env:
        creds_path = gmail_credentials_path()
        creds_path.parent.mkdir(parents=True, exist_ok=True)
        if not creds_path.exists():
            creds_path.write_text(_decode_secret(creds_env), encoding="utf-8")

    if token_env:
        token_path = gmail_token_path()
        token_path.parent.mkdir(parents=True, exist_ok=True)
        if not token_path.exists():
            token_path.write_text(_decode_secret(token_env), encoding="utf-8")


def persist_token(creds_json: str) -> None:
    """Save refreshed OAuth token so it survives restarts (Railway volume)."""
    token_path = gmail_token_path()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds_json, encoding="utf-8")
