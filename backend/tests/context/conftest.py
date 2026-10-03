from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from app.knowledge.models import MethodKnowledge
from app.knowledge.source import SourceReader
from app.knowledge.store import KnowledgeStore
from app.retrieval.embeddings import HashingEmbedder
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.indexer import RetrievalIndexer
from app.retrieval.store import RetrievalStore
from app.retrieval.vector_store import InMemoryVectorStore
from tests.shop_repo import World


@dataclass
class Env:
    world: World
    store: KnowledgeStore
    reader: SourceReader
    retriever: HybridRetriever

    @property
    def repo(self) -> str:
        return self.world.repository_id

    def method(self, name: str) -> MethodKnowledge:
        found = [m for m in self.store.iter_methods(self.repo) if m.name == name and not m.is_test]
        assert len(found) == 1, f"{name}: {len(found)}"
        return found[0]


@pytest.fixture
def env(shop: World, tmp_path: Path) -> Env:
    db = tmp_path / "ctx.sqlite3"
    store = KnowledgeStore(db)
    store.replace(shop.built.repository, shop.built.graph, "fp")
    retrieval = RetrievalStore(db)
    vectors, embedder = InMemoryVectorStore(), HashingEmbedder()
    k = shop.built.repository
    reader = SourceReader(shop.root)
    RetrievalIndexer(retrieval, vectors, embedder).index(
        shop.repository_id, k.classes, k.methods, reader
    )
    return Env(shop, store, reader, HybridRetriever(retrieval, vectors, embedder))


@pytest.fixture(scope="session")
def project_built(java_analyzer):  # type: ignore[no-untyped-def]
    from tests.conftest import PROJECT
    from tests.helpers import analyze_paths, build_knowledge

    return build_knowledge(analyze_paths(java_analyzer, PROJECT))


@pytest.fixture
def project_env(project_built, tmp_path: Path) -> Env:  # type: ignore[no-untyped-def]
    from tests.conftest import PROJECT

    store = KnowledgeStore(tmp_path / "proj.sqlite3")
    store.replace(project_built.repository, project_built.graph, "fp")
    retrieval = RetrievalStore(tmp_path / "proj.sqlite3")
    world = World(PROJECT, project_built)
    return Env(world, store, SourceReader(PROJECT), HybridRetriever(retrieval))
