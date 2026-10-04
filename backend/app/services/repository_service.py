from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.scanner.models import Repository, ScanResult, SourceFile
from app.scanner.scanner import RepositoryScanner
from app.scanner.store import ScanStore


class RepositoryService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._store = ScanStore(settings.db_path)

    def scan(self, root: Path | None = None) -> ScanResult:
        target = root or self._settings.source_root
        if target is None:
            raise ValueError("no repository path given and SOURCE_ROOT is not configured")
        scanner = RepositoryScanner(
            self._store,
            self._settings.ignore_dirs,
            self._settings.max_source_files,
            self._settings.max_file_bytes,
        )
        return scanner.scan(target)

    def list_repositories(self) -> list[Repository]:
        return self._store.list_repositories()

    def has_repository(self, repository_id: str) -> bool:
        return self._store.has_repository(repository_id)

    def list_files(
        self, repository_id: str, limit: int, offset: int
    ) -> tuple[int, list[SourceFile]]:
        return (
            self._store.count_files(repository_id),
            self._store.list_files(repository_id, limit, offset),
        )
