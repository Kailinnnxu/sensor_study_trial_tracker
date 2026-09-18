"""Gmail API client for fetching unprocessed emails."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

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
    return flow.run_local_server(port=0, open_browser=open_browser)


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


def fetch_recent_messages(
    *,
    max_results: int = 100,
    query: str = "",
) -> list[EmailMessage]:
    service = get_gmail_service()
    list_kwargs: dict = {"userId": "me", "maxResults": max_results}
    if query:
        list_kwargs["q"] = query

    response = service.users().messages().list(**list_kwargs).execute()
    message_refs = response.get("messages", [])
    messages: list[EmailMessage] = []

    for ref in message_refs:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=ref["id"], format="full")
            .execute()
        )
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
        messages.append(
            EmailMessage(
                message_id=msg["id"],
                sender=sender.lower(),
                subject=subject.strip(),
                body=body,
                received_at=received_at,
            )
        )

    return messages
