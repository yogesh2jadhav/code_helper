from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.logging_setup import KeyValueFormatter
from app.main import create_app


def test_health_returns_healthy() -> None:
    response = TestClient(create_app()).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_settings_defaults_and_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_CHAT_MODEL", "some-model:1b")
    monkeypatch.setenv("IGNORE_DIRS", "out, dist ,")
    monkeypatch.setenv("SOURCE_ROOT", "")
    monkeypatch.setenv("MAX_CALL_DEPTH", "4")
    s = Settings(_env_file=None)
    assert s.ollama_chat_model == "some-model:1b"
    assert s.ignore_dirs == ["out", "dist"]
    assert s.source_root is None
    assert s.max_call_depth == 4
    assert s.effective_chroma_path == s.data_root / "indexes" / "chroma"


def test_chroma_path_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "c"))
    assert Settings(_env_file=None).effective_chroma_path == tmp_path / "c"


def test_scan_endpoint_rejects_missing_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        client = TestClient(create_app())
        assert client.post("/api/repositories/scan", json={"path": str(tmp_path / "nope")}).status_code == 400
        assert client.post("/api/repositories/scan", json={}).status_code == 400
    finally:
        get_settings.cache_clear()


def test_key_value_formatter_appends_extras() -> None:
    record = logging.LogRecord("x", logging.INFO, "f", 1, "scan_done", None, None)
    record.count = 3
    line = KeyValueFormatter().format(record)
    assert line.endswith("scan_done count=3")
    assert " INFO x " in line
