"""Retrieval documents: what gets indexed, as separate kinds, with filterable metadata."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator
from typing import Literal

from pydantic import BaseModel

from app.knowledge.evidence import Evidence
from app.knowledge.models import ClassKnowledge, MethodKnowledge
from app.knowledge.source import SourceReader

DocType = Literal[
    "method_source", "class_source", "rule_candidate", "evidence", "test", "documentation"
]
MAX_TEXT = 3000


class Document(BaseModel):
    id: str
    type: DocType
    text: str
    repository_id: str
    file_path: str
    class_name: str = ""
    method_name: str = ""
    line_start: int = 0
    line_end: int = 0
    symbol_ids: list[str] = []

    @property
    def hash(self) -> str:
        payload = json.dumps(
            [self.type, self.text, self.file_path, self.symbol_ids, self.line_start, self.line_end],
            sort_keys=True,
        )
        return hashlib.sha1(payload.encode()).hexdigest()[:16]

    def metadata(self) -> dict[str, str | int | float | bool]:
        """Flat scalar metadata, as vector stores require."""
        return {
            "repository_id": self.repository_id,
            "source_type": self.type,
            "file_path": self.file_path,
            "class_name": self.class_name,
            "method_name": self.method_name,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "symbol_ids": ",".join(self.symbol_ids),
            "hash": self.hash,
        }


def build_documents(
    repository_id: str,
    classes: Iterable[ClassKnowledge],
    methods: Iterable[MethodKnowledge],
    reader: SourceReader | None = None,
) -> Iterator[Document]:
    """One document per method, class, rule candidate and test, plus one per distinct
    comment/README/doc section (an evidence item shared by several methods is indexed once, with
    all the symbols it relates to)."""
    by_class: dict[str, list[MethodKnowledge]] = {}
    evidence: dict[str, Document] = {}
    for m in methods:
        yield from _method_documents(repository_id, m, reader)
        by_class.setdefault(m.class_id, []).append(m)
        for e in m.evidence:
            _merge_evidence(evidence, repository_id, e, m.class_fqn, [m.method_id])
    for c in classes:
        yield _class_document(repository_id, c, by_class.get(c.id, []))
        for e in c.evidence:
            _merge_evidence(evidence, repository_id, e, c.fqn, [c.fqn])
    yield from evidence.values()


def _method_documents(
    repo: str, m: MethodKnowledge, reader: SourceReader | None
) -> Iterator[Document]:
    source = reader.lines(m.file, m.start_line, m.end_line) if reader else None
    if source is None:  # fall back to the stored snippet of the implementation
        source = next((e.snippet for e in m.evidence if e.relation == "implements"), "")
    head = f"{m.class_fqn}.{m.name} {m.signature}\npurpose: {m.purpose.text}\n"
    kind: DocType = "test" if m.is_test else "method_source"
    yield Document(
        id=f"{'ts' if m.is_test else 'ms'}:{m.id}",
        type=kind,
        text=(head + source)[:MAX_TEXT],
        repository_id=repo,
        file_path=m.file,
        class_name=m.class_fqn,
        method_name=m.name,
        line_start=m.start_line,
        line_end=m.end_line,
        symbol_ids=[m.method_id],
    )
    for r in m.rule_candidates:
        text = f"{m.class_fqn}.{m.name}: {r.summary}\ncondition: {r.condition or ''}\n"
        text += (
            f"scope: {r.scope or ''} calculation: {r.calculation or ''} action: {r.action or ''}"
        )
        yield Document(
            id=f"rc:{r.id}:{m.id}",
            type="rule_candidate",
            text=text[:MAX_TEXT],
            repository_id=repo,
            file_path=m.file,
            class_name=m.class_fqn,
            method_name=m.name,
            line_start=r.start_line,
            line_end=r.end_line,
            symbol_ids=[m.method_id],
        )


def _class_document(repo: str, c: ClassKnowledge, methods: list[MethodKnowledge]) -> Document:
    sigs = ", ".join(m.signature for m in methods[:40])
    fields = ", ".join(f"{f.type} {f.name}" for f in c.fields[:30])
    text = (
        f"{c.kind} {c.fqn}\npurpose: {c.purpose.text}\n{c.purpose.structure or ''}\n"
        f"fields: {fields}\nmethods: {sigs}\nuses: {', '.join(c.dependencies[:20])}"
    )
    return Document(
        id=f"cs:{c.id}",
        type="class_source",
        text=text[:MAX_TEXT],
        repository_id=repo,
        file_path=c.file,
        class_name=c.fqn,
        line_start=c.start_line,
        line_end=c.end_line,
        symbol_ids=[c.fqn],
    )


def _merge_evidence(
    into: dict[str, Document], repo: str, e: Evidence, owner: str, symbols: list[str]
) -> None:
    if e.source_type not in ("comment", "readme", "documentation"):
        return
    doc_type: DocType = "evidence" if e.source_type == "comment" else "documentation"
    key = f"ev:{e.id}"
    existing = into.get(key)
    if existing is not None:
        merged = sorted({*existing.symbol_ids, *symbols})
        into[key] = existing.model_copy(update={"symbol_ids": merged})
        return
    into[key] = Document(
        id=key,
        type=doc_type,
        text=e.snippet[:MAX_TEXT],
        repository_id=repo,
        file_path=e.file,
        class_name=e.class_name or owner,
        method_name=e.method or "",
        line_start=e.start_line,
        line_end=e.end_line,
        symbol_ids=sorted(symbols),
    )
