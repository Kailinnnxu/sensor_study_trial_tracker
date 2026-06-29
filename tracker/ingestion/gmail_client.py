"""Gmail API client for fetching unprocessed emails."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from email.utils import parseaddr
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from tracker.config import gmail_credentials_path, gmail_token_path
from tracker.gmail_secrets import ensure_gmail_files, persist_token

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


@dataclass
class EmailMessage:
    message_id: str
    sender: str
    subject: str
    body: str


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


def get_gmail_service():
    ensure_gmail_files()
    creds_path = gmail_credentials_path()
    token_path = gmail_token_path()
    creds = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            persist_token(creds.to_json())
        else:
            if not creds_path.exists():
                raise FileNotFoundError(
                    f"Gmail credentials not found at {creds_path}. "
                    "Run scripts/setup_gmail_oauth.py locally, or set "
                    "GMAIL_CREDENTIALS_JSON on Railway."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
            persist_token(creds.to_json())
    elif creds.valid:
        persist_token(creds.to_json())

    return build("gmail", "v1", credentials=creds)


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
        messages.append(
            EmailMessage(
                message_id=msg["id"],
                sender=sender.lower(),
                subject=subject.strip(),
                body=body,
            )
        )

    return messages
