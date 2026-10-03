"""Vector stores behind one interface: Chroma for real use, in-memory for tests."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Protocol

Metadata = dict[str, str | int | float | bool]
Where = dict[str, Any]


class VectorStoreError(RuntimeError):
    """The vector store failed (unavailable, corrupt, ...). Retrieval degrades to BM25 only."""


class VectorStore(Protocol):
    def upsert(
        self, ids: list[str], embeddings: list[list[float]], metadata: list[Metadata]
    ) -> None: ...

    def delete(self, ids: list[str]) -> None: ...

    def existing(self, ids: list[str]) -> dict[str, str]:
        """id -> content hash, for the ids that are stored."""
        ...

    def query(
        self, embedding: list[float], k: int, where: Where | None = None
    ) -> list[tuple[str, float]]: ...

    def count(self) -> int: ...


def _matches(metadata: Metadata, where: Where | None) -> bool:
    """Evaluate a Chroma-style filter: {"a": 1}, {"a": {"$in": [..]}}, {"$and": [..]}."""
    if not where:
        return True
    for key, cond in where.items():
        if key == "$and":
            if not all(_matches(metadata, c) for c in cond):
                return False
        elif key == "$or":
            if not any(_matches(metadata, c) for c in cond):
                return False
        elif isinstance(cond, dict):
            value = metadata.get(key)
            for op, arg in cond.items():
                if op == "$in" and value not in arg:
                    return False
                if op == "$eq" and value != arg:
                    return False
                if op == "$ne" and value == arg:
                    return False
        elif metadata.get(key) != cond:
            return False
    return True


class InMemoryVectorStore:
    def __init__(self) -> None:
        self._items: dict[str, tuple[list[float], Metadata]] = {}

    def upsert(
        self, ids: list[str], embeddings: list[list[float]], metadata: list[Metadata]
    ) -> None:
        for i, e, m in zip(ids, embeddings, metadata, strict=True):
            self._items[i] = (e, m)

    def delete(self, ids: list[str]) -> None:
        for i in ids:
            self._items.pop(i, None)

    def existing(self, ids: list[str]) -> dict[str, str]:
        return {i: str(self._items[i][1].get("hash", "")) for i in ids if i in self._items}

    def query(
        self, embedding: list[float], k: int, where: Where | None = None
    ) -> list[tuple[str, float]]:
        scored = [
            (doc_id, _cosine(embedding, vec))
            for doc_id, (vec, meta) in self._items.items()
            if _matches(meta, where)
        ]
        return sorted(scored, key=lambda t: (-t[1], t[0]))[:k]

    def count(self) -> int:
        return len(self._items)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


class ChromaVectorStore:
    """Persistent Chroma collection. Telemetry is disabled: nothing leaves the machine."""

    def __init__(self, path: Path, collection: str = "code_helper") -> None:
        try:
            import chromadb
            from chromadb.config import Settings as ChromaSettings

            path.mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(
                path=str(path), settings=ChromaSettings(anonymized_telemetry=False)
            )
            self._collection = client.get_or_create_collection(
                collection, metadata={"hnsw:space": "cosine"}
            )
        except Exception as exc:
            raise VectorStoreError(f"cannot open Chroma at {path}: {exc}") from exc

    def upsert(
        self, ids: list[str], embeddings: list[list[float]], metadata: list[Metadata]
    ) -> None:
        if not ids:
            return
        try:
            self._collection.upsert(ids=ids, embeddings=embeddings, metadatas=metadata)  # type: ignore[arg-type]
        except Exception as exc:
            raise VectorStoreError(f"Chroma upsert failed: {exc}") from exc

    def delete(self, ids: list[str]) -> None:
        if not ids:
            return
        try:
            self._collection.delete(ids=ids)
        except Exception as exc:
            raise VectorStoreError(f"Chroma delete failed: {exc}") from exc

    def existing(self, ids: list[str]) -> dict[str, str]:
        found: dict[str, str] = {}
        try:
            for start in range(0, len(ids), 500):
                got = self._collection.get(ids=ids[start : start + 500], include=["metadatas"])
                for doc_id, meta in zip(got["ids"], got["metadatas"] or [], strict=False):
                    found[doc_id] = str((meta or {}).get("hash", ""))
        except Exception as exc:
            raise VectorStoreError(f"Chroma read failed: {exc}") from exc
        return found

    def query(
        self, embedding: list[float], k: int, where: Where | None = None
    ) -> list[tuple[str, float]]:
        try:
            total = self._collection.count()
            if total == 0:
                return []
            result = self._collection.query(
                query_embeddings=[embedding],  # type: ignore[arg-type]
                n_results=min(k, total),
                where=where or None,
                include=["distances"],
            )
        except Exception as exc:
            raise VectorStoreError(f"Chroma query failed: {exc}") from exc
        ids = result["ids"][0]
        distances = (result.get("distances") or [[0.0] * len(ids)])[0]
        return [(i, 1.0 - float(d)) for i, d in zip(ids, distances, strict=True)]

    def count(self) -> int:
        try:
            return int(self._collection.count())
        except Exception as exc:
            raise VectorStoreError(f"Chroma count failed: {exc}") from exc
