"""Embedding providers behind one interface."""

from __future__ import annotations

import hashlib
import logging
import math
import time
from typing import Protocol

import httpx

from app.logging_setup import log_event
from app.retrieval.bm25 import tokenize

logger = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    """The embedding provider could not produce vectors (down, model missing, bad response)."""


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Deterministic bag-of-tokens feature hashing. No model, no network: for tests and offline use.

    It captures lexical overlap only, not meaning, so it is never a substitute for a real model in
    production, but it makes the vector path fully testable.
    """

    def __init__(self, dimension: int = 256) -> None:
        self.dimension = dimension
        self.name = f"hash-{dimension}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dimension
        for token in tokenize(text):
            digest = hashlib.md5(token.encode(), usedforsecurity=False).digest()
            index = int.from_bytes(digest[:4], "little") % self.dimension
            vec[index] += 1.0 if digest[4] % 2 == 0 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class OllamaEmbedder:
    def __init__(self, base_url: str, model: str, timeout: float = 120.0) -> None:
        self._url = base_url.rstrip("/") + "/api/embed"
        self._model = model
        self._timeout = timeout
        self.name = f"ollama:{model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        started = time.monotonic()
        try:
            response = httpx.post(
                self._url, json={"model": self._model, "input": texts}, timeout=self._timeout
            )
        except httpx.TimeoutException as exc:
            raise EmbeddingError(f"embedding request timed out after {self._timeout}s") from exc
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"cannot reach Ollama at {self._url}: {exc}") from exc
        if response.status_code == 404:
            raise EmbeddingError(
                f"embedding model '{self._model}' is not available; run `ollama pull {self._model}`"
            )
        if response.status_code != 200:
            raise EmbeddingError(f"Ollama returned HTTP {response.status_code}")
        try:
            vectors = response.json()["embeddings"]
        except (ValueError, KeyError) as exc:
            raise EmbeddingError("malformed embedding response") from exc
        if len(vectors) != len(texts) or not all(vectors):
            raise EmbeddingError("embedding response did not match the request")
        log_event(
            logger,
            "embedding_batch_completed",
            model=self._model,
            texts=len(texts),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return [[float(x) for x in v] for v in vectors]
