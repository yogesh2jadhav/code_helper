"""Index a repository: scan, then analyze only files whose analysis is missing or stale.

Results are persisted per batch, so an interrupted run resumes where it stopped, and an
unchanged repository re-indexes without launching the JVM at all.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from app.analyzer.base import JavaAnalyzer
from app.analyzer.java_parser import JavaParserAnalyzer
from app.analyzer.store import AnalysisStore
from app.config import Settings
from app.logging_setup import log_event
from app.scanner.models import ScanSummary
from app.scanner.scanner import RepositoryScanner
from app.scanner.store import ScanStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Progress:
    stage: str  # "scan" | "analyze" | "finalize"
    done: int
    total: int


ProgressFn = Callable[[Progress], None]


class IndexSummary(BaseModel):
    repository_id: str
    root: str
    total_files: int
    analyzed: int  # files sent to the analyzer in this run
    up_to_date: int  # files whose stored analysis already matched their hash
    ok: int
    parse_errors: int
    analyzer_errors: int  # retried on the next run
    pruned: int
    duration_ms: int
    scan: ScanSummary


class IndexingService:
    def __init__(self, settings: Settings, analyzer: JavaAnalyzer | None = None) -> None:
        self._settings = settings
        self._analyzer = analyzer or JavaParserAnalyzer(
            settings.java_bin, settings.analyzer_jar, settings.analyzer_timeout_seconds
        )
        self._scan_store = ScanStore(settings.db_path)
        self._analysis_store = AnalysisStore(settings.db_path)

    def resolve_root(self, root: Path | None) -> Path:
        target = root or self._settings.source_root
        if target is None:
            raise ValueError("no repository path given and SOURCE_ROOT is not configured")
        return target

    def index(
        self,
        root: Path | None = None,
        *,
        force: bool = False,
        batch_size: int | None = None,
        on_progress: ProgressFn | None = None,
    ) -> IndexSummary:
        started = time.monotonic()
        emit: ProgressFn = on_progress or (lambda _p: None)
        size = max(1, batch_size or self._settings.index_batch_size)

        emit(Progress("scan", 0, 0))
        scanner = RepositoryScanner(
            self._scan_store,
            self._settings.ignore_dirs,
            self._settings.max_source_files,
            self._settings.max_file_bytes,
        )
        scan = scanner.scan(self.resolve_root(root))
        repo_id = scan.repository_id

        stored = self._analysis_store.states(repo_id)
        pending = [
            f
            for f in scan.files
            if force
            or f.id not in stored
            or stored[f.id][0] != f.hash  # source changed since it was analysed
            or stored[f.id][1] == "analyzer_error"  # transient failure: retry
        ]
        total = len(pending)
        log_event(
            logger,
            "index_started",
            repository=repo_id,
            files=len(scan.files),
            pending=total,
            batch_size=size,
        )

        counts = {"ok": 0, "parse_error": 0, "analyzer_error": 0}
        emit(Progress("analyze", 0, total))
        for start in range(0, total, size):
            batch = pending[start : start + size]
            results = self._analyzer.analyze_files([Path(f.path) for f in batch])
            self._analysis_store.save_batch(
                repo_id, [(f.id, f.hash, r) for f, r in zip(batch, results, strict=True)]
            )
            for r in results:
                counts[r.status] += 1
            done = start + len(batch)
            emit(Progress("analyze", done, total))
            log_event(logger, "index_batch_completed", repository=repo_id, done=done, total=total)

        emit(Progress("finalize", total, total))
        pruned = self._analysis_store.prune(repo_id)
        summary = IndexSummary(
            repository_id=repo_id,
            root=scan.root,
            total_files=len(scan.files),
            analyzed=total,
            up_to_date=len(scan.files) - total,
            ok=counts["ok"],
            parse_errors=counts["parse_error"],
            analyzer_errors=counts["analyzer_error"],
            pruned=pruned,
            duration_ms=int((time.monotonic() - started) * 1000),
            scan=scan.summary(),
        )
        log_event(
            logger,
            "index_completed",
            repository=repo_id,
            analyzed=total,
            up_to_date=summary.up_to_date,
            duration_ms=summary.duration_ms,
        )
        return summary
