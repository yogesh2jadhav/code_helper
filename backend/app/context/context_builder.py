"""Build a bounded, prioritised context package for explaining one method.

Priority order (highest first): the method's semantic model, its source, direct callees, data-flow,
rule candidates, fields and types, tests, comments/docs, callers, retrieved extras. The budget is
split into four shares (target, callees, flow, evidence); unused share spills over to whatever did
not fit. Nothing outside the budget is ever sent: what is dropped is listed in `omitted`.
"""

from __future__ import annotations

import logging
import time
from typing import Literal

from pydantic import BaseModel, Field

from app.analyzer.control_flow import render_outline
from app.analyzer.resolution_models import ResolutionStatus
from app.config import Settings
from app.context.citations import Citation, CitationBook
from app.context.tokens import estimate_tokens, truncate_to_tokens
from app.knowledge.models import MethodKnowledge
from app.knowledge.source import SourceReader
from app.knowledge.store import KnowledgeStore
from app.logging_setup import log_event
from app.retrieval.hybrid import HybridRetriever

logger = logging.getLogger(__name__)
Category = Literal["target", "callees", "flow", "evidence"]
CATEGORIES: tuple[Category, ...] = ("target", "callees", "flow", "evidence")
MIN_PARTIAL_TOKENS = 60  # do not bother including a fragment smaller than this


class ContextItem(BaseModel):
    # target_model | target_source | callee | flow | rule | types | test | comment | doc | caller
    # | retrieved
    kind: str
    category: Category
    priority: int  # 1 = most important
    title: str
    text: str
    tokens: int
    label: str | None = None  # citation label of the primary source range
    truncated: bool = False
    source: tuple[str, int, int, str] | None = None  # file, start, end, source_type


class Budget(BaseModel):
    max_tokens: int
    allotted: dict[str, int]
    used: dict[str, int] = Field(default_factory=dict)


class ContextPackage(BaseModel):
    method_id: str
    items: list[ContextItem]
    citations: list[Citation]
    budget: Budget
    total_tokens: int
    omitted: list[str] = Field(default_factory=list)  # titles dropped for lack of budget
    complete: bool = True  # False if anything was dropped or truncated

    def render(self) -> str:
        """The context as text for a prompt: one section per item, tagged with its citation."""
        blocks = []
        for item in self.items:
            tag = f" [{item.label}]" if item.label else ""
            blocks.append(f"### {item.title}{tag}\n{item.text}")
        if self.omitted:
            blocks.append(
                "### Not included (context budget)\n" + "\n".join(f"- {o}" for o in self.omitted)
            )
        return "\n\n".join(blocks)


class ContextBuilder:
    def __init__(
        self,
        repository_id: str,
        store: KnowledgeStore,
        reader: SourceReader | None,
        settings: Settings,
        retriever: HybridRetriever | None = None,
    ) -> None:
        self._repo = repository_id
        self._store = store
        self._reader = reader
        self._settings = settings
        self._retriever = retriever

    # ==== public ================================================================================

    def build(
        self,
        method: MethodKnowledge,
        *,
        depth: int = 1,
        include_tests: bool = True,
        include_docs: bool = True,
        include_callers: bool = True,
        query: str | None = None,
        max_tokens: int | None = None,
    ) -> ContextPackage:
        started = time.monotonic()
        limit = max_tokens or self._settings.max_context_tokens
        allotted = self._allotments(limit)
        items: list[ContextItem] = [
            self._target_model(method),
            *self._target_source(method),
            *self._callees(method, depth),
            *self._flow(method),
            *self._rules(method),
            self._types(method),
        ]
        if include_tests:
            items += self._tests(method)
        if include_docs:
            items += self._comments_and_docs(method)
        if include_callers:
            items += self._callers(method)
        items += self._retrieved(
            method, query, include_tests, include_docs, {i.title for i in items}
        )
        items = [i for i in items if i.text]

        kept, omitted, used = self._assemble(items, allotted)
        book = CitationBook()
        for item in kept:
            if item.source:
                item.label = book.cite(
                    *item.source[:3], source_type=item.source[3], note=item.title
                ).label
        total = sum(i.tokens for i in kept)
        package = ContextPackage(
            method_id=method.method_id,
            items=kept,
            citations=book.citations,
            budget=Budget(max_tokens=limit, allotted=allotted, used=used),
            total_tokens=total,
            omitted=omitted,
            complete=not omitted and not any(i.truncated for i in kept),
        )
        log_event(
            logger,
            "context_built",
            method=method.method_id,
            items=len(kept),
            tokens=total,
            omitted=len(omitted),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return package

    # ==== budgeting =============================================================================

    def _allotments(self, limit: int) -> dict[str, int]:
        s = self._settings
        weights = {
            "target": s.context_budget_target,
            "callees": s.context_budget_callees,
            "flow": s.context_budget_flow,
            "evidence": s.context_budget_evidence,
        }
        total = sum(weights.values()) or 1.0
        return {k: int(limit * v / total) for k, v in weights.items()}

    def _assemble(
        self, items: list[ContextItem], allotted: dict[str, int]
    ) -> tuple[list[ContextItem], list[str], dict[str, int]]:
        """Fill each category by priority; then spend unused tokens on what did not fit."""
        kept: list[ContextItem] = []
        overflow: list[ContextItem] = []
        used: dict[str, int] = dict.fromkeys(CATEGORIES, 0)
        for cat in CATEGORIES:
            for item in sorted((i for i in items if i.category == cat), key=lambda i: i.priority):
                room = allotted[cat] - used[cat]
                if item.tokens <= room:
                    kept.append(item)
                    used[cat] += item.tokens
                else:
                    overflow.append(item)
        pool = sum(max(0, allotted[c] - used[c]) for c in CATEGORIES)
        omitted: list[str] = []
        for item in sorted(overflow, key=lambda i: i.priority):
            if item.tokens <= pool:
                kept.append(item)
                used[item.category] += item.tokens
                pool -= item.tokens
            elif pool >= MIN_PARTIAL_TOKENS:  # lines are ordered by importance, so cutting is safe
                text, cut = truncate_to_tokens(item.text, pool)
                part = item.model_copy(
                    update={"text": text, "tokens": estimate_tokens(text), "truncated": cut}
                )
                kept.append(part)
                used[item.category] += part.tokens
                pool -= part.tokens
                omitted.append(f"{item.title} (shortened)")
            else:
                omitted.append(item.title)
        kept.sort(key=lambda i: (i.priority, i.title))
        return kept, omitted, used

    def _item(
        self,
        kind: str,
        category: Category,
        priority: int,
        title: str,
        text: str,
        source: tuple[str, int, int, str] | None = None,
    ) -> ContextItem:
        return ContextItem(
            kind=kind,
            category=category,
            priority=priority,
            title=title,
            text=text,
            tokens=estimate_tokens(text),
            source=source,
        )

    # ==== 1. target model =======================================================================

    def _target_model(self, m: MethodKnowledge) -> ContextItem:
        lines = [
            f"{m.class_fqn}.{m.signature}  ({m.visibility}, {m.kind})"
            f" in {m.file}:{m.start_line}-{m.end_line}",
            f"purpose [{m.purpose.basis}/{m.purpose.level}]: {m.purpose.text}",
        ]
        if m.purpose.structure:
            lines.append(f"structure: {m.purpose.structure}")
        for p in m.parameters:
            lines.append(f"input {p.type} {p.name}: " + "; ".join(p.comes_from[:3]))
        lines.append(
            f"returns {m.output.type or 'void'}: " + ("; ".join(m.output.returns[:5]) or "-")
        )
        if m.output.side_effects:
            lines.append("side effects: " + "; ".join(m.output.side_effects[:5]))
        lines.append(
            f"complexity: cyclomatic {m.complexity.cyclomatic}, "
            f"nesting {m.complexity.nesting_depth}, {m.complexity.lines} lines"
        )
        if m.risks:
            lines.append("risks: " + "; ".join(f"[{r.level}] {r.message}" for r in m.risks[:6]))
        if m.unknowns:
            lines.append("not established: " + "; ".join(u.message for u in m.unknowns[:6]))
        return self._item(
            "target_model",
            "target",
            1,
            "Target method (analysis)",
            "\n".join(lines),
            (m.file, m.start_line, m.end_line, "source_code"),
        )

    # ==== 2. target source ======================================================================

    def _target_source(self, m: MethodKnowledge) -> list[ContextItem]:
        source = self._reader.lines(m.file, m.start_line, m.end_line) if self._reader else None
        if source is None:
            source = next((e.snippet for e in m.evidence if e.relation == "implements"), "")
        if not source:
            return []
        numbered = "\n".join(
            f"{m.start_line + i}: {line}" for i, line in enumerate(source.split("\n"))
        )
        ref = (m.file, m.start_line, m.end_line, "source_code")
        source_item = self._item(
            "target_source", "target", 2, "Target method source", numbered, ref
        )
        # A long method also gets its control-flow outline. It is listed first so that, when the
        # source does not fit, the compact outline is kept in full and the source is shortened.
        if m.complexity.lines > 60 and m.control_flow.nodes:
            outline = render_outline(m.control_flow, lines=True)
            return [
                self._item("flow", "target", 2, "Target method structure (outline)", outline, ref),
                source_item,
            ]
        return [source_item]

    # ==== 3. callees ============================================================================

    def _callees(self, m: MethodKnowledge, depth: int) -> list[ContextItem]:
        items: list[ContextItem] = []
        externals: dict[str, list[int]] = {}
        seen: set[str] = set()
        for c in m.callees:
            if c.method_id is None:
                if c.status is ResolutionStatus.RESOLVED and c.owner_type:
                    externals.setdefault(f"{c.owner_type}.{c.name}", []).extend(c.site_lines)
                continue
            if c.method_id in seen:
                continue
            seen.add(c.method_id)
            items.append(self._callee_item(c.method_id, c.site_lines, 3, depth, c.may_dispatch_to))
        for c in m.callees:
            if c.status is ResolutionStatus.AMBIGUOUS and c.candidates:
                names = ", ".join(x.split("#")[-1] for x in c.candidates)
                items.append(
                    self._item(
                        "callee",
                        "callees",
                        3,
                        f"Ambiguous call {c.name}() at L{c.site_lines[0]}",
                        f"could be any of: {names} ({c.reason})",
                    )
                )
        if externals:
            lines = [
                f"{owner} (L{', L'.join(str(x) for x in sorted(set(ls))[:3])})"
                for owner, ls in externals.items()
            ]
            items.append(
                self._item(
                    "callee",
                    "callees",
                    4,
                    "External calls (library/JDK, no source)",
                    "\n".join(lines[:20]),
                )
            )
        return items

    def _callee_item(
        self, callee_id: str, site_lines: list[int], priority: int, depth: int, overrides: list[str]
    ) -> ContextItem:
        found = self._store.find_method(self._repo, callee_id)
        if not found:
            return self._item(
                "callee",
                "callees",
                priority,
                callee_id,
                f"{callee_id} (not in the knowledge model)",
            )
        c = found[0]
        lines = [
            f"{c.class_fqn}.{c.signature}  at {c.file}:{c.start_line}-{c.end_line};"
            f" called from L{', L'.join(map(str, site_lines))}",
            f"purpose [{c.purpose.basis}]: {c.purpose.text}",
            f"returns {c.return_type or 'void'}: " + ("; ".join(c.output.returns[:3]) or "-"),
        ]
        if c.output.side_effects:
            lines.append("side effects: " + "; ".join(c.output.side_effects[:3]))
        if overrides:
            lines.append(
                "runtime may use an override: "
                + ", ".join(o.split("#")[0].rsplit(".", 1)[-1] for o in overrides[:5])
            )
        if depth >= 2:
            inner = [
                x.name + (f" ({x.purpose})" if x.purpose else "") for x in c.callees if x.method_id
            ][:6]
            if inner:
                lines.append("it calls: " + "; ".join(inner))
        source = (
            self._reader.lines(c.file, c.start_line, min(c.end_line, c.start_line + 30))
            if self._reader
            else None
        )
        if source and c.complexity.lines <= 40:
            lines.append("source:\n" + source)
        return self._item(
            "callee",
            "callees",
            priority,
            f"Callee {c.class_fqn.rsplit('.', 1)[-1]}.{c.name}",
            "\n".join(lines),
            (c.file, c.start_line, c.end_line, "source_code"),
        )

    # ==== 4-5. data flow, rules =================================================================

    def _flow(self, m: MethodKnowledge) -> list[ContextItem]:
        lines = []
        for v in m.data_flow.variables:
            life = v.lifecycle()
            if not any(
                k in life
                for k in (
                    "modified",
                    "passed",
                    "returned",
                    "grouped",
                    "filtered",
                    "collected",
                    "aggregated",
                    "mapped",
                    "iterated",
                )
            ):
                continue
            parts = [f"created: {v.created}" if v.created else "created: ?"]
            for kind in (
                "iterated",
                "filtered",
                "mapped",
                "grouped",
                "collected",
                "aggregated",
                "modified",
                "passed",
                "returned",
            ):
                if kind in life:
                    parts.append(f"{kind}: " + "; ".join(life[kind][:3]))
            lines.append(f"{v.kind} {v.name} ({v.type or '?'}): " + " | ".join(parts))
        if not lines:
            return []
        return [
            self._item(
                "flow",
                "flow",
                4,
                "Data flow (how values change)",
                "\n".join(lines[:25]),
                (m.file, m.start_line, m.end_line, "source_code"),
            )
        ]

    def _rules(self, m: MethodKnowledge) -> list[ContextItem]:
        if not m.rule_candidates:
            return []
        lines = []
        for r in m.rule_candidates:
            parts = [f"L{r.start_line} [{r.kind}]"]
            if r.meaning:
                parts.append(f"when {r.meaning}")
            if r.calculation:
                parts.append(r.calculation)
            if r.action:
                parts.append(f"-> {r.action}")
            if r.otherwise:
                parts.append(f"otherwise {r.otherwise}")
            if r.literals:
                parts.append(f"constants: {', '.join(r.literals)}")
            lines.append(" ".join(parts))
        first, last = m.rule_candidates[0], m.rule_candidates[-1]
        return [
            self._item(
                "rule",
                "flow",
                5,
                "Business-rule candidates (patterns found in the code)",
                "\n".join(lines),
                (m.file, first.start_line, last.end_line, "source_code"),
            )
        ]

    # ==== 6. fields and types ===================================================================

    def _types(self, m: MethodKnowledge) -> ContextItem:
        klass = self._store.find_class(self._repo, m.class_fqn)
        lines: list[str] = []
        if klass:
            k = klass[0]
            lines.append(f"{k.kind} {k.fqn}: {k.purpose.text}")
            if k.fields:
                lines.append("fields: " + ", ".join(f"{f.type} {f.name}" for f in k.fields[:15]))
        mentioned = {p.type.split("<")[0] for p in m.parameters} | (
            {m.return_type.split("<")[0]} if m.return_type else set()
        )
        for name in sorted(mentioned):
            for c in self._store.list_classes(self._repo, query=name, limit=3):
                if c.name == name and c.fqn != m.class_fqn:
                    info = self._store.find_class(self._repo, c.fqn)
                    if info:
                        f = ", ".join(f"{x.type} {x.name}" for x in info[0].fields[:8])
                        lines.append(
                            f"type {c.fqn}: {info[0].purpose.text}"
                            + (f"; fields: {f}" if f else "")
                        )
        return self._item(
            "types", "target", 6, "Class, fields and types involved", "\n".join(lines)
        )

    # ==== 7-9. tests, comments/docs, callers ====================================================

    def _tests(self, m: MethodKnowledge) -> list[ContextItem]:
        items = []
        for e in [e for e in m.evidence if e.source_type == "test"][:3]:
            text, cut = truncate_to_tokens(e.snippet, 350)
            item = self._item(
                "test",
                "evidence",
                7,
                f"Test {e.class_name}.{e.method}",
                text,
                (e.file, e.start_line, e.end_line, "test"),
            )
            item.truncated = cut
            items.append(item)
        return items

    def _comments_and_docs(self, m: MethodKnowledge) -> list[ContextItem]:
        items = []
        for e in m.evidence:
            if e.source_type == "comment" and e.relation != "commented_code":
                items.append(
                    self._item(
                        "comment",
                        "evidence",
                        8,
                        f"Comment ({e.relation}) at L{e.start_line}",
                        e.snippet,
                        (e.file, e.start_line, e.end_line, "comment"),
                    )
                )
            elif e.source_type in ("readme", "documentation"):
                text, cut = truncate_to_tokens(e.snippet, 300)
                item = self._item(
                    "doc",
                    "evidence",
                    8,
                    f"Documentation ({e.file})",
                    text,
                    (e.file, e.start_line, e.end_line, e.source_type),
                )
                item.truncated = cut
                items.append(item)
        return items

    def _callers(self, m: MethodKnowledge) -> list[ContextItem]:
        if not m.callers:
            return []
        lines = []
        for c in m.callers[:8]:
            who = (
                c.method_id.split("#")[0].rsplit(".", 1)[-1]
                + "."
                + c.method_id.split("#")[1].split("(")[0]
            )
            flag = " (may call it)" if c.ambiguous else ""
            lines.append(
                f"{who}() at L{', L'.join(map(str, c.site_lines))}{flag}"
                + (f": {c.purpose}" if c.purpose else "")
            )
        more = f"\n(+{len(m.callers) - 8} more callers)" if len(m.callers) > 8 else ""
        return [
            self._item("caller", "evidence", 9, "Callers (how it is used)", "\n".join(lines) + more)
        ]

    # ==== 10. retrieved extras ==================================================================

    def _retrieved(
        self,
        m: MethodKnowledge,
        query: str | None,
        include_tests: bool,
        include_docs: bool,
        have: set[str],
    ) -> list[ContextItem]:
        if self._retriever is None:
            return []
        question = query or f"{m.name} {m.purpose.text}"
        types = ["evidence", "rule_candidate", "class_source"]
        if include_docs:
            types.append("documentation")
        if include_tests:
            types.append("test")
        try:
            result = self._retriever.search(self._repo, question, k=6, types=types)  # type: ignore[arg-type]
        except Exception as exc:  # retrieval is optional extra evidence; never fail the explanation
            logger.warning("retrieval_skipped", extra={"error": str(exc)[:200]})
            return []
        items = []
        for hit in result.hits:
            d = hit.doc
            if d.method_name == m.name and d.class_name == m.class_fqn:
                continue  # already covered by the target's own sections
            title = f"Related {d.type.replace('_', ' ')}: {d.class_name.rsplit('.', 1)[-1]}" + (
                f".{d.method_name}" if d.method_name else ""
            )
            if title in have:
                continue
            text, cut = truncate_to_tokens(d.text, 250)
            item = self._item(
                "retrieved",
                "evidence",
                10,
                title,
                text,
                (d.file_path, d.line_start, d.line_end, "retrieved"),
            )
            item.truncated = cut
            items.append(item)
        return items[:4]
