"""Phase 5: control-flow trees, compared against golden outlines."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.analyzer import JavaParserAnalyzer
from app.analyzer.control_flow import ControlFlow, FlowNode, build_control_flow, render_outline
from tests.conftest import FIXTURES
from tests.helpers import Analysis, ByMethod, analyze_paths, analyze_sources

PREFIX = "com.example.fixtures"


def _build(method, method_id, refs, _params):  # type: ignore[no-untyped-def]
    return build_control_flow(method, method_id, refs)


class Flows(ByMethod[ControlFlow]):
    def __init__(self, analysis: Analysis) -> None:
        super().__init__(analysis, _build)


@pytest.fixture(scope="module")
def flows(java_analyzer: JavaParserAnalyzer) -> Flows:
    return Flows(analyze_paths(java_analyzer, FIXTURES))


def outline(flows: Flows, suffix: str, **kw: bool) -> str:
    return render_outline(flows(suffix), **kw)


def test_nested_if_else_chain(flows: Flows) -> None:
    assert outline(flows, "ShippingPolicy#classify(Parcel,boolean)") == "\n".join(
        [
            "if parcel == null",
            '  return "INVALID"',
            "else if parcel.weightKg() > 30 || parcel.lengthCm() > 150  (calls weightKg, lengthCm)",
            "  if express && parcel.weightKg() <= 50  (calls weightKg)",
            '    return "FREIGHT_EXPRESS"',
            "  else",
            '    return "FREIGHT"',
            "else if parcel.weightKg() >= 10  (calls weightKg)",
            '  return "HEAVY"',
            "else",
            "  return label",
        ]
    )
    summary = flows("ShippingPolicy#classify(Parcel,boolean)").summary
    assert (summary.decisions, summary.returns, summary.max_depth) == (4, 5, 2)


def test_chain_is_structured_as_branches_not_flattened(flows: Flows) -> None:
    (first,) = flows("ShippingPolicy#classify(Parcel,boolean)").nodes
    assert first.kind == "if" and [c.kind for c in first.children] == ["return"]
    (else_if,) = first.branches
    assert else_if.kind == "else_if" and [c.kind for c in else_if.children] == ["if"]
    final = else_if.branches[0].branches[0]  # else if -> else if -> else
    assert final.kind == "else"
    assert [n.start_line for n in first.walk()] == sorted(n.start_line for n in first.walk())


def test_for_loop_with_continue_and_mutation(flows: Flows) -> None:
    assert outline(flows, "LoopSamples#sumPositives(List<Integer>)") == "\n".join(
        [
            "for int i = 0; i < values.size(); i++  (calls size)",
            "  if v <= 0",
            "    continue",
            "  set total += [local_variable]",
            "return total",
        ]
    )


def test_while_do_foreach_break(flows: Flows) -> None:
    assert outline(flows, "LoopSamples#firstNegativeIndex(int[])") == "\n".join(
        [
            "while index < values.length",
            "  if values[index] < 0",
            "    break",
            "  set index ++ [local_variable]",
            "do index > 100",
            "  set index -- [local_variable]",
            "foreach int v : values",
            "  set index += [local_variable]",
            "return index",
        ]
    )
    kinds = [
        (n.kind, n.loop_kind)
        for n in flows("LoopSamples#firstNegativeIndex(int[])").walk()
        if n.kind == "loop"
    ]
    assert kinds == [("loop", "while"), ("loop", "do"), ("loop", "foreach")]


def test_classic_switch_cases_are_branches(flows: Flows) -> None:
    assert outline(flows, "SwitchSamples#legacyRate(String)") == "\n".join(
        [
            "switch category",
            '  case "A"',
            "    set rate = [local_variable]",
            "    break",
            '  case "B"',
            '  case "C"',
            "    set rate = [local_variable]",
            "    break",
            "  default",
            "    set rate = [local_variable]",
            "return rate",
        ]
    )
    (switch,) = [n for n in flows("SwitchSamples#legacyRate(String)").nodes if n.kind == "switch"]
    assert [b.kind for b in switch.branches] == ["case", "case", "case", "default"]


def test_switch_expression_cases_hang_off_the_return(flows: Flows) -> None:
    flow = flows("SwitchSamples#modernLabel(int)")
    (ret,) = flow.nodes
    assert ret.kind == "return" and [c.kind for c in ret.children] == ["case", "case", "default"]


def test_try_catch_finally_is_an_exception_path(flows: Flows) -> None:
    assert outline(flows, "SwitchSamples#parseOrDefault(String,int)") == "\n".join(
        [
            "try",
            "  return Integer.parseInt(text)  (calls parseInt)",
            "catch NumberFormatException e",
            "  return fallback",
            "finally",
        ]
    )
    (try_node,) = flows("SwitchSamples#parseOrDefault(String,int)").nodes
    assert [b.kind for b in try_node.branches] == ["catch", "finally"]
    assert flows("SwitchSamples#parseOrDefault(String,int)").summary.try_blocks == 1


def test_throw_guarded_by_condition(flows: Flows) -> None:
    flow = flows("SwitchSamples#requirePositive(int)")
    (if_node,) = flow.nodes
    assert if_node.kind == "if" and [c.kind for c in if_node.children] == ["throw"]
    assert if_node.children[0].text == 'new IllegalArgumentException("value must be positive")'


def test_stream_pipelines_with_group_and_collectors(flows: Flows) -> None:
    assert outline(flows, "StreamSamples#openVisitsByFacility(List<Visit>)") == "\n".join(
        [
            'return visits.stream().filter(Objects::nonNull).filter(v -> !"CLOSED".equals(v.status()))'
            ".collect(Collectors.groupingBy(Visit::facility))",
            "  ~ stream visits: stream > filter > filter > collect[groupingBy]",
        ]
    )
    (ret,) = flows("StreamSamples#openVisitsByFacility(List<Visit>)").nodes
    (pipe,) = ret.header_streams
    assert pipe.source == "visits"
    assert [(o.name, o.category) for o in pipe.ops] == [
        ("stream", "source"),
        ("filter", "filter"),
        ("filter", "filter"),
        ("collect", "group"),
    ]
    assert pipe.ops[1].args == ["Objects::nonNull"]
    assert pipe.ops[3].collectors == ["groupingBy"]


def test_collector_with_downstream_aggregation(flows: Flows) -> None:
    (ret,) = flows("StreamSamples#countByStatus(List<Visit>)").nodes
    (pipe,) = ret.header_streams
    assert (
        pipe.ops[-1].collectors == ["groupingBy", "counting"] and pipe.ops[-1].category == "group"
    )
    longest = flows("StreamSamples#longestStay(List<Visit>)").nodes[0].header_streams[0]
    assert [(o.name, o.category) for o in longest.ops][1:] == [
        ("mapToInt", "map"),
        ("max", "aggregate"),
    ]


def test_zero_comment_method_flow_shows_the_steps(flows: Flows) -> None:
    text = outline(flows, "InvoiceBatch#process(List<Row>,int,boolean)")
    assert text.split("\n")[:3] == [
        "foreach Row row : rows",
        "  if row.account() == null  (calls account)",
        "    if strict",
    ]
    assert "mutate kept via add() [local_variable]" in text  # collection updates are visible
    assert "stream kept: stream > collect[groupingBy+summingDouble]" in text
    s = flows("InvoiceBatch#process(List<Row>,int,boolean)").summary
    assert (s.loops, s.decisions, s.throws, s.streams, s.mutations) == (1, 3, 1, 1, 2)


def test_call_leaves_point_at_project_methods(flows: Flows) -> None:
    flow = flows("InvoiceBatch#process(List<Row>,int,boolean)")
    calls = [n for n in flow.walk() if n.kind == "call"]
    assert {c.callee.callee_id for c in calls if c.callee} == {
        f"{PREFIX}.zero.InvoiceBatch.Row#withAmount(double)",
        f"{PREFIX}.zero.InvoiceBatch.Row#amount()",
    }


def test_positions_and_ordering(flows: Flows) -> None:
    flow = flows("InvoiceBatch#process(List<Row>,int,boolean)")
    starts = [(n.start_line, n.start_column) for n in flow.nodes]
    assert starts == sorted(starts)
    for node in flow.walk():
        assert node.start_line <= node.end_line
        for child in node.children:
            assert node.start_line <= child.start_line and child.end_line <= node.end_line


# ---- behaviour that needs purpose-built sources ---------------------------------------------


SRC = """package p;
import java.util.*;
import java.util.stream.*;
public class Flow {
    private int count;
    private final List<String> log = new ArrayList<>();
    static class Dto { void setName(String n) {} void setAge(int a) {} String name() { return ""; } }

    void fieldsAndSetters(Dto dto, int[] arr, String s) {
        count++;
        this.count += 2;
        log.add(s);
        dto.setName(s);
        dto.setAge(arr.length);
        arr[0] = 5;
        String t = s.trim();
    }

    boolean headerStream(List<String> xs) {
        if (xs.stream().anyMatch(x -> x.isEmpty())) {
            return true;
        }
        return false;
    }

    long nestedPipelines(List<List<String>> groups) {
        return groups.stream().map(g -> g.stream().filter(x -> !x.isEmpty()).count()).count();
    }

    void lambdaWork(List<String> xs) {
        xs.forEach(x -> helper(x));
        Runnable r = () -> helper("late");
    }

    void helper(String v) {}

    int ambiguousCall(Object o) { return pick(o.hashCode() > 0 ? missing() : 0); }
    int pick(String a) { return 1; }
    int pick(int a) { return 2; }
    int missing() { return 0; }
}
"""


@pytest.fixture(scope="module")
def custom(java_analyzer: JavaParserAnalyzer, tmp_path_factory: pytest.TempPathFactory) -> Flows:
    return Flows(
        analyze_sources(java_analyzer, tmp_path_factory.mktemp("cf"), {"p/Flow.java": SRC})
    )


def test_field_and_collection_mutations(custom: Flows) -> None:
    flow = custom("Flow#fieldsAndSetters(Dto,int[],String)")
    mutations = [(n.target, n.operator, n.target_kind) for n in flow.walk() if n.kind == "mutation"]
    assert mutations == [
        ("count", "++", "field"),
        ("this.count", "+=", "field"),
        ("log", "add", "field"),
        ("dto", "setName", "parameter"),
        ("dto", "setAge", "parameter"),
        ("arr[0]", "=", "parameter"),  # the assigned element lives in the `arr` parameter
    ]
    assert flow.summary.mutations == 6


def test_stream_in_a_condition_is_part_of_the_header(custom: Flows) -> None:
    flow = custom("Flow#headerStream(List<String>)")
    if_node = flow.nodes[0]
    assert if_node.kind == "if" and [c.kind for c in if_node.children] == ["return"]
    assert len(if_node.header_streams) == 1  # not mistaken for a step inside the if body
    assert if_node.header_streams[0].ops[-1].name == "anyMatch"
    assert flow.summary.streams == 0


def test_pipeline_inside_another_pipeline_belongs_to_the_outer_one(custom: Flows) -> None:
    flow = custom("Flow#nestedPipelines(List<List<String>>)")
    (ret,) = flow.nodes
    assert len(ret.header_streams) == 1 and [o.name for o in ret.header_streams[0].ops] == [
        "stream",
        "map",
        "count",
    ]
    assert not [n for n in flow.walk() if n.kind == "stream"]


def test_calls_inside_lambdas_are_flagged(custom: Flows) -> None:
    flow = custom("Flow#lambdaWork(List<String>)")
    calls = [n for n in flow.walk() if n.kind == "call"]
    assert [(c.callee.name if c.callee else None, c.in_lambda) for c in calls] == [
        ("helper", True),
        ("helper", True),
    ]


def test_ambiguous_calls_are_kept_with_candidates(custom: Flows) -> None:
    flow = custom("Flow#ambiguousCall(Object)")
    (ret,) = flow.nodes  # the call is in the return's header, so it is listed there, not as a step
    (call,) = [c for c in ret.header_calls if c.name == "pick"]
    assert call.callee_id is None and call.status.value == "ambiguous"
    assert sorted(call.candidates) == ["p.Flow#pick(String)", "p.Flow#pick(int)"]


def test_empty_method_has_an_empty_flow(java_analyzer: JavaParserAnalyzer, tmp_path: Path) -> None:
    flows = Flows(
        analyze_sources(java_analyzer, tmp_path, {"p/E.java": "package p; class E { void f() {} }"})
    )
    flow = flows("E#f()")
    assert flow.nodes == [] and flow.summary.max_depth == 0
    assert render_outline(flow) == ""


def test_flow_nodes_are_json_round_trippable(flows: Flows) -> None:
    flow = flows("InvoiceBatch#process(List<Row>,int,boolean)")
    again = ControlFlow.model_validate_json(flow.model_dump_json())
    assert render_outline(again) == render_outline(flow)
    assert isinstance(next(flow.nodes[0].walk()), FlowNode)
