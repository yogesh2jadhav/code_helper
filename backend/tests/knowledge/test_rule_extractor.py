"""Phase 7: business-rule candidates."""

from __future__ import annotations

import pytest

from app.analyzer import JavaParserAnalyzer
from app.analyzer.control_flow import build_control_flow
from app.analyzer.rule_extractor import (
    RuleCandidate,
    explain_condition,
    extract_rules,
    lambda_body,
    parse_atom,
    split_call,
    split_condition,
    split_ternary,
)
from tests.conftest import FIXTURES
from tests.helpers import Analysis, ByMethod, analyze_paths, analyze_sources


def _build(method, method_id, refs, _params):  # type: ignore[no-untyped-def]
    flow = build_control_flow(method, method_id, refs)
    return extract_rules(method, method_id, flow, f"{method_id.split('#')[0]}.java")


class Rules(ByMethod[list[RuleCandidate]]):
    def __init__(self, analysis: Analysis) -> None:
        super().__init__(analysis, _build)


def kinds(rules: list[RuleCandidate]) -> list[str]:
    return [r.kind for r in rules]


# ---- condition parsing ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind", "meaning", "literals"),
    [
        ("parcel == null", "null", "parcel is missing", []),
        ("null != x", "null", "x is present", []),
        ("days > 14", "threshold", "days is greater than 14", ["14"]),
        ("14 <= days", "threshold", "14 is at most days", ["14"]),
        ("rate >= 0.5", "threshold", "rate is at least 0.5", ["0.5"]),
        ('region.equals("EU")', "literal_match", 'region equals "EU"', ['"EU"']),
        ('"CLOSED".equals(status)', "literal_match", '"CLOSED" equals status', ['"CLOSED"']),
        (
            "status == Status.ACTIVE",
            "literal_match",
            "status equals Status.ACTIVE",
            ["Status.ACTIVE"],
        ),
        ("flag == true", "literal_match", "flag equals true", ["true"]),
        ("a > b", "comparison", "a is greater than b", []),
        ("a.isAfter(b)", "comparison", "a is after b", []),
        ("a.compareTo(b)", "comparison", "compare a with b", []),
        ("items.isEmpty()", "test", "items is empty", []),
        ('name.startsWith("X")', "literal_match", 'name starts with "X"', ['"X"']),
        ("isEligible(c)", "test", "isEligible(c)", []),
    ],
)
def test_atoms_are_classified_and_described(
    text: str, kind: str, meaning: str, literals: list[str]
) -> None:
    atom = parse_atom(text)
    assert (atom.kind, atom.meaning, atom.literals) == (kind, meaning, literals)


def test_negation_keeps_the_classification() -> None:
    atom = parse_atom('!"CLOSED".equals(v.status())')
    assert atom.kind == "literal_match" and atom.meaning == 'not ("CLOSED" equals v.status())'
    assert parse_atom("!(a > b)").meaning == "not (a is greater than b)"
    assert parse_atom("x != y").kind == "comparison"  # `!=` is not a negation prefix


def test_operators_inside_calls_do_not_split_or_classify() -> None:
    assert split_condition("a(x && y) && b") == [("", "a(x && y)"), ("&&", "b")]
    assert split_condition('s.equals("a || b") || t') == [("", 's.equals("a || b")'), ("||", "t")]
    assert split_condition("(a && b)") == [("", "a && b")]
    atom = parse_atom("list.stream().anyMatch(x -> x > 3)")
    assert atom.kind == "test"  # the `>` is inside the call


def test_compound_conditions_explain_every_part() -> None:
    atoms, meaning = explain_condition(
        'row.amount() > threshold && row.region().equals("EU") || force'
    )
    assert [a.kind for a in atoms] == ["comparison", "literal_match", "test"]
    assert meaning == (
        'row.amount() is greater than threshold AND row.region() equals "EU" OR force'
    )


def test_call_and_ternary_splitting() -> None:
    assert split_call('"CLOSED".equals(v.status())') == ('"CLOSED"', "equals", "v.status()")
    assert split_call("a.b().equals(x)") == ("a.b()", "equals", "x")
    assert split_call("plain") is None
    assert split_ternary('x != null ? x : "n/a"') == ("x != null", "x", '"n/a"')
    assert split_ternary('f(a ? 1 : 2) ? "y" : "n"') == ("f(a ? 1 : 2)", '"y"', '"n"')
    assert split_ternary("no ternary here") is None


def test_lambda_bodies_and_method_references() -> None:
    assert lambda_body("v -> v.days() > 14") == "v.days() > 14"
    assert lambda_body("(a) -> { return a > 3; }") == "a > 3"
    assert lambda_body("Objects::nonNull") == "element != null"
    assert lambda_body("Visit::isOpen") == "Visit::isOpen(element)"


# ---- extraction on the fixtures ---------------------------------------------------------------


@pytest.fixture(scope="module")
def fixtures(java_analyzer: JavaParserAnalyzer) -> Rules:
    return Rules(analyze_paths(java_analyzer, FIXTURES))


def test_decision_chain_becomes_one_candidate_per_branch(fixtures: Rules) -> None:
    rules = fixtures("ShippingPolicy#classify(Parcel,boolean)")
    decisions = [r for r in rules if r.condition]
    assert [(r.kind, r.meaning) for r in decisions] == [
        ("null_handling", "parcel is missing"),
        (
            "threshold",
            "parcel.weightKg() is greater than 30 OR parcel.lengthCm() is greater than 150",
        ),
        ("threshold", "express AND parcel.weightKg() is at most 50"),
        ("threshold", "parcel.weightKg() is at least 10"),
        ("decision", "express"),  # the ternary `express ? "STANDARD_EXPRESS" : "STANDARD"`
    ]
    first = decisions[0]
    assert first.confidence == "high" and first.action == 'return "INVALID"'
    assert first.otherwise == "otherwise test: parcel.weightKg() > 30 || parcel.lengthCm() > 150"
    nested = decisions[2]
    assert (nested.action, nested.otherwise) == ('return "FREIGHT_EXPRESS"', 'return "FREIGHT"')
    assert decisions[1].action == "decide on express && parcel.weightKg() <= 50"
    ternary = decisions[4]
    assert ternary.action == '"STANDARD_EXPRESS" when true, else "STANDARD"'
    assert ternary.confidence == "medium"


def test_magic_numbers_are_recorded_as_unestablished_literals(fixtures: Rules) -> None:
    rules = fixtures("ShippingPolicy#classify(Parcel,boolean)")
    literals = {x for r in rules for x in r.literals}
    assert {"30", "150", "50", "10"} <= literals


def test_throw_guard_is_a_validation(fixtures: Rules) -> None:
    (rule,) = [r for r in fixtures("SwitchSamples#requirePositive(int)") if r.condition]
    assert rule.kind == "validation" and rule.meaning == "value is at most 0"
    assert rule.action == 'throw new IllegalArgumentException("value must be positive")'
    assert rule.literals == ["0"]


def test_classic_switch_lists_branches_with_actions(fixtures: Rules) -> None:
    (rule,) = [r for r in fixtures("SwitchSamples#legacyRate(String)") if r.kind == "switch"]
    assert rule.decision == "category" and rule.literals == ['"A"', '"B"', '"C"']
    assert [(b.when, b.action) for b in rule.branches] == [
        ('"A"', "set rate = 10; stop the loop/switch"),
        ('"B"', "falls through"),
        ('"C"', "set rate = 20; stop the loop/switch"),
        ("default", "set rate = 0"),
    ]


def test_switch_expression_is_found_through_its_return(fixtures: Rules) -> None:
    (rule,) = [r for r in fixtures("SwitchSamples#modernLabel(int)") if r.kind == "switch"]
    assert rule.decision == "level"
    assert [b.when for b in rule.branches] == ["1", "2", "default"]


def test_stream_filter_group_and_pipeline_candidates(fixtures: Rules) -> None:
    rules = fixtures("StreamSamples#openVisitsByFacility(List<Visit>)")
    assert kinds(rules) == ["filter", "filter", "grouping", "pipeline"]
    exclude = next(r for r in rules if r.kind == "filter" and "CLOSED" in (r.condition or ""))
    assert exclude.meaning == 'not ("CLOSED" equals v.status())'
    assert exclude.literals == ['"CLOSED"'] and exclude.scope == "visits"
    present = next(r for r in rules if r.kind == "filter" and r.condition == "element != null")
    assert present.meaning == "element is present"
    group = next(r for r in rules if r.kind == "grouping")
    assert group.calculation == "group visits by Visit::facility"
    pipeline = rules[-1]
    assert [s.step for s in pipeline.steps] == ["source", "filter", "filter", "group"]
    assert pipeline.steps[1].description == "keep elements where element != null"


def test_aggregation_through_collectors_and_stream_terminals(fixtures: Rules) -> None:
    by_status = fixtures("StreamSamples#countByStatus(List<Visit>)")
    assert [r.calculation for r in by_status if r.kind == "aggregation"] == [
        "count (collector counting)"
    ]
    longest = fixtures("StreamSamples#longestStay(List<Visit>)")
    assert [r.calculation for r in longest if r.kind == "aggregation"] == ["maximum"]
    batch = fixtures("InvoiceBatch#process(List<Row>,int,boolean)")
    assert "sum (collector summingDouble)" in [
        r.calculation for r in batch if r.kind == "aggregation"
    ]


def test_zero_comment_method_still_yields_meaningful_rules(fixtures: Rules) -> None:
    rules = fixtures("InvoiceBatch#process(List<Row>,int,boolean)")
    assert [(r.start_line, r.kind) for r in rules] == [
        (12, "null_handling"),
        (13, "validation"),
        (18, "literal_match"),
        (24, "aggregation"),
        (24, "grouping"),
        (26, "filter"),
        (26, "pipeline"),
    ]
    null_rule, validation, literal, aggregation, grouping, filt, pipeline = rules
    assert null_rule.meaning == "row.account() is missing"
    assert null_rule.action == "decide on strict; skip to the next iteration"
    assert validation.condition == "strict"
    assert validation.action == 'throw new IllegalStateException("missing account")'
    assert literal.meaning == (
        'row.amount() is greater than threshold AND row.region() equals "EU"'
    )
    assert literal.literals == ['"EU"'] and literal.confidence == "high"
    assert literal.action == "update kept via add(); call withAmount(); call amount()"
    assert literal.otherwise == "update kept via add()"
    assert aggregation.calculation == "sum (collector summingDouble)"
    assert (grouping.scope, grouping.calculation) == ("kept", "group kept by Row::account")
    assert filt.meaning == "s.total() is greater than 0" and filt.literals == ["0"]
    assert [s.step for s in pipeline.steps] == ["source", "map", "filter", "collect"]


def test_every_rule_points_at_real_source(fixtures: Rules) -> None:
    for method_id, rules in fixtures.results.items():
        for r in rules:
            assert r.method_id == method_id and r.evidence
            ref = r.evidence[0]
            assert ref.start_line <= ref.end_line and ref.snippet, (method_id, r.kind, ref)
            assert len(r.id) == 12
    ids = [r.id for rules in fixtures.results.values() for r in rules]
    assert len(ids) == len(set(ids))


# ---- patterns that need purpose-built sources -------------------------------------------------

SRC = """package p;
import java.util.*;
public class Patterns {
    static final int LIMIT = 100;

    String name(String raw, Optional<String> opt, Integer count) {
        if (raw == null) {
            raw = "n/a";
        }
        String o = opt.orElse("none");
        String t = raw != null ? raw : "x";
        int capped = Math.min(count, LIMIT);
        Objects.requireNonNull(count);
        String u = opt.orElseThrow();
        return t + o + u + capped;
    }

    boolean tiered(int age, String tier, List<String> tags) {
        if (age >= 65 && tier.equals("GOLD") || tags.contains("vip")) {
            return true;
        } else if (age < 18) {
            return false;
        }
        return tags.stream().anyMatch(x -> x.length() > 3);
    }
}
"""


@pytest.fixture(scope="module")
def custom(java_analyzer: JavaParserAnalyzer, tmp_path_factory: pytest.TempPathFactory) -> Rules:
    a = analyze_sources(java_analyzer, tmp_path_factory.mktemp("rules"), {"p/Patterns.java": SRC})
    return Rules(a)


def test_null_guard_that_assigns_is_a_default_value(custom: Rules) -> None:
    rules = custom("Patterns#name(String,Optional<String>,Integer)")
    default = next(r for r in rules if r.kind == "default_value" and r.condition == "raw == null")
    assert default.meaning == "raw is missing" and default.action == 'set raw = "n/a"'


def test_default_calls_and_ternary_defaults(custom: Rules) -> None:
    rules = custom("Patterns#name(String,Optional<String>,Integer)")
    calls = [r for r in rules if r.kind == "default_value" and r.calculation]
    assert [r.calculation for r in calls] == ['falls back to "none" when absent']
    assert calls[0].literals == ['"none"'] and calls[0].scope == "opt"
    ternary = next(r for r in rules if r.kind == "default_value" and r.condition == "raw != null")
    assert ternary.action == 'raw when true, else "x"' and ternary.literals == ['"x"']


def test_aggregation_and_validation_calls(custom: Rules) -> None:
    rules = custom("Patterns#name(String,Optional<String>,Integer)")
    (agg,) = [r for r in rules if r.kind == "aggregation"]
    assert agg.calculation == "minimum of count, LIMIT"
    validations = [r for r in rules if r.kind == "validation"]
    assert {r.meaning for r in validations} == {
        "requireNonNull check on count",
        "orElseThrow check on opt",
    }


def test_compound_conditions_and_else_if(custom: Rules) -> None:
    rules = custom("Patterns#tiered(int,String,List<String>)")
    first, second, any_match = rules
    assert [a.kind for a in first.atoms] == ["threshold", "literal_match", "literal_match"]
    assert first.meaning == 'age is at least 65 AND tier equals "GOLD" OR tags contains "vip"'
    assert first.literals == ["65", '"GOLD"', '"vip"']
    assert first.otherwise == "otherwise test: age < 18"
    assert second.meaning == "age is less than 18" and second.kind == "threshold"
    assert (any_match.kind, any_match.condition) == ("filter", "x.length() > 3")
    assert any_match.scope == "tags" and any_match.literals == ["3"]
    assert any_match.action == "true if any element of tags satisfies this"


def test_rule_summaries_read_naturally(custom: Rules) -> None:
    first = next(r for r in custom("Patterns#tiered(int,String,List<String>)") if r.condition)
    assert first.summary.startswith("threshold: when age is at least 65")
    assert "-> return true" in first.summary


def test_candidates_are_json_round_trippable(fixtures: Rules) -> None:
    rule = fixtures("StreamSamples#openVisitsByFacility(List<Visit>)")[-1]
    assert RuleCandidate.model_validate_json(rule.model_dump_json()) == rule
