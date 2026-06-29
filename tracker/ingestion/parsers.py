"""Email body parsers registered by key for ingestion sources."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable


@dataclass
class ParseResult:
    study_id: str
    event_date: date


@dataclass
class ParseError:
    reason: str


ParseOutcome = ParseResult | ParseError


def _parse_hai_assessment(body: str) -> ParseOutcome:
    """Parse HAI assessment-complete email body.

  Expected patterns:
    - Study ID: "<id> has completed..."
    - Date: "completed on MM-DD-YYYY"
    """
    study_match = re.search(r"(\S+)\s+has completed", body, re.IGNORECASE)
    if not study_match:
        return ParseError("Could not find study ID (expected '<id> has completed...')")

    date_match = re.search(r"completed on\s+(\d{2}-\d{2}-\d{4})", body, re.IGNORECASE)
    if not date_match:
        return ParseError("Could not find completion date (expected 'completed on MM-DD-YYYY')")

    try:
        event_date = datetime.strptime(date_match.group(1), "%m-%d-%Y").date()
    except ValueError:
        return ParseError(f"Invalid date format: {date_match.group(1)}")

    return ParseResult(study_id=study_match.group(1).strip(), event_date=event_date)


def _parse_hai_sensor(body: str) -> ParseOutcome:
    """Parse sensor collection trigger email body.

  Expected patterns:
    ID: <record_id>
    Date: <sensor_startdate>
    """
    id_match = re.search(r"^ID:\s*(.+)$", body, re.IGNORECASE | re.MULTILINE)
    if not id_match:
        return ParseError("Could not find study ID (expected 'ID: <record_id>')")

    date_match = re.search(r"^Date:\s*(.+)$", body, re.IGNORECASE | re.MULTILINE)
    if not date_match:
        return ParseError("Could not find date (expected 'Date: <sensor_startdate>')")

    raw_date = date_match.group(1).strip()
    for fmt in ("%m-%d-%Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            event_date = datetime.strptime(raw_date, fmt).date()
            break
        except ValueError:
            continue
    else:
        return ParseError(f"Unrecognized date format: {raw_date}")

    return ParseResult(study_id=id_match.group(1).strip(), event_date=event_date)


PARSERS: dict[str, Callable[[str], ParseOutcome]] = {
    "hai_assessment": _parse_hai_assessment,
    "hai_sensor": _parse_hai_sensor,
}


def parse_body(parser_key: str, body: str) -> ParseOutcome:
    parser = PARSERS.get(parser_key)
    if parser is None:
        return ParseError(f"Unknown parser: {parser_key}")
    return parser(body)
