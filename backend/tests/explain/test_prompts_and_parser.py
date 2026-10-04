from __future__ import annotations

import pytest

from app.context.explanation_plan import build_plan
from app.explain.mentor_format import render_mentor_answer
from app.llm.ollama_client import Message
from app.llm.prompts import (
    EXPLAIN_SECTIONS,
    SYSTEM_PROMPT,
    explain_prompt,
    followup_messages,
    render_plan_text,
    trace_prompt,
    why_prompt,
)
from app.llm.response_parser import parse_answer
from tests.helpers import Env

# ---- prompts ----------------------------------------------------------------------------------


def test_the_system_prompt_carries_the_non_negotiable_rules() -> None:
    for must in (
        "If evidence does not establish why something exists, say that the reason is not established.",
        "Never invent business intent",
        "FACT",
        "INFERENCE",
        "UNKNOWN",
        "Only use labels that appear in the material",
        "Start from the mental model",
        "Do not narrate Java syntax",
    ):
        assert must in SYSTEM_PROMPT.replace("\\\n", "")


def test_the_plan_text_keeps_evidence_levels_and_citations(env: Env) -> None:
    text = render_plan_text(build_plan(env.method("shippingFee")))
    assert text.startswith("Method: shop.ShippingCalculator.shippingFee(double,boolean)")
    assert "Purpose [FACT, comment]: Returns the shipping fee; heavy parcels cost extra. [E" in text
    assert (
        "[FACT] At L8 the code applies this check or calculation: weightKg is greater than 30. [E"
        in text
    )
    assert "[INFERENCE] L8 looks like a threshold business rule." in text
    assert "[UNKNOWN] The repository does not establish why 30 was chosen" in text
    assert (
        "(constants: 30)" in text
        and "Business-rule candidates (patterns in the code, not confirmed rules)" in text
    )


def test_explain_prompt_specifies_structure_and_includes_only_provided_material(env: Env) -> None:
    plan = build_plan(env.method("shippingFee"))
    prompt = explain_prompt(
        plan, "### Target method source [E1]\n6: public double shippingFee(...)"
    )
    for i, section in enumerate(EXPLAIN_SECTIONS, 1):
        assert f"{i}. {section}" in prompt
    assert prompt.index("1. What this method does") < prompt.index("9. Evidence and unknowns")
    assert (
        "### Target method source [E1]" in prompt and "say its reason is not established" in prompt
    )
    assert "InvoicePrinter" not in prompt  # nothing outside the target's own material


def test_other_prompts_ask_for_their_own_structure() -> None:
    trace = trace_prompt("a -> b", "ctx", "customer")
    assert "`customer`" in trace and '"Where it comes from"' in trace and "a -> b" in trace
    why = why_prompt("if (x > 30)", "evidence", "ctx")
    assert '"Confirmed", "Likely interpretation", "Unknown"' in why
    assert "Do not invent a historical or business reason" in why.replace("\\\n", "")


def test_followups_keep_the_method_context_and_the_conversation() -> None:
    history = [
        Message(role="user", content="What does it do?"),
        Message(role="assistant", content="It prices."),
    ]
    messages = followup_messages(history, "And for express?", "SOURCE", "SUMMARY")
    assert [m.role for m in messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert messages[0].content == SYSTEM_PROMPT
    assert "SUMMARY" in messages[1].content and "SOURCE" in messages[1].content
    assert messages[-1] == Message(role="user", content="And for express?")
    assert messages[3:5] == history


# ---- response parsing -------------------------------------------------------------------------

GOOD = """\
## 1. What this method does
It prices a parcel. [E1]

## 2. High-level flow
weight check
↓
fee

## Inputs
- weightKg [E2]

**Major processing stages**
1. Check weight [E1, E2]

### Important rules
- Over 30 kg costs extra [E3]. The reason for 30 is not established.

## Important dependencies
None.

## Output
A double.

## What to be careful about:
Nothing.

## Evidence and unknowns
FACT: threshold at L8.
"""


def test_a_well_formed_answer_is_split_into_the_expected_sections() -> None:
    parsed = parse_answer(GOOD, {"E1", "E2", "E3"})
    assert [s.key for s in parsed.sections] == EXPLAIN_SECTIONS
    assert parsed.missing_sections == [] and parsed.invalid_citations == []
    assert parsed.citations_used == [
        "E1",
        "E2",
        "E3",
    ]  # in order of first use, multi-labels expanded
    assert parsed.section("What this method does") is not None
    assert parsed.section("Inputs").text == "- weightKg [E2]"  # type: ignore[union-attr]
    assert parsed.states_unknowns is True


def test_invented_citations_and_missing_sections_are_reported() -> None:
    text = "## What this method does\nIt does things [E1][E9].\n\n## Output\nA value [E7, E1]."
    parsed = parse_answer(text, {"E1", "E2"})
    assert parsed.invalid_citations == ["E9", "E7"]
    assert parsed.missing_sections == [
        s for s in EXPLAIN_SECTIONS if s not in ("What this method does", "Output")
    ]
    assert not parsed.states_unknowns


def test_preamble_unknown_headings_and_reasoning_blocks() -> None:
    text = "<think>planning</think>\nSure, here you go.\n\n## What this method does\nFoo.\n\n## Fun facts\nBar."
    parsed = parse_answer(text, set())
    assert parsed.sections[0].key == "preamble" and parsed.sections[0].text == "Sure, here you go."
    assert [s.key for s in parsed.sections][1:] == ["What this method does"]
    assert "planning" not in parsed.raw
    assert "Fun facts" in parsed.sections[1].text  # unrecognised headings stay inside the section


def test_other_answer_kinds_use_their_own_sections() -> None:
    why = parse_answer(
        "## Confirmed\nA [E1].\n## Likely interpretation\nB.\n## Unknown\nC.",
        {"E1"},
        ["Confirmed", "Likely interpretation", "Unknown"],
    )
    assert why.missing_sections == [] and why.states_unknowns
    assert parse_answer("just prose, no headings", set()).missing_sections == EXPLAIN_SECTIONS


@pytest.mark.parametrize(
    "phrase",
    [
        "the reason is not established",
        "no comment explains it",
        "this is unknown",
        "it cannot be determined",
    ],
)
def test_admissions_of_ignorance_are_detected(phrase: str) -> None:
    assert parse_answer(f"## Output\nThe constant: {phrase}.", set()).states_unknowns


# ---- deterministic mentor answer ---------------------------------------------------------------


def test_mentor_answer_has_every_section_in_order_and_valid_citations(env: Env) -> None:
    plan = build_plan(env.method("shippingFee"))
    answer = render_mentor_answer(plan)
    parsed = parse_answer(answer, {c.label for c in plan.citations})
    assert [s.key for s in parsed.sections] == EXPLAIN_SECTIONS
    assert parsed.invalid_citations == [] and parsed.states_unknowns
    assert "(stated in a comment [E" in answer
    assert "the reason for 30 is not established" in answer
    assert "Inferred:\n- L8 looks like a threshold business rule." in answer


def test_mentor_answer_marks_undocumented_purpose_as_inference(env: Env) -> None:
    answer = render_mentor_answer(build_plan(env.method("render")))
    assert "This is an inference: no comment states the purpose" in answer
    assert "no comment, test or documentation mentions this method" in answer


def test_the_unavailable_note_is_shown_first(env: Env) -> None:
    answer = render_mentor_answer(build_plan(env.method("render")), note="LLM unavailable (down).")
    assert answer.startswith("> LLM unavailable (down).\n\n## What this method does")
