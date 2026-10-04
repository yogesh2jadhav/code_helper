"""The evaluation framework: the metrics must catch bad answers, and the shipped benchmarks must
stay at the quality they were measured at (a drop is a regression, a rise should be recorded)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import Settings, get_settings
from app.evaluation.benchmark import Benchmark, ExpectedMethod
from app.evaluation.metrics import MethodMetrics, Ratio, evaluate_method
from app.evaluation.report import full_report, method_report, summary, to_json
from app.evaluation.runner import run_benchmark
from app.explain.explanation_service import ExplainOptions, ExplanationService
from tests.explain.test_explanation_service import FakeLLM
from tests.helpers import Env

BENCHMARKS = Path(__file__).parents[3] / "benchmarks"


# ---- the metrics catch what they claim to catch ------------------------------------------------


def test_ratio_percent() -> None:
    assert (
        Ratio(3, 4).percent == 75.0 and Ratio(0, 0).percent is None and Ratio(0, 2).percent == 0.0
    )


def explain_with(env: Env, settings: Settings, answer: str):  # type: ignore[no-untyped-def]
    method = env.method("shippingFee")
    service = ExplanationService(
        env.repo, env.store, env.reader, settings, FakeLLM(answer), env.retriever
    )
    knowledge = env.store.get_method(method.id)
    assert knowledge is not None
    source = env.reader.lines(knowledge.file, knowledge.start_line, knowledge.end_line) or ""
    return knowledge, source, service.explain(method.id, ExplainOptions())


EXPECTED = ExpectedMethod(
    method="shop.ShippingCalculator#shippingFee(double,boolean)",
    inputs=["double weightKg", "boolean express"],
    output="double",
    stages=["if", "return"],
    rules=[{"kind": "threshold", "literal": "30"}],  # type: ignore[list-item]
    data_flow=[("weightKg", "return")],
    unknowns=["why 30 was chosen"],
)


def test_invented_identifiers_numbers_and_citations_are_counted(
    env: Env, isolated_settings: Settings
) -> None:
    answer = (
        "## What this method does\n- It prices parcels using `calculateTax` [E1].\n"
        "## Important rules\n- Parcels above 30 kg pay 17 extra, per `weightKg` [E1][E77].\n"
    )
    knowledge, source, result = explain_with(env, isolated_settings, answer)
    metrics = evaluate_method(knowledge, source, result, EXPECTED)
    assert metrics.unsupported_claims == [
        "`calculateTax` (What this method does)",
        "number 17 (Important rules)",
    ]
    assert metrics.checked_claims == 4  # calculateTax, weightKg, 30, 17
    assert metrics.invalid_citations == 1  # E77 does not exist
    assert metrics.hallucination_rate == 50.0
    assert metrics.answer_source == "llm"


def test_a_grounded_answer_has_no_unsupported_claims(env: Env, isolated_settings: Settings) -> None:
    answer = (
        "## What this method does\n- It adds 20 when `weightKg` exceeds 30 [E1].\n"
        "## Output\n- Returns `fee` doubled when `express` is set [E1].\n"
    )
    knowledge, source, result = explain_with(env, isolated_settings, answer)
    metrics = evaluate_method(knowledge, source, result, EXPECTED)
    assert metrics.unsupported_claims == [] and metrics.checked_claims == 5
    assert metrics.evidence_cited == metrics.evidence_lines == 2
    assert metrics.hallucination_rate == 0.0 and metrics.invalid_citations == 0


def test_language_words_need_no_support_but_invented_exceptions_do(
    env: Env, isolated_settings: Settings
) -> None:
    answer = (
        "## What to be careful about\n"
        "- It can return `true` or `false` or `null` [E1].\n"
        "- It may throw `NumberFormatException` [E1].\n"
    )
    knowledge, source, result = explain_with(env, isolated_settings, answer)
    metrics = evaluate_method(knowledge, source, result, EXPECTED)
    assert metrics.unsupported_claims == ["`NumberFormatException` (What to be careful about)"]
    assert metrics.checked_claims == 4


def test_uncited_claim_lines_lower_evidence_coverage(env: Env, isolated_settings: Settings) -> None:
    answer = "## Output\n- Returns the fee [E1].\n- Also returns something else.\n"
    knowledge, source, result = explain_with(env, isolated_settings, answer)
    metrics = evaluate_method(knowledge, source, result, EXPECTED)
    assert (metrics.evidence_cited, metrics.evidence_lines) == (1, 2)
    assert metrics.evidence_coverage == 50.0


def test_missing_expectations_are_listed_not_hidden(env: Env, isolated_settings: Settings) -> None:
    wrong = EXPECTED.model_copy(
        update={
            "inputs": ["double weightKg", "String nope"],
            "output": "int",
            "stages": ["if", "loop"],
            "data_flow": [("express", "weightKg")],
            "unknowns": ["why 30 was chosen", "a statement nobody makes"],
            "dependencies": ["Ghost.call"],
        }
    )
    knowledge, source, result = explain_with(env, isolated_settings, GOOD_ENOUGH)
    m = evaluate_method(knowledge, source, result, wrong)
    assert m.structure.missing == [
        "input 'String nope'",
        "output 'int' (got 'double')",
        "stage 'loop'",
    ]
    assert m.structure.found == 2 and m.structure.expected == 5
    assert m.structure_extra == ["stage 'return'"]
    assert m.data_flow.missing == ["express -> weightKg"]
    assert m.unknowns.missing == ["a statement nobody makes"] and m.unknowns.found == 1
    assert m.dependencies.missing == ["Ghost.call"]


GOOD_ENOUGH = "## Output\n- Returns the fee [E1].\n"


# ---- reports -----------------------------------------------------------------------------------


def test_report_lists_every_metric_and_never_a_single_score(
    env: Env, isolated_settings: Settings
) -> None:
    knowledge, source, result = explain_with(env, isolated_settings, GOOD_ENOUGH)
    m = evaluate_method(knowledge, source, result, EXPECTED)
    text = method_report(m)
    for label in (
        "Purpose coverage",
        "Structural coverage",
        "Data-flow coverage",
        "Dependency coverage",
        "Rule coverage",
        "Unknowns correctly stated",
        "Unsupported claims",
        "Hallucination rate",
        "Evidence coverage",
    ):
        assert label in text
    assert "score" not in text.lower() and "overall" not in text.lower()
    data = json.loads(to_json(_run_of(m)))
    assert set(data["methods"][0]) >= {"purpose", "structure", "data_flow", "hallucination_rate"}
    assert "score" not in json.dumps(data).lower()


def _run_of(m: MethodMetrics):  # type: ignore[no-untyped-def]
    from app.evaluation.runner import EvaluationRun

    return EvaluationRun(benchmark="x", mode="analysis", methods=[m])


# ---- the shipped benchmarks --------------------------------------------------------------------


def test_benchmark_files_load_and_name_real_methods() -> None:
    for name in ("basic", "enterprise"):
        bench = Benchmark.load(BENCHMARKS / name)
        assert bench.methods and bench.source_root(BENCHMARKS / name).is_dir()
        assert len({m.method for m in bench.methods}) == len(bench.methods)
    basic = Benchmark.load(BENCHMARKS / "basic")
    assert {m.method.split("#")[0].rsplit(".", 1)[-1] for m in basic.methods} >= {
        "Cart",
        "Grader",
        "Stats",
        "Orders",
        "Router",
        "Pipeline",
        "Converter",
        "Discounts",
        "Ledger",
        "Throttle",
    }  # fixtures A-J


@pytest.fixture(scope="module")
def basic_run(java_analyzer, tmp_path_factory):  # type: ignore[no-untyped-def]
    return _run(java_analyzer, tmp_path_factory, "basic")


@pytest.fixture(scope="module")
def enterprise_run(java_analyzer, tmp_path_factory):  # type: ignore[no-untyped-def]
    return _run(java_analyzer, tmp_path_factory, "enterprise")


def _run(java_analyzer, tmp_path_factory, name):  # type: ignore[no-untyped-def]
    data = tmp_path_factory.mktemp(f"eval-{name}")
    settings = get_settings().model_copy(update={"data_root": data, "source_root": None})
    return run_benchmark(BENCHMARKS / name, settings)


def missing_of(run, field: str) -> dict[str, list[str]]:  # type: ignore[no-untyped-def]
    return {
        m.method.split("#")[1]: getattr(m, field).missing
        for m in run.methods
        if getattr(m, field).missing
    }


def test_basic_benchmark_measured_quality(basic_run) -> None:  # type: ignore[no-untyped-def]
    run = basic_run
    assert run.not_found == [] and len(run.methods) == 10
    for field in ("purpose", "structure", "data_flow", "rules", "unknowns"):
        # Nothing expected may be missing except the known analyzer gap listed below.
        assert missing_of(run, field) == {}, field
    # Known gap: lambda parameter types are not inferred, so `o.isCancelled()` stays unresolved.
    assert missing_of(run, "dependencies") == {
        "revenueByRegion(List<Order>)": ["Order.isCancelled"]
    }
    assert sum(len(m.unsupported_claims) for m in run.methods) == 0
    assert sum(m.invalid_citations for m in run.methods) == 0
    cited = sum(m.evidence_cited for m in run.methods)
    lines = sum(m.evidence_lines for m in run.methods)
    assert cited / lines >= 0.80


def test_zero_comment_fixtures_are_explained_structurally_and_honestly(basic_run) -> None:  # type: ignore[no-untyped-def]
    by = {m.method.split("#")[1]: m for m in basic_run.methods}
    for name in ("settle(List<Double>,double)", "grade(int,boolean)", "limit(int)"):
        m = by[name]
        assert m.structure.percent == 100.0 and m.purpose.percent == 100.0
        assert m.unknowns.found == m.unknowns.expected >= 1  # states what is not established
    throttle = by["limit(int)"]
    assert throttle.rules.percent == 100.0 and throttle.unknowns.missing == []  # "why 8472"


def test_enterprise_benchmark_measured_quality(enterprise_run) -> None:  # type: ignore[no-untyped-def]
    run = enterprise_run
    assert run.not_found == [] and len(run.methods) == 4
    for field in ("purpose", "structure", "data_flow", "dependencies", "unknowns"):
        assert missing_of(run, field) == {}, field
    # Known gap: a comparison inside a lambda (`c -> c.getAmount() > 14`) is not a threshold rule.
    assert missing_of(run, "rules") == {
        "bill(Account,List<Shipment>,boolean,double)": ["rule threshold 14"]
    }
    assert sum(len(m.unsupported_claims) for m in run.methods) == 0


def test_summary_and_full_report(basic_run) -> None:  # type: ignore[no-untyped-def]
    text = summary(basic_run)
    assert text.startswith("Benchmark: basic   mode: analysis   methods: 10")
    assert "Dependency coverage:        92%" in text and "Unsupported claims:" in text
    assert "Method: bench.Cart#calculateTotal(double,int)" in full_report(basic_run)
