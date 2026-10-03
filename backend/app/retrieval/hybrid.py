"""Hybrid retrieval: BM25 + vector search, fused with reciprocal rank fusion.

Metadata filters are applied *before* ranking in both legs, so documents that do not match never
occupy a result slot. If the vector leg fails (embedder or store down), the result is BM25 only
and says so, rather than failing the request.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel

from app.logging_setup import log_event
from app.retrieval.bm25 import BM25Index
from app.retrieval.documents import DocType, Document
from app.retrieval.embeddings import Embedder, EmbeddingError
from app.retrieval.rrf import reciprocal_rank_fusion
from app.retrieval.store import RetrievalStore
from app.retrieval.vector_store import VectorStore, VectorStoreError

logger = logging.getLogger(__name__)


class SearchHit(BaseModel):
    doc: Document
    score: float
    bm25_rank: int | None = None
    vector_rank: int | None = None


class SearchResult(BaseModel):
    query: str
    hits: list[SearchHit]
    degraded: bool = False  # the vector leg was unavailable; results are BM25 only
    degraded_reason: str | None = None


class HybridRetriever:
    def __init__(
        self,
        store: RetrievalStore,
        vectors: VectorStore | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        self._store = store
        self._vectors = vectors
        self._embedder = embedder
        self._bm25: dict[str, BM25Index] = {}
        self._bm25_count: dict[str, int] = {}

    def bm25_for(self, repository_id: str) -> BM25Index:
        count = self._store.count(repository_id)
        index = self._bm25.get(repository_id)
        if index is None or self._bm25_count.get(repository_id) != count:
            index = BM25Index()
            for doc in self._store.iter_docs(repository_id):
                index.add(doc.id, doc.text + " " + doc.class_name + " " + doc.method_name)
            self._bm25[repository_id] = index
            self._bm25_count[repository_id] = count
        return index

    def search(
        self,
        repository_id: str,
        query: str,
        *,
        k: int = 10,
        types: Iterable[DocType] | None = None,
        class_name: str | None = None,
        method_name: str | None = None,
        file_path: str | None = None,
        symbol_id: str | None = None,
        candidates: int | None = None,
    ) -> SearchResult:
        started = time.monotonic()
        log_event(logger, "retrieval_started", repository=repository_id, k=k)
        n = candidates or max(k * 3, 20)
        allowed = self._store.filter_ids(
            repository_id,
            types=types,
            class_name=class_name,
            method_name=method_name,
            file_path=file_path,
            symbol_id=symbol_id,
        )
        if not allowed:
            return SearchResult(query=query, hits=[])

        bm25 = self.bm25_for(repository_id).search(query, n, allowed)
        where = _where(repository_id, types, class_name, method_name, file_path)
        # symbol_id cannot be expressed as a Chroma filter, so fetch extra and filter afterwards
        fetch = n * 5 if symbol_id else n * 2
        vector, reason = self._vector_leg(query, n, fetch, allowed, where)

        bm25_rank = {doc_id: i for i, (doc_id, _) in enumerate(bm25, 1)}
        vector_rank = {doc_id: i for i, (doc_id, _) in enumerate(vector, 1)}
        legs = [[d for d, _ in bm25]] + ([[d for d, _ in vector]] if vector else [])
        fused = reciprocal_rank_fusion(legs)[:k]
        docs = self._store.get_many([d for d, _ in fused])
        hits = [
            SearchHit(
                doc=docs[d], score=s, bm25_rank=bm25_rank.get(d), vector_rank=vector_rank.get(d)
            )
            for d, s in fused
            if d in docs
        ]
        log_event(
            logger,
            "retrieval_completed",
            repository=repository_id,
            hits=len(hits),
            degraded=reason is not None,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return SearchResult(
            query=query, hits=hits, degraded=reason is not None, degraded_reason=reason
        )

    def _vector_leg(
        self, query: str, n: int, fetch: int, allowed: set[str], where: dict[str, Any]
    ) -> tuple[list[tuple[str, float]], str | None]:
        if self._vectors is None or self._embedder is None:
            return [], "vector search is not configured"
        try:
            embedding = self._embedder.embed([query])[0]
            found = self._vectors.query(embedding, fetch, where)
        except (EmbeddingError, VectorStoreError) as exc:
            logger.warning("vector_search_unavailable", extra={"error": str(exc)[:200]})
            return [], str(exc)
        return [(d, s) for d, s in found if d in allowed][:n], None


def _where(
    repository_id: str,
    types: Iterable[DocType] | None,
    class_name: str | None,
    method_name: str | None,
    file_path: str | None,
) -> dict[str, Any]:
    clauses: list[dict[str, Any]] = [{"repository_id": repository_id}]
    if types:
        clauses.append({"source_type": {"$in": list(types)}})
    for key, value in (
        ("class_name", class_name),
        ("method_name", method_name),
        ("file_path", file_path),
    ):
        if value:
            clauses.append({key: value})
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}
