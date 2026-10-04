"""Phase 10: persistence round trips and queries."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.analyzer import JavaParserAnalyzer
from app.knowledge.builder import BuiltKnowledge
from app.knowledge.store import KNOWLEDGE_VERSION, KnowledgeStore
from tests.conftest import PROJECT
from tests.helpers import analyze_paths, analyze_sources, build_knowledge

PS = "com.acme.service.PricingService"


@pytest.fixture(scope="module")
def built(java_analyzer: JavaParserAnalyzer) -> BuiltKnowledge:
    return build_knowledge(analyze_paths(java_analyzer, PROJECT))


@pytest.fixture
def store(tmp_path: Path, built: BuiltKnowledge) -> KnowledgeStore:
    s = KnowledgeStore(tmp_path / "k.sqlite3")
    s.replace(built.repository, built.graph, "fp-1")
    return s


def repo(built: BuiltKnowledge) -> str:
    return built.repository.repository_id


def test_documents_round_trip_exactly(store: KnowledgeStore, built: BuiltKnowledge) -> None:
    for m in built.repository.methods:
        assert store.get_method(m.id) == m
    for c in built.repository.classes:
        assert store.get_class(c.id) == c
    assert store.get_method("m_missing") is None and store.get_class("c_missing") is None


def test_run_is_recorded_with_fingerprint_and_stats(
    store: KnowledgeStore, built: BuiltKnowledge
) -> None:
    run = store.last_successful_run(repo(built))
    assert run is not None
    assert (run.fingerprint, run.knowledge_version, run.status) == (
        "fp-1",
        KNOWLEDGE_VERSION,
        "succeeded",
    )
    s = built.repository.stats
    assert (run.classes, run.methods, run.call_edges, run.rule_candidates) == (
        s.classes,
        s.methods,
        s.call_edges,
        s.rule_candidates,
    )
    assert run.finished_at is not None
    assert store.last_successful_run("other-repo") is None


def test_listing_and_searching(store: KnowledgeStore, built: BuiltKnowledge) -> None:
    r = repo(built)
    classes = store.list_classes(r)
    assert len(classes) == store.count_classes(r) == 13
    assert [c.fqn for c in classes] == sorted(c.fqn for c in classes)
    everything = [c.fqn for c in store.list_classes(r, query="Pricing")]
    assert everything == ["com.acme.service.BasePricing", PS, f"{PS}.Helper"]
    assert [c.fqn for c in store.list_classes(r, query="Pricing", limit=1, offset=1)] == [PS]

    pricing = next(c for c in classes if c.fqn == PS)
    methods = store.list_methods(pricing.id)
    assert pricing.method_count == len(methods)
    assert [m.start_line for m in methods] == sorted(m.start_line for m in methods)
    price = [m for m in methods if m.name == "price"]
    assert len(price) == 3 and all(m.purpose_basis == "name" for m in price)

    hits = store.search_methods(r, "price")
    assert hits and hits[0].name == "price"  # exact name matches rank first
    assert not [h for h in hits if h.is_test]
    assert [h.method_id for h in store.search_methods(r, "Customer#rename")] != []
    assert store.search_methods(r, "zzzz-nothing") == []


def test_find_by_method_id_and_fqn(store: KnowledgeStore, built: BuiltKnowledge) -> None:
    r = repo(built)
    (m,) = store.find_method(r, f"{PS}#price(int)")
    assert m.name == "price" and m.signature == "price(int)"
    assert store.find_method(r, "nope#x()") == []
    things = store.find_class(r, "com.acme.dup.Thing")
    assert len(things) == 2  # duplicates are both kept, distinguishable by file
    assert [c.file for c in things] == sorted(c.file for c in things)


def test_stored_call_graph_matches_the_in_memory_one(
    store: KnowledgeStore, built: BuiltKnowledge
) -> None:
    r, g = repo(built), built.graph
    for caller in (f"{PS}#overloads(Customer)", f"{PS}#price(Customer)", f"{PS}#inherited()"):
        stored = [
            (c.callee_id, c.name, c.site_line)
            for c in store.calls_from(r, caller)
            if c.callee_id is not None
        ]
        memory = [(t, e.name, e.site_line) for e in g.calls(caller) for t in e.targets]
        assert stored == memory
    callee = "com.acme.service.BasePricing#base(int)"
    assert [(c.caller_id, c.site_line) for c in store.calls_to(r, callee)] == [
        (e.caller_id, e.site_line) for e in g.called_by(callee)
    ]
    ambiguous = [c for c in store.calls_to(r, callee) if c.ambiguous]
    assert [c.caller_id for c in ambiguous] == [f"{PS}#scoping(Customer)"]


def test_traversal_over_the_stored_graph(store: KnowledgeStore, built: BuiltKnowledge) -> None:
    r, g = repo(built), built.graph
    start = f"{PS}#overloads(Customer)"
    for depth in (1, 2, 3):
        stored = [(x.method_id, x.depth) for x in store.reach(r, start, depth)]
        memory = [(x.method_id, x.depth) for x in g.callees(start, depth)]
        assert stored == memory
    callers = store.reach(r, "com.acme.util.Strings#len(String)", 2, direction="callers")
    assert [(x.method_id, x.depth) for x in callers] == [
        (x.method_id, x.depth) for x in g.callers("com.acme.util.Strings#len(String)", 2)
    ]
    strict = store.reach(r, f"{PS}#scoping(Customer)", 1, include_candidates=False)
    assert "com.acme.service.BasePricing#base(int)" not in [x.method_id for x in strict]
    loose = store.reach(r, f"{PS}#scoping(Customer)", 1)
    assert any(x.ambiguous and x.method_id.endswith("#base(int)") for x in loose)


def test_overrides_are_persisted_and_followed_on_request(
    java_analyzer: JavaParserAnalyzer, tmp_path: Path
) -> None:
    sources = {
        "p/Shape.java": "package p; public interface Shape { double area(); }",
        "p/Sq.java": "package p; public class Sq implements Shape { double area() { return 1; } }",
        "p/Ci.java": "package p; public class Ci implements Shape { double area() { return 2; } }",
        "p/Use.java": "package p; public class Use { double go(Shape s) { return s.area(); } }",
    }
    b = build_knowledge(analyze_sources(java_analyzer, tmp_path / "src", sources))
    s = KnowledgeStore(tmp_path / "ov.sqlite3")
    s.replace(b.repository, b.graph, "fp")
    r = b.repository.repository_id
    assert s.overrides_of(r, "p.Shape#area()") == ["p.Ci#area()", "p.Sq#area()"]
    assert s.overrides_of(r, "nothing#x()") == []

    assert [x.method_id for x in s.reach(r, "p.Use#go(Shape)", 1)] == ["p.Shape#area()"]
    dynamic = {
        x.method_id: x.via_override
        for x in s.reach(r, "p.Use#go(Shape)", 1, include_overrides=True)
    }
    assert dynamic == {"p.Shape#area()": False, "p.Ci#area()": True, "p.Sq#area()": True}
    memory = {
        x.method_id: x.via_override
        for x in b.graph.callees("p.Use#go(Shape)", 1, include_overrides=True)
    }
    assert dynamic == memory


def test_relational_tables_agree_with_the_documents(tmp_path: Path, built: BuiltKnowledge) -> None:
    path = tmp_path / "rel.sqlite3"
    s = KnowledgeStore(path)
    s.replace(built.repository, built.graph, "fp")
    m = next(x for x in built.repository.methods if x.method_id == f"{PS}#overloads(Customer)")
    with sqlite3.connect(path) as conn:

        def n(sql: str, *args: object) -> int:
            return int(conn.execute(sql, args).fetchone()[0])

        assert n("SELECT COUNT(*) FROM parameters WHERE method_pk = ?", m.id) == len(m.parameters)
        assert n("SELECT COUNT(*) FROM variables WHERE method_pk = ?", m.id) == len(
            m.data_flow.variables
        )
        assert n("SELECT COUNT(*) FROM data_flow_edges WHERE method_pk = ?", m.id) == len(
            m.data_flow.edges
        )
        assert n("SELECT COUNT(*) FROM rule_candidates WHERE method_pk = ?", m.id) == len(
            m.rule_candidates
        )
        assert n("SELECT COUNT(*) FROM evidence WHERE method_pk = ?", m.id) == len(m.evidence)
        nodes = sum(1 for _ in m.control_flow.walk())
        assert n("SELECT COUNT(*) FROM control_flow_nodes WHERE method_pk = ?", m.id) == nodes
        roots = n(
            "SELECT COUNT(*) FROM control_flow_nodes WHERE method_pk = ? AND parent_ord IS NULL",
            m.id,
        )
        assert roots == len(m.control_flow.nodes)
        # the compatibility view lists callers per callee
        assert n(
            "SELECT COUNT(*) FROM method_callers WHERE method_id = ?", f"{PS}#price(int)"
        ) == len(s.calls_to(built.repository.repository_id, f"{PS}#price(int)"))


def test_rules_and_evidence_queries(store: KnowledgeStore, built: BuiltKnowledge) -> None:
    m = next(x for x in built.repository.methods if x.rule_candidates)
    assert store.rules(m.id) == sorted(m.rule_candidates, key=lambda r: (r.start_line, r.kind))
    assert store.evidence_for_method(m.id) == sorted(m.evidence, key=lambda e: (e.start_line, e.id))
    e = m.evidence[0]
    assert store.evidence_by_id(e.id) == e and store.evidence_by_id("nope") is None


def test_argument_sources_across_callers(store: KnowledgeStore) -> None:
    rows = store.argument_sources("com.acme.service.BasePricing#base(int)", "x")
    assert [(r["caller_id"].split("#")[1], r["source_kind"], r["source_name"]) for r in rows] == [
        ("inherited()", "literal", "int"),
        ("inherited()", "literal", "int"),
        ("price(int)", "parameter", "units"),
    ]


def test_replace_swaps_the_whole_model_atomically(
    java_analyzer: JavaParserAnalyzer, tmp_path: Path, built: BuiltKnowledge
) -> None:
    s = KnowledgeStore(tmp_path / "swap.sqlite3")
    s.replace(built.repository, built.graph, "fp-1")
    r = repo(built)
    before = s.counts(r)

    small = build_knowledge(
        analyze_sources(
            java_analyzer,
            tmp_path / "src",
            {"p/A.java": "package p; class A { void f() { g(); } void g() {} }"},
        )
    )
    small_repo = small.repository.model_copy(update={"repository_id": r})
    run = s.replace(small_repo, small.graph, "fp-2")
    after = s.counts(r)
    assert run.fingerprint == "fp-2" and after["methods"] == small.repository.stats.methods == 2
    assert before["methods"] > after["methods"] and after["classes"] == 1
    assert (
        s.find_class(r, PS) == [] and s.find_method(r, f"{PS}#price(int)") == []
    )  # nothing left over
    with sqlite3.connect(tmp_path / "swap.sqlite3") as conn:  # child rows went too
        assert conn.execute("SELECT COUNT(*) FROM parameters").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM evidence WHERE repository_id = ?", (r,)).fetchone()[
                0
            ]
            == after["evidence"]
        )
    assert [x.fingerprint for x in s.runs(r)] == ["fp-2", "fp-1"]


def test_other_repositories_are_untouched_by_replace(tmp_path: Path, built: BuiltKnowledge) -> None:
    s = KnowledgeStore(tmp_path / "multi.sqlite3")
    s.replace(built.repository, built.graph, "fp")
    other = built.repository.model_copy(update={"repository_id": "other"})
    # same ids would collide on the primary keys, so give the copy distinct ones
    renamed = other.model_copy(
        update={
            "classes": [c.model_copy(update={"id": c.id + "x"}) for c in other.classes],
            "methods": [
                m.model_copy(update={"id": m.id + "x", "class_id": m.class_id + "x"})
                for m in other.methods
            ],
        }
    )
    s.replace(renamed, built.graph, "fp-other")
    s.replace(built.repository, built.graph, "fp-again")  # replacing the first leaves the second
    assert s.counts("other")["methods"] == built.repository.stats.methods
    assert s.counts(repo(built))["methods"] == built.repository.stats.methods


def test_failed_replace_rolls_back(tmp_path: Path, built: BuiltKnowledge) -> None:
    s = KnowledgeStore(tmp_path / "rb.sqlite3")
    s.replace(built.repository, built.graph, "good")
    broken = built.repository.model_copy(
        update={"methods": [*built.repository.methods, built.repository.methods[0]]}
    )  # duplicate primary key
    with pytest.raises(sqlite3.IntegrityError):
        s.replace(broken, built.graph, "bad")
    r = repo(built)
    assert s.counts(r)["methods"] == built.repository.stats.methods  # old model intact
    run = s.last_successful_run(r)
    assert run is not None and run.fingerprint == "good"


def test_documents_are_compressed(tmp_path: Path, built: BuiltKnowledge) -> None:
    path = tmp_path / "z.sqlite3"
    KnowledgeStore(path).replace(built.repository, built.graph, "fp")
    with sqlite3.connect(path) as conn:
        stored = conn.execute("SELECT SUM(LENGTH(doc)) FROM methods").fetchone()[0]
    raw = sum(len(m.model_dump_json()) for m in built.repository.methods)
    assert stored < raw / 3
