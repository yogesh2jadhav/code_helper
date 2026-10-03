from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.config import get_settings
from app.scanner.models import Repository, ScanSummary, SourceFile
from app.services.indexing_service import IndexingService
from app.services.jobs import Job, JobAlreadyRunningError, get_job_manager
from app.services.repository_service import RepositoryService

router = APIRouter(prefix="/api/repositories")


class ScanRequest(BaseModel):
    path: str | None = None


class IndexRequest(BaseModel):
    path: str | None = None
    force: bool = False
    batch_size: int | None = None


class FilePage(BaseModel):
    total: int
    limit: int
    offset: int
    files: list[SourceFile]


@router.post("/scan")
def scan(request: ScanRequest) -> ScanSummary:
    """Fast file discovery only (counts, no file list). Use /index for analysis."""
    service = RepositoryService(get_settings())
    try:
        return service.scan(Path(request.path) if request.path else None).summary()
    except (ValueError, NotADirectoryError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/index", status_code=202)
def start_index(request: IndexRequest) -> Job:
    """Start indexing in the background and return a job to poll at GET /api/jobs/{id}."""
    settings = get_settings()
    service = IndexingService(settings)
    try:
        root = service.resolve_root(Path(request.path) if request.path else None).expanduser()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    root = root.resolve()
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"source root is not a directory: {root}")
    try:
        return get_job_manager().submit(
            "index", str(root),
            lambda progress: service.index(
                root, force=request.force, batch_size=request.batch_size, on_progress=progress
            ),
        )
    except JobAlreadyRunningError as exc:
        raise HTTPException(
            status_code=409, detail={"message": str(exc), "job_id": exc.job_id}
        ) from exc


@router.get("")
def list_repositories() -> list[Repository]:
    return RepositoryService(get_settings()).list_repositories()


@router.get("/{repository_id}/files")
def list_files(
    repository_id: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> FilePage:
    service = RepositoryService(get_settings())
    if not service.has_repository(repository_id):
        raise HTTPException(status_code=404, detail="unknown repository")
    total, files = service.list_files(repository_id, limit, offset)
    return FilePage(total=total, limit=limit, offset=offset, files=files)
