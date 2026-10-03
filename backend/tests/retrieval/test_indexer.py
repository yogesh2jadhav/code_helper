from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.cli import main as cli_main
from app.config import Settings
from app.knowledge.source import SourceReader
from app.retrieval.embeddings import EmbeddingError, HashingEmbedder
from app.retrieval.indexer import RetrievalIndexer
from app.retrieval.store import RetrievalStore
from app.retrieval.vector_store import InMemoryVectorStore, VectorStoreError
from app.services.pipeline_service import PipelineService
from app.services.retrieval_service import RetrievalService, collection_name
from tests.retrieval.conftest import World


class CountingEmbedder(HashingEmbedder):
    def __init__(self, fail_after_batches: int | None = None) -> None:
        super().__init__(64)
        self.texts_embedded = 0
        self.calls = 0
        self.fail_after = fail_after_batches

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.fail_after is not None and self.calls >= self.fail_after:
            raise EmbeddingError("Ollama went away")
        self.calls += 1
        self.texts_embedded += len(texts)
        return super().embed(texts)


def run(
    shop: World,
    store: RetrievalStore,
    vectors: InMemoryVectorStore | None,
    embedder: HashingEmbedder | None,
    *,
    methods: list | None = None,
    batch: int = 4,  # type: ignore[type-arg]
    progress: list | None = None,  # type: ignore[type-arg]
):
    k = shop.built.repository
    indexer = RetrievalIndexer(store, vectors, embedder, batch_size=batch)
    callback = (
        (lambda stage, done, total: progress.append((stage, done, total)))
        if progress is not None
        else None
    )
    return indexer.index(
        shop.repository_id,
        k.classes,
        k.methods if methods is None else methods,
        SourceReader(shop.root),
        callback,
    )


def test_first_run_stores_and_embeds_everything(shop: World, tmp_path: Path) -> None:
    store, vectors, embedder = (
        RetrievalStore(tmp_path / "r.db"),
        InMemoryVectorStore(),
        CountingEmbedder(),
    )
    events: list[tuple[str, int, int]] = []
    report = run(shop, store, vectors, embedder, progress=events)
    assert (
        report.documents == report.new == store.count(shop.repository_id) == vectors.count() == 15
    )
    assert (report.changed, report.unchanged, report.removed, report.embedded) == (0, 0, 0, 15)
    assert report.pending_vectors == 0 and report.vector_enabled and report.vector_error is None
    assert embedder.texts_embedded == 15 and embedder.calls == 4  # batches of 4
    assert events[0] == ("documents", 15, 15)
    assert [e for e in events if e[0] == "embeddings"][-1] == ("embeddings", 15, 15)


def test_second_run_does_no_work(shop: World, tmp_path: Path) -> None:
    store, vectors, embedder = (
        RetrievalStore(tmp_path / "r.db"),
        InMemoryVectorStore(),
        CountingEmbedder(),
    )
    run(shop, store, vectors, embedder)
    embedder.calls = embedder.texts_embedded = 0
    report = run(shop, store, vectors, embedder)
    assert (report.new, report.changed, report.unchanged, report.embedded) == (0, 0, 15, 0)
    assert embedder.calls == 0  # nothing re-embedded


def test_only_changed_documents_are_reembedded(shop: World, tmp_path: Path) -> None:
    store, vectors, embedder = (
        RetrievalStore(tmp_path / "r.db"),
        InMemoryVectorStore(),
        CountingEmbedder(),
    )
    run(shop, store, vectors, embedder)
    embedder.calls = embedder.texts_embedded = 0
    edited = [
        m.model_copy(
            update={"purpose": m.purpose.model_copy(update={"text": "Totally new purpose."})}
        )
        if m.name == "render"
        else m
        for m in shop.built.repository.methods
    ]
    report = run(shop, store, vectors, embedder, methods=edited)
    assert report.changed == 1 and report.new == 0 and report.unchanged == 14
    assert embedder.texts_embedded == 1
    doc = next(
        d
        for d in store.iter_docs(shop.repository_id)
        if d.method_name == "render" and d.type == "method_source"
    )
    assert "Totally new purpose." in doc.text


def test_removed_documents_leave_both_stores(shop: World, tmp_path: Path) -> None:
    store, vectors, embedder = (
        RetrievalStore(tmp_path / "r.db"),
        InMemoryVectorStore(),
        CountingEmbedder(),
    )
    run(shop, store, vectors, embedder)
    kept = [m for m in shop.built.repository.methods if m.name != "loyaltyDiscount"]
    report = run(shop, store, vectors, embedder, methods=kept)
    assert report.removed >= 2  # the method document, its rule and its javadoc evidence
    assert store.count(shop.repository_id) == vectors.count() == 15 - report.removed
    assert not [
        d for d in store.iter_docs(shop.repository_id) if d.method_name == "loyaltyDiscount"
    ]


def test_embedder_failure_keeps_text_search_complete_and_resumes(
    shop: World, tmp_path: Path
) -> None:
    store, vectors = RetrievalStore(tmp_path / "r.db"), InMemoryVectorStore()
    flaky = CountingEmbedder(fail_after_batches=1)
    broken = run(shop, store, vectors, flaky)
    assert broken.vector_error == "Ollama went away"
    assert (broken.embedded, broken.pending_vectors) == (4, 11)
    assert store.count(shop.repository_id) == 15  # every document is still searchable via BM25
    assert vectors.count() == 4

    healthy = CountingEmbedder()
    resumed = run(shop, store, vectors, healthy)
    assert resumed.vector_error is None and resumed.pending_vectors == 0
    assert (resumed.embedded, healthy.texts_embedded) == (11, 11)  # only the missing ones
    assert vectors.count() == 15


class FailingExisting(InMemoryVectorStore):
    def existing(self, ids: list[str]) -> dict[str, str]:
        raise VectorStoreError("Chroma read failed")


def test_vector_store_failure_is_reported_not_raised(shop: World, tmp_path: Path) -> None:
    store = RetrievalStore(tmp_path / "r.db")
    report = run(shop, store, FailingExisting(), CountingEmbedder())
    assert report.vector_error == "Chroma read failed"
    assert (report.embedded, report.pending_vectors) == (0, 15)
    assert store.count(shop.repository_id) == 15


def test_without_vectors_only_the_text_index_is_built(shop: World, tmp_path: Path) -> None:
    store = RetrievalStore(tmp_path / "r.db")
    report = run(shop, store, None, None)
    assert not report.vector_enabled and report.embedded == 0 and report.pending_vectors == 0
    assert store.count(shop.repository_id) == 15


def test_store_filters(shop: World, tmp_path: Path) -> None:
    store = RetrievalStore(tmp_path / "r.db")
    run(shop, store, None, None)
    rid = shop.repository_id
    assert len(store.filter_ids(rid, types=["test"])) == 1
    assert len(store.filter_ids(rid, types=["method_source", "test"])) == 4
    assert len(store.filter_ids(rid, class_name="shop.InvoicePrinter")) == 3
    assert store.filter_ids(rid, method_name="render", types=["method_source"]) != set()
    assert store.filter_ids("other", types=["test"]) == set()
    hashes = store.hashes(rid)
    assert len(hashes) == 15 and all(len(h) == 16 for h in hashes.values())
    assert store.get_many(["nope"]) == {}


# ---- the service and CLI, with real Chroma ----------------------------------------------------


@pytest.fixture
def indexed_shop(
    isolated_settings: Settings, java_analyzer: object, shop: World, monkeypatch: pytest.MonkeyPatch
) -> Settings:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "hash")
    from app.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    PipelineService(settings).run(shop.root)
    return settings


def test_service_indexes_into_chroma_and_searches(indexed_shop: Settings, shop: World) -> None:
    service = RetrievalService(indexed_shop)
    report = service.index(shop.root)
    assert report.documents == 15 and report.embedded == 15 and report.vector_error is None
    assert service.vectors is not None and service.vectors.count() == 15

    again = RetrievalService(indexed_shop)  # a new process would reopen the persisted collection
    assert again.index(shop.root).embedded == 0
    rid = again.repository_id(shop.root)
    result = again.retriever().search(rid, "heavy parcels shipping fee", k=3)
    assert not result.degraded and result.hits[0].doc.class_name == "shop.ShippingCalculator"
    assert any(h.vector_rank for h in result.hits)


def test_collection_is_named_per_embedding_model() -> None:
    assert collection_name(HashingEmbedder(256)) == "code_helper_hash-256"
    from app.retrieval.embeddings import OllamaEmbedder

    name = collection_name(OllamaEmbedder("http://x", "nomic-embed-text:latest"))
    assert name == "code_helper_ollama_nomic-embed-text_latest" and 3 <= len(name) <= 63


def test_service_requires_a_knowledge_model(isolated_settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(LookupError, match="run `index` first"):
        RetrievalService(isolated_settings, use_vectors=False).index(tmp_path)
    with pytest.raises(ValueError, match="SOURCE_ROOT"):
        RetrievalService(isolated_settings, use_vectors=False).repository_id(None)


def test_unusable_chroma_path_degrades_to_text_search(
    indexed_shop: Settings, shop: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    monkeypatch.setenv("CHROMA_PATH", str(blocker / "chroma"))
    from app.config import get_settings

    get_settings.cache_clear()
    service = RetrievalService(get_settings())
    assert service.vectors is None and "cannot open Chroma" in (service.vector_error or "")
    report = service.index(shop.root)
    assert report.documents == 15 and report.vector_error is not None
    result = service.retriever().search(service.repository_id(shop.root), "loyalty discount")
    assert result.hits and result.degraded


def test_cli_embed_and_search(
    indexed_shop: Settings, shop: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_main(["embed", str(shop.root)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["documents"] == 15 and report["embedded"] == 15

    assert cli_main(["search", "loyalty discount", "--path", str(shop.root), "-k", "2"]) == 0
    found = json.loads(capsys.readouterr().out)
    assert not found["degraded"] and len(found["hits"]) == 2
    assert found["hits"][0]["method"] == "loyaltyDiscount"

    assert cli_main(["search", "x", "--path", str(shop.root), "--type", "test", "--bm25-only"]) == 0
    only_tests = json.loads(capsys.readouterr().out)
    assert {h["type"] for h in only_tests["hits"]} <= {"test"}
    assert only_tests["degraded"] is True  # --bm25-only means no vector leg


def test_cli_embed_needs_an_index(
    isolated_settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_main(["embed", str(tmp_path), "--bm25-only"]) == 4
    assert "run `index` first" in capsys.readouterr().err
