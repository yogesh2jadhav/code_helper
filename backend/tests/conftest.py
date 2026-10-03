from __future__ import annotations

from pathlib import Path

import pytest

from app.analyzer import AnalyzerUnavailableError, JavaParserAnalyzer, ParsedFile
from app.config import get_settings

FIXTURES = Path(__file__).parent / "fixtures" / "java"


@pytest.fixture(scope="session")
def java_analyzer() -> JavaParserAnalyzer:
    """Real analyzer. Fails (does not skip) when the jar/JDK is missing so gaps are visible."""
    settings = get_settings()
    analyzer = JavaParserAnalyzer(settings.java_bin, settings.analyzer_jar)
    try:
        analyzer._check_environment()
    except AnalyzerUnavailableError as exc:
        pytest.fail(f"Java analyzer unavailable: {exc}. Run `make analyzer` and set JAVA_BIN.")
    return analyzer


@pytest.fixture(scope="session")
def parsed(java_analyzer: JavaParserAnalyzer) -> dict[str, ParsedFile]:
    """Every fixture parsed in one JVM run, keyed by path relative to the fixtures dir."""
    paths = sorted(FIXTURES.rglob("*.java"))
    results = java_analyzer.analyze_files(paths)
    return {p.relative_to(FIXTURES).as_posix(): r for p, r in zip(paths, results, strict=True)}
