from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.scanner.models import Repository, ScanResult
from app.services.repository_service import RepositoryService

router = APIRouter(prefix="/api/repositories")


class ScanRequest(BaseModel):
    path: str | None = None


@router.post("/scan")
def scan(request: ScanRequest) -> ScanResult:
    service = RepositoryService(get_settings())
    try:
        return service.scan(Path(request.path) if request.path else None)
    except (ValueError, NotADirectoryError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("")
def list_repositories() -> list[Repository]:
    return RepositoryService(get_settings()).list_repositories()
