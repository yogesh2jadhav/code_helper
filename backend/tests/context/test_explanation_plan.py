from __future__ import annotations

import pytest

from app.analyzer import JavaParserAnalyzer
from app.context.citations import CitationBook
from app.context.explanation_plan import ExplanationPlan, build_plan
from app.knowledge.builder import BuiltKnowledge
from app.knowledge.models import MethodKnowledge
from tests.conftest import FIXTURES, PROJECT
from tests.helpers import Env, analyze_paths, analyze_sources, build_knowledge


@pytest.fixture(scope="module")
def fixtures(java_analyzer: JavaParserAnalyzer) -> BuiltKnowledge:
    return build_knowledge(analyze_paths(java_analyzer, FIXTURES))


@pytest.fixture(scope="module")
def project(java_analyzer: JavaParserAnalyzer) -> BuiltKnowledge:
    return build_knowledge(analyze_paths(java_analyzer, PROJECT))


def get(b: BuiltKnowledge, suffix: str) -> MethodKnowledge:
    found = [m for m in b.repository.methods if m.method_id.endswith(suffix)]
    assert len(found) == 1, f"{suffix}: {len(found)}"
    return found[0]


def stages(plan: ExplanationPlan) -> list[tuple[str, str]]:
    return [(s.kind, s.title) for s in plan.major_stages]


# ---- identity, purpose, claims on the documented shop method -----------------------------------


def test_documented_method_plan(env: Env) -> None:
    plan = build_plan(env.method("shippingFee"))
    assert plan.method_identity == (
        "shop.ShippingCalculator.shippingFee(double,boolean) [public method]"
        " in src/main/shop/ShippingCalculator.java:6-12"
    )
    assert (plan.purpose.basis, plan.purpose.level) == ("comment", "fact")
    assert plan.purpose.text == "Returns the shipping fee; heavy parcels cost extra."
    assert stages(plan) == [
        ("decision", "Decide on weightKg > 30"),
        ("return", "Produce the result"),
    ]
    assert plan.major_stages[0].description == "if weightKg > 30: set fee += 20"
    assert plan.major_stages[1].description == "returns express ? fee * 2 : fee"
    assert [(r.kind, r.lines, r.literals) for r in plan.business_rule_candidates] == [
        ("threshold", "8-10", ["30"]),
        ("decision", "11", []),
    ]
    assert plan.input_summary[0].startswith(
        "double weightKg: ShippingCalculatorTest.heavyParcelsCostExtra() L6"
    )
    assert plan.output_summary == ["returns double: parameter express; local fee"]


def test_history_comes_from_comments_tests_and_docs_only(env: Env) -> None:
    plan = build_plan(env.method("shippingFee"))
    assert len(plan.historical_context) == 2
    assert plan.historical_context[0].startswith(
        'tested by ShippingCalculatorTest.heavyParcelsCostExtra ("heavy parcels cost extra") [E'
    )
    assert (
        'comment (documents) at L5: "Returns the shipping fee; heavy parcels cost extra."'
        in plan.historical_context[1]
    )
    bare = build_plan(env.method("render"))
    assert bare.historical_context == [
        "no comment, test or documentation mentions this method (git history is not analyzed)"
    ]


def test_claims_are_classified_and_never_unsupported(env: Env) -> None:
    plan = build_plan(env.method("shippingFee"))
    by_level: dict[str, list[str]] = {"fact": [], "inference": [], "unknown": []}
    for c in plan.claims:
        by_level[c.level].append(c.text)
    assert (
        "shippingFee takes double weightKg, boolean express and returns double." in by_level["fact"]
    )
    assert (
        "A comment states its purpose: Returns the shipping fee; heavy parcels cost extra."
        in by_level["fact"]
    )
    assert (
        "At L8 the code applies this check or calculation: weightKg is greater than 30."
        in by_level["fact"]
    )
    assert by_level["inference"] == ["L8 looks like a threshold business rule."]
    assert by_level["unknown"] == [
        "The repository does not establish why 30 was chosen (no comment or test mentions it)."
    ]
    labels = {c.label for c in plan.citations}
    assert all(c.refs and set(c.refs) <= labels for c in plan.claims)  # every claim is traceable


def test_undocumented_purpose_is_an_inference_with_an_unknown(env: Env) -> None:
    plan = build_plan(env.method("render"))
    assert (plan.purpose.basis, plan.purpose.level) == ("name", "inference")
    levels = {c.level for c in plan.claims}
    assert levels == {"fact", "inference", "unknown"}
    assert any(
        "Judging by its name and structure" in c.text and c.level == "inference"
        for c in plan.claims
    )
    assert any(
        "no comment states the purpose" in c.text.lower()
        for c in plan.claims
        if c.level == "unknown"
    )


# ---- stages -----------------------------------------------------------------------------------


def test_loop_and_return(fixtures: BuiltKnowledge) -> None:
    plan = build_plan(get(fixtures, "LoopSamples#sumPositives(List<Integer>)"))
    assert stages(plan) == [
        ("loop", "Loop (for) int i = 0; i < values.size(); i++"),
        ("return", "Produce the result"),
    ]
    assert plan.major_stages[0].description == "on each pass: decide on v <= 0; set total += v"
    assert plan.data_transformations == [
        "values -[receiver]-> get() -[declare]-> v -[compound_assign]-> total -[return]-> return value"
    ]


def test_zero_comment_method_is_explained_from_structure_alone(fixtures: BuiltKnowledge) -> None:
    plan = build_plan(get(fixtures, "InvoiceBatch#process(List<Row>,int,boolean)"))
    assert [s.kind for s in plan.major_stages] == ["loop", "pipeline", "return"]
    loop, pipe, ret = plan.major_stages
    assert loop.title == "Loop (foreach) Row row : rows"
    assert loop.description.startswith("on each pass: decide on row.account() == null;")
    assert (
        pipe.title == "Pipeline over kept"
        and pipe.description == "stream the elements -> group by Row::account"
    )
    assert "computed by a pipeline over byAccount.entrySet()" in ret.description
    assert plan.purpose.basis == "name" and plan.historical_context[0].startswith("no comment")
    chain = plan.data_transformations[0]
    assert chain.startswith(
        "rows -[iterate]-> row -[mutation]-> kept -[stream: stream, collect, groupingBy, summingDouble]-> collect()"
    )
    assert "-[declare]-> byAccount" in chain and chain.endswith("-> return value")
    deps = {d.name: d for d in plan.dependencies}
    assert deps["Row.withAmount"].purpose == "returns a copy with amount"
    assert "new Summary" in deps and deps["new Summary"].kind == "project"
    assert any(r.kind == "grouping" for r in plan.business_rule_candidates)


def test_switch_stage_lists_the_cases(fixtures: BuiltKnowledge) -> None:
    plan = build_plan(get(fixtures, "SwitchSamples#legacyRate(String)"))
    assert stages(plan) == [("switch", "Branch on category"), ("return", "Produce the result")]
    assert plan.major_stages[0].description == 'cases: "A", "B", "C", default'
    assert plan.data_transformations == [
        "rate: created -> modified -> returned"
    ]  # no parameter reaches it


def test_try_catch_finally_is_one_guarded_stage(fixtures: BuiltKnowledge) -> None:
    plan = build_plan(get(fixtures, "SwitchSamples#parseOrDefault(String,int)"))
    assert stages(plan) == [("guarded", "Guarded section")]
    assert plan.major_stages[0].description == (
        "does: return Integer.parseInt(text); catch NumberFormatException e: return fallback; finally: nothing"
    )


def test_abstract_methods_have_no_steps(project: BuiltKnowledge) -> None:
    plan = build_plan(get(project, "Auditable#audit()"))
    assert stages(plan) == [("other", "No steps")]
    assert plan.major_stages[0].description == "has no body (abstract or interface method)"
    assert plan.data_transformations == [] and plan.dependencies == []


def test_consecutive_calls_are_folded_into_one_setup_stage(project: BuiltKnowledge) -> None:
    plan = build_plan(get(project, "PricingService#overloads(Customer)"))
    assert [s.kind for s in plan.major_stages] == ["setup"]
    only = plan.major_stages[0]
    assert only.title.startswith("Call price(); Call price(); Call price()") and (
        only.start_line,
        only.end_line,
    ) == (38, 44)
    assert len(only.refs) == 8  # every folded step keeps its own citation


MANY = (
    "package p; public class Many { int f(int a) {\n"
    + "".join(f"  if (a == {i}) {{ return {i}; }}\n" for i in range(20))
    + "  return -1; } }\n"
)


def test_stage_count_is_capped_without_losing_the_tail(
    java_analyzer: JavaParserAnalyzer, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    built = build_knowledge(analyze_sources(java_analyzer, tmp_path, {"p/Many.java": MANY}))
    plan = build_plan(get(built, "Many#f(int)"))
    assert len(plan.major_stages) == 12
    last = plan.major_stages[-1]
    assert last.title == "10 further steps" and last.end_line == 22
    assert last.description.count("Decide on") + last.description.count("Produce") >= 9


# ---- dependencies, risks and citations --------------------------------------------------------


def test_dependencies_distinguish_project_ambiguous_and_external(project: BuiltKnowledge) -> None:
    plan = build_plan(get(project, "PricingService#problems(Customer)"))
    assert {d.kind for d in plan.dependencies} == {"project", "ambiguous"}
    ambiguous = [d for d in plan.dependencies if d.kind == "ambiguous"]
    assert ambiguous and all("candidates" in (d.note or "") for d in ambiguous)
    external = build_plan(get(project, "PricingService#external(Customer)"))
    assert {"java.util.List", "java.io.PrintStream"} <= {
        d.name for d in external.dependencies if d.kind == "external"
    }
    assert all(
        d.note == "library/JDK type; members not verified"
        for d in external.dependencies
        if d.kind == "external"
    )


def test_risks_and_unknowns_are_listed_with_lines(
    java_analyzer: JavaParserAnalyzer, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    src = (
        "package p; public class R { private static int n; int g(int a) {\n  n++;\n"
        "  try { return a; } catch (Exception e) { }\n  return a > 41 ? 1 : 2; } }\n"
    )
    plan = build_plan(
        get(
            build_knowledge(analyze_sources(java_analyzer, tmp_path, {"p/R.java": src})), "R#g(int)"
        )
    )
    assert any(
        r.startswith("[high] writes static field n") and r.endswith("(L2)") for r in plan.risks
    )
    assert any("catches Exception" in r for r in plan.risks) and any(
        "does nothing" in r for r in plan.risks
    )
    assert any("why 41 was chosen" in u for u in plan.unknowns)


def test_plan_reuses_context_citations_so_labels_agree(env: Env) -> None:
    from app.config import get_settings
    from app.context.context_builder import ContextBuilder

    method = env.method("shippingFee")
    pkg = ContextBuilder(env.repo, env.store, env.reader, get_settings()).build(method)
    plan = build_plan(method, pkg.citations)
    context = {c.label: (c.file, c.start_line, c.end_line, c.source_type) for c in pkg.citations}
    shared = {
        c.label: (c.file, c.start_line, c.end_line, c.source_type)
        for c in plan.citations
        if c.label in context
    }
    assert shared == context  # same label, same location, in prompt and plan
    assert len(plan.citations) > len(pkg.citations)  # the plan adds finer-grained ones after them
    labels = [c.label for c in plan.citations]
    assert labels == [f"E{n}" for n in range(1, len(labels) + 1)]


def test_citation_book_is_stable() -> None:
    book = CitationBook()
    a = book.cite("A.java", 1, 5, "source_code")
    assert book.cite("A.java", 1, 5, "source_code") is a and a.label == "E1"
    assert book.cite("A.java", 1, 5, "test").label == "E2"  # same range, different kind
    assert a.location == "A.java:1-5" and book.cite("A.java", 7, 7).location == "A.java:7"
    rebuilt = CitationBook.from_citations(book.citations)
    assert rebuilt.cite("A.java", 1, 5, "source_code").label == "E1"
    assert rebuilt.cite("B.java", 1, 1).label == "E4"
    assert book.get("E2") is not None and book.get("E99") is None


def test_plan_round_trips_and_is_deterministic(fixtures: BuiltKnowledge) -> None:
    m = get(fixtures, "InvoiceBatch#process(List<Row>,int,boolean)")
    plan = build_plan(m)
    assert ExplanationPlan.model_validate_json(plan.model_dump_json()) == plan
    assert build_plan(m).model_dump_json() == plan.model_dump_json()
