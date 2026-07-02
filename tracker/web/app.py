"""Flask dashboard for participant touchpoint tracking."""

from __future__ import annotations

import csv
import io
from datetime import date
from typing import Any

from flask import Flask, flash, jsonify, make_response, redirect, render_template, request, url_for

from tracker.env import load_env
from tracker.config import (
    ANCHOR_EVENT_TYPES,
    DASHBOARD_PHASES,
    TOUCHPOINT_DEFINITIONS,
    TOUCHPOINT_BY_KEY,
    TOUCHPOINT_OUTCOME_DONE,
    TOUCHPOINT_OUTCOME_LABELS,
    TOUCHPOINT_OUTCOME_NO_LONGER_INTERESTED,
    TOUCHPOINT_OUTCOME_PENDING,
    TOUCHPOINT_OUTCOME_VISIT_SCHEDULED,
    TOUCHPOINT_OUTCOMES,
    flask_secret_key,
    get_touchpoints_for_event_type,
)
from tracker.db import (
    get_all_study_ids,
    get_anchor_events,
    get_closed_touchpoint_records,
    get_or_create_touchpoint_status,
    get_review_emails,
    get_touchpoint_outcome_log,
    get_touchpoint_statuses_for_study,
    init_db,
    set_touchpoint_outcome,
    upsert_anchor_event,
)
from tracker.ingestion.run import run_ingestion


def _touchpoint_due_info(
    offsets: tuple[int, ...],
    offsets_sent: list[int],
    anchor_date: date,
    done: bool,
) -> dict[str, Any]:
    """Summarize reminder state for dashboard display."""
    if done:
        return {"all_sent": False, "overdue": [], "next_future": None}

    days_since = (date.today() - anchor_date).days
    unsent = [o for o in offsets if o not in offsets_sent]
    if not unsent:
        return {"all_sent": True, "overdue": [], "next_future": None}

    overdue = [o for o in unsent if days_since >= o]
    future = [o for o in unsent if days_since < o]
    return {
        "all_sent": False,
        "overdue": overdue,
        "next_future": min(future) if future else None,
    }


def build_dashboard_rows(
    *,
    anchor_event_type: str | None = None,
    pending_only: bool = True,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    study_ids = get_all_study_ids()

    for study_id in study_ids:
        anchors = {a.event_type: a for a in get_anchor_events(study_id=study_id)}
        statuses = get_touchpoint_statuses_for_study(study_id)
        touchpoints: list[dict[str, Any]] = []

        for tp in TOUCHPOINT_DEFINITIONS:
            if anchor_event_type and tp.anchor_event_type != anchor_event_type:
                continue
            anchor = anchors.get(tp.anchor_event_type)
            if anchor is None:
                continue

            status = statuses.get(tp.key)
            if status is None:
                status = get_or_create_touchpoint_status(study_id, tp.key)

            if pending_only and status.outcome != TOUCHPOINT_OUTCOME_PENDING:
                continue

            due = _touchpoint_due_info(
                tp.offsets, status.offsets_sent, anchor.event_date, status.done
            )
            touchpoints.append(
                {
                    "key": tp.key,
                    "label": tp.label,
                    "anchor_date": anchor.event_date,
                    "anchor_type": tp.anchor_event_type,
                    "email_received_at": anchor.email_received_at,
                    "anchor_source": anchor.source,
                    "outcome": status.outcome,
                    "outcome_label": TOUCHPOINT_OUTCOME_LABELS.get(
                        status.outcome, status.outcome
                    ),
                    "done_date": status.done_date,
                    "offsets_sent": status.offsets_sent,
                    "offsets": tp.offsets,
                    "due_info": due,
                    "action_type": tp.action_type,
                }
            )

        if touchpoints:
            rows.append({"study_id": study_id, "touchpoints": touchpoints})

    return rows


def _closed_count_for_phase(anchor_event_type: str) -> int:
    return sum(
        1
        for record in get_closed_touchpoint_records()
        if TOUCHPOINT_BY_KEY.get(record.touchpoint_key)
        and TOUCHPOINT_BY_KEY[record.touchpoint_key].anchor_event_type
        == anchor_event_type
    )


def build_dashboard_phases() -> list[dict[str, Any]]:
    return [
        {
            "key": phase["key"],
            "title": phase["title"],
            "rows": build_dashboard_rows(
                anchor_event_type=phase["anchor_event_type"],
                pending_only=True,
            ),
            "closed_count": _closed_count_for_phase(phase["anchor_event_type"]),
            "show_actions": bool(phase.get("show_actions", True)),
            "action_mode": str(phase.get("action_mode", "outcomes")),
        }
        for phase in DASHBOARD_PHASES
    ]


def _redirect_after_outcome(outcome: str) -> str:
    if outcome == TOUCHPOINT_OUTCOME_PENDING:
        return url_for("index")
    return url_for("outcome_registry")


def _outcome_message(study_id: str, outcome: str) -> str:
    if outcome == TOUCHPOINT_OUTCOME_PENDING:
        return f"Returned {study_id} to the active dashboard."
    if outcome == TOUCHPOINT_OUTCOME_VISIT_SCHEDULED:
        return f"{study_id} moved to repository (visit scheduled)."
    if outcome == TOUCHPOINT_OUTCOME_NO_LONGER_INTERESTED:
        return f"{study_id} moved to repository (no longer interested)."
    if outcome == TOUCHPOINT_OUTCOME_DONE:
        return f"{study_id} moved to repository (done)."
    return f"Updated {study_id}."


def _phase_closed_counts() -> dict[str, int]:
    return {
        phase["key"]: _closed_count_for_phase(phase["anchor_event_type"])
        for phase in DASHBOARD_PHASES
    }


def _wants_json_response() -> bool:
    return request.headers.get("X-Requested-With") == "fetch"


def _enrich_outcome_records(records: list) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for record in records:
        tp_def = TOUCHPOINT_BY_KEY.get(record.touchpoint_key)
        enriched.append(
            {
                **record.__dict__,
                "touchpoint_label": tp_def.label if tp_def else record.touchpoint_key,
                "outcome_label": TOUCHPOINT_OUTCOME_LABELS.get(
                    record.outcome, record.outcome
                ),
            }
        )
    return enriched


def _enrich_outcome_history(entries: list) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for entry in entries:
        tp_def = TOUCHPOINT_BY_KEY.get(entry.touchpoint_key)
        prev_label = (
            TOUCHPOINT_OUTCOME_LABELS.get(entry.previous_outcome, entry.previous_outcome)
            if entry.previous_outcome
            else None
        )
        enriched.append(
            {
                **entry.__dict__,
                "touchpoint_label": tp_def.label if tp_def else entry.touchpoint_key,
                "outcome_label": TOUCHPOINT_OUTCOME_LABELS.get(
                    entry.outcome, entry.outcome
                ),
                "previous_outcome_label": prev_label,
            }
        )
    return enriched


def _outcome_counts() -> dict[str, int]:
    all_closed = get_closed_touchpoint_records()
    return {
        "visit_scheduled": sum(
            1 for r in all_closed if r.outcome == TOUCHPOINT_OUTCOME_VISIT_SCHEDULED
        ),
        "no_longer_interested": sum(
            1
            for r in all_closed
            if r.outcome == TOUCHPOINT_OUTCOME_NO_LONGER_INTERESTED
        ),
        "done": sum(1 for r in all_closed if r.outcome == TOUCHPOINT_OUTCOME_DONE),
        "total": len(all_closed),
    }


def create_app() -> Flask:
    load_env()
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.secret_key = flask_secret_key()

    @app.before_request
    def _ensure_db() -> None:
        init_db()

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.route("/")
    def index():
        return render_template(
            "index.html",
            phases=build_dashboard_phases(),
            anchor_event_types=ANCHOR_EVENT_TYPES,
            touchpoint_definitions=TOUCHPOINT_DEFINITIONS,
            review_emails=get_review_emails(),
        )

    @app.route("/anchor", methods=["POST"])
    def add_anchor():
        study_id = request.form.get("study_id", "").strip()
        event_type = request.form.get("event_type", "").strip()
        event_date_str = request.form.get("event_date", "").strip()

        if study_id and event_type and event_date_str:
            event_date = date.fromisoformat(event_date_str)
            upsert_anchor_event(study_id, event_type, event_date, source="manual")
            for tp in get_touchpoints_for_event_type(event_type):
                get_or_create_touchpoint_status(study_id, tp.key)

        return redirect(url_for("index"))

    @app.route("/ingest", methods=["POST"])
    def ingest_emails():
        try:
            stats = run_ingestion()
        except Exception as exc:
            flash(f"Email fetch failed: {exc}", "error")
            return redirect(url_for("index"))

        if stats.ingested:
            flash(
                f"Fetched HAI emails: {stats.ingested} new participant(s) ingested.",
                "success",
            )
        elif stats.event_dates_updated:
            flash(
                f"Updated assessment/event dates for {stats.event_dates_updated} "
                "participant(s) from email bodies.",
                "success",
            )
        elif stats.received_dates_backfilled:
            flash(
                f"Updated email received time for {stats.received_dates_backfilled} "
                "participant(s).",
                "success",
            )
        elif stats.flagged_for_review:
            flash(
                f"Fetched emails: {stats.flagged_for_review} need manual review "
                "(see below).",
                "warning",
            )
        elif stats.skipped_already_processed:
            flash(
                f"No new emails — {stats.skipped_already_processed} already processed.",
                "info",
            )
        else:
            flash("No matching HAI emails found in Gmail.", "info")

        return redirect(url_for("index"))

    @app.route(
        "/touchpoint/<study_id>/<touchpoint_key>/outcome/<outcome>",
        methods=["POST"],
    )
    def set_outcome(study_id: str, touchpoint_key: str, outcome: str):
        if outcome not in TOUCHPOINT_OUTCOMES:
            message = f"Invalid outcome: {outcome}"
            if _wants_json_response():
                return jsonify({"success": False, "message": message}), 400
            flash(message, "error")
            return redirect(url_for("index"))

        set_touchpoint_outcome(study_id, touchpoint_key, outcome)
        message = _outcome_message(study_id, outcome)

        if _wants_json_response():
            return jsonify(
                {
                    "success": True,
                    "message": message,
                    "study_id": study_id,
                    "touchpoint_key": touchpoint_key,
                    "outcome": outcome,
                    "remove_row": True,
                    "counts": _outcome_counts(),
                    "phase_closed_counts": _phase_closed_counts(),
                }
            )

        flash(message, "success")
        return redirect(_redirect_after_outcome(outcome))

    @app.route("/outcomes")
    def outcome_registry():
        filter_outcome = request.args.get("outcome", "").strip() or None
        if filter_outcome and filter_outcome not in TOUCHPOINT_OUTCOMES:
            filter_outcome = None
        if filter_outcome == "pending":
            filter_outcome = None

        records = _enrich_outcome_records(
            get_closed_touchpoint_records(outcome=filter_outcome)
        )
        history = _enrich_outcome_history(get_touchpoint_outcome_log(limit=200))
        return render_template(
            "outcomes.html",
            records=records,
            history=history,
            filter_outcome=filter_outcome,
            counts=_outcome_counts(),
        )

    @app.route("/outcomes.csv")
    def outcome_registry_csv():
        filter_outcome = request.args.get("outcome", "").strip() or None
        if filter_outcome and filter_outcome not in TOUCHPOINT_OUTCOMES:
            filter_outcome = None
        if filter_outcome == "pending":
            filter_outcome = None

        records = _enrich_outcome_records(
            get_closed_touchpoint_records(outcome=filter_outcome)
        )
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "study_id",
                "touchpoint_key",
                "touchpoint_label",
                "outcome",
                "outcome_date",
                "anchor_event_type",
                "anchor_event_date",
                "email_received_utc",
                "offsets_sent",
            ]
        )
        for r in records:
            received = r["email_received_at"]
            writer.writerow(
                [
                    r["study_id"],
                    r["touchpoint_key"],
                    r["touchpoint_label"],
                    r["outcome_label"],
                    r["outcome_date"] or "",
                    r["anchor_event_type"],
                    r["anchor_event_date"],
                    received.strftime("%Y-%m-%d %H:%M") if received else "",
                    str(r["offsets_sent"]),
                ]
            )

        response = make_response(output.getvalue())
        response.headers["Content-Type"] = "text/csv; charset=utf-8"
        response.headers["Content-Disposition"] = "attachment; filename=outcome_registry.csv"
        return response

    return app


app = create_app()
