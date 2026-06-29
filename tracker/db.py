"""SQLite persistence for anchor events, touchpoint status, and email processing."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Generator, Iterable

from tracker.config import database_path


@dataclass
class AnchorEvent:
    id: int
    study_id: str
    event_type: str
    event_date: date
    source: str
    created_at: datetime


@dataclass
class TouchpointStatus:
    id: int
    study_id: str
    touchpoint_key: str
    done: bool
    done_date: date | None
    offsets_sent: list[int]


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
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(study_id, event_type)
            );

            CREATE TABLE IF NOT EXISTS touchpoint_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                study_id TEXT NOT NULL,
                touchpoint_key TEXT NOT NULL,
                done INTEGER NOT NULL DEFAULT 0,
                done_date TEXT,
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
            """
        )


def _row_to_anchor(row: sqlite3.Row) -> AnchorEvent:
    return AnchorEvent(
        id=row["id"],
        study_id=row["study_id"],
        event_type=row["event_type"],
        event_date=date.fromisoformat(row["event_date"]),
        source=row["source"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_status(row: sqlite3.Row) -> TouchpointStatus:
    return TouchpointStatus(
        id=row["id"],
        study_id=row["study_id"],
        touchpoint_key=row["touchpoint_key"],
        done=bool(row["done"]),
        done_date=date.fromisoformat(row["done_date"]) if row["done_date"] else None,
        offsets_sent=json.loads(row["offsets_sent"]),
    )


def upsert_anchor_event(
    study_id: str,
    event_type: str,
    event_date: date,
    source: str,
    *,
    db_path: Path | None = None,
) -> AnchorEvent:
    with get_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO anchor_events (study_id, event_type, event_date, source)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(study_id, event_type) DO UPDATE SET
                event_date = excluded.event_date,
                source = excluded.source
            """,
            (study_id.strip(), event_type, event_date.isoformat(), source),
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


def set_touchpoint_done(
    study_id: str,
    touchpoint_key: str,
    done: bool,
    *,
    db_path: Path | None = None,
) -> TouchpointStatus:
    done_date = date.today().isoformat() if done else None
    with get_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO touchpoint_status (study_id, touchpoint_key, done, done_date)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(study_id, touchpoint_key) DO UPDATE SET
                done = excluded.done,
                done_date = excluded.done_date
            """,
            (study_id, touchpoint_key, int(done), done_date),
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
