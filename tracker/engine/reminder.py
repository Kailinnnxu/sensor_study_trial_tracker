"""Daily reminder engine: evaluate offsets and dispatch actions."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from tracker.config import TOUCHPOINT_DEFINITIONS, TouchpointDefinition
from tracker.db import (
    get_anchor_events,
    get_or_create_touchpoint_status,
    record_offset_sent,
)
from tracker.engine.actions import ActionContext, dispatch_action

logger = logging.getLogger(__name__)


@dataclass
class ReminderAction:
    study_id: str
    touchpoint_key: str
    offset: int
    action_type: str


@dataclass
class ReminderStats:
    participants_checked: int = 0
    actions_taken: list[ReminderAction] = field(default_factory=list)


def _due_offsets(
    touchpoint: TouchpointDefinition,
    anchor_date: date,
    offsets_sent: list[int],
    today: date,
) -> list[int]:
    days_since = (today - anchor_date).days
    due: list[int] = []
    for offset in touchpoint.offsets:
        if offset in offsets_sent:
            continue
        if days_since >= offset:
            due.append(offset)
    return due


def run_reminders(*, today: date | None = None, dry_run: bool = False) -> ReminderStats:
    """Evaluate all touchpoints and fire due actions."""
    today = today or date.today()
    stats = ReminderStats()

    for touchpoint in TOUCHPOINT_DEFINITIONS:
        anchors = get_anchor_events(event_type=touchpoint.anchor_event_type)
        for anchor in anchors:
            stats.participants_checked += 1
            status = get_or_create_touchpoint_status(anchor.study_id, touchpoint.key)

            if status.done:
                continue

            due = _due_offsets(touchpoint, anchor.event_date, status.offsets_sent, today)
            for offset in due:
                ctx = ActionContext(
                    study_id=anchor.study_id,
                    touchpoint_key=touchpoint.key,
                    offset=offset,
                    anchor_date=anchor.event_date,
                    days_since_anchor=(today - anchor.event_date).days,
                )
                if not dry_run:
                    dispatch_action(touchpoint.action_type, ctx)
                    record_offset_sent(anchor.study_id, touchpoint.key, offset)
                stats.actions_taken.append(
                    ReminderAction(
                        study_id=anchor.study_id,
                        touchpoint_key=touchpoint.key,
                        offset=offset,
                        action_type=touchpoint.action_type,
                    )
                )
                logger.info(
                    "Action %s for %s/%s offset %d",
                    touchpoint.action_type,
                    anchor.study_id,
                    touchpoint.key,
                    offset,
                )

    return stats
