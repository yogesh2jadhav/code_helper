"""Explain a method (and trace / why / follow-ups) using static analysis plus the local LLM.

The LLM is only the communication layer: the analysis, plan and evidence are deterministic. If the
LLM is unavailable, the deterministic answer is returned with a note, so the user is never left
with nothing.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, Field

from app.config import Settings
from app.context.citations import Citation
from app.context.context_builder import ContextBuilder, ContextPackage
from app.context.explanation_plan import ExplanationPlan, build_plan
from app.explain.conversations import Conversation, ConversationStore
from app.explain.mentor_format import render_mentor_answer
from app.knowledge.models import MethodKnowledge
from app.knowledge.source import SourceReader
from app.knowledge.store import KnowledgeStore, MethodSummary
from app.llm.ollama_client import GenerationResult, LLMClient, LLMError, Message
from app.llm.prompts import (
    SYSTEM_PROMPT,
    explain_prompt,
    followup_messages,
    trace_prompt,
    why_prompt,
)
from app.llm.response_parser import ParsedAnswer, parse_answer
from app.logging_setup import log_event
from app.retrieval.hybrid import HybridRetriever
from app.services.trace_service import TraceResult, TraceService
from app.services.why_service import WhyResult, WhyService

logger = logging.getLogger(__name__)
_BRACKET = re.compile(r"\[(E\d+(?:\s*[,;]\s*E\d+)*)\]")
TRACE_SECTIONS = [
    "Where it comes from",
    "How it changes",
    "Where it goes",
    "What to be careful about",
    "Evidence and unknowns",
]
WHY_SECTIONS = ["Confirmed", "Likely interpretation", "Unknown"]


class ExplainOptions(BaseModel):
    depth: int = Field(default=1, ge=1, le=3)
    include_tests: bool = True
    include_docs: bool = True
    use_llm: bool = True
    query: str | None = None


class AnswerInfo(BaseModel):
    answer: str
    answer_source: Literal["llm", "deterministic"]
    parsed: ParsedAnswer
    warnings: list[str] = Field(default_factory=list)
    model: str | None = None
    llm_error: str | None = None
    duration_ms: int = 0


class ExplainResult(AnswerInfo):
    method: MethodSummary
    explanation_plan: ExplanationPlan
    evidence: list[Citation]
    unknowns: list[str]
    context_tokens: int
    context_omitted: list[str] = Field(default_factory=list)
    conversation_id: str | None = None


class TraceExplanation(AnswerInfo):
    trace: TraceResult


class WhyExplanation(AnswerInfo):
    why: WhyResult


class ChatResult(BaseModel):
    conversation_id: str
    answer: str
    warnings: list[str] = Field(default_factory=list)
    model: str | None = None
    duration_ms: int = 0


def clean_citations(text: str, valid: set[str]) -> tuple[str, list[str]]:
    """Drop citation labels the model invented. Returns (clean text, removed labels)."""
    removed: list[str] = []

    def fix(match: re.Match[str]) -> str:
        labels = re.split(r"\s*[,;]\s*", match.group(1))
        kept = [x for x in labels if x in valid]
        removed.extend(x for x in labels if x not in valid)
        return f"[{', '.join(kept)}]" if kept else ""

    return _BRACKET.sub(fix, text).replace("  ", " "), removed


def removed_warning(removed: list[str]) -> str:
    return "removed citation labels that do not exist: " + ", ".join(sorted(set(removed)))


class ExplanationService:
    def __init__(
        self,
        repository_id: str,
        store: KnowledgeStore,
        reader: SourceReader | None,
        settings: Settings,
        llm: LLMClient | None = None,
        retriever: HybridRetriever | None = None,
        conversations: ConversationStore | None = None,
    ) -> None:
        self._repo = repository_id
        self._store = store
        self._reader = reader
        self._settings = settings
        self._llm = llm
        self._retriever = retriever
        self.conversations = conversations or ConversationStore()

    # ==== explain ===============================================================================

    def explain(self, method_pk: str, options: ExplainOptions | None = None) -> ExplainResult:
        opts = options or ExplainOptions()
        started = time.monotonic()
        method = self._method(method_pk)
        package = self._context(method, opts)
        plan = build_plan(method, package.citations)
        valid = {c.label for c in plan.citations}

        info = self._answer(
            opts.use_llm,
            lambda llm: llm.generate(explain_prompt(plan, package.render()), system=SYSTEM_PROMPT),
            lambda note: render_mentor_answer(plan, note),
            valid,
            None,
        )
        conversation = self.conversations.create(
            method_pk=method.id,
            method_id=method.method_id,
            repository_id=self._repo,
            summary=_summary(method),
            context=package.render(),
            valid_labels=sorted(valid),
            history=[Message(role="assistant", content=info.answer)],
        )
        summary = self._store.list_methods(method.class_id)
        listing = next((s for s in summary if s.id == method.id), None)
        assert listing is not None
        info.duration_ms = int((time.monotonic() - started) * 1000)
        log_event(
            logger,
            "explanation_completed",
            method=method.method_id,
            source=info.answer_source,
            duration_ms=info.duration_ms,
        )
        return ExplainResult(
            **info.model_dump(),
            method=listing,
            explanation_plan=plan,
            evidence=plan.citations,
            unknowns=plan.unknowns,
            context_tokens=package.total_tokens,
            context_omitted=package.omitted,
            conversation_id=conversation.id,
        )

    # ==== trace and why =========================================================================

    def trace(
        self, method_pk: str, variable: str, depth: int = 2, use_llm: bool = True
    ) -> TraceExplanation:
        started = time.monotonic()
        method = self._method(method_pk)
        trace = TraceService(self._store).trace(method_pk, variable, depth)
        package = self._context(method, ExplainOptions(use_llm=use_llm))
        valid = {c.label for c in trace.citations} | {c.label for c in package.citations}
        info = self._answer(
            use_llm,
            lambda llm: llm.generate(
                trace_prompt(trace.as_text(), package.render(), variable), system=SYSTEM_PROMPT
            ),
            lambda note: f"> {note}\n\n{trace.as_text()}",
            valid,
            TRACE_SECTIONS,
        )
        info.duration_ms = int((time.monotonic() - started) * 1000)
        return TraceExplanation(**info.model_dump(), trace=trace)

    def why(
        self,
        method_pk: str,
        start_line: int | None = None,
        end_line: int | None = None,
        use_llm: bool = True,
    ) -> WhyExplanation:
        started = time.monotonic()
        method = self._method(method_pk)
        why = WhyService(self._store, self._reader).why(method_pk, start_line, end_line)
        package = self._context(method, ExplainOptions(use_llm=use_llm))
        valid = {c.label for c in why.citations} | {c.label for c in package.citations}
        info = self._answer(
            use_llm,
            lambda llm: llm.generate(
                why_prompt(why.selection, why.as_text(), package.render()), system=SYSTEM_PROMPT
            ),
            lambda note: f"> {note}\n\n{why.as_text()}",
            valid,
            WHY_SECTIONS,
        )
        info.duration_ms = int((time.monotonic() - started) * 1000)
        return WhyExplanation(**info.model_dump(), why=why)

    # ==== follow-ups ============================================================================

    def chat(self, conversation_id: str, question: str) -> ChatResult:
        conversation = self.conversations.get(conversation_id)
        if conversation is None:
            raise LookupError(f"unknown conversation {conversation_id}")
        if self._llm is None:
            raise LLMError("no language model is configured")
        started = time.monotonic()
        messages = followup_messages(
            conversation.history, question, conversation.context, conversation.summary
        )
        result = self._llm.chat(messages)  # LLMError propagates: there is no sensible fallback here
        text, removed = clean_citations(result.text, set(conversation.valid_labels))
        warnings = [removed_warning(removed)] if removed else []
        self.conversations.append(
            conversation_id,
            Message(role="user", content=question),
            Message(role="assistant", content=text),
        )
        return ChatResult(
            conversation_id=conversation_id,
            answer=text,
            warnings=warnings,
            model=result.model,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # ==== internals =============================================================================

    def _method(self, method_pk: str) -> MethodKnowledge:
        method = self._store.get_method(method_pk)
        if method is None:
            raise LookupError(f"unknown method {method_pk}")
        return method

    def _context(self, method: MethodKnowledge, opts: ExplainOptions) -> ContextPackage:
        builder = ContextBuilder(
            self._repo, self._store, self._reader, self._settings, self._retriever
        )
        return builder.build(
            method,
            depth=opts.depth,
            include_tests=opts.include_tests,
            include_docs=opts.include_docs,
            query=opts.query,
        )

    def _answer(
        self,
        use_llm: bool,
        generate: Callable[[LLMClient], GenerationResult],
        fallback: Callable[[str], str],
        valid: set[str],
        sections: list[str] | None,
    ) -> AnswerInfo:
        """Ask the LLM; on any LLM failure return the deterministic answer instead."""
        started = time.monotonic()
        warnings: list[str] = []
        if use_llm and self._llm is not None:
            try:
                result = generate(self._llm)
                text, removed = clean_citations(result.text, valid)
                if removed:
                    warnings.append(removed_warning(removed))
                parsed = parse_answer(text, valid, sections)
                if parsed.missing_sections:
                    warnings.append(
                        f"the answer lacks sections: {', '.join(parsed.missing_sections)}"
                    )
                return AnswerInfo(
                    answer=text,
                    answer_source="llm",
                    parsed=parsed,
                    warnings=warnings,
                    model=result.model,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            except LLMError as exc:
                note = f"LLM generation unavailable ({exc}). Showing the analysis-only explanation."
                error: str | None = str(exc)
        else:
            note = (
                "LLM generation was not requested; showing the analysis-only explanation."
                if self._llm
                else "No language model is configured; showing the analysis-only explanation."
            )
            error = None
        text = fallback(note)
        return AnswerInfo(
            answer=text,
            answer_source="deterministic",
            parsed=parse_answer(text, valid, sections),
            warnings=warnings,
            llm_error=error,
            duration_ms=int((time.monotonic() - started) * 1000),
        )


def _summary(m: MethodKnowledge) -> str:
    return (
        f"{m.class_fqn}.{m.signature} in {m.file}:{m.start_line}-{m.end_line}. "
        f"Purpose [{m.purpose.basis}]: {m.purpose.text}. {m.purpose.structure or ''}"
    )


__all__ = [
    "Conversation",
    "ExplainOptions",
    "ExplainResult",
    "ExplanationService",
    "clean_citations",
]
