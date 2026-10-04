"""Check and structure an LLM answer: its sections, its citations, whether it admits unknowns."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from app.llm.ollama_client import strip_reasoning
from app.llm.prompts import EXPLAIN_SECTIONS

_CITATION = re.compile(r"\[(E\d+(?:\s*[,;]\s*E\d+)*)\]")
_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s*|\*\*|__)?(?:\d+[.)]\s*)?(?P<title>[A-Za-z][^\n*_#:]{2,60}?)\s*(?:\*\*|__)?\s*:?\s*$"
)
_NOT_ESTABLISHED = re.compile(
    r"not\s+establish|not\s+established|does\s+not\s+say|no\s+(?:comment|evidence|test|documentation)|"
    r"unknown|cannot\s+be\s+determined|unclear",
    re.IGNORECASE,
)


class Section(BaseModel):
    key: str  # the canonical heading, or the heading as written if it is not a known one
    title: str
    text: str


class ParsedAnswer(BaseModel):
    raw: str
    sections: list[Section]
    citations_used: list[str] = Field(default_factory=list)  # labels, in order of first use
    invalid_citations: list[str] = Field(default_factory=list)  # labels that do not exist
    missing_sections: list[str] = Field(default_factory=list)
    states_unknowns: bool = False  # says somewhere that something is not established

    def section(self, key: str) -> Section | None:
        return next((s for s in self.sections if s.key == key), None)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z ]", "", text.lower().replace("-", "")).strip()


def _canonical(title: str, expected: list[str]) -> str | None:
    t = _norm(title)
    for name in expected:
        n = _norm(name)
        if t == n or t.startswith(n) or n in t:
            return name
    return None


def parse_answer(
    text: str,
    valid_labels: set[str],
    expected_sections: list[str] | None = None,
) -> ParsedAnswer:
    """Split into sections by heading and validate citations against the labels that exist."""
    expected = expected_sections if expected_sections is not None else EXPLAIN_SECTIONS
    clean = strip_reasoning(text)
    sections: list[Section] = []
    current: tuple[str, str, list[str]] | None = None
    preamble: list[str] = []
    for line in clean.split("\n"):
        match = _HEADING.match(line) if line.strip() else None
        key = _canonical(match.group("title"), expected) if match else None
        if key is not None and match is not None:
            if current:
                sections.append(
                    Section(key=current[0], title=current[1], text="\n".join(current[2]).strip())
                )
            current = (key, match.group("title").strip(), [])
        elif current:
            current[2].append(line)
        else:
            preamble.append(line)
    if current:
        sections.append(
            Section(key=current[0], title=current[1], text="\n".join(current[2]).strip())
        )
    if "".join(preamble).strip():
        sections.insert(0, Section(key="preamble", title="", text="\n".join(preamble).strip()))

    used: list[str] = []
    for group in _CITATION.findall(clean):
        for label in re.split(r"\s*[,;]\s*", group):
            if label not in used:
                used.append(label)
    present = {s.key for s in sections}
    return ParsedAnswer(
        raw=clean,
        sections=sections,
        citations_used=used,
        invalid_citations=[c for c in used if c not in valid_labels],
        missing_sections=[e for e in expected if e not in present],
        states_unknowns=bool(_NOT_ESTABLISHED.search(clean)),
    )
