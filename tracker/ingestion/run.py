"""Daily email ingestion: match sources, parse bodies, create anchor events."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from tracker.config import INGESTION_SOURCES, IngestionSource, get_touchpoints_for_event_type
from tracker.db import (
    backfill_email_received_at,
    flag_email_for_review,
    get_or_create_touchpoint_status,
    is_email_processed,
    mark_email_processed,
    reconcile_anchor_event_date,
    upsert_anchor_event,
)
from tracker.ingestion.gmail_client import EmailMessage, fetch_recent_messages
from tracker.ingestion.parsers import ParseError, ParseResult, parse_body

logger = logging.getLogger(__name__)


@dataclass
class IngestionStats:
    processed: int = 0
    ingested: int = 0
    skipped_already_processed: int = 0
    flagged_for_review: int = 0
    no_match: int = 0
    received_dates_backfilled: int = 0
    event_dates_updated: int = 0


def _match_source(email: EmailMessage) -> IngestionSource | None:
    for source in INGESTION_SOURCES:
        if email.sender.lower() != source.sender.lower():
            continue
        if source.subject_regex.match(email.subject.strip()):
            return source
    return None


def _try_sync_processed_email(email: EmailMessage, *, dry_run: bool = False) -> str:
    """Reconcile stored anchor dates from already-processed emails.

    Uses parsed assessment/event dates from the body, not when Gmail received the email.
    Returns: 'date_updated', 'backfilled', or ''.
    """
    source = _match_source(email)
    if source is None:
        return ""

    outcome = parse_body(source.parser_key, email.body)
    if isinstance(outcome, ParseError):
        return ""

    if dry_run:
        return "date_updated"

    if reconcile_anchor_event_date(
        outcome.study_id,
        source.event_type,
        outcome.event_date,
        email_received_at=email.received_at,
    ):
        return "date_updated"

    if email.received_at and backfill_email_received_at(
        outcome.study_id,
        source.event_type,
        email.received_at,
        matching_event_date=outcome.event_date,
    ):
        return "backfilled"

    return ""


@dataclass
class _ParsedEmail:
    email: EmailMessage
    source: IngestionSource
    result: ParseResult
    superseded_by_date: date | None = None


def _email_received_at(email: EmailMessage):
    return email.received_at


def _prefer_newer_email(candidate: _ParsedEmail, incumbent: _ParsedEmail) -> bool:
    """When assessment/event dates tie, prefer the later-arriving email."""
    cand_recv = _email_received_at(candidate.email)
    inc_recv = _email_received_at(incumbent.email)
    if cand_recv and inc_recv:
        return cand_recv > inc_recv
    return bool(cand_recv and not inc_recv)


def _select_latest_emails_per_study(
    emails: list[EmailMessage],
) -> tuple[list[_ParsedEmail], list[_ParsedEmail], list[EmailMessage]]:
    """When multiple emails map to the same study, keep the latest event date."""
    winners: dict[tuple[str, str], _ParsedEmail] = {}
    superseded: list[_ParsedEmail] = []
    unparseable: list[EmailMessage] = []

    for email in emails:
        source = _match_source(email)
        if source is None:
            continue

        outcome = parse_body(source.parser_key, email.body)
        if isinstance(outcome, ParseError):
            unparseable.append(email)
            continue

        key = (source.event_type, outcome.study_id)
        parsed = _ParsedEmail(email=email, source=source, result=outcome)
        existing = winners.get(key)
        if existing is None:
            winners[key] = parsed
            continue
        if outcome.event_date > existing.result.event_date:
            existing.superseded_by_date = outcome.event_date
            superseded.append(existing)
            winners[key] = parsed
        elif outcome.event_date == existing.result.event_date and _prefer_newer_email(
            parsed, existing
        ):
            parsed.superseded_by_date = existing.result.event_date
            superseded.append(existing)
            winners[key] = parsed
        else:
            parsed.superseded_by_date = existing.result.event_date
            superseded.append(parsed)

    return list(winners.values()), superseded, unparseable


def _ingest_parsed_email(parsed: _ParsedEmail, *, dry_run: bool = False) -> str:
    if not dry_run:
        upsert_anchor_event(
            parsed.result.study_id,
            parsed.source.event_type,
            parsed.result.event_date,
            source="email",
            email_received_at=parsed.email.received_at,
        )
        for tp in get_touchpoints_for_event_type(parsed.source.event_type):
            get_or_create_touchpoint_status(parsed.result.study_id, tp.key)
        mark_email_processed(
            parsed.email.message_id,
            outcome="success",
            detail=f"{parsed.source.event_type}:{parsed.result.study_id}",
        )
    return "ingested"


def process_email(email: EmailMessage, *, dry_run: bool = False) -> str:
    """Process a single email. Returns outcome label."""
    if is_email_processed(email.message_id):
        return "already_processed"

    source = _match_source(email)
    if source is None:
        return "no_match"

    outcome = parse_body(source.parser_key, email.body)
    if isinstance(outcome, ParseError):
        if not dry_run:
            mark_email_processed(
                email.message_id,
                outcome="needs_review",
                detail=outcome.reason,
            )
            flag_email_for_review(
                email.message_id,
                email.sender,
                email.subject,
                email.body,
                source.key,
                outcome.reason,
            )
        return "needs_review"

    assert isinstance(outcome, ParseResult)
    if not dry_run:
        upsert_anchor_event(
            outcome.study_id,
            source.event_type,
            outcome.event_date,
            source="email",
            email_received_at=email.received_at,
        )
        for tp in get_touchpoints_for_event_type(source.event_type):
            get_or_create_touchpoint_status(outcome.study_id, tp.key)
        mark_email_processed(
            email.message_id,
            outcome="success",
            detail=f"{source.event_type}:{outcome.study_id}",
        )
    return "ingested"


def run_ingestion(*, dry_run: bool = False, max_results: int = 100) -> IngestionStats:
    """Fetch recent Gmail messages and ingest matching emails."""
    stats = IngestionStats()
    senders = {s.sender for s in INGESTION_SOURCES}
    query = " OR ".join(f"from:{sender}" for sender in senders)

    emails = fetch_recent_messages(max_results=max_results, query=query)
    logger.info("Fetched %d messages from Gmail", len(emails))

    pending: list[EmailMessage] = []
    processed: list[EmailMessage] = []
    for email in emails:
        if is_email_processed(email.message_id):
            stats.skipped_already_processed += 1
            processed.append(email)
            continue

        source = _match_source(email)
        if source is None:
            stats.no_match += 1
            continue

        pending.append(email)

    for email in processed:
        sync_result = _try_sync_processed_email(email, dry_run=dry_run)
        if sync_result == "date_updated":
            stats.event_dates_updated += 1
        elif sync_result == "backfilled":
            stats.received_dates_backfilled += 1

    winners, superseded, unparseable = _select_latest_emails_per_study(pending)
    for parsed in superseded:
        stats.processed += 1
        if not dry_run:
            mark_email_processed(
                parsed.email.message_id,
                outcome="superseded",
                detail=(
                    f"{parsed.source.event_type}:{parsed.result.study_id}:"
                    f"older_than:{parsed.superseded_by_date.isoformat()}"
                ),
            )
        assert parsed.superseded_by_date is not None
        logger.info(
            "Skipped older duplicate for %s (%s < %s)",
            parsed.result.study_id,
            parsed.result.event_date.isoformat(),
            parsed.superseded_by_date.isoformat(),
        )

    for parsed in winners:
        stats.processed += 1
        _ingest_parsed_email(parsed, dry_run=dry_run)
        stats.ingested += 1
        logger.info(
            "Ingested %s from message %s",
            parsed.source.event_type,
            parsed.email.message_id,
        )

    for email in unparseable:
        stats.processed += 1
        result = process_email(email, dry_run=dry_run)
        if result == "needs_review":
            stats.flagged_for_review += 1
            source = _match_source(email)
            logger.warning(
                "Flagged message %s for manual review (source=%s)",
                email.message_id,
                source.key if source else "unknown",
            )

    return stats
