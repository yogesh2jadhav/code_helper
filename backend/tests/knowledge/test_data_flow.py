"""Phase 6: data-flow edges and variable lifecycles."""

from __future__ import annotations

import pytest

from app.analyzer import JavaParserAnalyzer
from app.analyzer.data_flow import DataFlowEdge, MethodDataFlow, build_data_flow
from tests.conftest import FIXTURES, PROJECT
from tests.helpers import Analysis, ByMethod, analyze_paths, analyze_sources


def _build(method, method_id, refs, params):  # type: ignore[no-untyped-def]
    return build_data_flow(method, method_id, refs, params)


class Flows(ByMethod[MethodDataFlow]):
    def __init__(self, analysis: Analysis) -> None:
        super().__init__(analysis, _build)


def shape(flow: MethodDataFlow) -> list[tuple[str, str, str]]:
    """(source, via, target) with `kind:name` labels, in source order."""
    return [
        (f"{e.source.kind}:{e.source.name}", e.via, f"{e.target.kind}:{e.target.name}")
        for e in flow.edges
    ]


SRC = """package p;

import java.util.List;
import java.util.stream.Collectors;

public class DF {
    static class Dto {
        String name;
        void setName(String n) {}
        String getName() { return name; }
    }

    private int total;

    Dto build(String raw, int n) {
        String trimmed = raw.trim();
        Dto d = new Dto();
        d.setName(trimmed);
        int k = n + helper(trimmed, 5);
        total = total + k;
        total += 1;
        k++;
        int[] arr = new int[3];
        arr[0] = k;
        return d;
    }

    int helper(String s, int times) {
        return times;
    }

    String chain(List<String> items) {
        List<String> kept = items.stream().filter(x -> !x.isEmpty()).collect(Collectors.toList());
        for (String it : kept) {
            total += it.length();
        }
        return String.join(",", kept);
    }

    int unused(int a) {
        return 1;
    }

    void nothing() {}
}
"""


@pytest.fixture(scope="module")
def custom(java_analyzer: JavaParserAnalyzer, tmp_path_factory: pytest.TempPathFactory) -> Flows:
    a = analyze_sources(java_analyzer, tmp_path_factory.mktemp("df"), {"p/DF.java": SRC})
    return Flows(a)


@pytest.fixture(scope="module")
def fixtures(java_analyzer: JavaParserAnalyzer) -> Flows:
    return Flows(analyze_paths(java_analyzer, FIXTURES))


@pytest.fixture(scope="module")
def project(java_analyzer: JavaParserAnalyzer) -> Flows:
    return Flows(analyze_paths(java_analyzer, PROJECT))


# ---- edges ------------------------------------------------------------------------------------


def test_edges_for_declarations_assignments_arguments_and_returns(custom: Flows) -> None:
    assert shape(custom("DF#build(String,int)")) == [
        ("call_result:trim", "declare", "local:trimmed"),
        ("parameter:raw", "receiver", "call_result:trim"),
        ("new:Dto", "declare", "local:d"),
        ("local:trimmed", "argument", "callee_parameter:n"),
        ("local:trimmed", "mutation", "local:d"),
        ("parameter:n", "declare", "local:k"),
        ("call_result:helper", "declare", "local:k"),
        ("local:trimmed", "argument", "callee_parameter:s"),
        ("literal:int", "argument", "callee_parameter:times"),
        ("field:total", "assign", "field:total"),
        ("local:k", "assign", "field:total"),
        ("literal:int", "compound_assign", "field:total"),
        ("field:total", "compound_assign", "field:total"),
        ("local:k", "compound_assign", "local:k"),
        ("local:k", "assign", "local:arr"),
        ("local:d", "return", "return:return"),
    ]


def test_arguments_flow_into_the_callees_named_parameters(custom: Flows) -> None:
    flow = custom("DF#build(String,int)")
    helper = [
        e for e in flow.edges if e.via == "argument" and e.callee_id == "p.DF#helper(String,int)"
    ]
    assert [(e.source.name, e.param, e.target.symbol_id) for e in helper] == [
        ("trimmed", "s", "p.DF#helper(String,int)$s"),
        ("int", "times", "p.DF#helper(String,int)$times"),
    ]
    setter = next(e for e in flow.edges if e.callee_id == "p.DF.Dto#setName(String)")
    assert (setter.source.name, setter.param) == ("trimmed", "n")


def test_derivation_through_a_call_result(custom: Flows) -> None:
    flow = custom("DF#build(String,int)")
    # raw -> trim() -> trimmed : the receiver edge and the declare edge chain together
    assert [e for e in flow.edges_to("trimmed")][0].source.name == "trim"
    assert [e.target.name for e in flow.edges_from("raw")] == ["trim"]


def test_method_with_no_data_movement_is_small_and_valid(custom: Flows) -> None:
    flow = custom("DF#nothing()")
    assert flow.variables == [] and flow.edges == []
    unused = custom("DF#unused(int)")
    assert [v.name for v in unused.variables] == ["a"]  # the unused parameter is still listed
    assert shape(unused) == [("literal:int", "return", "return:return")]  # `return 1;`


# ---- lifecycles -------------------------------------------------------------------------------


def test_variable_lifecycles(custom: Flows) -> None:
    flow = custom("DF#build(String,int)")
    trimmed = flow.variable("trimmed")
    assert trimmed is not None and trimmed.created == "initialized from call trim()"
    assert trimmed.lifecycle()["passed"] == ["L18 to setName(n)", "L19 to helper(s)"]

    d = flow.variable("d")
    assert d is not None and d.lifecycle() == {
        "created": ["L17 initialized from new Dto"],
        "modified": ["L18 setName(trimmed)"],
        "returned": ["L25 returned"],
    }
    k = flow.variable("k")
    assert k is not None and k.created == "initialized from parameter n, call helper()"
    assert [e.kind for e in k.events if e.kind != "read"] == [
        "created",
        "derived",
        "modified",
        "derived",
    ]
    assert k.of_kind("modified")[0].detail == "++"


def test_fields_are_tracked_with_their_declaration_line(custom: Flows) -> None:
    total = custom("DF#build(String,int)").variable("total")
    assert total is not None and total.kind == "field"
    assert total.declared_line == 13 and total.symbol_id == "p.DF#total"
    assert [e.detail for e in total.of_kind("modified")] == ["write: total + k", "update (+=): 1"]


def test_parameters_exist_even_when_unused(custom: Flows) -> None:
    a = custom("DF#unused(int)").variable("a")
    assert a is not None and a.kind == "parameter" and a.created == "parameter"
    assert a.symbol_id == "p.DF#unused(int)$a"


def test_reads_are_events_but_not_in_the_lifecycle_summary(custom: Flows) -> None:
    raw = custom("DF#build(String,int)").variable("raw")
    assert raw is not None
    assert [e.kind for e in raw.events] == ["created", "read"]
    assert list(raw.lifecycle()) == ["created"]


# ---- streams, loops, lambdas ------------------------------------------------------------------


def test_stream_pipeline_edge_and_events(custom: Flows) -> None:
    flow = custom("DF#chain(List<String>)")
    (stream,) = [e for e in flow.edges if e.via == "stream"]
    assert (stream.source.name, stream.target.name) == ("items", "collect")
    assert stream.ops == ["stream", "filter", "collect", "toList"]
    items = flow.variable("items")
    assert items is not None
    assert [(e.kind, e.detail) for e in items.events if e.kind in {"filtered", "collected"}] == [
        ("filtered", "filter: x -> !x.isEmpty()"),
        ("collected", "collect (toList): Collectors.toList()"),
    ]
    kept = flow.variable("kept")
    assert kept is not None and kept.created == "initialized from call collect()"


def test_foreach_and_lambda_parameters_trace_back_to_their_source(custom: Flows) -> None:
    flow = custom("DF#chain(List<String>)")
    it = flow.variable("it")
    assert it is not None and it.created == "element of kept"
    x = flow.variable("x")
    assert x is not None and x.created == "lambda parameter over items"
    assert ("parameter:items", "lambda_param", "local:x") in shape(flow)
    assert ("local:kept", "iterate", "local:it") in shape(flow)
    assert flow.variable("kept").lifecycle()["iterated"] == ["L34 into it"]  # type: ignore[union-attr]


def test_stream_internals_do_not_create_noise_edges(custom: Flows) -> None:
    edges = shape(custom("DF#chain(List<String>)"))
    assert not [e for e in edges if "collect#" in e[2] or "filter#" in e[2]]
    assert not [e for e in edges if e[0] == "call_result:toList"]  # Collectors.* is part of the op


# ---- real fixtures ----------------------------------------------------------------------------


def test_literals_flow_into_returns(fixtures: Flows) -> None:
    flow = fixtures("ShippingPolicy#classify(Parcel,boolean)")
    returns = [e for e in flow.edges if e.via == "return"]
    assert [(e.source.kind, e.source.name) for e in returns] == [
        ("literal", "String"),
        ("literal", "String"),
        ("literal", "String"),
        ("literal", "String"),
        ("local", "label"),
    ]
    label = flow.variable("label")
    assert label is not None and label.of_kind("returned")


def test_switch_assignments_from_constants(fixtures: Flows) -> None:
    flow = fixtures("SwitchSamples#legacyRate(String)")
    assert shape(flow) == [
        ("literal:int", "assign", "local:rate"),
        ("literal:int", "assign", "local:rate"),
        ("literal:int", "assign", "local:rate"),
        ("local:rate", "return", "return:return"),
    ]
    rate = flow.variable("rate")
    assert rate is not None and len(rate.of_kind("modified")) == 3


def test_zero_comment_method_has_the_expected_story(fixtures: Flows) -> None:
    flow = fixtures("InvoiceBatch#process(List<Row>,int,boolean)")
    kept = flow.variable("kept")
    assert kept is not None
    life = kept.lifecycle()
    assert life["created"] == ["L10 initialized from new ArrayList"]
    assert len(life["modified"]) == 2
    assert life["grouped"][0].startswith("L24 collect (groupingBy, summingDouble)")
    stream = next(e for e in flow.edges if e.via == "stream" and e.source.name == "kept")
    assert stream.target.name == "collect" and "groupingBy" in stream.ops
    # the result of that pipeline is what ends up in byAccount, which is then streamed again
    assert ("call_result:collect", "declare", "local:byAccount") in shape(flow)
    assert ("local:byAccount", "receiver", "call_result:entrySet") in shape(flow)
    assert ("call_result:collect", "return", "return:return") in shape(flow)


def test_arguments_to_project_methods_carry_callee_and_parameter(project: Flows) -> None:
    pricing = "com.acme.service.PricingService"
    flow = project("PricingService#price(Customer)")
    (arg,) = [
        e for e in flow.edges if e.via == "argument" and e.callee_id == f"{pricing}#price(String)"
    ]
    assert (arg.source.name, arg.param) == ("getName", "code")
    inputs = [e for e in project("PricingService#overloads(Customer)").edges if e.via == "argument"]
    to_price = {
        (e.source.name, e.callee_id, e.param)
        for e in inputs
        if e.callee_id and "price" in e.callee_id
    }
    assert ("owner", f"{pricing}#price(Customer)", "customer") in to_price


def test_flow_is_json_round_trippable_and_edges_are_typed(fixtures: Flows) -> None:
    flow = fixtures("InvoiceBatch#process(List<Row>,int,boolean)")
    again = MethodDataFlow.model_validate_json(flow.model_dump_json())
    assert shape(again) == shape(flow)
    assert all(isinstance(e, DataFlowEdge) for e in again.edges)
    assert [v.name for v in again.variables] == [v.name for v in flow.variables]
