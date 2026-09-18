"""Gmail auth should not launch a browser during email fetch."""

from __future__ import annotations

from unittest import mock

import pytest

from tracker.gmail_secrets import GmailAuthError, overwrite_token_from_env
from tracker.ingestion.gmail_client import get_gmail_service


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
