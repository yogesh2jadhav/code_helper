"""Output models for symbol resolution.

Every reference is classified, never silently assumed:

* `resolved`   – exactly one target was determined.
* `ambiguous`  – several targets remain (candidates are listed); not guessed between.
* `unresolved` – no target could be determined; `reason` says why.

`origin` says where a resolved target lives: `project` (source in this repository, so file/line
are known), `external` (a type outside the repository: the type is known, but its members cannot
be verified because there is no source), `local` (a variable/parameter), `primitive`, or
`type_variable`.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"


class Origin(StrEnum):
    PROJECT = "project"
    EXTERNAL = "external"
    LOCAL = "local"
    PRIMITIVE = "primitive"
    TYPE_VARIABLE = "type_variable"


class TypeResolution(BaseModel):
    """A resolved type reference such as the `Visit` in `List<Visit>`."""

    text: str  # as written in source
    status: ResolutionStatus
    kind: str = "class"  # class | primitive | type_variable | wildcard | union | null | unknown
    origin: Origin | None = None
    fqn: str | None = None
    dims: int = 0
    args: list[TypeResolution] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)
    reason: str | None = None


class Resolution(BaseModel):
    """Resolution of one expression (name use, call, field access, creation, declaration)."""

    expression_id: int
    kind: str  # see SymbolResolver; e.g. method, local_variable, field, type, constructor
    status: ResolutionStatus
    origin: Origin | None = None
    name: str
    owner_type: str | None = None  # fqn of the declaring/owning type for members
    symbol_id: str | None = None  # `fqn#signature` for methods, `fqn#name` for fields
    file: str | None = None  # file of the *target* (None for external/unknown targets)
    line: int | None = None  # line of the target's declaration
    site_line: int = 0  # line of the reference itself, in the file being resolved
    type_ref: TypeResolution | None = None  # for kind == "type": the type itself
    value_type: TypeResolution | None = None  # static type of the value, when known
    candidates: list[str] = Field(default_factory=list)
    reason: str | None = None
    implicit: bool = False  # target is compiler-provided (default constructor, record accessor)


class FieldTypeResolution(BaseModel):
    name: str
    type: TypeResolution


class MethodResolution(BaseModel):
    class_fqn: str
    method_id: str
    name: str
    signature: str
    kind: str
    parameters: list[TypeResolution] = Field(default_factory=list)
    return_type: TypeResolution | None = None
    throws: list[TypeResolution] = Field(default_factory=list)
    refs: list[Resolution] = Field(default_factory=list)


class ClassResolution(BaseModel):
    fqn: str
    kind: str
    superclass: TypeResolution | None = None
    interfaces: list[TypeResolution] = Field(default_factory=list)
    fields: list[FieldTypeResolution] = Field(default_factory=list)
    methods: list[MethodResolution] = Field(default_factory=list)


class FileResolution(BaseModel):
    file_id: str
    path: str
    classes: list[ClassResolution] = Field(default_factory=list)


class ResolutionSummary(BaseModel):
    files: int
    classes: int
    methods: int
    references: int
    by_kind: dict[str, dict[str, int]]  # kind -> status -> count
    calls: dict[str, int]  # project / external / ambiguous / unresolved
    unresolved_reasons: dict[str, int]
    ambiguous_reasons: dict[str, int]


TypeResolution.model_rebuild()
