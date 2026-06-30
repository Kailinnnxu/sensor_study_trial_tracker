"""Bootstrap Gmail credential files from env vars (for Railway / cloud deploy)."""

from __future__ import annotations

import base64
import json
import os

from tracker.config import gmail_credentials_path, gmail_token_path


def _normalize_b64(value: str) -> str:
    """Strip whitespace and fix padding for pasted Railway env vars."""
    cleaned = "".join(value.strip().split())
    pad = (-len(cleaned)) % 4
    return cleaned + ("=" * pad)


def _decode_secret(value: str, *, env_name: str = "secret") -> str:
    value = value.strip()
    if value.startswith("{"):
        return value
    normalized = _normalize_b64(value)
    try:
        decoded = base64.b64decode(normalized).decode("utf-8")
    except Exception as exc:
        raise ValueError(
            f"Invalid {env_name}: base64 decode failed ({exc}). "
            f"Length={len(value)} (need multiple of 4 after cleanup). "
            "Re-run scripts/export_railway_secrets.py and paste the full value "
            "with no line breaks or quotes."
        ) from exc
    try:
        json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid {env_name}: decoded JSON is truncated or corrupted ({exc}). "
            f"Decoded length={len(decoded)} chars. "
            "Expected ~410 chars (credentials) or ~736 chars (token). "
            "Re-run scripts/export_railway_secrets.py — use the file export "
            "or paste the complete B64 string with no line breaks."
        ) from exc
    return decoded


def _env_present(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())


def gmail_setup_diagnostics() -> dict:
    creds_path = gmail_credentials_path()
    token_path = gmail_token_path()
    return {
        "creds_path": str(creds_path),
        "token_path": str(token_path),
        "creds_file_exists": creds_path.exists(),
        "token_file_exists": token_path.exists(),
        "has_GMAIL_CREDENTIALS_B64": _env_present("GMAIL_CREDENTIALS_B64"),
        "has_GMAIL_CREDENTIALS_JSON": _env_present("GMAIL_CREDENTIALS_JSON"),
        "has_GMAIL_TOKEN_B64": _env_present("GMAIL_TOKEN_B64"),
        "has_GMAIL_TOKEN_JSON": _env_present("GMAIL_TOKEN_JSON"),
    }


def ensure_gmail_files() -> None:
    """Write credential/token files from env vars when provided."""
    creds_env = os.environ.get("GMAIL_CREDENTIALS_JSON") or os.environ.get(
        "GMAIL_CREDENTIALS_B64"
    )
    token_env = os.environ.get("GMAIL_TOKEN_JSON") or os.environ.get("GMAIL_TOKEN_B64")

    if creds_env:
        creds_path = gmail_credentials_path()
        creds_path.parent.mkdir(parents=True, exist_ok=True)
        env_name = (
            "GMAIL_CREDENTIALS_JSON"
            if os.environ.get("GMAIL_CREDENTIALS_JSON")
            else "GMAIL_CREDENTIALS_B64"
        )
        creds_path.write_text(
            _decode_secret(creds_env, env_name=env_name), encoding="utf-8"
        )

    if token_env:
        token_path = gmail_token_path()
        token_path.parent.mkdir(parents=True, exist_ok=True)
        env_name = (
            "GMAIL_TOKEN_JSON"
            if os.environ.get("GMAIL_TOKEN_JSON")
            else "GMAIL_TOKEN_B64"
        )
        token_path.write_text(_decode_secret(token_env, env_name=env_name), encoding="utf-8")


def persist_token(creds_json: str) -> None:
    """Save refreshed OAuth token so it survives restarts (Railway volume)."""
    token_path = gmail_token_path()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds_json, encoding="utf-8")


class GmailSetupError(RuntimeError):
    """Gmail OAuth files or env vars are not configured."""

    def __init__(self, diagnostics: dict) -> None:
        self.diagnostics = diagnostics
        missing = []
        if not diagnostics["creds_file_exists"] and not (
            diagnostics["has_GMAIL_CREDENTIALS_B64"]
            or diagnostics["has_GMAIL_CREDENTIALS_JSON"]
        ):
            missing.append("GMAIL_CREDENTIALS_B64")
        if not diagnostics["token_file_exists"] and not (
            diagnostics["has_GMAIL_TOKEN_B64"] or diagnostics["has_GMAIL_TOKEN_JSON"]
        ):
            missing.append("GMAIL_TOKEN_B64")

        hint = (
            "Run: python scripts/export_railway_secrets.py locally, then paste "
            "GMAIL_CREDENTIALS_B64 and GMAIL_TOKEN_B64 into Railway Variables "
            "on the **web** service (and cron). Redeploy after saving."
        )
        if missing:
            super().__init__(
                f"Gmail not configured. Missing env vars: {', '.join(missing)}. {hint}"
            )
        else:
            super().__init__(
                f"Gmail files missing at {diagnostics['creds_path']} and/or "
                f"{diagnostics['token_path']}. {hint}"
            )
