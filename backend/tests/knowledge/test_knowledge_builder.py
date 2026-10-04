"""Phase 9: the semantic code model assembled from all analysis results."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.analyzer import JavaParserAnalyzer
from app.analyzer.resolution_models import ResolutionStatus
from app.knowledge.builder import BuiltKnowledge, describe_name, split_words
from app.knowledge.models import MethodKnowledge, RepositoryKnowledge
from tests.conftest import FIXTURES, PROJECT
from tests.helpers import analyze_paths, analyze_sources, build_knowledge

# ---- name-based descriptions -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("calculateTotal", "calculates total"),
        ("getCustomerName", "returns the customer name"),
        ("isEligible", "checks whether eligible"),
        ("hasDiscount", "checks whether it has discount"),
        ("canRetry", "checks whether it can retry"),
        ("setRate", "sets rate"),
        ("validateOrder", "validates order"),
        ("toDto", "converts to dto"),
        ("parseHTTPResponse", "parses http response"),
        ("run", "runs"),
        ("frobnicate", "performs frobnicate"),
        ("withTax", "returns a copy with tax"),
    ],
)
def test_names_become_plain_phrases(name: str, expected: str) -> None:
    assert describe_name(name, "method", "Foo", None) == expected


def test_constructors_and_word_splitting() -> None:
    assert describe_name("PricingService", "constructor", "PricingService", None) == (
        "constructs a pricing service"
    )
    assert split_words("parseHTTPResponse2") == ["parse", "http", "response", "2"]


# ---- fixtures ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def project(java_analyzer: JavaParserAnalyzer) -> BuiltKnowledge:
    return build_knowledge(analyze_paths(java_analyzer, PROJECT))


@pytest.fixture(scope="module")
def fixtures(java_analyzer: JavaParserAnalyzer) -> BuiltKnowledge:
    return build_knowledge(analyze_paths(java_analyzer, FIXTURES))


def method(b: BuiltKnowledge, suffix: str) -> MethodKnowledge:
    found = [m for m in b.repository.methods if m.method_id.endswith(suffix)]
    assert len(found) == 1, f"{suffix}: {len(found)}"
    return found[0]


PS = "com.acme.service.PricingService"

# ---- structure and identity -------------------------------------------------------------------


def test_stats_and_identity(project: BuiltKnowledge) -> None:
    repo = project.repository
    assert repo.stats.files == 12 and repo.stats.classes == len(repo.classes) == 13
    assert repo.stats.methods == len(repo.methods)
    assert repo.stats.call_edges == sum(len(e) for e in project.graph.edges_from.values())
    assert len({m.id for m in repo.methods}) == len(repo.methods)  # opaque ids are unique
    assert all(m.id.startswith("m_") and len(m.id) == 12 for m in repo.methods)
    assert all(c.id.startswith("c_") for c in repo.classes)
    by_class = {c.id: c for c in repo.classes}
    assert all(m.class_id in by_class for m in repo.methods)
    for c in repo.classes:  # a class lists exactly its own methods, in source order
        own = [m for m in repo.methods if m.class_id == c.id]
        assert sorted(c.method_ids) == sorted(m.id for m in own)
        lines = [next(m for m in own if m.id == i).start_line for i in c.method_ids]
        assert lines == sorted(lines)


def test_duplicate_classes_do_not_collide(project: BuiltKnowledge) -> None:
    things = [c for c in project.repository.classes if c.fqn == "com.acme.dup.Thing"]
    assert len(things) == 2 and things[0].id != things[1].id
    assert {c.file for c in things} == {
        "main/com/acme/dup/Thing.java",
        "test/com/acme/dup/Thing.java",
    }


def test_class_knowledge(project: BuiltKnowledge) -> None:
    pricing = project.repository.klass(PS)
    assert pricing is not None
    assert pricing.superclass == "com.acme.service.BasePricing"
    assert [f.name for f in pricing.fields] == ["LIMIT", "owner", "history"]
    assert pricing.purpose.basis == "name" and pricing.purpose.level == "inference"
    assert pricing.purpose.structure is not None and pricing.purpose.structure.startswith(
        "class with"
    )
    assert {
        "com.acme.model.Customer",
        "com.acme.model.Money",
        "com.acme.service.BasePricing",
        "com.acme.util.Strings",
        "com.acme.model.Status",
    } <= set(pricing.dependencies)
    assert pricing.fqn not in pricing.dependencies
    customer = project.repository.klass("com.acme.model.Customer")
    assert customer is not None and PS in customer.dependents


# ---- purpose ----------------------------------------------------------------------------------


def test_javadoc_purpose_is_a_fact_name_purpose_is_an_inference(fixtures: BuiltKnowledge) -> None:
    total = method(fixtures, "OrderCalculator#calculateTotal(List<OrderLine>)")
    assert (total.purpose.text, total.purpose.basis, total.purpose.level) == (
        "Sum of price * quantity across lines.",
        "comment",
        "fact",
    )
    assert total.purpose.evidence_ids and total.purpose.evidence_ids[0] in {
        e.id for e in total.evidence
    }
    assert (
        total.purpose.structure == "1 loop, calls price, quantity, 1 state change, returns double"
    )
    assert not [u for u in total.unknowns if u.kind == "purpose_not_documented"]

    tax = method(fixtures, "OrderCalculator#withTax(double)")
    assert (tax.purpose.basis, tax.purpose.level) == ("name", "inference")
    assert [u.kind for u in tax.unknowns] == ["purpose_not_documented"]


def test_class_javadoc_becomes_the_class_purpose(fixtures: BuiltKnowledge) -> None:
    calc = fixtures.repository.klass("com.example.fixtures.simple.OrderCalculator")
    assert calc is not None
    assert (calc.purpose.text, calc.purpose.basis) == ("Totals up order lines.", "comment")


# ---- parameters, outputs, calls ---------------------------------------------------------------


def test_inputs_say_where_values_come_from(project: BuiltKnowledge) -> None:
    base = method(project, "BasePricing#base(int)")
    (x,) = base.parameters
    assert x.comes_from == [
        "PricingService.price() L26: parameter units",
        "PricingService.inherited() L59: literal int",
        "PricingService.inherited() L60: literal int",
    ]
    entry = method(project, "PricingService#overloads(Customer)")
    assert entry.parameters[0].comes_from == [
        "no project caller passes this (entry point or external caller)"
    ]
    price_str = method(project, "PricingService#price(String)")
    assert price_str.parameters[0].comes_from == [
        "PricingService.price() L34: result of getName()",
        "PricingService.overloads() L39: literal String",
        "PricingService.overloads() L41: result of getName()",
    ]


def test_outputs_and_side_effects(project: BuiltKnowledge) -> None:
    price = method(project, "PricingService#price(int)")
    assert price.output.type == "int" and price.output.returns == [
        "result of base()",
        "field LIMIT",
    ]
    assert price.output.side_effects == []
    getter = method(project, "Customer#getName()")
    assert getter.output.returns == ["field name"]


def test_callees_carry_purposes_and_callers_flag_ambiguity(project: BuiltKnowledge) -> None:
    price = method(project, "PricingService#price(Customer)")
    callees = {(c.name, c.method_id): c for c in price.callees}
    get_name = callees[("getName", "com.acme.model.Customer#getName()")]
    assert get_name.purpose == "returns the name" and get_name.site_lines == [34]
    assert price.callees[0].name in {"getName", "price"}
    base = method(project, "BasePricing#base(int)")
    flags = {c.method_id.split("#")[1]: c.ambiguous for c in base.callers}
    assert flags == {"inherited()": False, "price(int)": False, "scoping(Customer)": True}
    assert base.callers[0].purpose is not None


# ---- risks and unknowns -----------------------------------------------------------------------

RISKY = """package p;
import java.util.*;

public class Risky {
    private static int counter;
    private int state;

    /** Registers an entry. */
    void register(Map<String, Integer> target, String key) {
        counter++;
        state = 1;
        target.put(key, 1);
    }

    String swallow() {
        try {
            return Integer.toString(1);
        } catch (Exception e) {
        }
        return null;
    }

    int deep(int a) {
        if (a > 0) { if (a > 1) { if (a > 2) { for (int i = 0; i < a; i++) { if (i == 3) { return i; } } } } }
        return 0;
    }

    int rec(int n) { return n <= 0 ? 0 : rec(n - 1); }

    int branchy(int a, int b) {
        int r = 0;
        if (a > 1) r++;
        if (a > 2) r++;
        if (a > 3) r++;
        if (b > 1) r++;
        if (b > 2) r++;
        if (b > 3) r++;
        if (a > b) r++;
        if (b > a) r++;
        if (a == b) r++;
        return r;
    }

    // MAX_AGE is 120 because the registry rejects older records
    boolean tooOld(int age) { return age > 120; }
    boolean magic(int age) { return age > 77 && age != 0; }

    void caller1() { rec(1); }
    void caller2() { rec(2); }
    void caller3() { rec(3); }
    void caller4() { rec(4); }
    void caller5() { rec(5); }

    void unknown() { missing(); }
}

class RiskyTest {
    @Test
    void recurses() { new Risky().rec(3); }
}
"""


@pytest.fixture(scope="module")
def risky(
    java_analyzer: JavaParserAnalyzer, tmp_path_factory: pytest.TempPathFactory
) -> BuiltKnowledge:
    root = tmp_path_factory.mktemp("risky")
    (root / "README.md").write_text(
        "# Risky\nThe Risky class registers things.\n\n## Recursion\nCall `Risky.rec(` for recursion.\n"
    )
    analysis = analyze_sources(java_analyzer, root, {"src/main/p/Risky.java": RISKY})
    return build_knowledge(analysis, docs_root=root)


def risk_kinds(m: MethodKnowledge) -> list[str]:
    return [r.kind for r in m.risks]


def test_side_effects_and_state_risks(risky: BuiltKnowledge) -> None:
    reg = method(risky, "Risky#register(Map<String,Integer>,String)")
    assert reg.output.side_effects == [
        "modifies field counter (L10: ++)",
        "modifies field state (L11: write: 1)",
        "modifies parameter target (L12: put(key, 1))",
    ]
    risks = {r.kind: r for r in reg.risks}
    assert (
        risks["shared_state"].level == "high"
        and "static field counter" in risks["shared_state"].message
    )
    assert risks["field_write"].level == "medium"
    assert "changes its argument target" in risks["mutates_argument"].message


def test_exception_and_null_risks(risky: BuiltKnowledge) -> None:
    sw = method(risky, "Risky#swallow()")
    assert risk_kinds(sw) == ["broad_catch", "swallowed_exception", "returns_null"]
    assert "catches Exception" in sw.risks[0].message


def test_structure_risks(risky: BuiltKnowledge) -> None:
    deep = method(risky, "Risky#deep(int)")
    assert "nesting" in risk_kinds(deep) and deep.complexity.nesting_depth == 5
    branchy = method(risky, "Risky#branchy(int,int)")
    assert branchy.complexity.cyclomatic == 10 and risk_kinds(branchy) == ["complexity"]
    assert branchy.risks[0].level == "medium"
    rec = method(risky, "Risky#rec(int)")
    assert "recursion" in risk_kinds(rec)


def test_wide_impact_counts_non_test_callers_only(risky: BuiltKnowledge) -> None:
    rec = method(risky, "Risky#rec(int)")
    assert [c.method_id.split("#")[1] for c in rec.callers] == [
        "caller1()",
        "caller2()",
        "caller3()",
        "caller4()",
        "caller5()",
        "rec(int)",
        "recurses()",
    ]
    impact = next(r for r in rec.risks if r.kind == "wide_impact")
    assert "called from 6 methods" in impact.message  # caller1..5 and itself; the test is excluded


def test_unknowns_for_unresolved_calls_and_undocumented_constants(risky: BuiltKnowledge) -> None:
    unk = method(risky, "Risky#unknown()")
    assert [(u.kind, u.message) for u in unk.unknowns if u.kind == "unresolved_call"] == [
        ("unresolved_call", "cannot determine what missing() refers to (no_such_method)")
    ]
    magic = method(risky, "Risky#magic(int)")
    constants = [u.message for u in magic.unknowns if u.kind == "unestablished_constant"]
    assert constants == [
        "the repository does not establish why 77 was chosen (no comment or test mentions it)"
    ]
    # the comment above tooOld() mentions 120, so that constant is explained and not an unknown
    old = method(risky, "Risky#tooOld(int)")
    assert [r.meaning for r in old.rule_candidates] == ["age is greater than 120"]
    assert [e.relation for e in old.evidence if e.source_type == "comment"] == ["explains"]
    assert [u.kind for u in old.unknowns] == ["purpose_not_documented"]


def test_dynamic_dispatch_is_an_unknown(java_analyzer: JavaParserAnalyzer, tmp_path: Path) -> None:
    sources = {
        "p/Shape.java": "package p; public interface Shape { double area(); }",
        "p/Sq.java": "package p; public class Sq implements Shape { public double area() { return 1; } }",
        "p/Ci.java": "package p; public class Ci implements Shape { public double area() { return 2; } }",
        "p/Use.java": "package p; public class Use { double go(Shape s) { return s.area(); } }",
    }
    b = build_knowledge(analyze_sources(java_analyzer, tmp_path, sources))
    go = method(b, "Use#go(Shape)")
    (callee,) = go.callees
    assert sorted(callee.may_dispatch_to) == ["p.Ci#area()", "p.Sq#area()"]
    unknown = next(u for u in go.unknowns if u.kind == "dynamic_dispatch")
    assert "one of 2 overriding methods may run" in unknown.message


# ---- evidence ---------------------------------------------------------------------------------


def test_evidence_covers_code_comments_tests_and_docs(risky: BuiltKnowledge) -> None:
    rec = method(risky, "Risky#rec(int)")
    by_type: dict[str, list[str]] = {}
    for e in rec.evidence:
        by_type.setdefault(e.source_type, []).append(e.relation)
    assert sorted(by_type["source_code"]) == ["implements", "rule:threshold"]  # `n <= 0 ? ..`
    assert by_type["test"] == ["tested_by"]
    assert by_type["readme"] == ["mentions"]
    test = next(e for e in rec.evidence if e.source_type == "test")
    assert (test.class_name, test.method, test.confidence) == ("p.RiskyTest", "recurses", "high")
    assert "new Risky().rec(3)" in test.snippet
    readme = next(e for e in rec.evidence if e.source_type == "readme")
    assert (readme.file, readme.start_line, readme.confidence) == ("README.md", 4, "high")

    register = method(risky, "Risky#register(Map<String,Integer>,String)")
    assert [(e.source_type, e.relation) for e in register.evidence][:2] == [
        ("comment", "documents"),
        ("source_code", "implements"),
    ]
    cls = risky.repository.klass("p.Risky")
    assert cls is not None and [e.source_type for e in cls.evidence] == ["readme", "readme"]
    assert {e.relation for e in cls.evidence} == {"mentions"}  # both README sections name the class
    assert {e.confidence for e in cls.evidence} == {"medium"}


def test_test_methods_are_marked_and_not_counted_as_subjects(risky: BuiltKnowledge) -> None:
    test = method(risky, "RiskyTest#recurses()")
    assert test.is_test and risky.repository.klass("p.RiskyTest").is_test  # type: ignore[union-attr]
    assert not method(risky, "Risky#rec(int)").is_test
    assert not [e for e in test.evidence if e.source_type == "test"]


def test_test_evidence_can_be_turned_off(java_analyzer: JavaParserAnalyzer, tmp_path: Path) -> None:
    analysis = analyze_sources(java_analyzer, tmp_path, {"p/Risky.java": RISKY})
    b = build_knowledge(analysis, enable_tests=False)
    assert not [e for m in b.repository.methods for e in m.evidence if e.source_type == "test"]


def test_evidence_ids_are_unique_per_method_and_lines_valid(fixtures: BuiltKnowledge) -> None:
    for m in fixtures.repository.methods:
        ids = [e.id for e in m.evidence]
        assert len(ids) == len(set(ids))
        for e in m.evidence:
            assert 1 <= e.start_line <= e.end_line and e.snippet and e.file == m.file


# ---- determinism and serialisation ------------------------------------------------------------


def test_building_twice_gives_identical_output(java_analyzer: JavaParserAnalyzer) -> None:
    analysis = analyze_paths(java_analyzer, FIXTURES)
    a = build_knowledge(analysis).repository.model_dump_json()
    b = build_knowledge(analysis).repository.model_dump_json()
    assert a == b


def test_models_round_trip_through_json(fixtures: BuiltKnowledge) -> None:
    m = method(fixtures, "InvoiceBatch#process(List<Row>,int,boolean)")
    assert MethodKnowledge.model_validate_json(m.model_dump_json()) == m
    repo = RepositoryKnowledge.model_validate_json(fixtures.repository.model_dump_json())
    assert repo.stats == fixtures.repository.stats
    assert repo.method(m.id) is not None and repo.method(m.method_id) is not None
    assert repo.method("nope") is None


def test_unresolved_calls_stay_visible_in_callees(project: BuiltKnowledge) -> None:
    problems = method(project, "PricingService#problems(Customer)")
    statuses = {c.status for c in problems.callees}
    assert {
        ResolutionStatus.RESOLVED,
        ResolutionStatus.AMBIGUOUS,
        ResolutionStatus.UNRESOLVED,
    } <= statuses
    assert [u.kind for u in problems.unknowns].count("unresolved_call") >= 2
