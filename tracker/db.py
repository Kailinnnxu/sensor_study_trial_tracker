"""SQLite persistence for anchor events, touchpoint status, and email processing."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Generator, Iterable

from tracker.config import (
    database_path,
    TOUCHPOINT_OUTCOME_NO_LONGER_INTERESTED,
    TOUCHPOINT_OUTCOME_PENDING,
    TOUCHPOINT_OUTCOME_VISIT_SCHEDULED,
)


@dataclass
class AnchorEvent:
    id: int
    study_id: str
    event_type: str
    event_date: date
    source: str
    created_at: datetime
    email_received_at: datetime | None = None


@dataclass
class TouchpointStatus:
    id: int
    study_id: str
    touchpoint_key: str
    outcome: str
    done_date: date | None
    offsets_sent: list[int]

    @property
    def done(self) -> bool:
        """True when reminders should stop (any non-pending outcome)."""
        return self.outcome != TOUCHPOINT_OUTCOME_PENDING


@dataclass
class ProcessedEmail:
    gmail_message_id: str
    date_processed: datetime
    outcome: str
    detail: str | None = None


@dataclass
class ReviewEmail:
    id: int
    gmail_message_id: str
    sender: str
    subject: str
    body_preview: str
    source_key: str
    reason: str
    created_at: datetime
    resolved: bool


@dataclass
class TouchpointOutcomeRecord:
    study_id: str
    touchpoint_key: str
    outcome: str
    outcome_date: date | None
    anchor_event_type: str
    anchor_event_date: date
    email_received_at: datetime | None
    offsets_sent: list[int]


@dataclass
class TouchpointOutcomeLogEntry:
    id: int
    study_id: str
    touchpoint_key: str
    outcome: str
    previous_outcome: str | None
    recorded_at: datetime


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db(db_path: Path | None = None) -> Generator[sqlite3.Connection, None, None]:
    conn = _connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS anchor_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                study_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_date TEXT NOT NULL,
                source TEXT NOT NULL,
                email_received_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(study_id, event_type)
            );

            CREATE TABLE IF NOT EXISTS touchpoint_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                study_id TEXT NOT NULL,
                touchpoint_key TEXT NOT NULL,
                done INTEGER NOT NULL DEFAULT 0,
                done_date TEXT,
                outcome TEXT NOT NULL DEFAULT 'pending',
                offsets_sent TEXT NOT NULL DEFAULT '[]',
                UNIQUE(study_id, touchpoint_key)
            );

            CREATE TABLE IF NOT EXISTS processed_emails (
                gmail_message_id TEXT PRIMARY KEY,
                date_processed TEXT NOT NULL,
                outcome TEXT NOT NULL,
                detail TEXT
            );

            CREATE TABLE IF NOT EXISTS review_emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gmail_message_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                subject TEXT NOT NULL,
                body_preview TEXT NOT NULL,
                source_key TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                resolved INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS touchpoint_outcome_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                study_id TEXT NOT NULL,
                touchpoint_key TEXT NOT NULL,
                outcome TEXT NOT NULL,
                previous_outcome TEXT,
                recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_outcome_log_recorded
                ON touchpoint_outcome_log (recorded_at DESC);
            CREATE INDEX IF NOT EXISTS idx_outcome_log_study
                ON touchpoint_outcome_log (study_id, touchpoint_key);
            """
        )
        _migrate_schema(conn)


def _migrate_schema(conn: sqlite3.Connection) -> None:
    anchor_cols = {row[1] for row in conn.execute("PRAGMA table_info(anchor_events)")}
    if "email_received_at" not in anchor_cols:
        conn.execute("ALTER TABLE anchor_events ADD COLUMN email_received_at TEXT")

    tp_cols = {row[1] for row in conn.execute("PRAGMA table_info(touchpoint_status)")}
    if "outcome" not in tp_cols:
        conn.execute(
            "ALTER TABLE touchpoint_status ADD COLUMN outcome TEXT NOT NULL DEFAULT 'pending'"
        )
        conn.execute(
            f"""
            UPDATE touchpoint_status
            SET outcome = '{TOUCHPOINT_OUTCOME_VISIT_SCHEDULED}'
            WHERE done = 1
            """
        )

    log_exists = conn.execute(
        "SELECT 1 FROM touchpoint_outcome_log LIMIT 1"
    ).fetchone()
    if log_exists is None:
        conn.execute(
            f"""
            INSERT INTO touchpoint_outcome_log
                (study_id, touchpoint_key, outcome, previous_outcome, recorded_at)
            SELECT
                study_id,
                touchpoint_key,
                outcome,
                '{TOUCHPOINT_OUTCOME_PENDING}',
                COALESCE(done_date || 'T12:00:00', datetime('now'))
            FROM touchpoint_status
            WHERE outcome != '{TOUCHPOINT_OUTCOME_PENDING}'
            """
        )


def _parse_received_at(raw: str | None) -> datetime | None:
    if not raw:
        return None
    return datetime.fromisoformat(raw)


def _row_to_anchor(row: sqlite3.Row) -> AnchorEvent:
    keys = row.keys()
    received = row["email_received_at"] if "email_received_at" in keys else None
    return AnchorEvent(
        id=row["id"],
        study_id=row["study_id"],
        event_type=row["event_type"],
        event_date=date.fromisoformat(row["event_date"]),
        source=row["source"],
        created_at=datetime.fromisoformat(row["created_at"]),
        email_received_at=_parse_received_at(received),
    )


def _row_to_status(row: sqlite3.Row) -> TouchpointStatus:
    keys = row.keys()
    if "outcome" in keys and row["outcome"]:
        outcome = row["outcome"]
    elif bool(row["done"]):
        outcome = TOUCHPOINT_OUTCOME_VISIT_SCHEDULED
    else:
        outcome = TOUCHPOINT_OUTCOME_PENDING
    return TouchpointStatus(
        id=row["id"],
        study_id=row["study_id"],
        touchpoint_key=row["touchpoint_key"],
        outcome=outcome,
        done_date=date.fromisoformat(row["done_date"]) if row["done_date"] else None,
        offsets_sent=json.loads(row["offsets_sent"]),
    )


def upsert_anchor_event(
    study_id: str,
    event_type: str,
    event_date: date,
    source: str,
    *,
    email_received_at: datetime | None = None,
    db_path: Path | None = None,
) -> AnchorEvent:
    received_str = email_received_at.isoformat() if email_received_at else None
    with get_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO anchor_events (study_id, event_type, event_date, source, email_received_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(study_id, event_type) DO UPDATE SET
                event_date = CASE
                    WHEN excluded.event_date > anchor_events.event_date
                    THEN excluded.event_date
                    ELSE anchor_events.event_date
                END,
                source = CASE
                    WHEN excluded.event_date > anchor_events.event_date
                    THEN excluded.source
                    ELSE anchor_events.source
                END,
                email_received_at = CASE
                    WHEN excluded.event_date > anchor_events.event_date
                    THEN excluded.email_received_at
                    WHEN excluded.event_date = anchor_events.event_date
                    THEN COALESCE(excluded.email_received_at, anchor_events.email_received_at)
                    ELSE anchor_events.email_received_at
                END
            """,
            (study_id.strip(), event_type, event_date.isoformat(), source, received_str),
        )
        row = conn.execute(
            """
            SELECT * FROM anchor_events
            WHERE study_id = ? AND event_type = ?
            """,
            (study_id.strip(), event_type),
        ).fetchone()
        assert row is not None
        return _row_to_anchor(row)


def backfill_email_received_at(
    study_id: str,
    event_type: str,
    email_received_at: datetime,
    *,
    matching_event_date: date | None = None,
    db_path: Path | None = None,
) -> bool:
    """Set email_received_at when missing, only for emails matching the anchor date."""
    with get_db(db_path) as conn:
        row = conn.execute(
            """
            SELECT event_date, email_received_at FROM anchor_events
            WHERE study_id = ? AND event_type = ?
            """,
            (study_id.strip(), event_type),
        ).fetchone()
        if row is None or row["email_received_at"]:
            return False
        anchor_date = date.fromisoformat(row["event_date"])
        if matching_event_date is not None and matching_event_date != anchor_date:
            return False
        conn.execute(
            """
            UPDATE anchor_events
            SET email_received_at = ?
            WHERE study_id = ? AND event_type = ?
            """,
            (email_received_at.isoformat(), study_id.strip(), event_type),
        )
    return True


def reconcile_anchor_event_date(
    study_id: str,
    event_type: str,
    event_date: date,
    *,
    email_received_at: datetime | None = None,
    db_path: Path | None = None,
) -> bool:
    """Promote anchor to a later parsed assessment/event date. Returns True if updated."""
    received_str = email_received_at.isoformat() if email_received_at else None
    with get_db(db_path) as conn:
        row = conn.execute(
            """
            SELECT event_date FROM anchor_events
            WHERE study_id = ? AND event_type = ?
            """,
            (study_id.strip(), event_type),
        ).fetchone()
        if row is None:
            return False
        current = date.fromisoformat(row["event_date"])
        if event_date <= current:
            return False
        conn.execute(
            """
            UPDATE anchor_events
            SET event_date = ?, source = 'email',
                email_received_at = COALESCE(?, email_received_at)
            WHERE study_id = ? AND event_type = ?
            """,
            (
                event_date.isoformat(),
                received_str,
                study_id.strip(),
                event_type,
            ),
        )
    return True


def get_anchor_events(
    event_type: str | None = None,
    study_id: str | None = None,
    *,
    db_path: Path | None = None,
) -> list[AnchorEvent]:
    query = "SELECT * FROM anchor_events WHERE 1=1"
    params: list[str] = []
    if event_type:
        query += " AND event_type = ?"
        params.append(event_type)
    if study_id:
        query += " AND study_id = ?"
        params.append(study_id)
    query += " ORDER BY study_id, event_type"
    with get_db(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_anchor(r) for r in rows]


def get_all_study_ids(*, db_path: Path | None = None) -> list[str]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT study_id FROM anchor_events ORDER BY study_id"
        ).fetchall()
    return [r["study_id"] for r in rows]


def get_or_create_touchpoint_status(
    study_id: str,
    touchpoint_key: str,
    *,
    db_path: Path | None = None,
) -> TouchpointStatus:
    with get_db(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM touchpoint_status
            WHERE study_id = ? AND touchpoint_key = ?
            """,
            (study_id, touchpoint_key),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO touchpoint_status (study_id, touchpoint_key)
                VALUES (?, ?)
                """,
                (study_id, touchpoint_key),
            )
            row = conn.execute(
                """
                SELECT * FROM touchpoint_status
                WHERE study_id = ? AND touchpoint_key = ?
                """,
                (study_id, touchpoint_key),
            ).fetchone()
        assert row is not None
        return _row_to_status(row)


def _log_touchpoint_outcome_change(
    conn: sqlite3.Connection,
    study_id: str,
    touchpoint_key: str,
    outcome: str,
    previous_outcome: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO touchpoint_outcome_log
        (study_id, touchpoint_key, outcome, previous_outcome)
        VALUES (?, ?, ?, ?)
        """,
        (study_id, touchpoint_key, outcome, previous_outcome),
    )


def set_touchpoint_outcome(
    study_id: str,
    touchpoint_key: str,
    outcome: str,
    *,
    db_path: Path | None = None,
) -> TouchpointStatus:
    if outcome not in (
        TOUCHPOINT_OUTCOME_PENDING,
        TOUCHPOINT_OUTCOME_NO_LONGER_INTERESTED,
        TOUCHPOINT_OUTCOME_VISIT_SCHEDULED,
    ):
        raise ValueError(f"Invalid touchpoint outcome: {outcome}")

    closed = outcome != TOUCHPOINT_OUTCOME_PENDING
    done_date = date.today().isoformat() if closed else None
    with get_db(db_path) as conn:
        existing = conn.execute(
            """
            SELECT outcome FROM touchpoint_status
            WHERE study_id = ? AND touchpoint_key = ?
            """,
            (study_id, touchpoint_key),
        ).fetchone()
        previous_outcome = (
            existing["outcome"] if existing else TOUCHPOINT_OUTCOME_PENDING
        )

        conn.execute(
            """
            INSERT INTO touchpoint_status
            (study_id, touchpoint_key, done, done_date, outcome)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(study_id, touchpoint_key) DO UPDATE SET
                done = excluded.done,
                done_date = excluded.done_date,
                outcome = excluded.outcome
            """,
            (study_id, touchpoint_key, int(closed), done_date, outcome),
        )
        if previous_outcome != outcome:
            _log_touchpoint_outcome_change(
                conn, study_id, touchpoint_key, outcome, previous_outcome
            )
        row = conn.execute(
            """
            SELECT * FROM touchpoint_status
            WHERE study_id = ? AND touchpoint_key = ?
            """,
            (study_id, touchpoint_key),
        ).fetchone()
        assert row is not None
        return _row_to_status(row)


def set_touchpoint_done(
    study_id: str,
    touchpoint_key: str,
    done: bool,
    *,
    db_path: Path | None = None,
) -> TouchpointStatus:
    """Backward-compatible wrapper."""
    outcome = (
        TOUCHPOINT_OUTCOME_VISIT_SCHEDULED
        if done
        else TOUCHPOINT_OUTCOME_PENDING
    )
    return set_touchpoint_outcome(study_id, touchpoint_key, outcome, db_path=db_path)


def record_offset_sent(
    study_id: str,
    touchpoint_key: str,
    offset: int,
    *,
    db_path: Path | None = None,
) -> TouchpointStatus:
    status = get_or_create_touchpoint_status(study_id, touchpoint_key, db_path=db_path)
    offsets = list(status.offsets_sent)
    if offset not in offsets:
        offsets.append(offset)
        offsets.sort()
    with get_db(db_path) as conn:
        conn.execute(
            """
            UPDATE touchpoint_status
            SET offsets_sent = ?
            WHERE study_id = ? AND touchpoint_key = ?
            """,
            (json.dumps(offsets), study_id, touchpoint_key),
        )
        row = conn.execute(
            """
            SELECT * FROM touchpoint_status
            WHERE study_id = ? AND touchpoint_key = ?
            """,
            (study_id, touchpoint_key),
        ).fetchone()
        assert row is not None
        return _row_to_status(row)


def is_email_processed(gmail_message_id: str, *, db_path: Path | None = None) -> bool:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_emails WHERE gmail_message_id = ?",
            (gmail_message_id,),
        ).fetchone()
    return row is not None


def mark_email_processed(
    gmail_message_id: str,
    outcome: str,
    detail: str | None = None,
    *,
    db_path: Path | None = None,
) -> None:
    with get_db(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO processed_emails
            (gmail_message_id, date_processed, outcome, detail)
            VALUES (?, ?, ?, ?)
            """,
            (gmail_message_id, datetime.now().isoformat(), outcome, detail),
        )


def flag_email_for_review(
    gmail_message_id: str,
    sender: str,
    subject: str,
    body_preview: str,
    source_key: str,
    reason: str,
    *,
    db_path: Path | None = None,
) -> None:
    with get_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO review_emails
            (gmail_message_id, sender, subject, body_preview, source_key, reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (gmail_message_id, sender, subject, body_preview[:2000], source_key, reason),
        )


def get_review_emails(*, unresolved_only: bool = True, db_path: Path | None = None) -> list[ReviewEmail]:
    query = "SELECT * FROM review_emails"
    if unresolved_only:
        query += " WHERE resolved = 0"
    query += " ORDER BY created_at DESC"
    with get_db(db_path) as conn:
        rows = conn.execute(query).fetchall()
    return [
        ReviewEmail(
            id=r["id"],
            gmail_message_id=r["gmail_message_id"],
            sender=r["sender"],
            subject=r["subject"],
            body_preview=r["body_preview"],
            source_key=r["source_key"],
            reason=r["reason"],
            created_at=datetime.fromisoformat(r["created_at"]),
            resolved=bool(r["resolved"]),
        )
        for r in rows
    ]

def get_touchpoint_statuses_for_study(
    study_id: str,
    *,
    db_path: Path | None = None,
) -> dict[str, TouchpointStatus]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM touchpoint_status WHERE study_id = ?",
            (study_id,),
        ).fetchall()
    return {r["touchpoint_key"]: _row_to_status(r) for r in rows}


def get_closed_touchpoint_records(
    *,
    outcome: str | None = None,
    db_path: Path | None = None,
) -> list[TouchpointOutcomeRecord]:
    """Current closed touchpoints joined with their anchor events."""
    from tracker.config import TOUCHPOINT_BY_KEY

    records: list[TouchpointOutcomeRecord] = []
    with get_db(db_path) as conn:
        params: list[str] = [TOUCHPOINT_OUTCOME_PENDING]
        status_query = """
            SELECT * FROM touchpoint_status
            WHERE outcome != ?
        """
        if outcome:
            status_query += " AND outcome = ?"
            params.append(outcome)
        status_query += " ORDER BY done_date DESC, study_id, touchpoint_key"
        status_rows = conn.execute(status_query, params).fetchall()

        for row in status_rows:
            tp_def = TOUCHPOINT_BY_KEY.get(row["touchpoint_key"])
            if tp_def is None:
                continue
            anchor = conn.execute(
                """
                SELECT * FROM anchor_events
                WHERE study_id = ? AND event_type = ?
                """,
                (row["study_id"], tp_def.anchor_event_type),
            ).fetchone()
            if anchor is None:
                continue
            records.append(
                TouchpointOutcomeRecord(
                    study_id=row["study_id"],
                    touchpoint_key=row["touchpoint_key"],
                    outcome=row["outcome"],
                    outcome_date=(
                        date.fromisoformat(row["done_date"])
                        if row["done_date"]
                        else None
                    ),
                    anchor_event_type=anchor["event_type"],
                    anchor_event_date=date.fromisoformat(anchor["event_date"]),
                    email_received_at=_parse_received_at(anchor["email_received_at"]),
                    offsets_sent=json.loads(row["offsets_sent"]),
                )
            )
    return records


def get_touchpoint_outcome_log(
    *,
    limit: int | None = None,
    db_path: Path | None = None,
) -> list[TouchpointOutcomeLogEntry]:
    query = """
        SELECT * FROM touchpoint_outcome_log
        ORDER BY recorded_at DESC, id DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    with get_db(db_path) as conn:
        rows = conn.execute(query).fetchall()
    return [
        TouchpointOutcomeLogEntry(
            id=r["id"],
            study_id=r["study_id"],
            touchpoint_key=r["touchpoint_key"],
            outcome=r["outcome"],
            previous_outcome=r["previous_outcome"],
            recorded_at=datetime.fromisoformat(r["recorded_at"]),
        )
        for r in rows
    ]
