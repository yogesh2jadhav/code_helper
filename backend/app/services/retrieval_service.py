"""Wire retrieval components from settings and run indexing/search for a repository."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from app.config import Settings
from app.knowledge.source import SourceReader
from app.knowledge.store import KnowledgeStore
from app.retrieval.embeddings import Embedder, HashingEmbedder, OllamaEmbedder
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.indexer import ProgressFn, RetrievalIndexer, RetrievalReport
from app.retrieval.store import RetrievalStore
from app.retrieval.vector_store import ChromaVectorStore, VectorStore, VectorStoreError
from app.scanner.scanner import repository_id_for

logger = logging.getLogger(__name__)


def make_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "hash":
        return HashingEmbedder()
    return OllamaEmbedder(
        settings.ollama_base_url, settings.ollama_embed_model, settings.ollama_timeout_seconds
    )


def collection_name(embedder: Embedder) -> str:
    """One collection per embedder, since vectors from different models are not comparable."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", embedder.name)
    return f"code_helper_{safe}"[:63]


class RetrievalService:
    def __init__(
        self,
        settings: Settings,
        embedder: Embedder | None = None,
        vectors: VectorStore | None = None,
        use_vectors: bool = True,
    ) -> None:
        self._settings = settings
        self.store = RetrievalStore(settings.db_path)
        self.knowledge = KnowledgeStore(settings.db_path)
        self.embedder: Embedder | None = None
        self.vectors: VectorStore | None = None
        self.vector_error: str | None = None
        if use_vectors:
            self.embedder = embedder or make_embedder(settings)
            if vectors is not None:
                self.vectors = vectors
            else:
                try:
                    self.vectors = ChromaVectorStore(
                        settings.effective_chroma_path, collection_name(self.embedder)
                    )
                except VectorStoreError as exc:  # structural analysis and BM25 still work
                    self.vector_error = str(exc)
                    logger.warning("vector_store_unavailable", extra={"error": str(exc)[:200]})

    def repository_id(self, root: Path | None) -> str:
        target = root or self._settings.source_root
        if target is None:
            raise ValueError("no repository path given and SOURCE_ROOT is not configured")
        return repository_id_for(target.expanduser().resolve())

    def index(
        self, root: Path | None = None, on_progress: ProgressFn | None = None
    ) -> RetrievalReport:
        target = root or self._settings.source_root
        repo_id = self.repository_id(root)
        assert target is not None
        if self.knowledge.count_classes(repo_id) == 0:
            raise LookupError(f"no knowledge model stored for {target}; run `index` first")
        indexer = RetrievalIndexer(
            self.store, self.vectors, self.embedder, self._settings.embed_batch_size
        )
        report = indexer.index(
            repo_id,
            self.knowledge.iter_classes(repo_id),
            self.knowledge.iter_methods(repo_id),
            SourceReader(target),
            on_progress,
        )
        if self.vector_error and report.vector_error is None:
            report = report.model_copy(update={"vector_error": self.vector_error})
        return report

    def retriever(self) -> HybridRetriever:
        return HybridRetriever(self.store, self.vectors, self.embedder)
