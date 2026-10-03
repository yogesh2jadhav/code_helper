"""The whole analysis pipeline: scan -> analyze -> resolve -> knowledge model -> store."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from app.analyzer.base import JavaAnalyzer
from app.config import Settings
from app.services.indexing_service import IndexingService, IndexSummary, Progress, ProgressFn
from app.services.knowledge_service import KnowledgeService, KnowledgeSummary


class PipelineSummary(BaseModel):
    index: IndexSummary
    knowledge: KnowledgeSummary | None = None


class PipelineService:
    def __init__(self, settings: Settings, analyzer: JavaAnalyzer | None = None) -> None:
        self._settings = settings
        self._indexing = IndexingService(settings, analyzer)
        self._knowledge = KnowledgeService(settings)

    def run(
        self,
        root: Path | None = None,
        *,
        force: bool = False,
        batch_size: int | None = None,
        knowledge: bool = True,
        on_progress: ProgressFn | None = None,
    ) -> PipelineSummary:
        emit: ProgressFn = on_progress or (lambda _p: None)
        index = self._indexing.index(
            root, force=force, batch_size=batch_size, on_progress=on_progress
        )
        if not knowledge:
            return PipelineSummary(index=index)

        def stage(name: str) -> None:
            emit(Progress(name, 0, 0))

        summary = self._knowledge.build(root, force=force, on_stage=stage)
        return PipelineSummary(index=index, knowledge=summary)
