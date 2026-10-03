from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.analyzer import AnalyzerUnavailableError, JavaParserAnalyzer, ParsedFile
from app.config import Settings, get_settings
from tests.shop_repo import SHOP_FILES, World

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


@pytest.fixture
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Settings (and the job manager) pointed at a throwaway data dir."""
    from app.services.jobs import get_job_manager

    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("SOURCE_ROOT", "")
    get_settings.cache_clear()
    get_job_manager.cache_clear()
    yield get_settings()
    get_settings.cache_clear()
    get_job_manager.cache_clear()


PROJECT = Path(__file__).parent / "fixtures" / "project"


@pytest.fixture(scope="session")
def shop(java_analyzer: JavaParserAnalyzer, tmp_path_factory: pytest.TempPathFactory) -> World:
    """The shop repository analysed into a knowledge model (see tests/shop_repo.py)."""
    from tests.helpers import analyze_paths, build_knowledge

    root = tmp_path_factory.mktemp("shop")
    for rel, text in SHOP_FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    built = build_knowledge(analyze_paths(java_analyzer, root), docs_root=root)
    return World(root, built)
