"""Phase 2: deterministic AST facts extracted from small Java fixtures."""

from __future__ import annotations

from app.analyzer.ast_models import Method, ParsedFile, TypeDecl
from tests.conftest import FIXTURES


def only_type(parsed: dict[str, ParsedFile], rel: str, name: str | None = None) -> TypeDecl:
    f = parsed[rel]
    assert f.ok, f.errors
    return next(t for t in f.types if name is None or t.name == name)


def method(t: TypeDecl, name: str) -> Method:
    matches = [m for m in [*t.methods, *t.constructors] if m.name == name]
    assert len(matches) == 1, f"{name}: {len(matches)} matches"
    return matches[0]


def kinds(m: Method) -> list[str]:
    return [s.kind for s in m.statements]


def calls(m: Method, name: str) -> list[list[str]]:
    return [e.tags for e in m.expressions if e.kind == "method_call" and e.name == name]


# ---- Fixture A: simple method / class-level facts ---------------------------------------------


def test_class_level_facts(parsed: dict[str, ParsedFile]) -> None:
    f = parsed["simple/OrderCalculator.java"]
    assert f.status == "ok" and f.package_name == "com.example.fixtures.simple"
    assert [(i.name, i.is_static, i.is_asterisk) for i in f.imports] == [("java.util.List", False, False)]

    t = f.types[0]
    assert (t.kind, t.name, t.qualified_name, t.visibility) == (
        "class", "OrderCalculator", "com.example.fixtures.simple.OrderCalculator", "public")
    assert [c.text for c in t.comments] == ["Totals up order lines."]

    fields = {x.name: x for x in t.fields}
    assert fields["TAX_RATE"].modifiers == ["private", "static", "final"]
    assert fields["TAX_RATE"].initializer == "0.08"
    assert fields["currency"].visibility == "private" and fields["currency"].type == "String"

    assert [(c.signature, c.kind) for c in t.constructors] == [("OrderCalculator(String)", "constructor")]
    assert [m.name for m in t.methods] == ["calculateTotal", "withTax", "applyRate", "getCurrency"]

    (nested,) = t.nested_types
    assert nested.kind == "record" and nested.qualified_name.endswith("OrderCalculator.OrderLine")
    assert [(p.name, p.type) for p in nested.record_components] == [
        ("sku", "String"), ("price", "double"), ("quantity", "int")]


def test_method_signature_modifiers_and_exact_source(parsed: dict[str, ParsedFile]) -> None:
    t = only_type(parsed, "simple/OrderCalculator.java")
    total = method(t, "calculateTotal")
    assert total.signature == "calculateTotal(List<OrderLine>)"
    assert (total.visibility, total.return_type, total.is_static) == ("public", "double", False)
    assert [(p.name, p.type) for p in total.parameters] == [("lines", "List<OrderLine>")]

    apply_rate = method(t, "applyRate")
    assert (apply_rate.visibility, apply_rate.is_static) == ("private", True)
    assert apply_rate.signature == "applyRate(double,double)"

    # line range is exact: it slices the original file back to the stored source text
    lines = (FIXTURES / "simple/OrderCalculator.java").read_text().split("\n")
    assert "\n".join(lines[total.start_line - 1 : total.end_line]) == total.source_text
    assert total.source_text.lstrip().startswith("public double calculateTotal")  # whole lines, indent kept
    assert total.source_text.rstrip().endswith("}")


def test_comments_are_captured_as_evidence(parsed: dict[str, ParsedFile]) -> None:
    total = method(only_type(parsed, "simple/OrderCalculator.java"), "calculateTotal")
    assert sorted(c.kind for c in total.comments) == ["javadoc", "line"]
    assert any(c.text == "line subtotal" for c in total.comments)
    javadoc = next(c for c in total.comments if c.kind == "javadoc")
    assert javadoc.text == "Sum of price * quantity across lines."  # decorative '*' removed
    assert total.cyclomatic_complexity == 2 and total.max_nesting_depth == 1


def test_expression_kinds_in_simple_method(parsed: dict[str, ParsedFile]) -> None:
    t = only_type(parsed, "simple/OrderCalculator.java")
    total = method(t, "calculateTotal")
    assert [(e.kind, e.name, e.operator) for e in total.expressions if e.kind == "assignment"] == [
        ("assignment", "total", "+=")]
    assert [e.name for e in total.expressions if e.kind == "variable_declaration"] == ["total", "line"]  # incl. loop var
    ctor = method(t, "OrderCalculator")
    assert [(e.kind, e.name, e.scope) for e in ctor.expressions if e.kind == "field_access"] == [
        ("field_access", "currency", "this")]
    with_tax = method(t, "withTax")
    (call,) = [e for e in with_tax.expressions if e.kind == "method_call"]
    assert (call.kind, call.name, call.scope, call.arg_count) == ("method_call", "applyRate", None, 2)
    assert [e.name for e in with_tax.expressions if e.kind == "name_ref"] == ["amount", "TAX_RATE"]


# ---- Fixture B: branches ----------------------------------------------------------------------


def test_nested_if_else_chain(parsed: dict[str, ParsedFile]) -> None:
    m = method(only_type(parsed, "branches/ShippingPolicy.java"), "classify")
    assert kinds(m) == ["if", "return", "else_if", "if", "return", "else", "return",
                        "else_if", "return", "else", "return"]
    by_id = {s.id: s for s in m.statements}
    # else-if chain stays at the depth of the first `if`; nested ifs go deeper
    assert [s.depth for s in m.statements if s.kind in {"if", "else_if"}] == [0, 0, 1, 0]
    assert by_id[3].parent_id == 1 and by_id[8].parent_id == 3
    assert by_id[1].text == "parcel == null"
    assert by_id[4].text == "express && parcel.weightKg() <= 50"
    assert m.cyclomatic_complexity == 8  # 1 + if/else_if x4 + `||` + `&&` + ternary
    assert m.max_nesting_depth == 2


def test_conditions_null_checks_ternary_logical(parsed: dict[str, ParsedFile]) -> None:
    t = only_type(parsed, "branches/ShippingPolicy.java")
    m = method(t, "classify")
    assert [(e.kind, e.operator) for e in m.expressions if e.kind == "null_check"] == [("null_check", "==")]
    assert sorted(e.operator or "" for e in m.expressions if e.kind == "logical") == ["&&", "||"]
    assert sorted(e.operator or "" for e in m.expressions if e.kind == "comparison") == [
        "<=", ">", ">", ">="]
    assert [e.kind for e in m.expressions if e.kind == "ternary"] == ["ternary"]

    missing = method(t, "isMissing")
    assert [e.kind for e in missing.expressions if e.kind == "null_check"] == ["null_check"]
    assert missing.cyclomatic_complexity == 3  # 1 + `&&` + ternary


# ---- Fixture C: loops -------------------------------------------------------------------------


def test_for_loop_with_continue(parsed: dict[str, ParsedFile]) -> None:
    m = method(only_type(parsed, "loops/LoopSamples.java"), "sumPositives")
    assert kinds(m) == ["for", "if", "continue", "return"]
    assert m.statements[0].text == "int i = 0; i < values.size(); i++"
    assert (m.cyclomatic_complexity, m.max_nesting_depth) == (3, 2)
    assert m.statements[1].depth == 1 and m.statements[1].parent_id == m.statements[0].id
    # emitted in AST traversal order (a for-update is visited before the body), so compare unordered
    assert sorted(e.operator or "" for e in m.expressions if e.kind == "assignment") == ["++", "+="]


def test_while_do_foreach_break(parsed: dict[str, ParsedFile]) -> None:
    m = method(only_type(parsed, "loops/LoopSamples.java"), "firstNegativeIndex")
    assert kinds(m) == ["while", "if", "break", "do", "foreach", "return"]
    texts = {s.kind: s.text for s in m.statements}
    assert texts["do"] == "index > 100" and texts["foreach"] == "int v : values"
    assert (m.cyclomatic_complexity, m.max_nesting_depth) == (5, 2)


# ---- Fixture D: streams -----------------------------------------------------------------------


def test_stream_pipeline_tags(parsed: dict[str, ParsedFile]) -> None:
    m = method(only_type(parsed, "streams/StreamSamples.java"), "openVisitsByFacility")
    assert calls(m, "stream") == [["stream_source"]]
    assert calls(m, "filter") == [["stream_op"], ["stream_op"]]
    assert calls(m, "collect") == [["stream_op"]]
    assert calls(m, "groupingBy") == [["collector"]]
    refs = {e.name: e.tags for e in m.expressions if e.kind == "method_ref"}
    assert refs["nonNull"] == ["predicate", "null_check"]
    assert refs["facility"] == []
    (lam,) = [e for e in m.expressions if e.kind == "lambda"]
    assert lam.tags == ["predicate"]
    assert calls(m, "equals") == [["comparison"]]


def test_static_imported_collector_and_aggregation(parsed: dict[str, ParsedFile]) -> None:
    t = only_type(parsed, "streams/StreamSamples.java")
    assert calls(method(t, "countByStatus"), "counting") == [["collector"]]
    longest = method(t, "longestStay")
    assert calls(longest, "mapToInt") == [["stream_op"]]
    assert calls(longest, "max") == [["stream_op"]]
    assert calls(longest, "orElse") == [["optional"]]


def test_optional_chain_and_predicates(parsed: dict[str, ParsedFile]) -> None:
    t = only_type(parsed, "streams/StreamSamples.java")
    m = method(t, "facilityOf")
    assert [calls(m, n) for n in ("ofNullable", "map", "filter", "orElse")] == [[["optional"]]] * 4
    (lam,) = [e for e in m.expressions if e.kind == "lambda"]
    assert lam.tags == ["predicate"]
    any_long = method(t, "anyLong")
    assert calls(any_long, "anyMatch") == [["stream_op"]]
    assert [e.tags for e in any_long.expressions if e.kind == "lambda"] == [["predicate"]]
    assert [(e.kind, e.operator) for e in any_long.expressions if e.kind == "comparison"] == [("comparison", ">")]


# ---- Fixture E: switch / try / throw ----------------------------------------------------------


def test_classic_switch_with_fallthrough(parsed: dict[str, ParsedFile]) -> None:
    m = method(only_type(parsed, "switches/SwitchSamples.java"), "legacyRate")
    assert kinds(m) == ["switch", "case", "break", "case", "case", "break", "default", "return"]
    assert [s.text for s in m.statements if s.kind in {"switch", "case"}] == ['category', '"A"', '"B"', '"C"']
    assert m.cyclomatic_complexity == 4  # 1 + three cases (default is free)
    assert m.max_nesting_depth == 1
    assert all(s.depth == 1 for s in m.statements if s.kind in {"case", "default", "break"})


def test_switch_expression(parsed: dict[str, ParsedFile]) -> None:
    m = method(only_type(parsed, "switches/SwitchSamples.java"), "modernLabel")
    assert [e.kind for e in m.expressions if e.kind != "name_ref"] == ["switch_expr"]
    assert [e.name for e in m.expressions if e.kind == "name_ref"] == ["level"]
    assert kinds(m) == ["return", "case", "case", "default"]
    assert m.cyclomatic_complexity == 3 and m.max_nesting_depth == 1


def test_try_catch_finally_and_throw(parsed: dict[str, ParsedFile]) -> None:
    t = only_type(parsed, "switches/SwitchSamples.java")
    m = method(t, "parseOrDefault")
    assert kinds(m) == ["try", "return", "catch", "return", "finally"]
    assert m.statements[2].text == "NumberFormatException e"
    assert m.cyclomatic_complexity == 2
    guard = method(t, "requirePositive")
    assert kinds(guard) == ["if", "throw"]
    assert [(e.kind, e.name) for e in guard.expressions if e.kind == "object_creation"] == [
        ("object_creation", "IllegalArgumentException")]


# ---- Types: interfaces, enums, records, generics, annotations ---------------------------------


def test_class_generics_inheritance_annotations(parsed: dict[str, ParsedFile]) -> None:
    f = parsed["types/TypeZoo.java"]
    zoo = next(t for t in f.types if t.name == "TypeZoo")
    assert zoo.modifiers == ["public", "abstract"]
    assert zoo.superclass == "Base" and zoo.interfaces == ["Serializable", "Runnable"]
    assert zoo.type_parameters == ["T extends Comparable<T>"]
    assert [a.name for a in zoo.annotations] == ["Deprecated"]
    assert {t.name for t in f.types} == {"TypeZoo", "Base"}
    assert next(t for t in f.types if t.name == "Base").visibility == "package"

    fields = {x.name: x for x in zoo.fields}
    assert fields["LIMIT"].modifiers == ["public", "static", "final"]
    assert fields["items"].type == "List<T>" and fields["backup"].type == "List<T>"  # same decl
    assert fields["items"].initializer == "new ArrayList<>()" and fields["backup"].initializer is None


def test_abstract_varargs_throws_generic_method(parsed: dict[str, ParsedFile]) -> None:
    zoo = only_type(parsed, "types/TypeZoo.java", "TypeZoo")
    convert = method(zoo, "convert")
    assert convert.signature == "convert(T,R...)"
    assert convert.is_abstract and not convert.has_body
    assert convert.throws_types == ["java.io.IOException"] and convert.type_parameters == ["R"]
    assert [p.var_args for p in convert.parameters] == [False, True]
    helper = method(zoo, "helper")
    assert (helper.visibility, helper.is_static, helper.modifiers) == ("package", True, ["static", "synchronized"])
    assert [a.name for a in method(zoo, "run").annotations] == ["Override"]


def test_anonymous_class_body_is_part_of_enclosing_method(parsed: dict[str, ParsedFile]) -> None:
    run = method(only_type(parsed, "types/TypeZoo.java", "TypeZoo"), "run")
    (creation,) = [e for e in run.expressions if e.kind == "object_creation"]
    assert creation.tags == ["anonymous_class"]
    # the call inside the anonymous class is still seen, and marked as living in a local class
    assert calls(run, "println") == [["in_local_class"]]
    assert "in_local_class" not in creation.tags


def test_nested_interface_enum_record_annotation(parsed: dict[str, ParsedFile]) -> None:
    zoo = only_type(parsed, "types/TypeZoo.java", "TypeZoo")
    nested = {t.name: t for t in zoo.nested_types}
    assert {n: t.kind for n, t in nested.items()} == {
        "Shape": "interface", "Color": "enum", "Point": "record", "Marker": "annotation"}

    shape = nested["Shape"]
    assert shape.qualified_name == "com.example.fixtures.types.TypeZoo.Shape"
    area, describe = method(shape, "area"), method(shape, "describe")
    assert (area.visibility, area.is_abstract, area.has_body) == ("public", True, False)
    assert (describe.is_abstract, describe.has_body) == (False, True)
    sides = shape.fields[0]
    assert (sides.name, sides.visibility, sides.modifiers) == ("SIDES", "public", ["public", "static", "final"])

    color = nested["Color"]
    assert color.enum_constants == ["RED", "GREEN", "BLUE"] and color.interfaces == ["Serializable"]

    point = nested["Point"]
    assert [(p.name, p.type) for p in point.record_components] == [("x", "int"), ("y", "int")]
    (compact,) = point.constructors
    assert compact.kind == "compact_constructor" and compact.signature == "Point(int,int)"
    assert kinds(compact) == ["if", "throw"]


# ---- Fixture I: zero comments -----------------------------------------------------------------


def test_zero_comment_fixture_still_yields_full_structure(parsed: dict[str, ParsedFile]) -> None:
    f = parsed["zero_comments/InvoiceBatch.java"]
    assert f.ok
    assert FIXTURES.joinpath("zero_comments/InvoiceBatch.java").read_text().count("//") == 0
    batch = f.types[0]
    for m in [*batch.methods, *batch.constructors]:
        assert m.comments == []
    assert batch.comments == []

    m = method(batch, "process")
    assert kinds(m) == ["foreach", "if", "if", "throw", "continue", "if", "else", "return"]
    assert m.cyclomatic_complexity == 6  # 1 + foreach + 3 ifs + `&&`
    assert m.max_nesting_depth == 3
    assert calls(m, "groupingBy") == [["collector"]]
    assert calls(m, "summingDouble") == [["collector"]]
    assert calls(m, "toList") == [["collector"]]
    assert calls(m, "filter") == [["stream_op"]]
    assert calls(m, "map") == [["stream_op"]]
    assert calls(m, "stream") == [["stream_source"], ["stream_source"]]  # kept.stream(), entrySet().stream()
    assert calls(m, "entrySet") == [[]]
    declared = {e.name: e.tags for e in m.expressions if e.kind == "variable_declaration"}
    assert sorted(declared) == ["byAccount", "e", "kept", "row", "s"]
    assert declared["row"] == ["foreach"]
    assert declared["e"] == declared["s"] == ["lambda_param"]  # lambda params are declarations too
    assert [e.tags for e in m.expressions if e.kind == "lambda"] == [[], ["predicate"]]


# ---- Failure handling -------------------------------------------------------------------------


def test_invalid_java_is_marked_not_fatal(parsed: dict[str, ParsedFile]) -> None:
    broken = parsed["invalid/Broken.java"]
    assert broken.status == "parse_error"
    assert broken.errors and broken.types == []
    # the same batch still produced results for every other file
    others = [f for k, f in parsed.items() if k != "invalid/Broken.java"]
    assert others and all(f.ok for f in others)
    assert len(parsed) == len(list(FIXTURES.rglob("*.java")))
