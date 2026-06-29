"""Daily email ingestion: match sources, parse bodies, create anchor events."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from tracker.config import INGESTION_SOURCES, IngestionSource
from tracker.db import (
    flag_email_for_review,
    is_email_processed,
    mark_email_processed,
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


def _match_source(email: EmailMessage) -> IngestionSource | None:
    for source in INGESTION_SOURCES:
        if email.sender.lower() != source.sender.lower():
            continue
        if source.subject_regex.match(email.subject.strip()):
            return source
    return None


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
        )
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

    for email in emails:
        if is_email_processed(email.message_id):
            stats.skipped_already_processed += 1
            continue

        source = _match_source(email)
        if source is None:
            stats.no_match += 1
            continue

        stats.processed += 1
        result = process_email(email, dry_run=dry_run)
        if result == "ingested":
            stats.ingested += 1
            logger.info(
                "Ingested %s from message %s",
                source.event_type,
                email.message_id,
            )
        elif result == "needs_review":
            stats.flagged_for_review += 1
            logger.warning(
                "Flagged message %s for manual review (source=%s)",
                email.message_id,
                source.key,
            )

    return stats
