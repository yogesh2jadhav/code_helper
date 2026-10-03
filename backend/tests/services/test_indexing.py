from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import pytest

from app.analyzer.ast_models import ParsedFile
from app.analyzer.base import AnalyzerUnavailableError
from app.analyzer.store import AnalysisStore
from app.config import Settings
from app.services.indexing_service import IndexingService, Progress

Status = Literal["ok", "parse_error", "analyzer_error"]


class FakeAnalyzer:
    """Deterministic stand-in for the JVM analyzer that records every call."""

    def __init__(
        self, statuses: dict[str, Status] | None = None, fail_on_call: int | None = None
    ) -> None:
        self.calls: list[list[str]] = []
        self.statuses = statuses or {}
        self.fail_on_call = fail_on_call

    def analyze_files(self, paths: Sequence[Path]) -> list[ParsedFile]:
        self.calls.append([p.name for p in paths])
        if self.fail_on_call == len(self.calls):
            raise AnalyzerUnavailableError("simulated crash")
        out: list[ParsedFile] = []
        for p in paths:
            status = self.statuses.get(p.name, "ok")
            errors = [] if status == "ok" else ["x"]
            out.append(ParsedFile(path=str(p), status=status, errors=errors))
        return out

    @property
    def analyzed_names(self) -> list[str]:
        return [n for call in self.calls for n in call]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for name in "ABCDE":
        (root / f"{name}.java").write_text(f"class {name} {{}}")
    return root


@pytest.fixture
def settings(isolated_settings: Settings) -> Settings:
    return isolated_settings


def test_first_run_analyzes_everything_then_second_run_does_nothing(
    settings: Settings, repo: Path
) -> None:
    analyzer = FakeAnalyzer()
    service = IndexingService(settings, analyzer)
    first = service.index(repo)
    assert (first.total_files, first.analyzed, first.up_to_date, first.ok) == (5, 5, 0, 5)

    analyzer.calls.clear()
    second = service.index(repo)
    assert (second.analyzed, second.up_to_date) == (0, 5)
    assert analyzer.calls == []  # no JVM launch for an unchanged repository


def test_only_changed_and_new_files_are_reanalyzed(settings: Settings, repo: Path) -> None:
    analyzer = FakeAnalyzer()
    service = IndexingService(settings, analyzer)
    service.index(repo)
    analyzer.calls.clear()

    (repo / "B.java").write_text("class B { int changed; }")
    (repo / "F.java").write_text("class F {}")
    summary = service.index(repo)
    assert sorted(analyzer.analyzed_names) == ["B.java", "F.java"]
    assert (summary.analyzed, summary.up_to_date, summary.total_files) == (2, 4, 6)


def test_removed_files_are_pruned_from_stored_analysis(settings: Settings, repo: Path) -> None:
    service = IndexingService(settings, FakeAnalyzer())
    first = service.index(repo)
    store = AnalysisStore(settings.db_path)
    assert store.count(first.repository_id) == 5

    (repo / "E.java").unlink()
    second = service.index(repo)
    assert second.pruned == 1 and store.count(first.repository_id) == 4


def test_analyzer_errors_are_retried_but_parse_errors_are_not(
    settings: Settings, repo: Path
) -> None:
    flaky = FakeAnalyzer(statuses={"A.java": "analyzer_error", "B.java": "parse_error"})
    service = IndexingService(settings, flaky)
    first = service.index(repo)
    assert (first.ok, first.parse_errors, first.analyzer_errors) == (3, 1, 1)

    healthy = FakeAnalyzer()
    second = IndexingService(settings, healthy).index(repo)
    assert healthy.analyzed_names == ["A.java"]  # B's parse error is a stable result
    assert (second.analyzed, second.ok) == (1, 1)


def test_force_reanalyzes_everything(settings: Settings, repo: Path) -> None:
    analyzer = FakeAnalyzer()
    service = IndexingService(settings, analyzer)
    service.index(repo)
    analyzer.calls.clear()
    assert service.index(repo, force=True).analyzed == 5
    assert sorted(analyzer.analyzed_names) == ["A.java", "B.java", "C.java", "D.java", "E.java"]


def test_batching_and_progress_reporting(settings: Settings, repo: Path) -> None:
    analyzer = FakeAnalyzer()
    events: list[Progress] = []
    IndexingService(settings, analyzer).index(repo, batch_size=2, on_progress=events.append)

    assert [len(c) for c in analyzer.calls] == [2, 2, 1]
    assert events[0].stage == "scan"
    analyze = [(e.done, e.total) for e in events if e.stage == "analyze"]
    assert analyze == [(0, 5), (2, 5), (4, 5), (5, 5)]
    assert events[-1] == Progress("finalize", 5, 5)


def test_interrupted_run_resumes_with_only_the_remaining_files(
    settings: Settings, repo: Path
) -> None:
    crashing = FakeAnalyzer(fail_on_call=2)
    with pytest.raises(AnalyzerUnavailableError):
        IndexingService(settings, crashing).index(repo, batch_size=2)
    assert len(crashing.calls) == 2  # first batch (2 files) was persisted before the crash

    resumed = FakeAnalyzer()
    summary = IndexingService(settings, resumed).index(repo, batch_size=2)
    assert len(resumed.analyzed_names) == 3
    assert summary.up_to_date == 2 and summary.analyzed == 3


def test_stored_analysis_round_trips(settings: Settings, repo: Path) -> None:
    service = IndexingService(settings, FakeAnalyzer())
    summary = service.index(repo)
    store = AnalysisStore(settings.db_path)
    file_id = next(iter(store.states(summary.repository_id)))
    loaded = store.load(file_id)
    assert loaded is not None and loaded.status == "ok"
    assert store.load("missing") is None


def test_missing_root_is_a_clear_error(settings: Settings) -> None:
    with pytest.raises(ValueError, match="SOURCE_ROOT"):
        IndexingService(settings, FakeAnalyzer()).index(None)
