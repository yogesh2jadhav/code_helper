"""Build and persist the semantic code model for an indexed repository."""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from app.analyzer.ast_models import AST_SCHEMA_VERSION
from app.config import Settings
from app.knowledge.builder import BuildOptions, KnowledgeBuilder
from app.knowledge.evidence import load_docs
from app.knowledge.models import KnowledgeStats
from app.knowledge.store import KNOWLEDGE_VERSION, KnowledgeStore, RunInfo
from app.logging_setup import log_event
from app.scanner.scanner import repository_id_for
from app.scanner.store import ScanStore
from app.services.resolution_service import ResolutionService

logger = logging.getLogger(__name__)
StageFn = Callable[[str], None]


class KnowledgeSummary(BaseModel):
    repository_id: str
    skipped: bool  # nothing changed since the last successful run
    stats: KnowledgeStats
    run: RunInfo
    duration_ms: int


class KnowledgeService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.store = KnowledgeStore(settings.db_path)
        self._scan_store = ScanStore(settings.db_path)

    def fingerprint(self, root: Path) -> str:
        """Everything the model is derived from: sources, docs, versions, options."""
        repo_id = repository_id_for(root.expanduser().resolve())
        h = hashlib.sha256()
        h.update(f"v{KNOWLEDGE_VERSION}/ast{AST_SCHEMA_VERSION}".encode())
        h.update(f"tests={self._settings.enable_test_analysis}".encode())
        for rel, f in sorted(self._scan_store.known_files(repo_id).items()):
            h.update(f"{rel}:{f.hash}".encode())
        for section in load_docs(root.expanduser().resolve(), self._settings.ignore_dirs):
            h.update(f"{section.file}:{section.start_line}:{section.text}".encode())
        return h.hexdigest()

    def build(
        self, root: Path | None = None, *, force: bool = False, on_stage: StageFn | None = None
    ) -> KnowledgeSummary:
        started = time.monotonic()
        stage: StageFn = on_stage or (lambda _s: None)
        target = root or self._settings.source_root
        if target is None:
            raise ValueError("no repository path given and SOURCE_ROOT is not configured")
        target = target.expanduser().resolve()
        repo_id = repository_id_for(target)

        fingerprint = self.fingerprint(target)
        last = self.store.last_successful_run(repo_id)
        if (
            last is not None
            and not force
            and last.fingerprint == fingerprint
            and last.knowledge_version == KNOWLEDGE_VERSION
        ):
            log_event(logger, "knowledge_up_to_date", repository=repo_id)
            return KnowledgeSummary(
                repository_id=repo_id,
                skipped=True,
                stats=_stats(last),
                run=last,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        stage("resolve")
        run = ResolutionService(self._settings).resolve(target)
        stage("knowledge")
        options = BuildOptions(
            docs_root=target,
            enable_tests=self._settings.enable_test_analysis,
            ignore_dirs=tuple(self._settings.ignore_dirs),
        )
        built = KnowledgeBuilder(
            repo_id, run.files, run.table, run.resolver, run.resolutions, options
        ).build()
        stage("store")
        info = self.store.replace(built.repository, built.graph, fingerprint)
        log_event(
            logger,
            "knowledge_stored",
            repository=repo_id,
            methods=info.methods,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return KnowledgeSummary(
            repository_id=repo_id,
            skipped=False,
            stats=built.repository.stats,
            run=info,
            duration_ms=int((time.monotonic() - started) * 1000),
        )


def _stats(run: RunInfo) -> KnowledgeStats:
    return KnowledgeStats(
        files=run.files or 0,
        classes=run.classes or 0,
        methods=run.methods or 0,
        call_edges=run.call_edges or 0,
        rule_candidates=run.rule_candidates or 0,
        evidence=run.evidence or 0,
        risks=run.risks or 0,
        unknowns=run.unknowns or 0,
    )
