from __future__ import annotations

from pathlib import Path

import pytest

from app.analyzer import JavaParserAnalyzer
from app.analyzer.call_graph import CallGraph, build_call_graph
from app.analyzer.resolution_models import Origin, ResolutionStatus
from tests.conftest import PROJECT
from tests.helpers import analyze_paths, analyze_sources

R = ResolutionStatus

SOURCES = {
    "p/Calc.java": """package p;
public class Calc {
    public int total(int a) { return twice(inc(a)); }
    int twice(int x) { return x * 2; }
    int inc(int x) { return x + 1; }
    static int util(int x) { return x; }
    int viaStatic(int x) { return Calc.util(x) + util(x); }
    Calc make() { return new Calc(); }
    int fact(int n) { return n <= 1 ? 1 : n * fact(n - 1); }
    int ping(int n) { return pong(n); }
    int pong(int n) { return ping(n); }
    int show(String s) { return 1; }
    int show(int i) { return 2; }
    int pick() { return show("x") + show(3); }
    int broken() { return missing(1) + String.valueOf(1).length(); }
    int ambiguousArg(Object o) { return show(o.hashCode() > 0 ? missing(2) : 0); }
}
""",
    "p/Shape.java": """package p;
public interface Shape { double area(); default String label() { return "shape"; } }
""",
    "p/Square.java": """package p;
public class Square implements Shape {
    public double area() { return 4; }
}
""",
    "p/Circle.java": """package p;
public class Circle implements Shape {
    public double area() { return 3; }
    public String label() { return "circle"; }
}
""",
    "p/Painter.java": """package p;
public class Painter {
    double paint(Shape s) { return s.area() + helper().length(); }
    String helper() { return new Circle().label(); }
}
""",
}


@pytest.fixture(scope="module")
def graph(java_analyzer: JavaParserAnalyzer, tmp_path_factory: pytest.TempPathFactory) -> CallGraph:
    a = analyze_sources(java_analyzer, tmp_path_factory.mktemp("cg"), SOURCES)
    return build_call_graph(a.table, a.resolver, a.resolutions)


def ids(reaches: list) -> list[str]:  # type: ignore[type-arg]
    return [r.method_id for r in reaches]


def test_nodes_cover_every_method_and_implicit_constructors(graph: CallGraph) -> None:
    assert "p.Calc#total(int)" in graph.nodes
    assert graph.nodes["p.Calc#total(int)"].start_line == 3
    ctor = graph.nodes["p.Calc#Calc()"]
    assert ctor.kind == "constructor" and ctor.implicit  # default constructor


def test_direct_calls_in_source_order(graph: CallGraph) -> None:
    edges = graph.calls("p.Calc#total(int)")  # `twice(inc(a))`: outer call first
    assert [(e.name, e.callee_id) for e in edges] == [
        ("twice", "p.Calc#twice(int)"),
        ("inc", "p.Calc#inc(int)"),
    ]
    assert all(e.status is R.RESOLVED and e.origin is Origin.PROJECT for e in edges)
    assert {e.site_line for e in edges} == {3}


def test_callers_are_the_reverse_direction(graph: CallGraph) -> None:
    callers = graph.called_by("p.Calc#twice(int)")
    assert [(e.caller_id, e.site_line) for e in callers] == [("p.Calc#total(int)", 3)]
    assert ids(graph.callers("p.Calc#inc(int)")) == ["p.Calc#total(int)"]


def test_depth_controls_how_far_traversal_goes(graph: CallGraph) -> None:
    first = graph.callees("p.Painter#paint(Shape)", depth=1)
    assert ids(first) == ["p.Painter#helper()", "p.Shape#area()"]
    deeper = {r.method_id: r.depth for r in graph.callees("p.Painter#paint(Shape)", depth=3)}
    assert deeper["p.Painter#helper()"] == 1
    assert deeper["p.Circle#Circle()"] == 2 and deeper["p.Circle#label()"] == 2
    assert graph.callees("p.Painter#paint(Shape)", depth=0) == []


def test_nested_call_chain_and_callers_at_depth(graph: CallGraph) -> None:
    reach = {r.method_id: r.depth for r in graph.callers("p.Circle#Circle()", depth=2)}
    assert reach == {"p.Painter#helper()": 1, "p.Painter#paint(Shape)": 2}


def test_static_methods_and_constructors(graph: CallGraph) -> None:
    calls = graph.calls("p.Calc#viaStatic(int)")
    assert [e.callee_id for e in calls] == ["p.Calc#util(int)"] * 2  # Calc.util(x) and util(x)
    make = graph.calls("p.Calc#make()")
    assert [(e.kind, e.callee_id, e.implicit) for e in make] == [
        ("constructor", "p.Calc#Calc()", True)
    ]


def test_overloads_resolve_to_the_matching_signature(graph: CallGraph) -> None:
    targets = sorted(e.callee_id or "" for e in graph.calls("p.Calc#pick()"))
    assert targets == ["p.Calc#show(String)", "p.Calc#show(int)"]


def test_unresolved_and_external_calls_are_kept_not_dropped(graph: CallGraph) -> None:
    unresolved = graph.unresolved_calls("p.Calc#broken()")
    assert [(e.name, e.reason) for e in unresolved] == [
        ("missing", "no_such_method"),
        ("length", "receiver_type_unknown"),  # String.valueOf's return type is not known
    ]
    assert all(e.callee_id is None and e.targets == [] for e in unresolved)
    external = [(e.name, e.owner_type) for e in graph.external_calls("p.Calc#broken()")]
    assert external == [("valueOf", "java.lang.String")]
    assert graph.called_by("p.Calc#missing(int)") == []  # unresolved targets create no edges in


def test_ambiguous_calls_keep_every_candidate(graph: CallGraph) -> None:
    method = "p.Calc#ambiguousArg(Object)"
    (edge,) = [e for e in graph.calls(method) if e.name == "show"]
    assert edge.status is R.AMBIGUOUS and edge.callee_id is None
    assert sorted(edge.candidates) == ["p.Calc#show(String)", "p.Calc#show(int)"]

    shows = [r for r in graph.callees(method) if r.method_id.startswith("p.Calc#show")]
    assert sorted(r.method_id for r in shows) == ["p.Calc#show(String)", "p.Calc#show(int)"]
    assert all(r.ambiguous for r in shows)  # reachable, but only as one of several candidates
    strict = graph.callees(method, include_candidates=False)
    assert not [r for r in strict if r.method_id.startswith("p.Calc#show")]
    # show(int) is called for sure from pick() and possibly from ambiguousArg()
    assert ids(graph.callers("p.Calc#show(int)")) == [method, "p.Calc#pick()"]
    assert ids(graph.callers("p.Calc#show(int)", include_candidates=False)) == ["p.Calc#pick()"]


def test_recursion_and_mutual_recursion_terminate(graph: CallGraph) -> None:
    assert graph.is_recursive("p.Calc#fact(int)")
    assert graph.is_recursive("p.Calc#ping(int)") and graph.is_recursive("p.Calc#pong(int)")
    assert not graph.is_recursive("p.Calc#total(int)")
    assert ids(graph.callees("p.Calc#fact(int)", depth=10)) == []  # only itself: not re-reported
    assert ids(graph.callees("p.Calc#ping(int)", depth=10)) == ["p.Calc#pong(int)"]
    assert ids(graph.callers("p.Calc#pong(int)", depth=10)) == ["p.Calc#ping(int)"]


def test_overrides_expose_dynamic_dispatch(graph: CallGraph) -> None:
    assert sorted(graph.overrides["p.Shape#area()"]) == ["p.Circle#area()", "p.Square#area()"]
    assert graph.overrides["p.Shape#label()"] == ["p.Circle#label()"]  # default overridden once
    assert graph.overridden["p.Circle#area()"] == ["p.Shape#area()"]

    static_only = ids(graph.callees("p.Painter#paint(Shape)", depth=1))
    assert "p.Circle#area()" not in static_only  # resolved call stays on the static type
    dynamic = {
        r.method_id: r
        for r in graph.callees("p.Painter#paint(Shape)", depth=1, include_overrides=True)
    }
    assert {"p.Shape#area()", "p.Circle#area()", "p.Square#area()"} <= set(dynamic)
    assert dynamic["p.Circle#area()"].via_override and not dynamic["p.Shape#area()"].via_override


def test_project_fixture_graph_matches_resolution(java_analyzer: JavaParserAnalyzer) -> None:
    a = analyze_paths(java_analyzer, PROJECT)
    g = build_call_graph(a.table, a.resolver, a.resolutions)
    pricing = "com.acme.service.PricingService"
    price_int = f"{pricing}#price(int)"
    assert "com.acme.service.BasePricing#base(int)" in {e.callee_id for e in g.calls(price_int)}
    callers = {e.caller_id for e in g.called_by(price_int)}
    assert f"{pricing}#overloads(Customer)" in callers and f"{pricing}#inherited()" in callers
    audits = g.overrides["com.acme.model.Auditable#audit()"]
    assert audits == ["com.acme.service.BasePricing#audit()"]


def test_every_edge_has_a_known_caller(graph: CallGraph, tmp_path: Path) -> None:
    for caller, edges in graph.edges_from.items():
        assert caller in graph.nodes
        assert all(e.caller_id == caller for e in edges)
