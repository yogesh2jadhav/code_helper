from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class SourceFile(BaseModel):
    id: str
    repository_id: str
    path: str
    relative_path: str
    hash: str
    package: str | None
    size: int
    language: str = "java"
    last_scanned: datetime
    encoding_issue: bool = False


class SkippedFile(BaseModel):
    relative_path: str
    reason: str


class ScanResult(BaseModel):
    repository_id: str
    root: str
    files: list[SourceFile]
    added: list[str]
    changed: list[str]
    unchanged: list[str]
    removed: list[str]
    skipped: list[SkippedFile]
    ignored_dirs: int
    duration_ms: int

    @property
    def to_analyze(self) -> list[str]:
        """Relative paths needing (re)analysis under file-level invalidation."""
        return sorted([*self.added, *self.changed])


class Repository(BaseModel):
    id: str
    root_path: str
    last_scanned: datetime | None
    file_count: int
