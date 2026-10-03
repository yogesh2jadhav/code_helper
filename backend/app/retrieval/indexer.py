"""Build and refresh the retrieval index from the knowledge model, incrementally.

Documents are identified by id and content hash. Unchanged documents are neither re-stored nor
re-embedded; removed ones are deleted from both stores. If the embedder or vector store fails, the
text index (and so BM25 search) is still complete and the report says vectors are incomplete.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable

from pydantic import BaseModel

from app.knowledge.models import ClassKnowledge, MethodKnowledge
from app.knowledge.source import SourceReader
from app.logging_setup import log_event
from app.retrieval.documents import Document, build_documents
from app.retrieval.embeddings import Embedder, EmbeddingError
from app.retrieval.store import RetrievalStore
from app.retrieval.vector_store import VectorStore, VectorStoreError

logger = logging.getLogger(__name__)
ProgressFn = Callable[[str, int, int], None]


class RetrievalReport(BaseModel):
    repository_id: str
    documents: int
    new: int
    changed: int
    unchanged: int
    removed: int
    embedded: int
    pending_vectors: int  # documents that still lack a vector (0 when everything is embedded)
    vector_enabled: bool
    vector_error: str | None = None
    duration_ms: int


class RetrievalIndexer:
    def __init__(
        self,
        store: RetrievalStore,
        vectors: VectorStore | None = None,
        embedder: Embedder | None = None,
        batch_size: int = 32,
    ) -> None:
        self._store = store
        self._vectors = vectors
        self._embedder = embedder
        self._batch = max(1, batch_size)

    def index(
        self,
        repository_id: str,
        classes: Iterable[ClassKnowledge],
        methods: Iterable[MethodKnowledge],
        reader: SourceReader | None = None,
        on_progress: ProgressFn | None = None,
    ) -> RetrievalReport:
        started = time.monotonic()
        emit = on_progress or (lambda _stage, _done, _total: None)
        docs = list(build_documents(repository_id, classes, methods, reader))
        by_id = {d.id: d for d in docs}

        stored = self._store.hashes(repository_id)
        new = [d for d in docs if d.id not in stored]
        changed = [d for d in docs if d.id in stored and stored[d.id] != d.hash]
        removed = [i for i in stored if i not in by_id]
        self._store.upsert([*new, *changed])
        self._store.delete(removed)
        emit("documents", len(docs), len(docs))

        embedded, pending, error = self._embed(docs, removed, emit)
        report = RetrievalReport(
            repository_id=repository_id,
            documents=len(docs),
            new=len(new),
            changed=len(changed),
            unchanged=len(docs) - len(new) - len(changed),
            removed=len(removed),
            embedded=embedded,
            pending_vectors=pending,
            vector_enabled=self._vectors is not None and self._embedder is not None,
            vector_error=error,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        log_event(
            logger,
            "retrieval_index_completed",
            repository=repository_id,
            documents=report.documents,
            embedded=embedded,
            pending=pending,
            duration_ms=report.duration_ms,
        )
        return report

    def _embed(
        self, docs: list[Document], removed: list[str], emit: ProgressFn
    ) -> tuple[int, int, str | None]:
        """Returns (embedded this run, documents still without a vector, error or None)."""
        if self._vectors is None or self._embedder is None:
            return 0, 0, None
        embedded = 0
        outstanding = len(docs)  # until we know how many vectors are really missing
        try:
            self._vectors.delete(removed)
            have: dict[str, str] = {}
            ids = [d.id for d in docs]
            for start in range(0, len(ids), 500):
                have.update(self._vectors.existing(ids[start : start + 500]))
            todo = [d for d in docs if have.get(d.id) != d.hash]
            outstanding = len(todo)
            for start in range(0, len(todo), self._batch):
                batch = todo[start : start + self._batch]
                vectors = self._embedder.embed([d.text for d in batch])
                self._vectors.upsert([d.id for d in batch], vectors, [d.metadata() for d in batch])
                embedded += len(batch)
                emit("embeddings", embedded, len(todo))
        except (EmbeddingError, VectorStoreError) as exc:
            logger.warning(
                "embedding_stopped", extra={"error": str(exc)[:200], "embedded": embedded}
            )
            return embedded, outstanding - embedded, str(exc)
        return embedded, 0, None
