"""Acceptance tests for touchpoint tracker."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest import mock

from tracker.config import (
    TOUCHPOINT_OUTCOME_DONE,
    TOUCHPOINT_OUTCOME_NO_LONGER_INTERESTED,
    TOUCHPOINT_OUTCOME_PENDING,
    TOUCHPOINT_OUTCOME_VISIT_SCHEDULED,
)
from tracker.db import (
    get_anchor_events,
    get_closed_touchpoint_records,
    get_or_create_touchpoint_status,
    get_touchpoint_outcome_log,
    is_email_processed,
    record_offset_sent,
    set_touchpoint_done,
    set_touchpoint_outcome,
    upsert_anchor_event,
)
from tracker.engine.reminder import run_reminders
from tracker.ingestion.parsers import parse_body
from tracker.ingestion.run import _match_source, process_email
from tracker.ingestion.gmail_client import EmailMessage


class TestConfig:
    def test_database_path_stable_across_cwd(self, monkeypatch, tmp_path):
        import os

        from tracker.config import PROJECT_ROOT, database_path

        db_file = PROJECT_ROOT / "data" / "tracker.db"
        monkeypatch.setenv("TRACKER_DATABASE_PATH", "data/tracker.db")

        original_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            assert database_path() == db_file.resolve()
        finally:
            os.chdir(original_cwd)

    def test_anchor_event_persists_after_reopen(self, db_path):
        from tracker.db import get_anchor_events, upsert_anchor_event
        from datetime import date

        upsert_anchor_event("PERSIST1", "assessment_complete", date(2026, 6, 1), "manual")
        assert len(get_anchor_events()) == 1

        from tracker.db import init_db

        init_db(db_path)
        assert get_anchor_events(study_id="PERSIST1")[0].study_id == "PERSIST1"


class TestParsers:
    def test_hai_assessment_y1(self):
        body = "HAI-12345 has completed the visit. Assessment completed on 06-15-2025."
        result = parse_body("hai_assessment", body)
        assert result.study_id == "HAI-12345"
        assert result.event_date == date(2025, 6, 15)

    def test_hai_assessment_uses_latest_date_in_body(self):
        body = (
            "CCB400_G has completed screening. completed on 06-01-2026. "
            "Assessment completed on 06-25-2026."
        )
        result = parse_body("hai_assessment", body)
        assert result.study_id == "CCB400_G"
        assert result.event_date == date(2026, 6, 25)

    def test_hai_assessment_y4_subject_independent(self):
        body = "ABC99 has completed screening. Participant completed on 01-20-2024."
        result = parse_body("hai_assessment", body)
        assert result.study_id == "ABC99"
        assert result.event_date == date(2024, 1, 20)

    def test_sensor_collection_template(self):
        body = (
            "Hi,\n\n"
            "This is the trigger of the start of sensor data collection from HAI.\n\n"
            "ID: CCB101_G\n\n"
            "Date: 06-25-2026"
        )
        result = parse_body("sensor_collection", body)
        assert result.study_id == "CCB101_G"
        assert result.event_date == date(2026, 6, 25)

    def test_hai_sensor_alias(self):
        body = "ID: HAI-999\nDate: 03-10-2025\n"
        result = parse_body("hai_sensor", body)
        assert result.study_id == "HAI-999"
        assert result.event_date == date(2025, 3, 10)

    def test_hai_assessment_unparseable(self):
        result = parse_body("hai_assessment", "no useful content")
        assert hasattr(result, "reason")


class TestIngestionRouting:
    def test_phase1_subject_routes_to_assessment(self):
        email = EmailMessage(
            message_id="m1",
            sender="hai@hsl.harvard.edu",
            subject="HAI Y1 Visit Completed",
            body="X1 has completed visit. completed on 06-01-2025.",
        )
        source = _match_source(email)
        assert source is not None
        assert source.event_type == "assessment_complete"

    def test_phase2_subject_routes_to_sensor(self):
        email = EmailMessage(
            message_id="m2",
            sender="kailinxu@hsl.harvard.edu",
            subject="Sensor data collection trigger",
            body="ID: REC-1\nDate: 06-01-2025",
        )
        source = _match_source(email)
        assert source is not None
        assert source.event_type == "sensor_collection_start"
        assert source.key == "kailin_sensor_collection"

    def test_same_sender_different_subject(self):
        e1 = EmailMessage("a", "hai@hsl.harvard.edu", "HAI Y4 Visit Completed", "X has completed. completed on 01-01-2025.")
        e2 = EmailMessage("b", "kailinxu@hsl.harvard.edu", "Sensor data collection trigger", "ID: X\nDate: 01-01-2025")
        assert _match_source(e1).event_type == "assessment_complete"
        assert _match_source(e2).event_type == "sensor_collection_start"

    def test_idempotent_ingestion(self, db_path):
        received = datetime(2026, 6, 29, 14, 30, tzinfo=timezone.utc)
        email = EmailMessage(
            message_id="idem-1",
            sender="hai@hsl.harvard.edu",
            subject="HAI Y2 Visit Completed",
            body="P100 has completed. completed on 05-01-2025.",
            received_at=received,
        )
        assert process_email(email, dry_run=False) == "ingested"
        assert process_email(email, dry_run=False) == "already_processed"
        anchors = get_anchor_events()
        assert len(anchors) == 1
        assert anchors[0].email_received_at == received

    def test_backfill_received_at_on_refetch(self, db_path):
        from tracker.ingestion.run import run_ingestion

        received = datetime(2026, 6, 29, 18, 38, 18, tzinfo=timezone.utc)
        email = EmailMessage(
            message_id="backfill-1",
            sender="hai@hsl.harvard.edu",
            subject="HAI Y1 Visit Completed",
            body="BF100 has completed. completed on 06-22-2026.",
            received_at=received,
        )
        process_email(email, dry_run=False)

        with __import__("tracker.db", fromlist=["get_db"]).get_db() as conn:
            conn.execute(
                "UPDATE anchor_events SET email_received_at = NULL WHERE study_id = ?",
                ("BF100",),
            )

        with mock.patch("tracker.ingestion.run.fetch_recent_messages", return_value=[email]):
            stats = run_ingestion()

        assert stats.received_dates_backfilled == 1
        anchor = get_anchor_events(study_id="BF100")[0]
        assert anchor.email_received_at == received

    def test_unparseable_flagged_not_dropped(self, db_path):
        email = EmailMessage(
            message_id="bad-1",
            sender="hai@hsl.harvard.edu",
            subject="HAI Y1 Visit Completed",
            body="garbage body with no parseable fields",
        )
        result = process_email(email, dry_run=False)
        assert result == "needs_review"
        assert is_email_processed("bad-1")
        assert len(get_anchor_events()) == 0

    def test_duplicate_emails_keep_latest_date_in_batch(self, db_path):
        older = EmailMessage(
            message_id="dup-old",
            sender="kailinxu@hsl.harvard.edu",
            subject="Sensor data collection trigger",
            body="ID: CCB300_G\nDate: 06-01-2026",
        )
        newer = EmailMessage(
            message_id="dup-new",
            sender="kailinxu@hsl.harvard.edu",
            subject="Sensor data collection trigger",
            body="ID: CCB300_G\nDate: 06-25-2026",
        )

        with mock.patch("tracker.ingestion.run.fetch_recent_messages", return_value=[older, newer]):
            from tracker.ingestion.run import run_ingestion

            stats = run_ingestion()

        assert stats.ingested == 1
        anchor = get_anchor_events(study_id="CCB300_G")[0]
        assert anchor.event_date == date(2026, 6, 25)
        assert is_email_processed("dup-old")
        assert is_email_processed("dup-new")

    def test_duplicate_emails_keep_latest_date_any_order(self, db_path):
        older = EmailMessage(
            message_id="dup2-old",
            sender="hai@hsl.harvard.edu",
            subject="HAI Y1 Visit Completed",
            body="CCB301_G has completed. completed on 06-01-2026.",
        )
        newer = EmailMessage(
            message_id="dup2-new",
            sender="hai@hsl.harvard.edu",
            subject="HAI Y1 Visit Completed",
            body="CCB301_G has completed. completed on 06-25-2026.",
        )

        process_email(newer, dry_run=False)
        process_email(older, dry_run=False)

        anchor = get_anchor_events(study_id="CCB301_G")[0]
        assert anchor.event_date == date(2026, 6, 25)

    def test_reconcile_latest_assessment_from_processed_emails(self, db_path):
        from tracker.db import mark_email_processed
        from tracker.ingestion.run import run_ingestion

        older = EmailMessage(
            message_id="recon-old",
            sender="hai@hsl.harvard.edu",
            subject="HAI Y1 Visit Completed",
            body="CCB302_G has completed. completed on 06-01-2026.",
            received_at=datetime(2026, 6, 2, tzinfo=timezone.utc),
        )
        newer = EmailMessage(
            message_id="recon-new",
            sender="hai@hsl.harvard.edu",
            subject="HAI Y1 Visit Completed",
            body="CCB302_G has completed. completed on 06-25-2026.",
            received_at=datetime(2026, 6, 26, tzinfo=timezone.utc),
        )
        process_email(older, dry_run=False)
        mark_email_processed("recon-new", "success", "assessment_complete:CCB302_G")

        with mock.patch(
            "tracker.ingestion.run.fetch_recent_messages",
            return_value=[older, newer],
        ):
            stats = run_ingestion()

        anchor = get_anchor_events(study_id="CCB302_G")[0]
        assert anchor.event_date == date(2026, 6, 25)
        assert stats.event_dates_updated == 1

    def test_backfill_ignores_mismatched_assessment_date(self, db_path):
        from tracker.ingestion.run import run_ingestion

        anchor_email = EmailMessage(
            message_id="bf-anchor",
            sender="hai@hsl.harvard.edu",
            subject="HAI Y1 Visit Completed",
            body="BF200 has completed. completed on 06-22-2026.",
            received_at=datetime(2026, 6, 29, 18, 38, 18, tzinfo=timezone.utc),
        )
        other_email = EmailMessage(
            message_id="bf-other",
            sender="hai@hsl.harvard.edu",
            subject="HAI Y1 Visit Completed",
            body="BF200 has completed. completed on 06-01-2026.",
            received_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        )
        process_email(anchor_email, dry_run=False)

        with __import__("tracker.db", fromlist=["get_db"]).get_db() as conn:
            conn.execute(
                "UPDATE anchor_events SET email_received_at = NULL WHERE study_id = ?",
                ("BF200",),
            )
        process_email(other_email, dry_run=False)

        with mock.patch(
            "tracker.ingestion.run.fetch_recent_messages",
            return_value=[anchor_email, other_email],
        ):
            stats = run_ingestion()

        assert stats.received_dates_backfilled == 1
        assert stats.event_dates_updated == 0
        anchor = get_anchor_events(study_id="BF200")[0]
        assert anchor.event_date == date(2026, 6, 22)
        assert anchor.email_received_at == datetime(2026, 6, 29, 18, 38, 18, tzinfo=timezone.utc)


class TestReminderEngine:
    def test_sends_exact_offset_for_3_day_past_anchor(self, db_path):
        today = date(2025, 6, 10)
        anchor_date = today - timedelta(days=3)
        upsert_anchor_event("S1", "assessment_complete", anchor_date, "manual")
        record_offset_sent("S1", "schedule_home_visit", 0)

        with mock.patch("tracker.engine.reminder.dispatch_action") as dispatch:
            run_reminders(today=today, dry_run=False)
            dispatch.assert_called_once()
            call = dispatch.call_args
            assert call[0][0] == "email_kailin"
            ctx = call[0][1]
            assert ctx.offset == 3
            assert ctx.study_id == "S1"

        status = get_or_create_touchpoint_status("S1", "schedule_home_visit")
        assert status.offsets_sent == [0, 3]

    def test_no_double_send_same_day(self, db_path):
        today = date(2025, 6, 10)
        anchor_date = today - timedelta(days=3)
        upsert_anchor_event("S2", "assessment_complete", anchor_date, "manual")
        record_offset_sent("S2", "schedule_home_visit", 0)

        with mock.patch("tracker.engine.reminder.dispatch_action") as dispatch:
            run_reminders(today=today)
            run_reminders(today=today)
            assert dispatch.call_count == 1

    def test_mark_done_stops_later_offsets(self, db_path):
        today = date(2025, 6, 15)
        anchor_date = today - timedelta(days=5)
        upsert_anchor_event("S3", "assessment_complete", anchor_date, "manual")
        set_touchpoint_outcome("S3", "schedule_home_visit", TOUCHPOINT_OUTCOME_VISIT_SCHEDULED)

        with mock.patch("tracker.engine.reminder.dispatch_action") as dispatch:
            run_reminders(today=today)
            dispatch.assert_not_called()

    def test_no_longer_interested_stops_reminders(self, db_path):
        today = date(2025, 6, 15)
        anchor_date = today - timedelta(days=5)
        upsert_anchor_event("S3b", "assessment_complete", anchor_date, "manual")
        set_touchpoint_outcome(
            "S3b", "schedule_home_visit", TOUCHPOINT_OUTCOME_NO_LONGER_INTERESTED
        )

        with mock.patch("tracker.engine.reminder.dispatch_action") as dispatch:
            run_reminders(today=today)
            dispatch.assert_not_called()

    def test_undo_reenables_reminders(self, db_path):
        today = date(2025, 6, 10)
        anchor_date = today - timedelta(days=7)
        upsert_anchor_event("S4", "assessment_complete", anchor_date, "manual")
        set_touchpoint_outcome("S4", "schedule_home_visit", TOUCHPOINT_OUTCOME_VISIT_SCHEDULED)
        record_offset_sent("S4", "schedule_home_visit", 0)
        record_offset_sent("S4", "schedule_home_visit", 3)
        set_touchpoint_outcome("S4", "schedule_home_visit", TOUCHPOINT_OUTCOME_PENDING)

        with mock.patch("tracker.engine.reminder.dispatch_action") as dispatch:
            run_reminders(today=today)
            # offset 7 not yet sent, should fire
            assert dispatch.call_count == 1
            assert dispatch.call_args[0][1].offset == 7

    def test_catch_up_missed_offset(self, db_path):
        today = date(2025, 6, 20)
        anchor_date = today - timedelta(days=5)
        upsert_anchor_event("S5", "assessment_complete", anchor_date, "manual")

        with mock.patch("tracker.engine.reminder.dispatch_action") as dispatch:
            run_reminders(today=today)
            offsets = [c[0][1].offset for c in dispatch.call_args_list]
            assert offsets == [0, 3]

    def test_sensor_followup_email_day_2(self, db_path):
        today = date(2026, 6, 27)
        anchor_date = today - timedelta(days=2)
        upsert_anchor_event("S6a", "sensor_collection_start", anchor_date, "email")

        with mock.patch("tracker.engine.reminder.dispatch_action") as dispatch:
            run_reminders(today=today)
            dispatch.assert_called_once_with("email_kailin", mock.ANY)
            ctx = dispatch.call_args[0][1]
            assert ctx.touchpoint_key == "sensor_collection_followup"
            assert ctx.offset == 2

    def test_sensor_dropoff_webex_stub(self, db_path):
        today = date(2025, 6, 19)
        anchor_date = today - timedelta(days=9)
        upsert_anchor_event("S6", "sensor_collection_start", anchor_date, "email")
        record_offset_sent("S6", "sensor_collection_followup", 2)

        with mock.patch("tracker.engine.reminder.dispatch_action") as dispatch:
            run_reminders(today=today)
            dispatch.assert_called_once_with("webex_call", mock.ANY)
            ctx = dispatch.call_args[0][1]
            assert ctx.touchpoint_key == "sensor_dropoff_reminder"
            assert ctx.offset == 9

    def test_independent_touchpoint_status(self, db_path):
        upsert_anchor_event("S7", "assessment_complete", date(2025, 1, 1), "manual")
        upsert_anchor_event("S7", "sensor_collection_start", date(2025, 2, 1), "manual")
        set_touchpoint_outcome("S7", "schedule_home_visit", TOUCHPOINT_OUTCOME_VISIT_SCHEDULED)

        home = get_or_create_touchpoint_status("S7", "schedule_home_visit")
        followup = get_or_create_touchpoint_status("S7", "sensor_collection_followup")
        sensor = get_or_create_touchpoint_status("S7", "sensor_dropoff_reminder")
        assert home.outcome == TOUCHPOINT_OUTCOME_VISIT_SCHEDULED
        assert home.done is True
        assert followup.outcome == TOUCHPOINT_OUTCOME_PENDING
        assert sensor.outcome == TOUCHPOINT_OUTCOME_PENDING
        assert sensor.done is False

    def test_sensor_ingestion_creates_touchpoint_rows(self, db_path):
        email = EmailMessage(
            message_id="sensor-ingest-1",
            sender="kailinxu@hsl.harvard.edu",
            subject="Sensor data collection trigger",
            body="ID: CCB200_G\nDate: 06-25-2026",
        )
        assert process_email(email, dry_run=False) == "ingested"
        followup = get_or_create_touchpoint_status("CCB200_G", "sensor_collection_followup")
        dropoff = get_or_create_touchpoint_status("CCB200_G", "sensor_dropoff_reminder")
        assert followup.outcome == TOUCHPOINT_OUTCOME_PENDING
        assert dropoff.outcome == TOUCHPOINT_OUTCOME_PENDING
        anchor = get_anchor_events(study_id="CCB200_G")[0]
        assert anchor.event_type == "sensor_collection_start"
        assert anchor.event_date == date(2026, 6, 25)


class TestOutcomeRegistry:
    def test_outcome_change_is_logged(self, db_path):
        upsert_anchor_event("R1", "assessment_complete", date(2025, 3, 1), "manual")
        set_touchpoint_outcome("R1", "schedule_home_visit", TOUCHPOINT_OUTCOME_VISIT_SCHEDULED)

        log = get_touchpoint_outcome_log()
        assert len(log) == 1
        assert log[0].study_id == "R1"
        assert log[0].outcome == TOUCHPOINT_OUTCOME_VISIT_SCHEDULED
        assert log[0].previous_outcome == TOUCHPOINT_OUTCOME_PENDING

    def test_closed_records_join_anchor(self, db_path):
        upsert_anchor_event("R2", "assessment_complete", date(2025, 4, 1), "manual")
        set_touchpoint_outcome(
            "R2", "schedule_home_visit", TOUCHPOINT_OUTCOME_NO_LONGER_INTERESTED
        )

        records = get_closed_touchpoint_records()
        assert len(records) == 1
        assert records[0].study_id == "R2"
        assert records[0].anchor_event_type == "assessment_complete"
        assert records[0].outcome == TOUCHPOINT_OUTCOME_NO_LONGER_INTERESTED

    def test_outcome_registry_page(self, db_path):
        from tracker.web.app import create_app

        upsert_anchor_event("R3", "assessment_complete", date(2025, 5, 1), "manual")
        set_touchpoint_outcome("R3", "schedule_home_visit", TOUCHPOINT_OUTCOME_VISIT_SCHEDULED)

        app = create_app()
        client = app.test_client()
        resp = client.get("/outcomes")
        assert resp.status_code == 200
        assert b"R3" in resp.data
        assert b"Visit scheduled" in resp.data

    def test_outcome_registry_csv(self, db_path):
        from tracker.web.app import create_app

        upsert_anchor_event("R4", "assessment_complete", date(2025, 6, 1), "manual")
        set_touchpoint_outcome("R4", "schedule_home_visit", TOUCHPOINT_OUTCOME_VISIT_SCHEDULED)

        app = create_app()
        client = app.test_client()
        resp = client.get("/outcomes.csv")
        assert resp.status_code == 200
        assert "text/csv" in resp.content_type
        assert b"R4" in resp.data
        assert b"visit_scheduled" in resp.data or b"Visit scheduled" in resp.data

    def test_closed_participants_hidden_from_dashboard(self, db_path):
        from tracker.web.app import build_dashboard_rows, create_app

        upsert_anchor_event("R5", "assessment_complete", date(2025, 7, 1), "manual")
        set_touchpoint_outcome("R5", "schedule_home_visit", TOUCHPOINT_OUTCOME_VISIT_SCHEDULED)

        rows = build_dashboard_rows(anchor_event_type="assessment_complete", pending_only=True)
        study_ids = [r["study_id"] for r in rows]
        assert "R5" not in study_ids

        app = create_app()
        client = app.test_client()
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"R5" not in resp.data

    def test_set_outcome_redirects_to_repository(self, db_path):
        from tracker.web.app import create_app

        upsert_anchor_event("R6", "assessment_complete", date(2025, 8, 1), "manual")
        app = create_app()
        client = app.test_client()
        resp = client.post(
            "/touchpoint/R6/schedule_home_visit/outcome/visit_scheduled",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "/outcomes" in resp.headers["Location"]

    def test_reset_outcome_returns_to_dashboard(self, db_path):
        from tracker.web.app import create_app

        upsert_anchor_event("R7", "assessment_complete", date(2025, 9, 1), "manual")
        set_touchpoint_outcome("R7", "schedule_home_visit", TOUCHPOINT_OUTCOME_NO_LONGER_INTERESTED)

        app = create_app()
        client = app.test_client()
        resp = client.post(
            "/touchpoint/R7/schedule_home_visit/outcome/pending",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/")

    def test_set_outcome_json_no_page_reload(self, db_path):
        from tracker.web.app import create_app

        upsert_anchor_event("R8", "assessment_complete", date(2025, 10, 1), "manual")
        app = create_app()
        client = app.test_client()
        resp = client.post(
            "/touchpoint/R8/schedule_home_visit/outcome/visit_scheduled",
            headers={
                "X-Requested-With": "fetch",
                "Accept": "application/json",
            },
        )
        assert resp.status_code == 200
        assert resp.is_json
        data = resp.get_json()
        assert data["success"] is True
        assert data["remove_row"] is True
        assert data["study_id"] == "R8"
        assert data["counts"]["visit_scheduled"] == 1
        assert data["phase_closed_counts"]["phase1"] == 1


class TestActions:
    def test_email_message_differs_by_offset(self):
        from tracker.engine.actions import ActionContext, _message_for_schedule_home_visit

        ctx0 = ActionContext("ID1", "schedule_home_visit", 0, date(2025, 1, 1), 0)
        ctx3 = ActionContext("ID1", "schedule_home_visit", 3, date(2025, 1, 1), 3)
        subj0, body0 = _message_for_schedule_home_visit(ctx0)
        subj3, body3 = _message_for_schedule_home_visit(ctx3)
        assert "ready to call" in body0.lower() or "New participant" in subj0
        assert "3 days" in body3 or "been 3" in body3

    def test_sensor_followup_email_copy(self):
        from tracker.engine.actions import ActionContext, _message_for_sensor_followup

        ctx = ActionContext("CCB101_G", "sensor_collection_followup", 2, date(2026, 6, 25), 2)
        subject, body = _message_for_sensor_followup(ctx)
        assert "CCB101_G" in subject
        assert "follow-up" in body.lower() or "follow up" in body.lower()

    def test_webex_handler_logs(self, caplog):
        import logging
        from tracker.engine.actions import ActionContext, handle_webex_call

        with caplog.at_level(logging.INFO):
            handle_webex_call(
                ActionContext("WX1", "sensor_dropoff_reminder", 9, date(2025, 1, 1), 9)
            )
        assert "would place Webex call" in caplog.text
        assert "WX1" in caplog.text


class TestWebDashboard:
    def test_manual_ingest_route(self, db_path):
        from tracker.ingestion.run import IngestionStats
        from tracker.web.app import create_app

        app = create_app()
        with mock.patch("tracker.web.app.run_ingestion") as run:
            run.return_value = IngestionStats(ingested=2, processed=2)
            client = app.test_client()
            resp = client.post("/ingest", follow_redirects=True)

        assert resp.status_code == 200
        assert b"2 new participant" in resp.data
        run.assert_called_once()

    def test_dashboard_includes_column_sort(self, db_path):
        from datetime import date
        from tracker.db import upsert_anchor_event
        from tracker.web.app import create_app

        upsert_anchor_event("SORT1", "assessment_complete", date(2026, 6, 1), "manual")

        app = create_app()
        client = app.test_client()
        resp = client.get("/")
        html = resp.data.decode()
        assert resp.status_code == 200
        assert "sortable-table" in html
        assert "sort-icon" in html
        assert 'class="no-sort">Actions</th>' in html
        assert "actions-inner" in html
        assert "data-sort-value" in html
        assert "Click to sort" in html
        assert "function initAllTables" in html

    def test_phase2_table_has_mark_done_action(self, db_path):
        from datetime import date
        from tracker.db import upsert_anchor_event
        from tracker.web.app import create_app

        upsert_anchor_event("P2A", "sensor_collection_start", date(2026, 6, 25), "manual")
        upsert_anchor_event("P1A", "assessment_complete", date(2026, 6, 1), "manual")

        app = create_app()
        html = app.test_client().get("/").data.decode()
        phase1_start = html.index("Phase 1 — Home visit scheduling")
        phase2_start = html.index("Phase 2 — Sensor collection")
        phase1_section = html[phase1_start:phase2_start]
        phase2_section = html[phase2_start:]

        assert "No longer interested" in phase1_section
        assert "Visit scheduled" in phase1_section
        assert "Mark done" in phase2_section
        assert "No longer interested" not in phase2_section
        assert "Visit scheduled" not in phase2_section
        assert 'class="no-sort">Actions</th>' in phase1_section
        assert 'class="no-sort">Actions</th>' in phase2_section

    def test_mark_done_stops_phase2_reminders(self, db_path):
        from datetime import date, timedelta
        from tracker.config import TOUCHPOINT_OUTCOME_DONE
        from tracker.db import set_touchpoint_outcome, upsert_anchor_event

        today = date(2026, 6, 27)
        anchor_date = today - timedelta(days=2)
        upsert_anchor_event("P2R", "sensor_collection_start", anchor_date, "manual")
        set_touchpoint_outcome("P2R", "sensor_collection_followup", TOUCHPOINT_OUTCOME_DONE)

        with mock.patch("tracker.engine.reminder.dispatch_action") as dispatch:
            run_reminders(today=today)
            dispatch.assert_not_called()
