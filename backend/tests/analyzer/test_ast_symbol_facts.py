"""Facts the Phase 3 resolver depends on: references, receivers, argument hints, variable scopes."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.analyzer import JavaParserAnalyzer
from app.analyzer.ast_models import Expression, Method

SOURCE = """\
package demo;

import java.util.List;

class Facts {
    int count;
    String label;

    void flow(List<String> items, int limit, Facts other) {
        int total = 0;
        total += limit;
        count++;
        this.label = "x";
        other.count = total;
        helper(1, "two", null, new Facts(), (Object) other, -2.5, total);
        Object o = other;
        if (o instanceof String s && !s.isEmpty()) {
            total = s.length();
        }
        items.forEach(item -> System.out.println(item));
        try {
            helper(0, "", null, null, null, 0, 0);
        } catch (IllegalStateException | IllegalArgumentException ex) {
            total = 1;
        }
        for (int i = 0; i < limit; i++) {
            total += i;
        }
        var made = new Facts();
        switch (limit) {
            case 1:
                int inCase = 5;
                break;
            default:
                inCase = 6;
        }
        new Facts().other(this).label.length();
        Runnable r = new Runnable() {
            public void run(String arg) {
                helper(arg.length(), "", null, null, null, 0, 0);
            }
        };
    }

    void helper(int a, String b, Object c, Facts d, Object e, double f, long g) {}

    Facts other(Facts f) {
        return f;
    }
}
"""


@pytest.fixture(scope="module")
def flow(java_analyzer: JavaParserAnalyzer, tmp_path_factory: pytest.TempPathFactory) -> Method:
    path: Path = tmp_path_factory.mktemp("facts") / "Facts.java"
    path.write_text(SOURCE)
    (result,) = java_analyzer.analyze_files([path])
    assert result.ok, result.errors
    return next(m for m in result.types[0].methods if m.name == "flow")


def refs(m: Method, name: str) -> list[Expression]:
    return [e for e in m.expressions if e.kind == "name_ref" and e.name == name]


def decl(m: Method, name: str) -> Expression:
    (d,) = [e for e in m.expressions if e.kind == "variable_declaration" and e.name == name]
    return d


def lines_of(text: str, needle: str) -> int:
    return next(i for i, line in enumerate(text.split("\n"), 1) if needle in line)


def test_identifier_uses_are_recorded_as_name_refs(flow: Method) -> None:
    assert len(refs(flow, "limit")) >= 3
    targets = [e for e in flow.expressions if e.kind == "name_ref" and "assignment_target" in e.tags]
    # in source order: total +=, count++, total = s.length(), total = 1, i++, total += i, inCase =
    # (`this.label = ..` and `other.count = ..` are field accesses, not simple names)
    assert [t.name for t in targets] == ["total", "count", "total", "total", "i", "total", "inCase"]


def test_switch_case_enum_labels_are_not_name_refs(
    java_analyzer: JavaParserAnalyzer, tmp_path: Path
) -> None:
    # `case RED:` labels are constants of the selector's type, not variable references
    path = tmp_path / "Colors.java"
    path.write_text(
        "enum Color { RED, GREEN }\n"
        "class Colors {\n"
        "    int pick(Color c) {\n"
        "        switch (c) {\n"
        "            case RED: return 1;\n"
        "            default: return RED_VALUE;\n"
        "        }\n"
        "    }\n"
        "    static final int RED_VALUE = 9;\n"
        "}\n"
    )
    (result,) = java_analyzer.analyze_files([path])
    pick = result.types[1].methods[0]
    assert [e.name for e in pick.expressions if e.kind == "name_ref"] == ["c", "RED_VALUE"]


def test_receivers_are_linked_between_expressions(flow: Method) -> None:
    by_id = {e.id: e for e in flow.expressions}
    chain = next(e for e in flow.expressions if e.kind == "method_call" and e.name == "length"
                 and e.receiver_kind == "expr" and by_id[e.receiver_expr_id or 0].kind == "field_access")
    label = by_id[chain.receiver_expr_id or 0]
    assert (label.kind, label.name, label.receiver_kind) == ("field_access", "label", "expr")
    other_call = by_id[label.receiver_expr_id or 0]
    assert (other_call.kind, other_call.name) == ("method_call", "other")
    created = by_id[other_call.receiver_expr_id or 0]
    assert (created.kind, created.name) == ("object_creation", "Facts")

    this_assign = next(e for e in flow.expressions if e.kind == "field_access" and e.name == "label")
    assert this_assign.receiver_kind == "this"
    unqualified = next(e for e in flow.expressions if e.kind == "method_call" and e.name == "helper")
    assert unqualified.receiver_kind == "none"
    println = next(e for e in flow.expressions if e.kind == "method_call" and e.name == "println")
    assert by_id[println.receiver_expr_id or 0].name == "out"  # System.out
    assert by_id[by_id[println.receiver_expr_id or 0].receiver_expr_id or 0].name == "System"


def test_argument_hints(flow: Method) -> None:
    first = next(e for e in flow.expressions if e.kind == "method_call" and e.name == "helper")
    assert first.args is not None
    got = [(h.kind, h.type, h.name) for h in first.args]
    assert got == [
        ("literal", "int", None), ("literal", "String", None), ("literal", "null", None),
        ("new", "Facts", None), ("cast", "Object", None), ("literal", "double", None),
        ("name", None, "total"),
    ]


def test_variable_scopes_and_origins(flow: Method, java_analyzer: JavaParserAnalyzer) -> None:
    src = SOURCE
    total = decl(flow, "total")
    assert total.start_line == lines_of(src, "int total = 0") and total.tags == ["initialized"]
    assert total.scope_end_line == lines_of(src, "    void helper(") - 2  # closing brace of flow()

    i = decl(flow, "i")
    assert "for_init" in i.tags and i.scope_end_line == lines_of(src, "total += i;") + 1

    in_case = decl(flow, "inCase")
    # old-style case group: the variable stays visible to the whole switch block
    assert in_case.scope_end_line == lines_of(src, "inCase = 6;") + 1

    s = decl(flow, "s")
    assert s.tags == ["pattern"] and s.type == "String"

    item = decl(flow, "item")
    assert item.tags == ["lambda_param"] and item.type is None  # implicitly typed

    ex = decl(flow, "ex")
    assert ex.tags == ["catch_param"] and ex.type == "IllegalStateException|IllegalArgumentException"

    made = decl(flow, "made")
    assert made.type == "var" and made.initializer is not None
    assert (made.initializer.kind, made.initializer.type) == ("new", "Facts")


def test_inner_method_parameters_and_local_class_marker(flow: Method) -> None:
    arg = decl(flow, "arg")
    assert arg.tags == ["param", "in_local_class"] or set(arg.tags) == {"param", "in_local_class"}
    inner_call = next(e for e in flow.expressions if e.kind == "method_call" and e.name == "length"
                      and "in_local_class" in e.tags)
    assert inner_call.receiver_kind == "expr"
    outer_helper = [e for e in flow.expressions if e.kind == "method_call" and e.name == "helper"]
    assert [("in_local_class" in e.tags) for e in outer_helper] == [False, False, True]
