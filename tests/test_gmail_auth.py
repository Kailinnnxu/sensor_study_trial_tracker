"""Gmail auth should not launch a browser during email fetch."""

from __future__ import annotations

import json
from unittest import mock

import pytest
from googleapiclient.errors import HttpError

from tracker.gmail_secrets import GmailAuthError, overwrite_token_from_env
from tracker.ingestion.gmail_client import (
    GmailRateLimitError,
    fetch_messages_by_ids,
    fetch_recent_messages,
    get_gmail_service,
)


def _dead_creds() -> mock.Mock:
    creds = mock.Mock()
    creds.valid = False
    creds.expired = True
    creds.refresh_token = "dead"
    creds.refresh.side_effect = RuntimeError("invalid_grant")
    creds.to_json.return_value = "{}"
    return creds


def _valid_creds() -> mock.Mock:
    creds = mock.Mock()
    creds.valid = True
    creds.expired = False
    creds.refresh_token = "live"
    creds.to_json.return_value = '{"token": "ok"}'
    return creds


@pytest.fixture
def gmail_paths(tmp_path, monkeypatch):
    creds_path = tmp_path / "gmail_credentials.json"
    token_path = tmp_path / "gmail_token.json"
    creds_path.write_text("{}", encoding="utf-8")
    token_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "tracker.ingestion.gmail_client.gmail_credentials_path", lambda: creds_path
    )
    monkeypatch.setattr(
        "tracker.ingestion.gmail_client.gmail_token_path", lambda: token_path
    )
    monkeypatch.setattr(
        "tracker.gmail_secrets.gmail_token_path", lambda: token_path
    )
    monkeypatch.setattr("tracker.ingestion.gmail_client.ensure_gmail_files", lambda: None)
    return creds_path, token_path


class TestGetGmailService:
    def test_fetch_does_not_open_browser_when_token_dead(self, gmail_paths, monkeypatch):
        monkeypatch.setattr(
            "tracker.ingestion.gmail_client.Credentials.from_authorized_user_file",
            lambda *a, **k: _dead_creds(),
        )
        monkeypatch.setattr(
            "tracker.ingestion.gmail_client.overwrite_token_from_env", lambda: False
        )
        flow = mock.Mock()
        monkeypatch.setattr("tracker.ingestion.gmail_client.InstalledAppFlow", flow)

        with pytest.raises(GmailAuthError, match="cannot open a browser"):
            get_gmail_service()

        flow.from_client_secrets_file.assert_not_called()

    def test_interactive_setup_starts_local_oauth(self, gmail_paths, monkeypatch):
        monkeypatch.setattr(
            "tracker.ingestion.gmail_client.Credentials.from_authorized_user_file",
            lambda *a, **k: _dead_creds(),
        )
        monkeypatch.setattr(
            "tracker.ingestion.gmail_client.overwrite_token_from_env", lambda: False
        )
        new_creds = _valid_creds()
        flow = mock.Mock()
        flow.from_client_secrets_file.return_value.run_local_server.return_value = new_creds
        monkeypatch.setattr("tracker.ingestion.gmail_client.InstalledAppFlow", flow)
        monkeypatch.setattr(
            "tracker.ingestion.gmail_client.build", lambda *a, **k: "service"
        )
        persist = mock.Mock()
        monkeypatch.setattr("tracker.ingestion.gmail_client.persist_token", persist)

        assert get_gmail_service(interactive=True) == "service"
        flow.from_client_secrets_file.return_value.run_local_server.assert_called_once()
        persist.assert_called()

    def test_reloads_token_from_env_after_failed_refresh(self, gmail_paths, monkeypatch):
        states = {"n": 0}

        def load(*_a, **_k):
            states["n"] += 1
            return _dead_creds() if states["n"] == 1 else _valid_creds()

        monkeypatch.setattr(
            "tracker.ingestion.gmail_client.Credentials.from_authorized_user_file",
            load,
        )
        monkeypatch.setattr(
            "tracker.ingestion.gmail_client.overwrite_token_from_env", lambda: True
        )
        flow = mock.Mock()
        monkeypatch.setattr("tracker.ingestion.gmail_client.InstalledAppFlow", flow)
        monkeypatch.setattr(
            "tracker.ingestion.gmail_client.build", lambda *a, **k: "service"
        )

        assert get_gmail_service() == "service"
        flow.from_client_secrets_file.assert_not_called()

    def test_bad_env_token_does_not_block_interactive_setup(self, gmail_paths, monkeypatch):
        monkeypatch.setattr(
            "tracker.ingestion.gmail_client.Credentials.from_authorized_user_file",
            lambda *a, **k: _dead_creds(),
        )
        monkeypatch.setattr(
            "tracker.ingestion.gmail_client.overwrite_token_from_env",
            mock.Mock(side_effect=ValueError("truncated b64")),
        )
        new_creds = _valid_creds()
        flow = mock.Mock()
        flow.from_client_secrets_file.return_value.run_local_server.return_value = new_creds
        monkeypatch.setattr("tracker.ingestion.gmail_client.InstalledAppFlow", flow)
        monkeypatch.setattr(
            "tracker.ingestion.gmail_client.build", lambda *a, **k: "service"
        )
        monkeypatch.setattr("tracker.ingestion.gmail_client.persist_token", mock.Mock())

        assert get_gmail_service(interactive=True) == "service"
        flow.from_client_secrets_file.return_value.run_local_server.assert_called_once()


class TestOverwriteTokenFromEnv:
    def test_writes_json_token(self, tmp_path, monkeypatch):
        token_path = tmp_path / "gmail_token.json"
        monkeypatch.setattr("tracker.gmail_secrets.gmail_token_path", lambda: token_path)
        monkeypatch.setenv("GMAIL_TOKEN_JSON", '{"refresh_token": "new"}')

        assert overwrite_token_from_env() is True
        assert token_path.read_text(encoding="utf-8") == '{"refresh_token": "new"}'

    def test_returns_false_without_env(self, tmp_path, monkeypatch):
        token_path = tmp_path / "gmail_token.json"
        monkeypatch.setattr("tracker.gmail_secrets.gmail_token_path", lambda: token_path)
        monkeypatch.delenv("GMAIL_TOKEN_JSON", raising=False)
        monkeypatch.delenv("GMAIL_TOKEN_B64", raising=False)

        assert overwrite_token_from_env() is False
        assert not token_path.exists()


def _http_error(status: int = 403, reason: str = "rateLimitExceeded") -> HttpError:
    resp = mock.Mock()
    resp.status = status
    payload = {
        "error": {
            "errors": [
                {
                    "reason": reason,
                    "message": "Quota exceeded for quota metric 'Total Query Cost'",
                }
            ]
        }
    }
    return HttpError(resp, json.dumps(payload).encode())


def _message_payload(message_id: str) -> dict:
    return {
        "id": message_id,
        "internalDate": "1710000000000",
        "payload": {
            "headers": [
                {"name": "From", "value": "hai@hsl.harvard.edu"},
                {"name": "Subject", "value": "HAI Y1 Visit Completed"},
            ],
            "mimeType": "text/plain",
            "body": {"data": ""},
        },
    }


class TestGmailRateLimit:
    def test_retries_then_succeeds(self, monkeypatch):
        monkeypatch.setattr("tracker.ingestion.gmail_client.time.sleep", lambda *_a, **_k: None)
        request = mock.Mock()
        request.execute.side_effect = [
            _http_error(),
            _message_payload("m1"),
        ]
        service = mock.Mock()
        service.users.return_value.messages.return_value.get.return_value = request

        messages = fetch_messages_by_ids(["m1"], service=service)
        assert [m.message_id for m in messages] == ["m1"]
        assert request.execute.call_count == 2

    def test_raises_clear_error_after_retries(self, monkeypatch):
        monkeypatch.setattr("tracker.ingestion.gmail_client.time.sleep", lambda *_a, **_k: None)
        request = mock.Mock()
        request.execute.side_effect = _http_error()
        service = mock.Mock()
        service.users.return_value.messages.return_value.get.return_value = request

        with pytest.raises(GmailRateLimitError, match="rate limit"):
            fetch_messages_by_ids(["m1"], service=service)
        assert request.execute.call_count == 5

    def test_returns_partial_results_when_later_get_is_rate_limited(self, monkeypatch):
        monkeypatch.setattr("tracker.ingestion.gmail_client.time.sleep", lambda *_a, **_k: None)

        def get_request(*, userId, id, format):
            request = mock.Mock()
            if id == "m1":
                request.execute.return_value = _message_payload("m1")
            else:
                request.execute.side_effect = _http_error()
            return request

        service = mock.Mock()
        service.users.return_value.messages.return_value.get.side_effect = (
            lambda **kwargs: get_request(**kwargs)
        )

        messages = fetch_messages_by_ids(["m1", "m2"], service=service)
        assert [m.message_id for m in messages] == ["m1"]

    def test_fetch_skips_already_ingested_ids(self, monkeypatch):
        list_req = mock.Mock()
        list_req.execute.return_value = {"messages": [{"id": "old"}, {"id": "new"}]}
        get_req = mock.Mock()
        get_req.execute.return_value = _message_payload("new")
        service = mock.Mock()
        messages_api = service.users.return_value.messages.return_value
        messages_api.list.return_value = list_req
        messages_api.get.return_value = get_req
        monkeypatch.setattr(
            "tracker.ingestion.gmail_client.get_gmail_service", lambda: service
        )

        messages = fetch_recent_messages(skip_ids={"old"})
        assert [m.message_id for m in messages] == ["new"]
        messages_api.get.assert_called_once_with(userId="me", id="new", format="full")

    def test_list_paginates_when_uncapped(self):
        from tracker.ingestion.gmail_client import list_recent_message_ids

        list_req_one = mock.Mock()
        list_req_one.execute.return_value = {
            "messages": [{"id": "a"}],
            "nextPageToken": "page2",
        }
        list_req_two = mock.Mock()
        list_req_two.execute.return_value = {"messages": [{"id": "b"}]}
        service = mock.Mock()
        list_api = service.users.return_value.messages.return_value.list
        list_api.side_effect = [list_req_one, list_req_two]

        ids = list_recent_message_ids(max_results=None, query="from:x", service=service)
        assert ids == ["a", "b"]
        assert list_api.call_args_list[0].kwargs["maxResults"] == 500
        assert list_api.call_args_list[1].kwargs["pageToken"] == "page2"

