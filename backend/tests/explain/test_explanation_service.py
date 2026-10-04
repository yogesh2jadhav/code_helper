from __future__ import annotations

from typing import Any

import pytest

from app.config import Settings
from app.explain.conversations import ConversationStore
from app.explain.explanation_service import (
    ExplainOptions,
    ExplanationService,
    clean_citations,
)
from app.llm.ollama_client import (
    GenerationResult,
    LLMModelNotFoundError,
    LLMTimeoutError,
    Message,
)
from app.llm.prompts import EXPLAIN_SECTIONS
from tests.helpers import Env

GOOD = "\n\n".join(f"## {s}\nText for {s}. [E1]" for s in EXPLAIN_SECTIONS)


class FakeLLM:
    """Returns scripted answers and records what it was asked."""

    model = "fake-model"

    def __init__(self, *answers: str, error: Exception | None = None) -> None:
        self.answers = list(answers) or [GOOD]
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def _next(self) -> str:
        if self.error:
            raise self.error
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> GenerationResult:
        self.calls.append({"kind": "generate", "prompt": prompt, "system": system})
        return GenerationResult(text=self._next(), model=self.model, duration_ms=5)

    def chat(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> GenerationResult:
        self.calls.append({"kind": "chat", "messages": messages})
        return GenerationResult(text=self._next(), model=self.model, duration_ms=5)

    def stream_chat(self, messages: list[Message], **kw: Any):  # type: ignore[no-untyped-def]
        yield self._next()


def service(env: Env, settings: Settings, llm: FakeLLM | None, **kw: Any) -> ExplanationService:
    return ExplanationService(env.repo, env.store, env.reader, settings, llm, env.retriever, **kw)


# ---- citations --------------------------------------------------------------------------------


def test_invented_citation_labels_are_removed() -> None:
    text, removed = clean_citations("A [E1]. B [E9]. C [E1, E7]. D [E8, E9].", {"E1", "E2"})
    assert text == "A [E1]. B . C [E1]. D ."
    assert removed == ["E9", "E7", "E8", "E9"]
    assert clean_citations("no labels [x] here", {"E1"}) == ("no labels [x] here", [])


# ---- explain ----------------------------------------------------------------------------------


def test_explain_with_an_llm(env: Env, isolated_settings: Settings) -> None:
    llm = FakeLLM()
    method = env.method("shippingFee")
    result = service(env, isolated_settings, llm).explain(method.id)

    assert (
        result.answer_source == "llm" and result.model == "fake-model" and result.llm_error is None
    )
    assert result.answer == GOOD and result.parsed.missing_sections == []
    assert result.warnings == [] and result.method.name == "shippingFee"
    assert result.explanation_plan.method_id == method.method_id
    assert [c.label for c in result.evidence] == [
        c.label for c in result.explanation_plan.citations
    ]
    assert result.unknowns == result.explanation_plan.unknowns and result.context_tokens > 0
    assert result.conversation_id is not None

    (call,) = llm.calls
    assert call["kind"] == "generate" and "Never invent business intent" in call["system"]
    assert "weightKg > 30" in call["prompt"] and "Answer format" in call["prompt"]
    assert "InvoicePrinter" not in call["prompt"]  # only this method's material is sent


def test_the_model_is_asked_once_and_sees_the_plan_and_the_context(
    env: Env, isolated_settings: Settings
) -> None:
    llm = FakeLLM()
    service(env, isolated_settings, llm).explain(env.method("shippingFee").id)
    prompt = llm.calls[0]["prompt"]
    assert "# Analysis (derived by static analysis, not by you)" in prompt
    assert "[UNKNOWN] The repository does not establish why 30 was chosen" in prompt
    assert "### Target method source [E1]" in prompt


def test_invented_citations_and_missing_sections_become_warnings(
    env: Env, isolated_settings: Settings
) -> None:
    llm = FakeLLM(
        "## What this method does\nIt prices parcels [E1][E42].\n\n## Output\nA double [E99]."
    )
    result = service(env, isolated_settings, llm).explain(env.method("shippingFee").id)
    assert "[E42]" not in result.answer and "[E99]" not in result.answer and "[E1]" in result.answer
    assert result.warnings[0] == "removed citation labels that do not exist: E42, E99"
    assert result.warnings[1].startswith("the answer lacks sections: High-level flow")
    assert result.parsed.invalid_citations == []  # cleaned before parsing


@pytest.mark.parametrize(
    "error",
    [
        LLMTimeoutError("no response from Ollama within 120s"),
        LLMModelNotFoundError("model 'x' is not available"),
    ],
)
def test_llm_failure_falls_back_to_the_deterministic_answer(
    env: Env, isolated_settings: Settings, error: Exception
) -> None:
    result = service(env, isolated_settings, FakeLLM(error=error)).explain(
        env.method("shippingFee").id
    )
    assert result.answer_source == "deterministic" and result.model is None
    assert result.llm_error == str(error)
    assert result.answer.startswith(
        f"> LLM generation unavailable ({error}). Showing the analysis-only explanation."
    )
    assert result.parsed.missing_sections == []  # the fallback has all nine sections
    assert [s.key for s in result.parsed.sections if s.key != "preamble"] == EXPLAIN_SECTIONS
    assert result.explanation_plan.unknowns  # the analysis is still delivered in full


def test_no_llm_configured_and_use_llm_false(env: Env, isolated_settings: Settings) -> None:
    none = service(env, isolated_settings, None).explain(env.method("shippingFee").id)
    assert none.answer_source == "deterministic" and none.llm_error is None
    assert none.answer.startswith("> No language model is configured")
    llm = FakeLLM()
    off = service(env, isolated_settings, llm).explain(
        env.method("shippingFee").id, ExplainOptions(use_llm=False)
    )
    assert off.answer_source == "deterministic" and llm.calls == []
    assert off.answer.startswith("> LLM generation was not requested")


def test_options_shape_the_context(env: Env, isolated_settings: Settings) -> None:
    llm = FakeLLM()
    service(env, isolated_settings, llm).explain(
        env.method("shippingFee").id, ExplainOptions(include_tests=False, include_docs=False)
    )
    prompt = llm.calls[0]["prompt"]
    assert "### Test shop.ShippingCalculatorTest" not in prompt and "### Comment" not in prompt
    with pytest.raises(ValueError):
        ExplainOptions(depth=9)


def test_unknown_method(env: Env, isolated_settings: Settings) -> None:
    with pytest.raises(LookupError, match="unknown method"):
        service(env, isolated_settings, FakeLLM()).explain("m_nope")


# ---- follow-ups -------------------------------------------------------------------------------


def test_follow_up_questions_keep_the_method_context(env: Env, isolated_settings: Settings) -> None:
    llm = FakeLLM(GOOD, "Express doubles the fee [E1]. Also see [E55].", "Yes.")
    svc = service(env, isolated_settings, llm)
    first = svc.explain(env.method("shippingFee").id)
    cid = first.conversation_id or ""

    one = svc.chat(cid, "What happens for express delivery?")
    assert one.answer == "Express doubles the fee [E1]. Also see ." and one.model == "fake-model"
    assert one.warnings == ["removed citation labels that do not exist: E55"]

    svc.chat(cid, "And is that tested?")
    turn2 = llm.calls[-1]["messages"]
    roles = [m.role for m in turn2]
    assert roles == ["system", "user", "assistant", "assistant", "user", "assistant", "user"]
    assert (
        "weightKg" in turn2[1].content and "shippingFee" in turn2[1].content
    )  # the same method context
    assert turn2[3].content == GOOD  # the original explanation is part of the conversation
    assert (
        turn2[4].content == "What happens for express delivery?"
        and turn2[-1].content == "And is that tested?"
    )


def test_chat_errors(env: Env, isolated_settings: Settings) -> None:
    svc = service(env, isolated_settings, FakeLLM())
    with pytest.raises(LookupError, match="unknown conversation"):
        svc.chat("nope", "hi")
    cid = svc.explain(env.method("shippingFee").id).conversation_id or ""
    down = ExplanationService(
        env.repo,
        env.store,
        env.reader,
        isolated_settings,
        FakeLLM(error=LLMTimeoutError("slow")),
        conversations=svc.conversations,
    )
    with pytest.raises(LLMTimeoutError):
        down.chat(cid, "hello")
    with pytest.raises(Exception, match="no language model"):
        ExplanationService(
            env.repo,
            env.store,
            env.reader,
            isolated_settings,
            None,
            conversations=svc.conversations,
        ).chat(cid, "hello")


def test_conversation_store_is_bounded_and_isolated() -> None:
    store = ConversationStore(capacity=2)
    ids = [
        store.create(method_pk="m", method_id="x", repository_id="r", summary="s", context="c").id
        for _ in range(3)
    ]
    assert (
        store.get(ids[0]) is None
        and store.get(ids[1]) is not None
        and store.get(ids[2]) is not None
    )
    got = store.get(ids[2])
    assert got is not None
    got.history.append(Message(role="user", content="mutated copy"))
    again = store.get(ids[2])
    assert again is not None and again.history == []  # callers get copies


# ---- trace and why ----------------------------------------------------------------------------


def test_trace_explanation(project_env: Env, isolated_settings: Settings) -> None:
    llm = FakeLLM(
        "## Where it comes from\nFrom the callers [E1].\n## Where it goes\nBack out [E2].\n"
        "## How it changes\nUnchanged.\n## What to be careful about\nNothing.\n## Evidence and unknowns\nFine."
    )
    pk = next(
        m.id
        for m in project_env.store.iter_methods(project_env.repo)
        if m.method_id.endswith("BasePricing#base(int)")
    )
    svc = ExplanationService(
        project_env.repo, project_env.store, project_env.reader, isolated_settings, llm
    )
    result = svc.trace(pk, "x", depth=2)
    assert result.answer_source == "llm" and result.parsed.missing_sections == []
    assert result.trace.variable == "x" and result.trace.steps
    prompt = llm.calls[0]["prompt"]
    assert (
        "`x`" in prompt
        and "Where it comes from:" in prompt
        and "PricingService.price(int) passes parameter `units`" in prompt
    )


def test_trace_falls_back_to_the_deterministic_trace(
    project_env: Env, isolated_settings: Settings
) -> None:
    pk = next(
        m.id
        for m in project_env.store.iter_methods(project_env.repo)
        if m.method_id.endswith("BasePricing#base(int)")
    )
    svc = ExplanationService(
        project_env.repo,
        project_env.store,
        project_env.reader,
        isolated_settings,
        FakeLLM(error=LLMTimeoutError("slow")),
    )
    result = svc.trace(pk, "x")
    assert result.answer_source == "deterministic"
    assert "Where it comes from:" in result.answer and result.answer.startswith(
        "> LLM generation unavailable"
    )


def test_why_explanation(env: Env, isolated_settings: Settings) -> None:
    llm = FakeLLM(
        "## Confirmed\nThe code tests weight > 30 [E1].\n## Likely interpretation\nA tier.\n"
        "## Unknown\nWhy 30 is not established."
    )
    result = service(env, isolated_settings, llm).why(env.method("shippingFee").id, 8, 10)
    assert result.answer_source == "llm" and result.parsed.missing_sections == []
    assert result.parsed.states_unknowns and result.why.unknown[0].startswith("why 30 was chosen")
    prompt = llm.calls[0]["prompt"]
    assert (
        "if (weightKg > 30) {" in prompt
        and "UNKNOWN (not established by the repository):" in prompt
    )


def test_why_without_an_llm_returns_the_structured_evidence(
    env: Env, isolated_settings: Settings
) -> None:
    result = service(env, isolated_settings, None).why(env.method("shippingFee").id, 8, 10)
    assert result.answer_source == "deterministic"
    assert "CONFIRMED (code or author-written evidence):" in result.answer
    assert "why 30 was chosen" in result.answer
