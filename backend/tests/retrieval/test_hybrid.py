from __future__ import annotations

from pathlib import Path

import pytest

from app.knowledge.source import SourceReader
from app.retrieval.bm25 import tokenize
from app.retrieval.embeddings import EmbeddingError, HashingEmbedder
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.indexer import RetrievalIndexer
from app.retrieval.store import RetrievalStore
from app.retrieval.vector_store import InMemoryVectorStore, VectorStoreError
from tests.retrieval.conftest import World

CONCEPTS = {
    "cost": 0,
    "fee": 0,
    "price": 0,
    "charge": 0,
    "charges": 0,
    "extra": 0,
    "pay": 0,
    "discount": 1,
    "rebate": 1,
    "loyalty": 1,
    "print": 2,
    "render": 2,
    "invoice": 2,
    "invoices": 2,
    "parcel": 3,
    "parcels": 3,
    "package": 3,
    "packages": 3,
    "shipment": 3,
    "shipping": 3,
    "heavy": 4,
    "weight": 4,
    "weightkg": 4,
    "kilos": 4,
}


class SynonymEmbedder:
    """A stand-in for a semantic model: words with the same meaning share a dimension."""

    name = "synonyms"

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * 5
            for token in tokenize(text):
                if token in CONCEPTS:
                    vec[CONCEPTS[token]] += 1.0
            norm = sum(v * v for v in vec) ** 0.5 or 1.0
            out.append([v / norm for v in vec])
        return out


class BrokenEmbedder:
    name = "broken"

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingError("Ollama is down")


class BrokenVectors(InMemoryVectorStore):
    def query(self, *args: object, **kwargs: object) -> list[tuple[str, float]]:  # type: ignore[override]
        raise VectorStoreError("Chroma is unavailable")


def build(
    shop: World, tmp_path: Path, embedder: object | None, vectors: InMemoryVectorStore | None
) -> HybridRetriever:
    store = RetrievalStore(tmp_path / "r.sqlite3")
    k = shop.built.repository
    RetrievalIndexer(store, vectors, embedder).index(  # type: ignore[arg-type]
        shop.repository_id, k.classes, k.methods, SourceReader(shop.root)
    )
    return HybridRetriever(store, vectors, embedder)  # type: ignore[arg-type]


@pytest.fixture
def hybrid(shop: World, tmp_path: Path) -> HybridRetriever:
    return build(shop, tmp_path, SynonymEmbedder(), InMemoryVectorStore())


def methods_of(result) -> list[str]:  # type: ignore[no-untyped-def]
    return [h.doc.method_name for h in result.hits]


def test_lexical_query_finds_the_method_through_both_legs(
    shop: World, hybrid: HybridRetriever
) -> None:
    result = hybrid.search(shop.repository_id, "loyalty discount years")
    top = result.hits[0]
    assert (top.doc.type, top.doc.method_name) == ("method_source", "loyaltyDiscount")
    assert top.bm25_rank == 1 and top.vector_rank is not None
    assert not result.degraded and result.query == "loyalty discount years"
    scores = [h.score for h in result.hits]
    assert scores == sorted(scores, reverse=True)
    assert len({h.doc.id for h in result.hits}) == len(result.hits)


def test_semantic_match_with_no_shared_words_needs_the_vector_leg(
    shop: World, tmp_path: Path, hybrid: HybridRetriever
) -> None:
    query = "what do we charge for a package"  # shares no token with the code or docs
    bm25_only = build(shop, tmp_path / "b", None, None).search(shop.repository_id, query)
    assert bm25_only.hits == []  # no shared words, so lexical search finds nothing at all
    assert bm25_only.degraded and "not configured" in (bm25_only.degraded_reason or "")

    fused = hybrid.search(shop.repository_id, query, k=5)
    assert fused.hits and fused.hits[0].doc.class_name == "shop.ShippingCalculator"
    assert fused.hits[0].bm25_rank is None and fused.hits[0].vector_rank == 1
    assert "shippingFee" in methods_of(fused)


def test_fusion_combines_what_each_leg_finds(shop: World, hybrid: HybridRetriever) -> None:
    result = hybrid.search(shop.repository_id, "invoice customer total", k=6)
    assert result.hits[0].doc.class_name == "shop.InvoicePrinter"
    both = [h for h in result.hits if h.bm25_rank and h.vector_rank]
    assert both and both[0].score > result.hits[-1].score


def test_filters_apply_before_ranking(shop: World, hybrid: HybridRetriever) -> None:
    rid = shop.repository_id
    tests = hybrid.search(rid, "shipping fee heavy parcels", types=["test"])
    assert [h.doc.type for h in tests.hits] == ["test"] and tests.hits[
        0
    ].doc.method_name == "heavyParcelsCostExtra"

    docs = hybrid.search(rid, "shipping fee heavy parcels", types=["documentation"], k=5)
    assert {h.doc.type for h in docs.hits} == {"documentation"}
    assert docs.hits[0].doc.file_path == "README.md"

    cls = hybrid.search(rid, "render total", class_name="shop.InvoicePrinter")
    assert {h.doc.class_name for h in cls.hits} == {"shop.InvoicePrinter"}

    one = hybrid.search(rid, "discount", method_name="loyaltyDiscount")
    assert {h.doc.method_name for h in one.hits} == {"loyaltyDiscount"}
    sym = hybrid.search(
        rid, "shipping", symbol_id="shop.ShippingCalculator#shippingFee(double,boolean)"
    )
    assert sym.hits and all(
        "shop.ShippingCalculator#shippingFee(double,boolean)" in h.doc.symbol_ids for h in sym.hits
    )
    path = hybrid.search(rid, "price", file_path="src/main/shop/InvoicePrinter.java")
    assert {h.doc.file_path for h in path.hits} == {"src/main/shop/InvoicePrinter.java"}


def test_filtered_out_documents_never_take_a_slot(shop: World, hybrid: HybridRetriever) -> None:
    everything = hybrid.search(shop.repository_id, "shipping heavy parcels", k=2)
    assert len(everything.hits) == 2
    only_classes = hybrid.search(
        shop.repository_id, "shipping heavy parcels", k=2, types=["class_source"]
    )
    assert len(only_classes.hits) == 2 and {h.doc.type for h in only_classes.hits} == {
        "class_source"
    }


def test_no_match_is_an_empty_result_not_an_error(shop: World, hybrid: HybridRetriever) -> None:
    assert hybrid.search(shop.repository_id, "x", class_name="nope.Nothing").hits == []
    assert hybrid.search("another-repo", "shipping").hits == []
    assert hybrid.search(shop.repository_id, "zzzzqqqq", types=["test"], k=3).hits is not None


def test_k_limits_results(shop: World, hybrid: HybridRetriever) -> None:
    assert len(hybrid.search(shop.repository_id, "shipping heavy parcels fee", k=3).hits) == 3
    assert len(hybrid.search(shop.repository_id, "shipping heavy parcels fee", k=1).hits) == 1


@pytest.mark.parametrize(
    ("embedder", "vectors", "reason"),
    [
        (BrokenEmbedder(), InMemoryVectorStore(), "Ollama is down"),
        (SynonymEmbedder(), BrokenVectors(), "Chroma is unavailable"),
    ],
)
def test_vector_failure_degrades_to_bm25_instead_of_failing(
    shop: World, tmp_path: Path, embedder: object, vectors: InMemoryVectorStore, reason: str
) -> None:
    store = RetrievalStore(tmp_path / "d.sqlite3")
    k = shop.built.repository
    RetrievalIndexer(store, None, None).index(
        shop.repository_id, k.classes, k.methods, SourceReader(shop.root)
    )
    retriever = HybridRetriever(store, vectors, embedder)  # type: ignore[arg-type]
    result = retriever.search(shop.repository_id, "loyalty discount years")
    assert result.degraded and reason in (result.degraded_reason or "")
    assert result.hits[0].doc.method_name == "loyaltyDiscount"  # BM25 still answers
    assert all(h.vector_rank is None for h in result.hits)


def test_bm25_cache_notices_new_documents(shop: World, tmp_path: Path) -> None:
    store = RetrievalStore(tmp_path / "c.sqlite3")
    k = shop.built.repository
    fee = [m for m in k.methods if m.name == "shippingFee"]
    RetrievalIndexer(store).index(shop.repository_id, [], fee, None)
    retriever = HybridRetriever(store)
    assert retriever.search(shop.repository_id, "render invoice").hits == []
    assert retriever.search(shop.repository_id, "shipping fee").hits  # the cache is built now

    RetrievalIndexer(store).index(shop.repository_id, k.classes, k.methods, None)
    after = retriever.search(shop.repository_id, "render invoice")
    assert after.hits and after.hits[0].doc.class_name == "shop.InvoicePrinter"


def test_hashing_embedder_is_good_enough_for_lexical_queries(shop: World, tmp_path: Path) -> None:
    retriever = build(shop, tmp_path, HashingEmbedder(), InMemoryVectorStore())
    result = retriever.search(shop.repository_id, "loyaltyDiscount price years")
    assert result.hits[0].doc.method_name == "loyaltyDiscount" and not result.degraded
