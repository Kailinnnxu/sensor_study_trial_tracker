"""Gmail API client for fetching unprocessed emails."""

from __future__ import annotations

import base64
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from tracker.config import gmail_credentials_path, gmail_token_path
from tracker.gmail_secrets import (
    GmailAuthError,
    GmailSetupError,
    ensure_gmail_files,
    gmail_setup_diagnostics,
    overwrite_token_from_env,
    persist_token,
)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
logger = logging.getLogger(__name__)

# Gmail "units per minute per user" is small; messages.get costs 5 units each.
_RATE_LIMIT_ATTEMPTS = 5
_RATE_LIMIT_MARKERS = (
    "rateLimitExceeded",
    "userRateLimitExceeded",
    "Quota exceeded",
    "RESOURCE_EXHAUSTED",
)


class GmailRateLimitError(RuntimeError):
    """Gmail rejected a call because the per-minute quota was exhausted."""

    def __init__(self, detail: str = "") -> None:
        message = (
            "Gmail API rate limit hit (too many fetches in one minute). "
            "Wait about a minute and fetch again. Already ingested emails "
            "are skipped, so the next fetch only loads new messages."
        )
        if detail:
            message = f"{detail} {message}"
        super().__init__(message)


@dataclass
class EmailMessage:
    message_id: str
    sender: str
    subject: str
    body: str
    received_at: datetime | None = None


def _decode_body(data: str) -> str:
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")


def _extract_body(payload: dict) -> str:
    if not payload:
        return ""

    mime = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data")
    if body_data and mime in ("text/plain", "text/html"):
        return _decode_body(body_data)

    parts = payload.get("parts") or []
    plain = ""
    html = ""
    for part in parts:
        part_mime = part.get("mimeType", "")
        if part_mime == "multipart/alternative" and part.get("parts"):
            return _extract_body(part)
        part_body = part.get("body", {}).get("data")
        if not part_body:
            nested = _extract_body(part)
            if nested:
                return nested
            continue
        decoded = _decode_body(part_body)
        if part_mime == "text/plain":
            plain = decoded
        elif part_mime == "text/html":
            html = decoded
    if plain:
        return plain
    if html:
        return re.sub(r"<[^>]+>", " ", html)
    return ""


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _load_token(token_path) -> Credentials | None:
    if not token_path.exists():
        return None
    return Credentials.from_authorized_user_file(str(token_path), SCOPES)


def _refresh_credentials(creds: Credentials) -> Credentials | None:
    if not creds.refresh_token:
        return None
    try:
        creds.refresh(Request())
        persist_token(creds.to_json())
        return creds
    except Exception:
        return None


def _credentials_from_disk_or_env():
    token_path = gmail_token_path()
    creds = _load_token(token_path)
    if creds and creds.valid:
        persist_token(creds.to_json())
        return creds
    if creds and creds.expired:
        refreshed = _refresh_credentials(creds)
        if refreshed:
            return refreshed
        # Dead refresh token: a newly pasted GMAIL_TOKEN_B64 can replace it.
        try:
            replaced = overwrite_token_from_env()
        except ValueError:
            replaced = False
        if replaced:
            creds = _load_token(token_path)
            if creds and creds.valid:
                return creds
            if creds and creds.expired:
                refreshed = _refresh_credentials(creds)
                if refreshed:
                    return refreshed
    return None


def _browser_available() -> bool:
    import webbrowser

    try:
        webbrowser.get()
        return True
    except webbrowser.Error:
        return False


def _register_browser_fallback() -> None:
    import shutil
    import sys
    import webbrowser

    # Prefer `open` on macOS. Python's default MacOSXOSAScript often fails
    # from Cursor/agent terminals even when webbrowser.get() succeeds.
    if sys.platform == "darwin" and shutil.which("open"):
        webbrowser.register(
            "macos-open",
            None,
            webbrowser.BackgroundBrowser("open"),
            preferred=True,
        )
        return
    if _browser_available():
        return
    if shutil.which("xdg-open"):
        webbrowser.register(
            "xdg-open",
            None,
            webbrowser.BackgroundBrowser("xdg-open"),
            preferred=True,
        )


def _run_interactive_oauth(creds_path) -> Credentials:
    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    _register_browser_fallback()
    open_browser = _browser_available()
    if not open_browser:
        print(
            "No browser could be opened automatically. "
            "Copy the URL printed next into Chrome or Safari.",
            flush=True,
        )
    return flow.run_local_server(
        port=0,
        open_browser=open_browser,
        access_type="offline",
        prompt="consent",
    )


def get_gmail_service(*, interactive: bool = False):
    ensure_gmail_files()
    creds = _credentials_from_disk_or_env()
    if creds and creds.valid:
        return build("gmail", "v1", credentials=creds)

    if interactive:
        creds_path = gmail_credentials_path()
        if not creds_path.exists():
            raise GmailSetupError(gmail_setup_diagnostics())
        creds = _run_interactive_oauth(creds_path)
        persist_token(creds.to_json())
        return build("gmail", "v1", credentials=creds)

    raise GmailAuthError()


def _is_rate_limit_error(exc: BaseException) -> bool:
    if not isinstance(exc, HttpError):
        return False
    status = getattr(exc.resp, "status", None)
    if status not in (403, 429):
        return False
    content = getattr(exc, "content", b"") or b""
    if isinstance(content, bytes):
        text = content.decode("utf-8", errors="replace")
    else:
        text = str(content)
    text = f"{text} {exc}"
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)


def _execute_with_retry(request: Any) -> Any:
    for attempt in range(_RATE_LIMIT_ATTEMPTS):
        try:
            return request.execute()
        except HttpError as exc:
            if not _is_rate_limit_error(exc) or attempt == _RATE_LIMIT_ATTEMPTS - 1:
                if _is_rate_limit_error(exc):
                    raise GmailRateLimitError() from exc
                raise
            delay = (2**attempt) + random.uniform(0, 0.5)
            logger.warning("Gmail rate limit, retrying in %.1fs", delay)
            time.sleep(delay)
    raise GmailRateLimitError()


def _parse_message(msg: dict) -> EmailMessage:
    headers = msg.get("payload", {}).get("headers", [])
    raw_from = _header(headers, "From")
    _, sender = parseaddr(raw_from)
    subject = _header(headers, "Subject")
    body = _extract_body(msg.get("payload", {}))
    internal_date = msg.get("internalDate")
    received_at = None
    if internal_date:
        received_at = datetime.fromtimestamp(
            int(internal_date) / 1000, tz=timezone.utc
        )
    return EmailMessage(
        message_id=msg["id"],
        sender=sender.lower(),
        subject=subject.strip(),
        body=body,
        received_at=received_at,
    )


def list_recent_message_ids(
    *,
    max_results: int | None = 100,
    query: str = "",
    service=None,
) -> list[str]:
    """Return Gmail message IDs, newest first.

    ``max_results`` caps how many IDs to collect. ``None`` walks every page
    (use with a dated Gmail query for backfills). Each API page is at most 500.
    """
    if service is None:
        service = get_gmail_service()
    remaining = max_results
    ids: list[str] = []
    page_token: str | None = None
    while True:
        page_size = 500 if remaining is None else min(remaining, 500)
        if page_size <= 0:
            break
        list_kwargs: dict = {"userId": "me", "maxResults": page_size}
        if query:
            list_kwargs["q"] = query
        if page_token:
            list_kwargs["pageToken"] = page_token
        response = _execute_with_retry(
            service.users().messages().list(**list_kwargs)
        )
        batch = [ref["id"] for ref in response.get("messages", [])]
        ids.extend(batch)
        if remaining is not None:
            remaining -= len(batch)
            if remaining <= 0:
                return ids[:max_results]
        page_token = response.get("nextPageToken")
        if not page_token or not batch:
            return ids
    return ids


def fetch_messages_by_ids(message_ids: list[str], *, service=None) -> list[EmailMessage]:
    """Load full messages, stopping early if Gmail quota is exhausted."""
    if not message_ids:
        return []
    if service is None:
        service = get_gmail_service()

    messages: list[EmailMessage] = []
    for message_id in message_ids:
        request = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
        )
        try:
            msg = _execute_with_retry(request)
        except GmailRateLimitError:
            if messages:
                logger.warning(
                    "Stopped Gmail fetch after %d message(s) due to rate limit",
                    len(messages),
                )
                return messages
            raise
        messages.append(_parse_message(msg))
    return messages


def fetch_recent_messages(
    *,
    max_results: int = 100,
    query: str = "",
    skip_ids: set[str] | None = None,
) -> list[EmailMessage]:
    service = get_gmail_service()
    message_ids = list_recent_message_ids(
        max_results=max_results, query=query, service=service
    )
    if skip_ids:
        message_ids = [mid for mid in message_ids if mid not in skip_ids]
    return fetch_messages_by_ids(message_ids, service=service)
