"""Touchpoint definitions, ingestion sources, and environment configuration."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TouchpointDefinition:
    key: str
    anchor_event_type: str
    offsets: tuple[int, ...]
    action_type: str
    label: str


@dataclass(frozen=True)
class IngestionSource:
    key: str
    sender: str
    subject_pattern: str
    event_type: str
    parser_key: str

    @property
    def subject_regex(self) -> re.Pattern[str]:
        return re.compile(self.subject_pattern, re.IGNORECASE)


# ---------------------------------------------------------------------------
# Touchpoint definitions — add new touchpoints here only.
# ---------------------------------------------------------------------------

TOUCHPOINT_DEFINITIONS: tuple[TouchpointDefinition, ...] = (
    TouchpointDefinition(
        key="schedule_home_visit",
        anchor_event_type="assessment_complete",
        offsets=(0, 3, 7),
        action_type="email_kailin",
        label="Schedule home visit",
    ),
    TouchpointDefinition(
        key="sensor_dropoff_reminder",
        anchor_event_type="sensor_collection_start",
        offsets=(9,),
        action_type="webex_call",
        label="Sensor drop-off reminder",
    ),
)

TOUCHPOINT_BY_KEY = {tp.key: tp for tp in TOUCHPOINT_DEFINITIONS}

# ---------------------------------------------------------------------------
# Ingestion sources — add new email templates here only.
# ---------------------------------------------------------------------------

INGESTION_SOURCES: tuple[IngestionSource, ...] = (
    IngestionSource(
        key="hai_assessment_complete",
        sender="hai@hsl.harvard.edu",
        subject_pattern=r"^HAI Y\d+ Visit Completed$",
        event_type="assessment_complete",
        parser_key="hai_assessment",
    ),
    IngestionSource(
        key="hai_sensor_collection",
        sender="hai@hsl.harvard.edu",
        subject_pattern=r"^Sensor data collection trigger$",
        event_type="sensor_collection_start",
        parser_key="hai_sensor",
    ),
)

# Anchor event types available for manual entry (derived from ingestion + touchpoints).
ANCHOR_EVENT_TYPES: tuple[str, ...] = tuple(
    sorted(
        {src.event_type for src in INGESTION_SOURCES}
        | {tp.anchor_event_type for tp in TOUCHPOINT_DEFINITIONS}
    )
)


def get_touchpoints_for_event_type(event_type: str) -> list[TouchpointDefinition]:
    return [tp for tp in TOUCHPOINT_DEFINITIONS if tp.anchor_event_type == event_type]


# ---------------------------------------------------------------------------
# Environment configuration
# ---------------------------------------------------------------------------

def _env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


def database_path() -> Path:
    raw = _env("TRACKER_DATABASE_PATH", "data/tracker.db")
    return Path(raw)


def gmail_credentials_path() -> Path:
    return Path(_env("GMAIL_CREDENTIALS_PATH", "credentials/gmail_credentials.json"))


def gmail_token_path() -> Path:
    return Path(_env("GMAIL_TOKEN_PATH", "credentials/gmail_token.json"))


def kailin_email() -> str:
    value = _env("KAILIN_EMAIL")
    if not value:
        raise RuntimeError("KAILIN_EMAIL environment variable is required for email actions")
    return value


def smtp_host() -> str:
    return _env("SMTP_HOST", "smtp.gmail.com")


def smtp_port() -> int:
    return int(_env("SMTP_PORT", "587"))


def smtp_user() -> str | None:
    return _env("SMTP_USER")


def smtp_password() -> str | None:
    return _env("SMTP_PASSWORD")


def flask_secret_key() -> str:
    return _env("FLASK_SECRET_KEY", "dev-only-change-in-production")


def app_url() -> str:
    return _env("APP_URL", "http://localhost:5000")
