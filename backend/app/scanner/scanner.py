"""Repository scanner: discovers .java files, hashes them, supports incremental scans."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from app.logging_setup import log_event
from app.scanner.models import ScanResult, SkippedFile, SourceFile
from app.scanner.store import ScanStore

logger = logging.getLogger(__name__)

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"//[^\n]*")
_PACKAGE = re.compile(r"^\s*(?:@[\w.]+(?:\([^)]*\))?\s*)*package\s+([\w.]+)\s*;", re.MULTILINE)


def repository_id_for(root: Path) -> str:
    return hashlib.sha1(str(root).encode()).hexdigest()[:16]


def detect_package(text: str) -> str | None:
    match = _PACKAGE.search(_LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", text)))
    return match.group(1) if match else None


class RepositoryScanner:
    def __init__(
        self,
        store: ScanStore,
        ignore_dirs: Iterable[str],
        max_files: int = 20000,
        max_file_bytes: int = 2_000_000,
    ) -> None:
        self._store = store
        self._ignore = set(ignore_dirs)
        self._max_files = max_files
        self._max_file_bytes = max_file_bytes

    def scan(self, root: Path) -> ScanResult:
        started = time.monotonic()
        root = root.expanduser().resolve()
        if not root.is_dir():
            raise NotADirectoryError(f"source root is not a directory: {root}")
        repo_id = repository_id_for(root)
        log_event(logger, "repository_scan_started", repository=repo_id, root=str(root))

        known = self._store.known_files(repo_id)
        now = datetime.now(UTC)
        files: list[SourceFile] = []
        skipped: list[SkippedFile] = []
        added: list[str] = []
        changed: list[str] = []
        unchanged: list[str] = []
        seen_real: set[Path] = set()
        ignored_dirs = 0

        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            kept = sorted(d for d in dirnames if d not in self._ignore)
            ignored_dirs += len(dirnames) - len(kept)
            dirnames[:] = kept
            for name in sorted(filenames):
                if not name.endswith(".java"):
                    continue
                path = Path(dirpath, name)
                rel = path.relative_to(root).as_posix()
                real = path.resolve()
                if real in seen_real:
                    skipped.append(SkippedFile(relative_path=rel, reason="duplicate_path"))
                    continue
                seen_real.add(real)
                if len(files) >= self._max_files:
                    skipped.append(SkippedFile(relative_path=rel, reason="max_source_files"))
                    continue
                try:
                    size = path.stat().st_size
                    if size > self._max_file_bytes:
                        skipped.append(SkippedFile(relative_path=rel, reason="too_large"))
                        continue
                    data = path.read_bytes()
                except OSError as exc:
                    logger.warning("file_unreadable", extra={"file": rel, "error": str(exc)})
                    skipped.append(SkippedFile(relative_path=rel, reason="unreadable"))
                    continue

                digest = hashlib.sha256(data).hexdigest()
                try:
                    text, encoding_issue = data.decode("utf-8"), False
                except UnicodeDecodeError:
                    text, encoding_issue = data.decode("latin-1"), True
                    logger.warning("source_encoding_issue", extra={"file": rel})

                previous = known.get(rel)
                if previous is None:
                    added.append(rel)
                elif previous.hash != digest:
                    changed.append(rel)
                else:
                    unchanged.append(rel)
                files.append(
                    SourceFile(
                        id=hashlib.sha1(f"{repo_id}:{rel}".encode()).hexdigest()[:16],
                        repository_id=repo_id,
                        path=str(path),
                        relative_path=rel,
                        hash=digest,
                        package=detect_package(text),
                        size=size,
                        last_scanned=now,
                        encoding_issue=encoding_issue,
                    )
                )

        current = {f.relative_path for f in files}
        removed = sorted(set(known) - current)
        # Unchanged files keep their original last_scanned; only new/changed rows are rewritten.
        touched = set(added) | set(changed)
        self._store.save_scan(
            repo_id, str(root), [f for f in files if f.relative_path in touched], removed, now
        )

        result = ScanResult(
            repository_id=repo_id,
            root=str(root),
            files=files,
            added=added,
            changed=changed,
            unchanged=unchanged,
            removed=removed,
            skipped=skipped,
            ignored_dirs=ignored_dirs,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        log_event(logger, "files_discovered", repository=repo_id, count=len(files))
        log_event(logger, "files_changed", repository=repo_id, added=len(added),
                  changed=len(changed), removed=len(removed))
        log_event(logger, "files_skipped", repository=repo_id, count=len(skipped),
                  unchanged=len(unchanged), ignored_dirs=ignored_dirs)
        log_event(logger, "repository_scan_completed", repository=repo_id,
                  duration_ms=result.duration_ms)
        return result
