from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.services.jobs import Job, get_job_manager

router = APIRouter(prefix="/api/jobs")


@router.get("")
def list_jobs() -> list[Job]:
    return get_job_manager().list()


@router.get("/{job_id}")
def get_job(job_id: str) -> Job:
    job = get_job_manager().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")
    return job
