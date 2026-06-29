"""Acceptance tests for touchpoint tracker."""

from __future__ import annotations

from datetime import date, timedelta
from unittest import mock

from tracker.db import (
    get_anchor_events,
    get_or_create_touchpoint_status,
    is_email_processed,
    record_offset_sent,
    set_touchpoint_done,
    upsert_anchor_event,
)
from tracker.engine.reminder import run_reminders
from tracker.ingestion.parsers import parse_body
from tracker.ingestion.run import _match_source, process_email
from tracker.ingestion.gmail_client import EmailMessage


class TestParsers:
    def test_hai_assessment_y1(self):
        body = "HAI-12345 has completed the visit. Assessment completed on 06-15-2025."
        result = parse_body("hai_assessment", body)
        assert result.study_id == "HAI-12345"
        assert result.event_date == date(2025, 6, 15)

    def test_hai_assessment_y4_subject_independent(self):
        body = "ABC99 has completed screening. Participant completed on 01-20-2024."
        result = parse_body("hai_assessment", body)
        assert result.study_id == "ABC99"
        assert result.event_date == date(2024, 1, 20)

    def test_hai_sensor(self):
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
            sender="hai@hsl.harvard.edu",
            subject="Sensor data collection trigger",
            body="ID: REC-1\nDate: 06-01-2025",
        )
        source = _match_source(email)
        assert source is not None
        assert source.event_type == "sensor_collection_start"

    def test_same_sender_different_subject(self):
        e1 = EmailMessage("a", "hai@hsl.harvard.edu", "HAI Y4 Visit Completed", "X has completed. completed on 01-01-2025.")
        e2 = EmailMessage("b", "hai@hsl.harvard.edu", "Sensor data collection trigger", "ID: X\nDate: 01-01-2025")
        assert _match_source(e1).event_type == "assessment_complete"
        assert _match_source(e2).event_type == "sensor_collection_start"

    def test_idempotent_ingestion(self, db_path):
        email = EmailMessage(
            message_id="idem-1",
            sender="hai@hsl.harvard.edu",
            subject="HAI Y2 Visit Completed",
            body="P100 has completed. completed on 05-01-2025.",
        )
        assert process_email(email, dry_run=False) == "ingested"
        assert process_email(email, dry_run=False) == "already_processed"
        anchors = get_anchor_events()
        assert len(anchors) == 1

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
        set_touchpoint_done("S3", "schedule_home_visit", True)

        with mock.patch("tracker.engine.reminder.dispatch_action") as dispatch:
            run_reminders(today=today)
            dispatch.assert_not_called()

    def test_undo_reenables_reminders(self, db_path):
        today = date(2025, 6, 10)
        anchor_date = today - timedelta(days=7)
        upsert_anchor_event("S4", "assessment_complete", anchor_date, "manual")
        set_touchpoint_done("S4", "schedule_home_visit", True)
        record_offset_sent("S4", "schedule_home_visit", 0)
        record_offset_sent("S4", "schedule_home_visit", 3)
        set_touchpoint_done("S4", "schedule_home_visit", False)

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

    def test_sensor_dropoff_webex_stub(self, db_path):
        today = date(2025, 6, 19)
        anchor_date = today - timedelta(days=9)
        upsert_anchor_event("S6", "sensor_collection_start", anchor_date, "email")

        with mock.patch("tracker.engine.reminder.dispatch_action") as dispatch:
            run_reminders(today=today)
            dispatch.assert_called_once_with("webex_call", mock.ANY)
            ctx = dispatch.call_args[0][1]
            assert ctx.touchpoint_key == "sensor_dropoff_reminder"
            assert ctx.offset == 9

    def test_independent_touchpoint_status(self, db_path):
        upsert_anchor_event("S7", "assessment_complete", date(2025, 1, 1), "manual")
        upsert_anchor_event("S7", "sensor_collection_start", date(2025, 2, 1), "manual")
        set_touchpoint_done("S7", "schedule_home_visit", True)

        home = get_or_create_touchpoint_status("S7", "schedule_home_visit")
        sensor = get_or_create_touchpoint_status("S7", "sensor_dropoff_reminder")
        assert home.done is True
        assert sensor.done is False


class TestActions:
    def test_email_message_differs_by_offset(self):
        from tracker.engine.actions import ActionContext, _message_for_schedule_home_visit

        ctx0 = ActionContext("ID1", "schedule_home_visit", 0, date(2025, 1, 1), 0)
        ctx3 = ActionContext("ID1", "schedule_home_visit", 3, date(2025, 1, 1), 3)
        subj0, body0 = _message_for_schedule_home_visit(ctx0)
        subj3, body3 = _message_for_schedule_home_visit(ctx3)
        assert "ready to call" in body0.lower() or "New participant" in subj0
        assert "3 days" in body3 or "been 3" in body3

    def test_webex_handler_logs(self, caplog):
        import logging
        from tracker.engine.actions import ActionContext, handle_webex_call

        with caplog.at_level(logging.INFO):
            handle_webex_call(
                ActionContext("WX1", "sensor_dropoff_reminder", 9, date(2025, 1, 1), 9)
            )
        assert "would place Webex call" in caplog.text
        assert "WX1" in caplog.text
