"""The semantic code model: what is known about each class and method, with its evidence.

Everything here is built deterministically from static analysis. Where the repository does not
establish something (why a constant was chosen, which override runs, what an unresolved call
does), that is recorded as an explicit `Unknown` rather than left out or guessed.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, Field

from app.analyzer.control_flow import ControlFlow
from app.analyzer.data_flow import MethodDataFlow
from app.analyzer.resolution_models import Origin, ResolutionStatus
from app.analyzer.rule_extractor import RuleCandidate
from app.knowledge.evidence import Evidence

Level = Literal["low", "medium", "high"]


def short_id(prefix: str, *parts: object) -> str:
    """A short opaque id that is safe in URLs, e.g. `m_3f9a1c07d2`."""
    digest = hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:10]
    return f"{prefix}_{digest}"


class Purpose(BaseModel):
    """What a method/class is for. `level` says how much to trust it.

    * comment: the author wrote it. It is a fact that the comment says so (whether the comment is
      still accurate is another matter).
    * name / structure: inferred from naming and from what the code does.
    """

    text: str
    basis: Literal["comment", "name", "structure"]
    level: Literal["fact", "inference"]
    structure: str | None = None  # what the code does, mechanically ("iterates over rows, ...")
    evidence_ids: list[str] = Field(default_factory=list)


class ParameterInfo(BaseModel):
    name: str
    type: str
    var_args: bool = False
    comes_from: list[str] = Field(default_factory=list)  # what callers pass for this parameter


class OutputInfo(BaseModel):
    type: str | None = None
    returns: list[str] = Field(default_factory=list)  # where returned values come from
    side_effects: list[str] = Field(default_factory=list)  # fields/parameters it changes


class CalleeInfo(BaseModel):
    method_id: str | None = None  # resolved project method
    name: str
    owner_type: str | None = None  # external owner type
    status: ResolutionStatus
    origin: Origin | None = None
    site_lines: list[int] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)
    may_dispatch_to: list[str] = Field(
        default_factory=list
    )  # overriding methods (dynamic dispatch)
    reason: str | None = None
    purpose: str | None = None


class CallerInfo(BaseModel):
    method_id: str
    site_lines: list[int]
    ambiguous: bool = False  # the call may or may not target this method
    purpose: str | None = None


class Risk(BaseModel):
    kind: str
    level: Level
    message: str
    line: int | None = None


class Unknown(BaseModel):
    kind: str
    message: str
    line: int | None = None


class Complexity(BaseModel):
    cyclomatic: int
    nesting_depth: int
    lines: int
    statements: int
    expressions: int


class FieldInfo(BaseModel):
    name: str
    type: str
    visibility: str
    modifiers: list[str] = Field(default_factory=list)
    line: int


class MethodKnowledge(BaseModel):
    id: str  # opaque, URL-safe
    method_id: str  # `fqn#signature`
    class_id: str
    class_fqn: str
    name: str
    signature: str
    kind: str  # method | constructor | compact_constructor
    file: str
    file_id: str
    start_line: int
    end_line: int
    visibility: str
    modifiers: list[str] = Field(default_factory=list)
    annotations: list[str] = Field(default_factory=list)
    return_type: str | None = None
    is_test: bool = False
    purpose: Purpose
    parameters: list[ParameterInfo] = Field(default_factory=list)
    output: OutputInfo = Field(default_factory=OutputInfo)
    callees: list[CalleeInfo] = Field(default_factory=list)
    callers: list[CallerInfo] = Field(default_factory=list)
    control_flow: ControlFlow
    data_flow: MethodDataFlow
    rule_candidates: list[RuleCandidate] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    unknowns: list[Unknown] = Field(default_factory=list)
    complexity: Complexity


class ClassKnowledge(BaseModel):
    id: str
    fqn: str
    name: str
    kind: str
    package: str | None = None
    file: str
    file_id: str
    start_line: int
    end_line: int
    visibility: str = "package"
    modifiers: list[str] = Field(default_factory=list)
    annotations: list[str] = Field(default_factory=list)
    superclass: str | None = None
    interfaces: list[str] = Field(default_factory=list)
    purpose: Purpose
    fields: list[FieldInfo] = Field(default_factory=list)
    method_ids: list[str] = Field(default_factory=list)  # MethodKnowledge.id, in source order
    dependencies: list[str] = Field(default_factory=list)  # project classes this one uses
    dependents: list[str] = Field(default_factory=list)  # project classes that use this one
    evidence: list[Evidence] = Field(default_factory=list)
    is_test: bool = False


class KnowledgeStats(BaseModel):
    files: int = 0
    classes: int = 0
    methods: int = 0
    call_edges: int = 0
    rule_candidates: int = 0
    evidence: int = 0
    risks: int = 0
    unknowns: int = 0


class RepositoryKnowledge(BaseModel):
    repository_id: str
    classes: list[ClassKnowledge]
    methods: list[MethodKnowledge]
    stats: KnowledgeStats

    def method(self, method_id: str) -> MethodKnowledge | None:
        return next(
            (m for m in self.methods if m.method_id == method_id or m.id == method_id), None
        )

    def klass(self, key: str) -> ClassKnowledge | None:
        return next((c for c in self.classes if c.fqn == key or c.id == key), None)
