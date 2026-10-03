"""The benchmark dataset: for each method, what a correct analysis must contain.

Expectations are written by hand from reading the fixture source, never generated from the
system's own output (that would only measure self-agreement).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class ExpectedPurpose(BaseModel):
    mentions: list[str] = Field(default_factory=list)  # words the purpose must contain
    basis: str | None = None  # comment | name | structure
    level: str | None = None  # fact | inference


class ExpectedRule(BaseModel):
    kind: str
    literal: str | None = None  # a literal the rule candidate must carry
    contains: str | None = None  # text the condition/meaning must contain


class ExpectedMethod(BaseModel):
    method: str  # method_id, e.g. bench.Cart#calculateTotal(double,int)
    purpose: ExpectedPurpose = Field(default_factory=ExpectedPurpose)
    inputs: list[str] = Field(default_factory=list)  # "type name"
    output: str | None = None  # declared return type
    stages: list[str] = Field(default_factory=list)  # top-level control-flow kinds
    rules: list[ExpectedRule] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)  # "Owner.name"
    data_flow: list[tuple[str, str]] = Field(default_factory=list)  # value reaches target
    unknowns: list[str] = Field(default_factory=list)  # substrings of statements of ignorance
    note: str = ""  # why this method is in the benchmark


class Benchmark(BaseModel):
    name: str
    source: str = "src"  # directory (relative to the benchmark file) holding the Java code
    description: str = ""
    methods: list[ExpectedMethod]

    @classmethod
    def load(cls, path: Path) -> Benchmark:
        file = path / "expected.json" if path.is_dir() else path
        return cls.model_validate_json(file.read_text(encoding="utf-8"))

    def source_root(self, path: Path) -> Path:
        base = path if path.is_dir() else path.parent
        return (base / self.source).resolve()
