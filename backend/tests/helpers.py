"""Shared helpers for tests that need parsed + resolved Java built from inline sources."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.analyzer import JavaParserAnalyzer
from app.analyzer.resolution_models import FileResolution
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
