"""REST + CLI flow against the real analyzer, using the Java fixtures."""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.cli import main as cli_main
from app.config import Settings
from app.main import create_app
from app.services.jobs import get_job_manager
from app.services.pipeline_service import PipelineSummary
from tests.conftest import FIXTURES


@pytest.fixture
def client(isolated_settings: Settings, java_analyzer: object) -> TestClient:
    return TestClient(create_app())


def wait_for_job(client: TestClient, job_id: str, timeout: float = 60) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job: dict[str, Any] = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_scan_returns_counts_not_a_file_list(client: TestClient) -> None:
    body = client.post("/api/repositories/scan", json={"path": str(FIXTURES)}).json()
    assert body["total_files"] == 8 and body["added"] == 8
    assert "files" not in body


def test_index_runs_in_background_and_is_incremental(client: TestClient) -> None:
    response = client.post("/api/repositories/index", json={"path": str(FIXTURES)})
    assert response.status_code == 202
    job = wait_for_job(client, response.json()["id"])
    assert job["state"] == "succeeded", job
    assert (job["done"], job["total"]) == (8, 8)
    result = job["result"]["index"]
    assert (result["ok"], result["parse_errors"], result["analyzer_errors"]) == (7, 1, 0)
    knowledge = job["result"]["knowledge"]
    assert knowledge["skipped"] is False and knowledge["stats"]["classes"] > 0

    again = wait_for_job(
        client, client.post("/api/repositories/index", json={"path": str(FIXTURES)}).json()["id"]
    )
    assert (again["result"]["index"]["analyzed"], again["result"]["index"]["up_to_date"]) == (0, 8)
    assert again["result"]["knowledge"]["skipped"] is True  # nothing changed, so nothing rebuilt
    assert len(client.get("/api/jobs").json()) == 2


def test_file_listing_is_paginated(client: TestClient) -> None:
    repo_id = client.post("/api/repositories/scan", json={"path": str(FIXTURES)}).json()[
        "repository_id"
    ]
    page = client.get(f"/api/repositories/{repo_id}/files", params={"limit": 3, "offset": 6}).json()
    assert (page["total"], page["limit"], page["offset"], len(page["files"])) == (8, 3, 6, 2)
    first = client.get(f"/api/repositories/{repo_id}/files", params={"limit": 3}).json()["files"]
    assert [f["relative_path"] for f in first] == sorted(f["relative_path"] for f in first)


def test_second_index_while_one_is_running_gets_409(client: TestClient) -> None:
    release = threading.Event()

    def blocking(_p: object) -> PipelineSummary:
        release.wait(10)
        raise RuntimeError("released")

    running = get_job_manager().submit("index", str(FIXTURES.resolve()), blocking)
    try:
        response = client.post("/api/repositories/index", json={"path": str(FIXTURES)})
        assert response.status_code == 409
        assert response.json()["detail"]["job_id"] == running.id
    finally:
        release.set()
        get_job_manager().wait(running.id, 5)


def test_error_responses(client: TestClient, tmp_path: Path) -> None:
    assert client.post("/api/repositories/index", json={}).status_code == 400  # no SOURCE_ROOT
    assert (
        client.post("/api/repositories/index", json={"path": str(tmp_path / "nope")}).status_code
        == 400
    )
    assert client.get("/api/repositories/unknown/files").status_code == 404
    assert client.get("/api/jobs/unknown").status_code == 404


# ---- CLI --------------------------------------------------------------------------------------


def test_cli_scan_and_index(
    isolated_settings: Settings,
    java_analyzer: object,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy(FIXTURES / "simple/OrderCalculator.java", src)
    shutil.copy(FIXTURES / "invalid/Broken.java", src)

    assert cli_main(["scan", str(src)]) == 0
    assert '"total_files": 2' in capsys.readouterr().out

    assert cli_main(["index", str(src), "--batch-size", "1"]) == 0
    captured = capsys.readouterr()
    assert '"analyzed": 2' in captured.out and '"parse_errors": 1' in captured.out
    assert "[analyze] 2/2 files" in captured.err

    assert cli_main(["index", str(src)]) == 0
    assert '"analyzed": 0' in capsys.readouterr().out  # incremental: nothing to do


def test_cli_reports_bad_input(
    isolated_settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_main(["index", str(tmp_path / "missing")]) == 2
    assert "not a directory" in capsys.readouterr().err
    assert cli_main(["index"]) == 2  # no path and no SOURCE_ROOT
