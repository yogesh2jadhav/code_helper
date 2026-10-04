"""Evidence: every semantic statement points back to something a person can open.

Each piece carries where it came from (file, class, method, line range), what kind of source it is
(code, comment, test, README/documentation), a snippet, how directly it supports the claim
(`confidence`) and how it relates to the thing being explained (`relation`).
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from app.analyzer.ast_models import Comment

SourceType = Literal[
    "source_code",
    "comment",
    "test",
    "configuration",
    "sql",
    "readme",
    "documentation",
    "git_history",
]
Confidence = Literal["high", "medium", "low"]
SNIPPET_LIMIT = 800
_CODE_LIKE = re.compile(r"[;{}]\s*$|^\s*(if|for|while|return|int|String)\b.*[;{]")
_TEST_ANNOTATIONS = {"Test", "ParameterizedTest", "RepeatedTest", "TestFactory"}
_TEST_NAME = re.compile(r"(Tests?|IT|Spec)$")


class Evidence(BaseModel):
    id: str
    source_type: SourceType
    file: str
    class_name: str | None = None
    method: str | None = None
    start_line: int
    end_line: int
    snippet: str = ""
    confidence: Confidence = "medium"
    relation: str  # implements | documents | explains | tested_by | mentions | rule:<kind> | ...


def make_evidence(
    source_type: SourceType,
    file: str,
    start_line: int,
    end_line: int,
    snippet: str,
    relation: str,
    *,
    class_name: str | None = None,
    method: str | None = None,
    confidence: Confidence = "medium",
) -> Evidence:
    key = f"{source_type}|{file}|{class_name}|{method}|{start_line}|{end_line}|{relation}"
    return Evidence(
        id=hashlib.sha1(key.encode()).hexdigest()[:12],
        source_type=source_type,
        file=file,
        class_name=class_name,
        method=method,
        start_line=start_line,
        end_line=end_line,
        snippet=snippet[:SNIPPET_LIMIT],
        confidence=confidence,
        relation=relation,
    )


# ==== comments ================================================================


def comment_evidence(
    comments: Iterable[Comment], file: str, class_name: str | None, method: str | None
) -> list[Evidence]:
    """Comments as evidence: javadoc documents, inline comments explain, code-like ones are low."""
    out: list[Evidence] = []
    for c in comments:
        text = c.text.strip()
        if not text:
            continue
        confidence: Confidence
        if _CODE_LIKE.search(text):
            relation, confidence = "commented_code", "low"
        elif c.kind == "javadoc":
            relation, confidence = "documents", "high"
        else:
            relation, confidence = "explains", "medium"
        out.append(
            make_evidence(
                "comment",
                file,
                c.start_line,
                c.end_line,
                text,
                relation,
                class_name=class_name,
                method=method,
                confidence=confidence,
            )
        )
    return out


def first_sentence(text: str) -> str:
    """The first sentence of a comment, with javadoc tags and markup removed."""
    cleaned = re.sub(r"\{@\w+\s+([^}]*)\}", r"\1", text)
    cleaned = re.split(r"\n\s*@\w+", cleaned)[0]
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    match = re.match(r"(.+?[.!?])(\s|$)", cleaned)
    return (match.group(1) if match else cleaned).strip()


# ==== tests ===================================================================


def is_test_location(path: str, class_name: str, annotations: Iterable[str] = ()) -> bool:
    """A method counts as test code if it lives under a test directory, is in a *Test class, or
    carries a test annotation."""
    segments = {s.lower() for s in path.replace("\\", "/").split("/")}
    simple = class_name.rsplit(".", 1)[-1]
    return (
        "test" in segments
        or "tests" in segments
        or bool(_TEST_NAME.search(simple))
        or bool(_TEST_ANNOTATIONS & set(annotations))
    )


# ==== README / documentation ==================================================


class DocSection(BaseModel):
    file: str
    heading: str
    start_line: int
    end_line: int
    text: str
    is_readme: bool = False


def load_docs(
    root: Path,
    ignore_dirs: Iterable[str] = (".git", "node_modules", "target", "build"),
    max_bytes: int = 200_000,
) -> list[DocSection]:
    """Markdown sections of README*.md and any *.md under docs/ (read-only)."""
    ignore = set(ignore_dirs)
    sections: list[DocSection] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in ignore)
        for name in sorted(filenames):
            path = Path(dirpath, name)
            rel = path.relative_to(root).as_posix()
            is_readme = name.lower().startswith("readme") and name.lower().endswith(".md")
            in_docs = rel.lower().startswith("docs/") and name.lower().endswith(".md")
            if not (is_readme or in_docs):
                continue
            try:
                if path.stat().st_size > max_bytes:
                    continue
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            sections.extend(_split_markdown(rel, text, is_readme))
    return sections


def _split_markdown(file: str, text: str, is_readme: bool) -> list[DocSection]:
    lines = text.split("\n")
    out: list[DocSection] = []
    heading, start = "(top)", 1
    buffer: list[str] = []

    def flush(end: int) -> None:
        body = "\n".join(buffer).strip()
        if body:
            out.append(
                DocSection(
                    file=file,
                    heading=heading,
                    start_line=start,
                    end_line=end,
                    text=body,
                    is_readme=is_readme,
                )
            )

    for number, line in enumerate(lines, 1):
        if re.match(r"^#{1,6}\s+\S", line):
            flush(number - 1)
            heading, start, buffer = line.lstrip("# ").strip(), number, []
        else:
            buffer.append(line)
    flush(len(lines))
    return out


class DocIndex:
    """Finds the documentation sections that mention a class or method."""

    def __init__(self, sections: list[DocSection]) -> None:
        self.sections = sections
        self._tokens: dict[str, list[int]] = {}
        for i, section in enumerate(sections):
            for token in set(
                re.findall(r"[A-Za-z_][A-Za-z0-9_]*", section.text + " " + section.heading)
            ):
                self._tokens.setdefault(token, []).append(i)

    def mentions_class(self, simple_name: str) -> list[DocSection]:
        if len(simple_name) < 4:  # too short to be a meaningful mention
            return []
        return [self.sections[i] for i in self._tokens.get(simple_name, [])]

    def mentions_method(
        self, class_name: str, method: str, qualified_only: bool = False
    ) -> list[DocSection]:
        """Sections that call the method (`name(`). Short names must be written `Class.name(`."""
        qualifier = (
            rf"{re.escape(class_name)}\." if qualified_only else rf"(?:{re.escape(class_name)}\.)?"
        )
        pattern = re.compile(rf"\b{qualifier}{re.escape(method)}\s*\(")
        return [
            self.sections[i]
            for i in self._tokens.get(method, [])
            if pattern.search(self.sections[i].text)
        ]


def doc_evidence(
    sections: Iterable[DocSection],
    class_name: str | None,
    method: str | None,
    confidence: Confidence,
) -> list[Evidence]:
    return [
        make_evidence(
            "readme" if s.is_readme else "documentation",
            s.file,
            s.start_line,
            s.end_line,
            f"{s.heading}\n{s.text}",
            "mentions",
            class_name=class_name,
            method=method,
            confidence=confidence,
        )
        for s in sections
    ]
