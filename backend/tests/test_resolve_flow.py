"""Index + resolve end to end (real analyzer) through the service and the CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.cli import main as cli_main
from app.config import Settings
from app.services.indexing_service import IndexingService
from app.services.resolution_service import NotIndexedError, ResolutionService
from tests.conftest import PROJECT


@pytest.fixture
def indexed(isolated_settings: Settings, java_analyzer: object) -> Settings:
    IndexingService(isolated_settings).index(PROJECT)
    return isolated_settings


def test_resolve_requires_an_index(isolated_settings: Settings, java_analyzer: object) -> None:
    with pytest.raises(NotIndexedError, match="index"):
        ResolutionService(isolated_settings).resolve(PROJECT)
    with pytest.raises(ValueError, match="SOURCE_ROOT"):
        ResolutionService(isolated_settings).resolve(None)


def test_resolve_uses_relative_paths_and_reports_a_summary(indexed: Settings) -> None:
    run = ResolutionService(indexed).resolve(PROJECT)
    assert {f.path for f in run.files} >= {"com/acme/service/PricingService.java"}
    assert run.summary.files == 12 and run.summary.classes >= 13
    assert run.summary.calls["project"] > 20
    resolved = [fr for fr in run.resolutions if fr.path.endswith("PricingService.java")]
    assert resolved and resolved[0].classes[0].fqn == "com.acme.service.PricingService"


def test_examples_point_at_real_source_lines(indexed: Settings) -> None:
    run = ResolutionService(indexed).resolve(PROJECT)
    examples = ResolutionService.examples(run, per_reason=2)
    assert {
        "no_such_method",
        "external_unverifiable",
        "possible_lombok_generated",
        "overload_not_disambiguated",
    } <= set(examples)
    assert all(len(v) <= 2 for v in examples.values())
    for reason, items in examples.items():
        for e in items:  # each example's reported line really contains the reported expression
            source = (PROJECT / e.file).read_text().split("\n")
            assert e.text.split("(")[0] in source[e.line - 1], (reason, e)


def test_resolve_after_an_edit_sees_the_new_code(indexed: Settings, tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "A.java").write_text("class A { void f() { g(); } void g() {} }")
    service = IndexingService(indexed)
    service.index(src)
    first = ResolutionService(indexed).resolve(src)
    assert first.summary.calls == {"project": 1, "external": 0, "ambiguous": 0, "unresolved": 0}

    (src / "A.java").write_text("class A { void f() { g(); } }")  # g() removed
    service.index(src)
    second = ResolutionService(indexed).resolve(src)
    assert second.summary.calls["project"] == 0 and second.summary.unresolved_reasons == {
        "no_such_method": 1
    }


def test_cli_resolve(indexed: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["resolve", str(PROJECT), "--examples", "1"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["files"] == 12
    assert report["summary"]["calls"]["project"] > 20
    assert "no_such_method" in report["summary"]["unresolved_reasons"]
    assert report["examples"]["no_such_method"][0]["line"] >= 1


def test_cli_resolve_without_index_fails_clearly(
    isolated_settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_main(["resolve", str(tmp_path)]) == 4
    assert "run `index` first" in capsys.readouterr().err
