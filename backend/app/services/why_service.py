"""Gather the evidence for "why does the selected code do this?" and sort it into
confirmed / likely / unknown. Deterministic: it never invents a reason.

Confirmed = the code establishes it, or an author-written artifact (a nearby comment, a document,
a test) states it. Likely = a reasonable reading, labelled as inference. Unknown = the repository
does not establish it.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from app.analyzer.control_flow import FlowNode
from app.analyzer.rule_extractor import RuleCandidate
from app.context.citations import Citation, CitationBook
from app.knowledge.evidence import Evidence, first_sentence
from app.knowledge.models import MethodKnowledge
from app.knowledge.source import SourceReader
from app.knowledge.store import KnowledgeStore
from app.retrieval.bm25 import tokenize

NEAR = 3  # lines around the selection in which a comment counts as being about it
TRIVIAL = {"0", "1", "-1", "2", '""', "true", "false", "null"}

_MEANING = {
    "threshold": "separates cases by a numeric cut-off, a common way to encode a tier or limit",
    "validation": "guards against invalid input by failing early",
    "default_value": "supplies a fallback when a value is missing",
    "null_handling": "handles the case where a value is missing",
    "literal_match": "treats a specific value as a special case",
    "switch": "chooses behaviour by category",
    "filter": "restricts which elements take part",
    "grouping": "organises elements by a key before further processing",
    "aggregation": "computes a summary value",
}


class WhyPoint(BaseModel):
    text: str
    refs: list[str] = Field(default_factory=list)


class WhyResult(BaseModel):
    method_id: str
    start_line: int
    end_line: int
    selection: str
    confirmed: list[WhyPoint] = Field(default_factory=list)
    likely: list[WhyPoint] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)

    def as_text(self) -> str:
        lines = [f"Selected: {self.method_id} L{self.start_line}-{self.end_line}"]
        for title, points in (
            ("CONFIRMED (code or author-written evidence)", self.confirmed),
            ("LIKELY (inference)", self.likely),
        ):
            lines.append(f"{title}:" if points else f"{title}: (none)")
            for p in points:
                refs = f" [{', '.join(p.refs)}]" if p.refs else ""
                lines.append(f"  - {p.text}{refs}")
        lines.append(
            "UNKNOWN (not established by the repository):" if self.unknown else "UNKNOWN: (none)"
        )
        lines += [f"  - {u}" for u in self.unknown]
        return "\n".join(lines)


def _words(text: str) -> set[str]:
    return {t for t in tokenize(text) if len(t) > 2 and not t.isdigit()}


def _clip(text: str, n: int = 160) -> str:
    one = " ".join(text.split())
    return one if len(one) <= n else one[: n - 1] + "…"


def _short(method_id: str) -> str:
    cls, _, sig = method_id.partition("#")
    return f"{cls.rsplit('.', 1)[-1]}.{sig.split('(')[0]}()"


class WhyService:
    def __init__(self, store: KnowledgeStore, reader: SourceReader | None = None) -> None:
        self._store = store
        self._reader = reader

    def why(
        self, method_pk: str, start_line: int | None = None, end_line: int | None = None
    ) -> WhyResult:
        method = self._store.get_method(method_pk)
        if method is None:
            raise LookupError(f"unknown method {method_pk}")
        start = max(method.start_line, start_line or method.start_line)
        end = min(method.end_line, end_line or method.end_line)
        if start > end:
            raise ValueError(f"selection L{start_line}-{end_line} is outside {method.method_id}")
        repo = self._store.repository_of_method(method_pk) or ""
        return _Gather(self._store, self._reader, method, repo, start, end).run()


class _Gather:
    def __init__(
        self,
        store: KnowledgeStore,
        reader: SourceReader | None,
        method: MethodKnowledge,
        repo: str,
        start: int,
        end: int,
    ) -> None:
        self.store, self.m, self.repo, self.start, self.end = store, method, repo, start, end
        self.book = CitationBook()
        self.result = WhyResult(
            method_id=method.method_id,
            start_line=start,
            end_line=end,
            selection=self._selection(reader),
        )
        self.rules = [
            r for r in method.rule_candidates if r.start_line <= end and r.end_line >= start
        ]
        self.stated: list[str] = []  # text of the author-written artifacts found

    def cite(self, file: str, first: int, last: int, kind: str = "source_code") -> str:
        return self.book.cite(file, first, last, kind, self.m.name).label

    def run(self) -> WhyResult:
        self._what_the_code_does()
        self._comments_docs_tests()
        self._callers()
        self._class_docs()
        self._inferences()
        self._unknowns()
        self.result.citations = self.book.citations
        return self.result

    # ---- what the code does ------------------------------------------------------------------

    def _what_the_code_does(self) -> None:
        for r in self.rules:
            what = r.meaning or r.calculation or r.summary
            tail = (
                f"; then {r.action}"
                if r.action and r.kind not in ("grouping", "aggregation")
                else ""
            )
            self.result.confirmed.append(
                WhyPoint(
                    text=f"At L{r.start_line} the code applies: {what}{tail}",
                    refs=[self.cite(self.m.file, r.start_line, r.end_line)],
                )
            )
        if self.rules:
            return
        nodes = [
            n
            for n in self.m.control_flow.walk()
            if n.start_line >= self.start and n.end_line <= self.end
        ]
        summary = "; ".join(t for t in (_node_text(n) for n in nodes[:5]) if t)
        text = (
            f"The selected lines do: {summary}"
            if summary
            else "The selected lines contain no rule-like pattern"
        )
        self.result.confirmed.append(
            WhyPoint(text=text, refs=[self.cite(self.m.file, self.start, self.end)])
        )

    # ---- author-written evidence -------------------------------------------------------------

    def _comments_docs_tests(self) -> None:
        sel_words = _words(self.result.selection)
        for e in self.m.evidence:
            if e.source_type == "comment" and e.relation == "explains":
                self._inline_comment(e)
            elif e.source_type == "comment" and e.relation == "documents":
                self._method_doc(e, sel_words)
            elif e.source_type in ("readme", "documentation"):
                heading = e.snippet.splitlines()[0] if e.snippet else ""
                self.result.confirmed.append(
                    WhyPoint(
                        text=f'{e.file} (section "{heading}") mentions it',
                        refs=[self.cite(e.file, e.start_line, e.end_line, e.source_type)],
                    )
                )
                self.stated.append(e.snippet)
            elif e.source_type == "test":
                self._test(e)

    def _inline_comment(self, e: Evidence) -> None:
        if self.start - NEAR <= e.end_line and e.start_line <= self.end + 1:
            ref = self.cite(e.file, e.start_line, e.end_line, "comment")
            text = f'A nearby comment (L{e.start_line}) says: "{_clip(e.snippet)}"'
            self.result.confirmed.append(WhyPoint(text=text, refs=[ref]))
            self.stated.append(e.snippet)

    def _method_doc(self, e: Evidence, sel_words: set[str]) -> None:
        about_selection = len(sel_words & _words(e.snippet)) >= 2
        text = f'The method\'s documentation says: "{_clip(first_sentence(e.snippet))}"'
        if not about_selection:
            text += " (general documentation of the method, not about these lines specifically)"
        point = WhyPoint(text=text, refs=[self.cite(e.file, e.start_line, e.end_line, "comment")])
        (self.result.confirmed if about_selection else self.result.likely).append(point)
        self.stated.append(e.snippet)

    def _test(self, e: Evidence) -> None:
        ref = [self.cite(e.file, e.start_line, e.end_line, "test")]
        owner = (e.class_name or "").rsplit(".", 1)[-1]
        calls = re.findall(rf"\b{re.escape(self.m.name)}\s*\(([^)]*)\)", e.snippet)
        if calls:
            text = f"Test {owner}.{e.method} calls {self.m.name}({calls[0]})"
            self.result.confirmed.append(WhyPoint(text=text, refs=ref))
        words = " ".join(re.findall(r"[A-Z]?[a-z]+|\d+", e.method or "")).lower()
        self.result.likely.append(
            WhyPoint(
                text=f'The test is named "{words}", which suggests the behaviour it checks '
                "is intended",
                refs=ref,
            )
        )
        self.stated.append(e.snippet)

    def _callers(self) -> None:
        for c in self.m.callers[:4]:
            if c.purpose and c.purpose not in self.stated:
                text = (
                    f"It is called by {_short(c.method_id)} ({c.purpose}), "
                    "which shows how it is used"
                )
                ref = self.cite(self.m.file, self.m.start_line, self.m.end_line)
                self.result.likely.append(WhyPoint(text=text, refs=[ref]))

    def _class_docs(self) -> None:
        for klass in self.store.find_class(self.repo, self.m.class_fqn)[:1]:
            for e in klass.evidence:
                if e.source_type == "comment" and e.relation == "documents":
                    text = f'The class documentation says: "{_clip(first_sentence(e.snippet))}"'
                    ref = self.cite(e.file, e.start_line, e.end_line, "comment")
                    self.result.likely.append(WhyPoint(text=text, refs=[ref]))
                    self.stated.append(e.snippet)

    # ---- inferences and unknowns -------------------------------------------------------------

    def _inferences(self) -> None:
        for r in self.rules:
            if r.kind in _MEANING:
                text = f"L{r.start_line} {_MEANING[r.kind]} (a {r.kind.replace('_', ' ')} pattern)"
                self.result.likely.append(
                    WhyPoint(text=text, refs=[self.cite(self.m.file, r.start_line, r.end_line)])
                )

    def _unknowns(self) -> None:
        corpus = "\n".join(self.stated)
        seen: set[str] = set()
        for r in self.rules:
            for lit in r.literals:
                if lit in TRIVIAL or lit in seen or lit in corpus:
                    continue
                seen.add(lit)
                self.result.unknown.append(
                    f"why {lit} was chosen: no comment, test or document mentions it"
                )
        if not any(p.text.startswith("A nearby comment") for p in self.result.confirmed):
            self.result.unknown.append(
                f"no comment within {NEAR} lines of L{self.start}-{self.end} explains the intent"
            )
        if not any(e.source_type == "test" for e in self.m.evidence):
            self.result.unknown.append(
                "no test exercises this method, so its intended behaviour is not demonstrated"
            )
        self.result.unknown.append("why this logic was introduced (git history is not analyzed)")

    # ---- selection ---------------------------------------------------------------------------

    def _selection(self, reader: SourceReader | None) -> str:
        text = reader.lines(self.m.file, self.start, self.end) if reader else None
        if text is not None:
            return text
        snippet = next((e.snippet for e in self.m.evidence if e.relation == "implements"), "")
        rows = snippet.split("\n")
        return "\n".join(
            rows[max(0, self.start - self.m.start_line) : self.end - self.m.start_line + 1]
        )


def _node_text(n: FlowNode) -> str:
    if n.kind == "mutation":
        return f"set {n.target}"
    if n.kind == "call" and n.callee:
        return f"call {n.callee.name}()"
    if n.kind in ("return", "throw"):
        return f"{n.kind} {n.text}"
    return f"{n.kind} {n.text or ''}".strip()


__all__ = ["RuleCandidate", "WhyPoint", "WhyResult", "WhyService"]
