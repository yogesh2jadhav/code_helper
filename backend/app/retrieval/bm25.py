"""A small BM25 index with code-aware tokenization."""

from __future__ import annotations

import math
import re
from collections import Counter

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")
_PARTS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_STOP = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "in", "is",
        "it", "its", "of", "on", "or", "that", "the", "this", "to", "was", "with",
    }
)  # fmt: skip


def tokenize(text: str) -> list[str]:
    """Identifiers are kept whole and also split (camelCase, snake_case): `calculateTotal` ->
    calculatetotal, calculate, total."""
    tokens: list[str] = []
    for ident in _IDENT.findall(text):
        whole = ident.lower()
        parts = [p.lower() for p in _PARTS.findall(ident)]
        if len(parts) > 1:
            tokens.append(whole)
            tokens.extend(p for p in parts if p not in _STOP)
        elif whole not in _STOP:
            tokens.append(whole)
    return tokens


class BM25Index:
    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self._postings: dict[str, dict[str, int]] = {}
        self._lengths: dict[str, int] = {}
        self._tokens: dict[str, list[str]] = {}

    def __len__(self) -> int:
        return len(self._lengths)

    def add(self, doc_id: str, text: str) -> None:
        if doc_id in self._lengths:
            self.remove(doc_id)
        tokens = tokenize(text)
        self._tokens[doc_id] = tokens
        self._lengths[doc_id] = len(tokens)
        for token, count in Counter(tokens).items():
            self._postings.setdefault(token, {})[doc_id] = count

    def remove(self, doc_id: str) -> None:
        for token in set(self._tokens.pop(doc_id, [])):
            posting = self._postings.get(token)
            if posting is not None:
                posting.pop(doc_id, None)
                if not posting:
                    del self._postings[token]
        self._lengths.pop(doc_id, None)

    def search(
        self, query: str, k: int = 10, allowed: set[str] | None = None
    ) -> list[tuple[str, float]]:
        """Top-k (doc_id, score). `allowed` restricts scoring to those documents (applied *before*
        ranking, so filtered-out documents never take a slot)."""
        n = len(self._lengths)
        if n == 0:
            return []
        avg = sum(self._lengths.values()) / n
        scores: dict[str, float] = {}
        for token in set(tokenize(query)):
            posting = self._postings.get(token)
            if not posting:
                continue
            idf = math.log(1 + (n - len(posting) + 0.5) / (len(posting) + 0.5))
            for doc_id, tf in posting.items():
                if allowed is not None and doc_id not in allowed:
                    continue
                length = self._lengths[doc_id]
                norm = tf + self.k1 * (1 - self.b + self.b * length / avg)
                scores[doc_id] = scores.get(doc_id, 0.0) + idf * tf * (self.k1 + 1) / norm
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        return ranked[:k]
