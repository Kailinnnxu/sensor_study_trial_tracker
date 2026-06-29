"""Action handlers dispatched by the reminder engine."""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from datetime import date
from email.mime.text import MIMEText
from typing import Callable

from tracker.config import (
    TOUCHPOINT_BY_KEY,
    kailin_email,
    smtp_host,
    smtp_password,
    smtp_port,
    smtp_user,
)

logger = logging.getLogger(__name__)


@dataclass
class ActionContext:
    study_id: str
    touchpoint_key: str
    offset: int
    anchor_date: date
    days_since_anchor: int


def _message_for_schedule_home_visit(ctx: ActionContext) -> tuple[str, str]:
    tp = TOUCHPOINT_BY_KEY[ctx.touchpoint_key]
    if ctx.offset == 0:
        subject = f"[Tracker] New participant ready to call: {ctx.study_id}"
        body = (
            f"Study ID {ctx.study_id} has completed assessment "
            f"(anchor date {ctx.anchor_date.isoformat()}).\n\n"
            "Action needed: call to schedule a home visit."
        )
    else:
        subject = f"[Tracker] Home visit reminder ({ctx.offset}d): {ctx.study_id}"
        body = (
            f"It's been {ctx.days_since_anchor} days since assessment completion "
            f"for study ID {ctx.study_id} (anchor date {ctx.anchor_date.isoformat()}).\n\n"
            f"This is the {ctx.offset}-day reminder to schedule a home visit."
        )
    return subject, body


def _message_for_sensor_dropoff(ctx: ActionContext) -> tuple[str, str]:
    subject = f"[Tracker] Sensor drop-off due: {ctx.study_id}"
    body = (
        f"Study ID {ctx.study_id} reached the {ctx.offset}-day sensor drop-off "
        f"reminder (sensor collection started {ctx.anchor_date.isoformat()})."
    )
    return subject, body


MESSAGE_BUILDERS: dict[str, Callable[[ActionContext], tuple[str, str]]] = {
    "schedule_home_visit": _message_for_schedule_home_visit,
    "sensor_dropoff_reminder": _message_for_sensor_dropoff,
}


def _send_email(subject: str, body: str) -> None:
    user = smtp_user()
    password = smtp_password()
    if not user or not password:
        logger.info("SMTP not configured; would send email:\nSubject: %s\n%s", subject, body)
        return

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = kailin_email()

    with smtplib.SMTP(smtp_host(), smtp_port()) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(user, [kailin_email()], msg.as_string())

    logger.info("Sent email to %s: %s", kailin_email(), subject)


def handle_email_kailin(ctx: ActionContext) -> None:
    builder = MESSAGE_BUILDERS.get(ctx.touchpoint_key)
    if builder:
        subject, body = builder(ctx)
    else:
        subject = f"[Tracker] Reminder for {ctx.study_id}"
        body = (
            f"Touchpoint {ctx.touchpoint_key} offset {ctx.offset} is due "
            f"for study ID {ctx.study_id}."
        )
    _send_email(subject, body)


def handle_webex_call(ctx: ActionContext) -> None:
    """Stub: real Webex Contact Center API integration goes here later."""
    logger.info(
        "would place Webex call for study_id=%s touchpoint=%s offset=%d",
        ctx.study_id,
        ctx.touchpoint_key,
        ctx.offset,
    )


ACTION_HANDLERS: dict[str, Callable[[ActionContext], None]] = {
    "email_kailin": handle_email_kailin,
    "webex_call": handle_webex_call,
}


def dispatch_action(action_type: str, ctx: ActionContext) -> None:
    handler = ACTION_HANDLERS.get(action_type)
    if handler is None:
        raise ValueError(f"No handler registered for action type: {action_type}")
    handler(ctx)
