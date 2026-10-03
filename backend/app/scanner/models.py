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

    def summary(self) -> ScanSummary:
        skipped: dict[str, int] = {}
        for item in self.skipped:
            skipped[item.reason] = skipped.get(item.reason, 0) + 1
        return ScanSummary(
            repository_id=self.repository_id, root=self.root, total_files=len(self.files),
            added=len(self.added), changed=len(self.changed), unchanged=len(self.unchanged),
            removed=len(self.removed), skipped=skipped, ignored_dirs=self.ignored_dirs,
            duration_ms=self.duration_ms,
        )

    @property
    def to_analyze(self) -> list[str]:
        """Relative paths needing (re)analysis under file-level invalidation."""
        return sorted([*self.added, *self.changed])


class ScanSummary(BaseModel):
    """Counts only; the full file list is served separately (it can be thousands of entries)."""

    repository_id: str
    root: str
    total_files: int
    added: int
    changed: int
    unchanged: int
    removed: int
    skipped: dict[str, int]
    ignored_dirs: int
    duration_ms: int


class Repository(BaseModel):
    id: str
    root_path: str
    last_scanned: datetime | None
    file_count: int
