"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from tracker.db import init_db


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "test.db"
    monkeypatch.setenv("TRACKER_DATABASE_PATH", str(path))
    init_db(path)
    return path
