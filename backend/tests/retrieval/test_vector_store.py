"""The same behavioural contract against the in-memory store and a real Chroma instance."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.retrieval.vector_store import (
    ChromaVectorStore,
    InMemoryVectorStore,
    VectorStore,
    VectorStoreError,
)


@pytest.fixture(params=["memory", "chroma"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> VectorStore:
    if request.param == "memory":
        return InMemoryVectorStore()
    return ChromaVectorStore(tmp_path / "chroma", "test_collection")


def meta(
    repo: str = "r1", kind: str = "method_source", h: str = "h", cls: str = "A"
) -> dict[str, str | int | float | bool]:
    return {"repository_id": repo, "source_type": kind, "hash": h, "class_name": cls}


def fill(store: VectorStore) -> None:
    store.upsert(
        ["x", "y", "z", "w"],
        [[1.0, 0.0], [0.9, 0.1], [0.0, 1.0], [0.7, 0.7]],
        [
            meta(h="hx"),
            meta(kind="test", h="hy"),
            meta(repo="r2", h="hz", cls="B"),
            meta(h="hw", cls="B"),
        ],
    )


def test_nearest_neighbours_by_cosine(store: VectorStore) -> None:
    fill(store)
    hits = store.query([1.0, 0.0], 3)
    assert [d for d, _ in hits] == ["x", "y", "w"]
    assert hits[0][1] == pytest.approx(1.0, abs=1e-4)
    assert hits[0][1] > hits[1][1] > hits[2][1]
    assert store.count() == 4


def test_metadata_filters(store: VectorStore) -> None:
    fill(store)
    assert [d for d, _ in store.query([1.0, 0.0], 10, {"repository_id": "r1"})] == ["x", "y", "w"]
    only_tests = store.query(
        [1.0, 0.0], 10, {"$and": [{"repository_id": "r1"}, {"source_type": {"$in": ["test"]}}]}
    )
    assert [d for d, _ in only_tests] == ["y"]
    by_class = store.query([1.0, 0.0], 10, {"$and": [{"repository_id": "r1"}, {"class_name": "B"}]})
    assert [d for d, _ in by_class] == ["w"]
    assert store.query([1.0, 0.0], 10, {"repository_id": "nobody"}) == []


def test_existing_hashes_upsert_replaces_and_delete(store: VectorStore) -> None:
    fill(store)
    assert store.existing(["x", "w", "missing"]) == {"x": "hx", "w": "hw"}
    store.upsert(["x"], [[0.0, 1.0]], [meta(h="hx2")])
    assert store.existing(["x"]) == {"x": "hx2"} and store.count() == 4
    assert store.query([0.0, 1.0], 1, {"repository_id": "r1"})[0][0] in {"x"}
    store.delete(["x", "y", "never-existed"])
    assert store.count() == 2 and store.existing(["x", "y"]) == {}


def test_empty_store_and_noop_calls(store: VectorStore) -> None:
    assert store.count() == 0 and store.query([1.0, 0.0], 5) == []
    store.upsert([], [], [])
    store.delete([])
    assert store.existing([]) == {}


def test_chroma_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "persist"
    first = ChromaVectorStore(path, "coll")
    first.upsert(["a"], [[1.0, 0.0]], [meta(h="ha")])
    del first
    again = ChromaVectorStore(path, "coll")
    assert again.count() == 1 and again.existing(["a"]) == {"a": "ha"}
    assert again.query([1.0, 0.0], 1)[0][0] == "a"


def test_chroma_collections_are_independent(tmp_path: Path) -> None:
    a = ChromaVectorStore(tmp_path / "multi", "model_a")
    b = ChromaVectorStore(tmp_path / "multi", "model_b")
    a.upsert(["x"], [[1.0, 0.0]], [meta()])
    assert a.count() == 1 and b.count() == 0  # vectors from different models never mix


def test_chroma_failure_is_wrapped(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    with pytest.raises(VectorStoreError, match="cannot open Chroma"):
        ChromaVectorStore(blocker / "sub", "coll")
