"""Parser-independent AST models. The JSON wire format uses camelCase (see Model.java)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class _Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class Annotation(_Model):
    name: str
    text: str


class Import(_Model):
    name: str
    is_static: bool
    is_asterisk: bool


class Parameter(_Model):
    name: str
    type: str
    var_args: bool = False
    is_final: bool = False
    annotations: list[Annotation] = Field(default_factory=list)


class FieldDecl(_Model):
    name: str
    type: str
    visibility: str
    modifiers: list[str] = Field(default_factory=list)
    annotations: list[Annotation] = Field(default_factory=list)
    start_line: int
    end_line: int
    initializer: str | None = None


StatementKind = Literal[
    "if", "else_if", "else", "switch", "case", "default", "for", "foreach", "while", "do",
    "try", "catch", "finally", "return", "throw", "break", "continue",
]
ExpressionKind = Literal[
    "method_call", "field_access", "assignment", "object_creation", "lambda", "method_ref",
    "ternary", "comparison", "null_check", "logical", "switch_expr", "variable_declaration",
]


class Statement(_Model):
    id: int
    parent_id: int | None = None
    kind: StatementKind
    start_line: int
    end_line: int
    depth: int
    text: str | None = None


class Expression(_Model):
    id: int
    statement_id: int | None = None
    kind: ExpressionKind
    start_line: int
    end_line: int
    text: str
    name: str | None = None
    scope: str | None = None
    type: str | None = None
    arg_count: int | None = None
    operator: str | None = None
    # Syntactic heuristics: stream_source, stream_op, collector, optional, predicate,
    # comparison, null_check, anonymous_class, initialized
    tags: list[str] = Field(default_factory=list)


class Comment(_Model):
    kind: Literal["javadoc", "block", "line"]
    start_line: int
    end_line: int
    text: str


class Method(_Model):
    name: str
    signature: str
    kind: Literal["method", "constructor", "compact_constructor"]
    visibility: str
    modifiers: list[str] = Field(default_factory=list)
    is_static: bool = False
    is_final: bool = False
    is_abstract: bool = False
    return_type: str | None = None
    parameters: list[Parameter] = Field(default_factory=list)
    annotations: list[Annotation] = Field(default_factory=list)
    throws_types: list[str] = Field(default_factory=list)
    type_parameters: list[str] = Field(default_factory=list)
    start_line: int
    end_line: int
    source_text: str
    has_body: bool
    cyclomatic_complexity: int
    max_nesting_depth: int
    comments: list[Comment] = Field(default_factory=list)
    statements: list[Statement] = Field(default_factory=list)
    expressions: list[Expression] = Field(default_factory=list)


class TypeDecl(_Model):
    kind: Literal["class", "interface", "enum", "record", "annotation", "unknown"]
    name: str
    qualified_name: str
    visibility: str
    modifiers: list[str] = Field(default_factory=list)
    superclass: str | None = None
    interfaces: list[str] = Field(default_factory=list)
    annotations: list[Annotation] = Field(default_factory=list)
    type_parameters: list[str] = Field(default_factory=list)
    enum_constants: list[str] = Field(default_factory=list)
    record_components: list[Parameter] = Field(default_factory=list)
    fields: list[FieldDecl] = Field(default_factory=list)
    constructors: list[Method] = Field(default_factory=list)
    methods: list[Method] = Field(default_factory=list)
    nested_types: list[TypeDecl] = Field(default_factory=list)
    start_line: int
    end_line: int
    comments: list[Comment] = Field(default_factory=list)


class ParsedFile(_Model):
    path: str
    status: Literal["ok", "parse_error", "analyzer_error"]
    errors: list[str] = Field(default_factory=list)
    package_name: str | None = None
    imports: list[Import] = Field(default_factory=list)
    types: list[TypeDecl] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"
