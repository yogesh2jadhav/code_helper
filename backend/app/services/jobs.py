"""In-memory background jobs for long-running work (indexing).

Jobs live only as long as the server process; indexing itself is resumable, so losing job
history on restart loses nothing but the progress display.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache

from pydantic import BaseModel

from app.services.indexing_service import IndexSummary, Progress, ProgressFn

logger = logging.getLogger(__name__)


class JobState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Job(BaseModel):
    id: str
    kind: str
    root: str
    state: JobState
    stage: str = "queued"
    done: int = 0
    total: int = 0
    error: str | None = None
    result: IndexSummary | None = None
    created_at: datetime
    finished_at: datetime | None = None


class JobAlreadyRunningError(RuntimeError):
    def __init__(self, job_id: str) -> None:
        super().__init__(f"a job for this repository is already running: {job_id}")
        self.job_id = job_id


class JobManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._threads: dict[str, threading.Thread] = {}

    def submit(self, kind: str, root: str, work: Callable[[ProgressFn], IndexSummary]) -> Job:
        """Start `work` on a background thread. One running job per repository root."""
        with self._lock:
            for existing in self._jobs.values():
                if existing.root == root and existing.state is JobState.RUNNING:
                    raise JobAlreadyRunningError(existing.id)
            job = Job(
                id=uuid.uuid4().hex[:12],
                kind=kind,
                root=root,
                state=JobState.RUNNING,
                created_at=datetime.now(UTC),
            )
            self._jobs[job.id] = job
            thread = threading.Thread(
                target=self._run, args=(job.id, work), daemon=True, name=f"job-{job.id}"
            )
            self._threads[job.id] = thread
            thread.start()
            return job.model_copy()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.model_copy() if job else None

    def list(self) -> list[Job]:
        with self._lock:
            return sorted(
                (j.model_copy() for j in self._jobs.values()),
                key=lambda j: j.created_at,
                reverse=True,
            )

    def wait(self, job_id: str, timeout: float | None = None) -> None:
        thread = self._threads.get(job_id)
        if thread:
            thread.join(timeout)

    def _update(self, job_id: str, **fields: object) -> None:
        with self._lock:
            job = self._jobs[job_id]
            for key, value in fields.items():
                setattr(job, key, value)

    def _run(self, job_id: str, work: Callable[[ProgressFn], IndexSummary]) -> None:
        def on_progress(p: Progress) -> None:
            self._update(job_id, stage=p.stage, done=p.done, total=p.total)

        try:
            result = work(on_progress)
        except Exception as exc:  # job boundary: record any failure instead of dying silently
            logger.exception("job_failed", extra={"job": job_id})
            self._update(
                job_id,
                state=JobState.FAILED,
                error=f"{type(exc).__name__}: {exc}",
                finished_at=datetime.now(UTC),
            )
        else:
            self._update(
                job_id,
                state=JobState.SUCCEEDED,
                stage="done",
                result=result,
                finished_at=datetime.now(UTC),
            )


@lru_cache
def get_job_manager() -> JobManager:
    return JobManager()
