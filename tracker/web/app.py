"""Flask dashboard for participant touchpoint tracking."""

from __future__ import annotations

from datetime import date
from typing import Any

from flask import Flask, redirect, render_template, request, url_for

from tracker.env import load_env
from tracker.config import (
    ANCHOR_EVENT_TYPES,
    TOUCHPOINT_DEFINITIONS,
    TOUCHPOINT_BY_KEY,
    flask_secret_key,
    get_touchpoints_for_event_type,
)
from tracker.db import (
    get_all_study_ids,
    get_anchor_events,
    get_or_create_touchpoint_status,
    get_review_emails,
    get_touchpoint_statuses_for_study,
    init_db,
    set_touchpoint_done,
    upsert_anchor_event,
)


def _next_due_offset(
    offsets: tuple[int, ...],
    offsets_sent: list[int],
    anchor_date: date,
    done: bool,
) -> int | None:
    if done:
        return None
    days_since = (date.today() - anchor_date).days
    for offset in offsets:
        if offset not in offsets_sent and days_since < offset:
            return offset
    return None


def build_dashboard_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    study_ids = get_all_study_ids()

    for study_id in study_ids:
        anchors = {a.event_type: a for a in get_anchor_events(study_id=study_id)}
        statuses = get_touchpoint_statuses_for_study(study_id)
        touchpoints: list[dict[str, Any]] = []

        for tp in TOUCHPOINT_DEFINITIONS:
            anchor = anchors.get(tp.anchor_event_type)
            if anchor is None:
                continue

            status = statuses.get(tp.key)
            if status is None:
                status = get_or_create_touchpoint_status(study_id, tp.key)

            touchpoints.append(
                {
                    "key": tp.key,
                    "label": tp.label,
                    "anchor_date": anchor.event_date,
                    "anchor_type": tp.anchor_event_type,
                    "done": status.done,
                    "done_date": status.done_date,
                    "offsets_sent": status.offsets_sent,
                    "offsets": tp.offsets,
                    "next_due_offset": _next_due_offset(
                        tp.offsets, status.offsets_sent, anchor.event_date, status.done
                    ),
                    "action_type": tp.action_type,
                }
            )

        if touchpoints:
            rows.append({"study_id": study_id, "touchpoints": touchpoints})

    return rows


def create_app() -> Flask:
    load_env()
    app = Flask(__name__, template_folder="../templates")
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
            rows=build_dashboard_rows(),
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

    @app.route("/touchpoint/<study_id>/<touchpoint_key>/done", methods=["POST"])
    def mark_done(study_id: str, touchpoint_key: str):
        set_touchpoint_done(study_id, touchpoint_key, done=True)
        return redirect(url_for("index"))

    @app.route("/touchpoint/<study_id>/<touchpoint_key>/undo", methods=["POST"])
    def undo_done(study_id: str, touchpoint_key: str):
        set_touchpoint_done(study_id, touchpoint_key, done=False)
        return redirect(url_for("index"))

    return app


app = create_app()
