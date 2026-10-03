"""Pipeline behaviour: when the knowledge model is rebuilt and when it is reused."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.cli import main as cli_main
from app.config import Settings
from app.knowledge.store import KnowledgeStore
from app.scanner.scanner import repository_id_for
from app.services.knowledge_service import KnowledgeService
from app.services.pipeline_service import PipelineService
from app.services.resolution_service import NotIndexedError
from tests.conftest import PROJECT

SRC = {
    "src/main/p/A.java": "package p;\n/** Adds. */\npublic class A { int add(int a, int b) { return a + b; } }\n",
    "src/main/p/B.java": "package p;\npublic class B { int use(A a) { return a.add(1, 2); } }\n",
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for rel, text in SRC.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (root / "README.md").write_text("# Demo\nThe A class adds numbers.\n")
    return root


def test_first_run_builds_then_unchanged_repo_is_skipped(
    isolated_settings: Settings, java_analyzer: object, repo: Path
) -> None:
    pipeline = PipelineService(isolated_settings)
    first = pipeline.run(repo)
    assert first.knowledge is not None and first.knowledge.skipped is False
    assert first.knowledge.stats.classes == 2 and first.knowledge.stats.methods == 2

    second = pipeline.run(repo)
    assert second.knowledge is not None and second.knowledge.skipped is True
    assert second.knowledge.run.id == first.knowledge.run.id  # no new run was recorded
    assert second.knowledge.stats == first.knowledge.stats
    assert second.index.analyzed == 0


def test_editing_code_rebuilds_the_model(
    isolated_settings: Settings, java_analyzer: object, repo: Path
) -> None:
    pipeline = PipelineService(isolated_settings)
    first = pipeline.run(repo)
    (repo / "src/main/p/B.java").write_text(
        "package p;\npublic class B { int use(A a) { return a.add(1, 2) + a.add(3, 4); } }\n"
    )
    second = pipeline.run(repo)
    assert second.knowledge is not None and second.knowledge.skipped is False
    assert second.knowledge.run.id > (first.knowledge.run.id if first.knowledge else 0)
    store = KnowledgeStore(isolated_settings.db_path)
    rid = repository_id_for(repo.resolve())
    (use,) = store.find_method(rid, "p.B#use(A)")
    assert use.callees[0].site_lines == [2] and len(store.calls_from(rid, "p.B#use(A)")) == 2


def test_editing_the_readme_rebuilds_the_model(
    isolated_settings: Settings, java_analyzer: object, repo: Path
) -> None:
    pipeline = PipelineService(isolated_settings)
    pipeline.run(repo)
    (repo / "README.md").write_text("# Demo\nThe A class adds numbers.\n\nMore about A here.\n")
    again = pipeline.run(repo)
    assert again.knowledge is not None and again.knowledge.skipped is False


def test_force_rebuilds_even_when_nothing_changed(
    isolated_settings: Settings, java_analyzer: object, repo: Path
) -> None:
    pipeline = PipelineService(isolated_settings)
    pipeline.run(repo)
    forced = pipeline.run(repo, force=True)
    assert forced.knowledge is not None and forced.knowledge.skipped is False
    assert forced.index.analyzed == 2  # force re-analyzes too


def test_knowledge_can_be_skipped_entirely(
    isolated_settings: Settings, java_analyzer: object, repo: Path
) -> None:
    result = PipelineService(isolated_settings).run(repo, knowledge=False)
    assert result.knowledge is None
    store = KnowledgeStore(isolated_settings.db_path)
    assert store.count_classes(repository_id_for(repo.resolve())) == 0


def test_test_analysis_flag_is_part_of_the_fingerprint(
    isolated_settings: Settings, java_analyzer: object, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import get_settings

    PipelineService(isolated_settings).run(repo)
    monkeypatch.setenv("ENABLE_TEST_ANALYSIS", "false")
    get_settings.cache_clear()
    flipped = PipelineService(get_settings()).run(repo)
    assert flipped.knowledge is not None and flipped.knowledge.skipped is False


def test_building_without_an_index_is_a_clear_error(
    isolated_settings: Settings, repo: Path
) -> None:
    with pytest.raises(NotIndexedError, match="index"):
        KnowledgeService(isolated_settings).build(repo)
    with pytest.raises(ValueError, match="SOURCE_ROOT"):
        KnowledgeService(isolated_settings).build(None)


def test_project_fixture_knowledge_is_queryable_after_the_pipeline(
    isolated_settings: Settings, java_analyzer: object
) -> None:
    PipelineService(isolated_settings).run(PROJECT)
    store = KnowledgeStore(isolated_settings.db_path)
    rid = repository_id_for(PROJECT.resolve())
    assert store.count_classes(rid) == 13
    (method,) = store.find_method(rid, "com.acme.service.BasePricing#base(int)")
    assert [c.method_id.split("#")[1] for c in method.callers] == [
        "inherited()",
        "price(int)",
        "scoping(Customer)",
    ]


def test_cli_index_builds_knowledge_and_can_skip_it(
    isolated_settings: Settings,
    java_analyzer: object,
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli_main(["index", str(repo)]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["index"]["analyzed"] == 2 and first["knowledge"]["skipped"] is False
    assert first["knowledge"]["stats"]["methods"] == 2

    assert cli_main(["index", str(repo)]) == 0
    assert json.loads(capsys.readouterr().out)["knowledge"]["skipped"] is True

    assert cli_main(["index", str(repo), "--no-knowledge", "--force"]) == 0
    assert json.loads(capsys.readouterr().out)["knowledge"] is None
