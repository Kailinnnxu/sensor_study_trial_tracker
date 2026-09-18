"""Touchpoint definitions, ingestion sources, and environment configuration."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


# Tracker project root (parent of the `tracker` package).
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_project_path(raw: str) -> Path:
    """Resolve config paths relative to PROJECT_ROOT, not the process cwd."""
    path = Path(raw)
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()


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
        key="sensor_collection_followup",
        anchor_event_type="sensor_collection_start",
        offsets=(2,),
        action_type="email_kailin",
        label="Sensor collection follow-up call",
    ),
    TouchpointDefinition(
        key="sensor_dropoff_reminder",
        anchor_event_type="sensor_collection_start",
        offsets=(9,),
        action_type="webex_call",
        label="Sensor drop-off (Webex call)",
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
        key="kailin_sensor_collection",
        sender="kailinxu@hsl.harvard.edu",
        subject_pattern=r"^Sensor data collection trigger$",
        event_type="sensor_collection_start",
        parser_key="sensor_collection",
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


DASHBOARD_PHASES: tuple[dict[str, str | bool], ...] = (
    {
        "key": "phase1",
        "title": "Phase 1 — Home visit scheduling",
        "anchor_event_type": "assessment_complete",
        "show_actions": True,
    },
    {
        "key": "phase2",
        "title": "Phase 2 — Sensor collection",
        "anchor_event_type": "sensor_collection_start",
        "show_actions": True,
    },
)


# ---------------------------------------------------------------------------
# Environment configuration
# ---------------------------------------------------------------------------

def _env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


def _writable_dir(path: Path) -> bool:
    try:
        return path.is_dir() and os.access(path, os.W_OK)
    except OSError:
        return False


def persistent_volume_dir() -> Path | None:
    """Writable volume mount (Railway `/data`), if present."""
    env_mount = _env("RAILWAY_VOLUME_MOUNT_PATH")
    candidates = [Path(env_mount)] if env_mount else []
    candidates.append(Path("/data"))
    seen: set[Path] = set()
    for path in candidates:
        resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        if _writable_dir(resolved):
            return resolved
    return None


def _path_is_on_volume(path: Path, volume: Path) -> bool:
    try:
        path.resolve().relative_to(volume.resolve())
        return True
    except ValueError:
        return False


def database_path() -> Path:
    configured = _env("TRACKER_DATABASE_PATH", "data/tracker.db")
    path = _resolve_project_path(configured)
    volume = persistent_volume_dir()
    # Keep SQLite on the mounted volume so Railway deploys cannot wipe records.
    if volume is not None and not _path_is_on_volume(path, volume):
        return (volume / "tracker.db").resolve()
    return path


def database_is_ephemeral() -> bool:
    """True when running on Railway without a usable persistent volume."""
    on_railway = bool(_env("RAILWAY_ENVIRONMENT") or _env("RAILWAY_PROJECT_ID"))
    if not on_railway:
        return False
    volume = persistent_volume_dir()
    if volume is None:
        return True
    return not _path_is_on_volume(database_path(), volume)


def gmail_credentials_path() -> Path:
    return _resolve_project_path(
        _env("GMAIL_CREDENTIALS_PATH", "credentials/gmail_credentials.json")
    )


def gmail_token_path() -> Path:
    return _resolve_project_path(_env("GMAIL_TOKEN_PATH", "credentials/gmail_token.json"))


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


# Touchpoint action outcomes. Pending stays on the dashboard; all others
# move to the repository and stop reminders.
TOUCHPOINT_OUTCOME_PENDING = "pending"
TOUCHPOINT_OUTCOME_NOT_WITHIN_TIMEFRAME = "not_within_timeframe"
TOUCHPOINT_OUTCOME_CALL_NOT_ANSWERED = "call_not_answered"
TOUCHPOINT_OUTCOME_REJECTED = "rejected"
TOUCHPOINT_OUTCOME_ENROLLED = "enrolled"
TOUCHPOINT_OUTCOME_ONGOING = "ongoing"
TOUCHPOINT_OUTCOME_FINISHED = "finished"

TOUCHPOINT_ACTION_OUTCOMES: tuple[str, ...] = (
    TOUCHPOINT_OUTCOME_NOT_WITHIN_TIMEFRAME,
    TOUCHPOINT_OUTCOME_CALL_NOT_ANSWERED,
    TOUCHPOINT_OUTCOME_REJECTED,
    TOUCHPOINT_OUTCOME_ENROLLED,
    TOUCHPOINT_OUTCOME_ONGOING,
    TOUCHPOINT_OUTCOME_FINISHED,
)

TOUCHPOINT_OUTCOMES: tuple[str, ...] = (
    TOUCHPOINT_OUTCOME_PENDING,
    *TOUCHPOINT_ACTION_OUTCOMES,
)

TOUCHPOINT_OUTCOME_LABELS: dict[str, str] = {
    TOUCHPOINT_OUTCOME_PENDING: "Pending",
    TOUCHPOINT_OUTCOME_NOT_WITHIN_TIMEFRAME: "Not within timeframe",
    TOUCHPOINT_OUTCOME_CALL_NOT_ANSWERED: "Call not answered",
    TOUCHPOINT_OUTCOME_REJECTED: "Rejected",
    TOUCHPOINT_OUTCOME_ENROLLED: "Enrolled",
    TOUCHPOINT_OUTCOME_ONGOING: "Ongoing",
    TOUCHPOINT_OUTCOME_FINISHED: "Finished",
}

TOUCHPOINT_ACTION_CHOICES: tuple[tuple[str, str], ...] = tuple(
    (key, TOUCHPOINT_OUTCOME_LABELS[key]) for key in TOUCHPOINT_ACTION_OUTCOMES
)

# Legacy values rewritten on startup.
LEGACY_OUTCOME_MAP: dict[str, str] = {
    "visit_scheduled": TOUCHPOINT_OUTCOME_ENROLLED,
    "no_longer_interested": TOUCHPOINT_OUTCOME_REJECTED,
    "done": TOUCHPOINT_OUTCOME_FINISHED,
}
