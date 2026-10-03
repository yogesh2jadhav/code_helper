"""Shared helpers for tests that need parsed + resolved Java built from inline sources."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.analyzer import JavaParserAnalyzer
from app.analyzer.ast_models import Method
from app.analyzer.resolution_models import FileResolution, Resolution
from app.analyzer.symbol_resolver import SymbolResolver
from app.analyzer.symbol_table import AnalyzedFile, SymbolTable


@dataclass
class Analysis:
    files: list[AnalyzedFile]
    table: SymbolTable
    resolver: SymbolResolver
    resolutions: list[FileResolution]


def analyze_paths(analyzer: JavaParserAnalyzer, root: Path) -> Analysis:
    paths = sorted(root.rglob("*.java"))
    parsed = analyzer.analyze_files(paths)
    files = [
        AnalyzedFile(str(i), p.relative_to(root).as_posix(), r)
        for i, (p, r) in enumerate(zip(paths, parsed, strict=True))
    ]
    table = SymbolTable(files)
    resolver = SymbolResolver(table)
    return Analysis(files, table, resolver, resolver.resolve_all(files))


def analyze_sources(
    analyzer: JavaParserAnalyzer, tmp_path: Path, sources: dict[str, str]
) -> Analysis:
    """Write `{relative path: java source}` under tmp_path and analyze + resolve it."""
    for rel, text in sources.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return analyze_paths(analyzer, tmp_path)


ParamNames = Callable[[str], "list[str] | None"]


class ByMethod[T]:
    """Build something per method of an analysis; look results up by `Class#signature` suffix."""

    def __init__(
        self,
        analysis: Analysis,
        build: Callable[[Method, str, dict[int, Resolution], ParamNames], T],
    ) -> None:
        asts: dict[tuple[str, str], Method] = {}
        for f in analysis.files:
            stack = list(f.parsed.types)
            while stack:
                t = stack.pop()
                stack.extend(t.nested_types)
                for m in [*t.methods, *t.constructors]:
                    asts[(t.qualified_name, m.signature)] = m
        params: dict[str, list[str]] = {}
        for classes in analysis.table.classes.values():
            for cls in classes:
                symbols = [m for ms in cls.methods.values() for m in ms] + cls.constructors
                params.update({m.id: m.param_names for m in symbols})
        self.results: dict[str, T] = {}
        for fr in analysis.resolutions:
            for cr in fr.classes:
                for mr in cr.methods:
                    refs = {r.expression_id: r for r in mr.refs}
                    self.results[mr.method_id] = build(
                        asts[(cr.fqn, mr.signature)], mr.method_id, refs, params.get
                    )

    def __call__(self, suffix: str) -> T:
        matches = [v for k, v in self.results.items() if k.endswith(suffix)]
        assert len(matches) == 1, f"{suffix}: {len(matches)} matches"
        return matches[0]
