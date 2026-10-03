from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.scanner.models import Repository, ScanResult
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
