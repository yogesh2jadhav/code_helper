from __future__ import annotations

import math

import httpx
import pytest

from app.retrieval.bm25 import BM25Index, tokenize
from app.retrieval.embeddings import EmbeddingError, HashingEmbedder, OllamaEmbedder
from app.retrieval.rrf import reciprocal_rank_fusion

# ---- tokenization and BM25 ---------------------------------------------------------------------


def test_identifiers_are_kept_whole_and_split() -> None:
    assert tokenize("calculateTotal") == ["calculatetotal", "calculate", "total"]
    assert tokenize("MAX_RETRY_COUNT") == ["max_retry_count", "max", "retry", "count"]
    assert tokenize("parseHTTPResponse2") == [
        "parsehttpresponse2",
        "parse",
        "http",
        "response",
        "2",
    ]
    assert tokenize("the price of an item") == ["price", "item"]  # stopwords dropped
    assert tokenize("x + 42") == ["x", "42"]


def test_bm25_ranks_by_term_frequency_rarity_and_length() -> None:
    index = BM25Index()
    index.add("a", "shipping fee for heavy parcels")
    index.add("b", "invoice printer renders invoices")
    index.add("c", "heavy heavy heavy parcels shipping fee calculator with many other words here")
    index.add("d", "totally unrelated text about printing")
    hits = index.search("heavy parcels fee")
    assert [d for d, _ in hits][:2] == ["a", "c"]  # same terms; the shorter document wins
    assert "b" not in [d for d, _ in hits] and "d" not in [d for d, _ in hits]
    assert all(score > 0 for _, score in hits)


def test_rare_terms_outweigh_common_ones() -> None:
    index = BM25Index()
    for i in range(20):
        index.add(f"common{i}", "service handler method")
    index.add("rare", "service handler method zebra")
    assert index.search("service zebra")[0][0] == "rare"


def test_camel_case_query_matches_split_words() -> None:
    index = BM25Index()
    index.add("m", "double shippingFee(double weightKg)")
    assert index.search("shipping fee")[0][0] == "m"
    assert index.search("weight")[0][0] == "m"
    assert index.search("shippingFee")[0][0] == "m"


def test_allowed_filter_is_applied_before_ranking() -> None:
    index = BM25Index()
    index.add("best", "heavy parcels heavy parcels")
    index.add("ok1", "heavy parcels here")
    index.add("ok2", "parcels there heavy")
    assert [d for d, _ in index.search("heavy parcels", k=1)] == ["best"]
    # excluding the best document must surface the next ones, not return an empty slot
    assert {d for d, _ in index.search("heavy parcels", k=2, allowed={"ok1", "ok2"})} == {
        "ok1",
        "ok2",
    }
    assert index.search("heavy", allowed=set()) == []


def test_bm25_add_replace_remove_and_empty() -> None:
    index = BM25Index()
    assert index.search("anything") == [] and len(index) == 0
    index.add("a", "alpha beta")
    index.add("a", "gamma delta")  # replaces
    assert len(index) == 1 and index.search("alpha") == [] and index.search("gamma")[0][0] == "a"
    index.remove("a")
    index.remove("a")  # removing twice is harmless
    assert len(index) == 0 and index.search("gamma") == []


def test_bm25_ties_are_deterministic() -> None:
    index = BM25Index()
    for doc_id in ("b", "a", "c"):
        index.add(doc_id, "same words")
    assert [d for d, _ in index.search("same words")] == ["a", "b", "c"]


# ---- reciprocal rank fusion --------------------------------------------------------------------


def test_rrf_scores_and_order() -> None:
    fused = dict(reciprocal_rank_fusion([["a", "b", "c"], ["b", "c", "d"]], k=60))
    assert fused["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert fused["a"] == pytest.approx(1 / 61) and fused["d"] == pytest.approx(1 / 63)
    order = [d for d, _ in reciprocal_rank_fusion([["a", "b", "c"], ["b", "c", "d"]])]
    assert order == ["b", "c", "a", "d"]  # agreement across lists beats a single first place


def test_rrf_weights_and_edge_cases() -> None:
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([["a"], []]) == [("a", pytest.approx(1 / 61))]
    weighted = [d for d, _ in reciprocal_rank_fusion([["x"], ["y"]], weights=[1.0, 3.0])]
    assert weighted == ["y", "x"]
    assert [d for d, _ in reciprocal_rank_fusion([["b"], ["a"]])] == ["a", "b"]  # tie -> by id


# ---- embedders --------------------------------------------------------------------------------


def test_hashing_embedder_is_deterministic_normalised_and_lexical() -> None:
    e = HashingEmbedder(64)
    a, b, c = e.embed(["heavy parcels fee", "heavy parcels fee", "invoice printer"])
    assert a == b and len(a) == 64
    assert math.sqrt(sum(x * x for x in a)) == pytest.approx(1.0)
    similar = sum(x * y for x, y in zip(a, e.embed(["fee for heavy parcels"])[0], strict=True))
    different = sum(x * y for x, y in zip(a, c, strict=True))
    assert similar > 0.8 > different
    assert e.embed([]) == [] and e.embed([""])[0] == [0.0] * 64


class FakeResponse:
    def __init__(self, status: int = 200, payload: object = None) -> None:
        self.status_code, self._payload = status, payload

    def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def patch_post(monkeypatch: pytest.MonkeyPatch, result: object) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def fake_post(url: str, json: dict[str, object], timeout: float) -> object:
        calls.append({"url": url, "json": json, "timeout": timeout})
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


def test_ollama_embedder_sends_a_batch_and_parses_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = patch_post(monkeypatch, FakeResponse(200, {"embeddings": [[1, 2], [3, 4]]}))
    e = OllamaEmbedder("http://host:11434/", "nomic-embed-text", timeout=7)
    assert e.embed(["a", "b"]) == [[1.0, 2.0], [3.0, 4.0]]
    assert calls == [
        {
            "url": "http://host:11434/api/embed",
            "json": {"model": "nomic-embed-text", "input": ["a", "b"]},
            "timeout": 7,
        }
    ]
    assert e.name == "ollama:nomic-embed-text" and e.embed([]) == []


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (httpx.ConnectError("refused"), "cannot reach Ollama"),
        (httpx.ReadTimeout("slow"), "timed out"),
        (FakeResponse(404, {}), "ollama pull nomic-embed-text"),
        (FakeResponse(500, {}), "HTTP 500"),
        (FakeResponse(200, ValueError("bad json")), "malformed"),
        (FakeResponse(200, {"other": 1}), "malformed"),
        (FakeResponse(200, {"embeddings": [[1.0]]}), "did not match"),
        (FakeResponse(200, {"embeddings": [[1.0], []]}), "did not match"),
    ],
)
def test_ollama_failures_are_reported_clearly(
    monkeypatch: pytest.MonkeyPatch, result: object, message: str
) -> None:
    patch_post(monkeypatch, result)
    with pytest.raises(EmbeddingError, match=message):
        OllamaEmbedder("http://h", "nomic-embed-text").embed(["a", "b"])
