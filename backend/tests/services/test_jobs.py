from __future__ import annotations

import threading

import pytest

from app.scanner.models import ScanSummary
from app.services.indexing_service import IndexSummary, Progress, ProgressFn
from app.services.jobs import JobAlreadyRunningError, JobManager, JobState


def dummy_summary() -> IndexSummary:
    scan = ScanSummary(repository_id="r", root="/x", total_files=1, added=1, changed=0, unchanged=0,
                       removed=0, skipped={}, ignored_dirs=0, duration_ms=1)
    return IndexSummary(repository_id="r", root="/x", total_files=1, analyzed=1, up_to_date=0, ok=1,
                        parse_errors=0, analyzer_errors=0, pruned=0, duration_ms=1, scan=scan)


def test_successful_job_reports_progress_and_result() -> None:
    manager = JobManager()

    def work(progress: ProgressFn) -> IndexSummary:
        progress(Progress("analyze", 3, 10))
        return dummy_summary()

    job = manager.submit("index", "/repo", work)
    manager.wait(job.id, 5)
    done = manager.get(job.id)
    assert done is not None
    assert done.state is JobState.SUCCEEDED and done.result is not None
    assert (done.stage, done.done, done.total) == ("done", 3, 10)
    assert done.finished_at is not None and done.error is None


def test_failed_job_records_the_error() -> None:
    manager = JobManager()

    def work(_p: ProgressFn) -> IndexSummary:
        raise RuntimeError("kaboom")

    job = manager.submit("index", "/repo", work)
    manager.wait(job.id, 5)
    failed = manager.get(job.id)
    assert failed is not None and failed.state is JobState.FAILED
    assert failed.error == "RuntimeError: kaboom" and failed.result is None


def test_one_running_job_per_repository_root() -> None:
    manager = JobManager()
    release = threading.Event()

    def blocking(_p: ProgressFn) -> IndexSummary:
        release.wait(5)
        return dummy_summary()

    first = manager.submit("index", "/repo", blocking)
    with pytest.raises(JobAlreadyRunningError) as err:
        manager.submit("index", "/repo", blocking)
    assert err.value.job_id == first.id
    other = manager.submit("index", "/other", lambda _p: dummy_summary())  # different root is fine
    assert other.id != first.id

    release.set()
    manager.wait(first.id, 5)
    manager.wait(other.id, 5)
    again = manager.submit("index", "/repo", lambda _p: dummy_summary())  # free again once finished
    manager.wait(again.id, 5)
    assert [j.id for j in manager.list()][0] == again.id  # newest first


def test_unknown_job_is_none() -> None:
    assert JobManager().get("nope") is None
