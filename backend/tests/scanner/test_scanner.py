from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.scanner.scanner import RepositoryScanner, detect_package
from app.scanner.store import ScanStore

IGNORE = ["target", "build", ".git", "node_modules", "generated"]


def make_scanner(tmp_path: Path, **kw: int) -> RepositoryScanner:
    return RepositoryScanner(ScanStore(tmp_path / "db" / "t.sqlite3"), IGNORE, **kw)


def write(root: Path, rel: str, text: str = "class A {}") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))  # no newline translation: sizes must be exact
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


def test_discovers_java_files_and_metadata(tmp_path: Path, repo: Path) -> None:
    write(repo, "src/com/acme/Foo.java", "package com.acme;\nclass Foo {}")
    write(repo, "src/Bar.java", "class Bar {}")
    write(repo, "README.md", "ignored")
    result = make_scanner(tmp_path).scan(repo)

    by_rel = {f.relative_path: f for f in result.files}
    assert set(by_rel) == {"src/com/acme/Foo.java", "src/Bar.java"}
    foo = by_rel["src/com/acme/Foo.java"]
    assert foo.package == "com.acme"
    assert foo.language == "java"
    assert foo.size == len("package com.acme;\nclass Foo {}")
    assert len(foo.hash) == 64
    assert by_rel["src/Bar.java"].package is None
    assert sorted(result.added) == sorted(by_rel)


@pytest.mark.parametrize("ignored", ["target", "build", ".git", "node_modules", "generated"])
def test_ignored_directories_are_skipped(tmp_path: Path, repo: Path, ignored: str) -> None:
    write(repo, "src/Keep.java")
    write(repo, f"{ignored}/Skip.java")
    write(repo, f"module/{ignored}/classes/Skip2.java")
    result = make_scanner(tmp_path).scan(repo)
    assert [f.relative_path for f in result.files] == ["src/Keep.java"]
    assert result.ignored_dirs == 2


def test_empty_repository(tmp_path: Path, repo: Path) -> None:
    result = make_scanner(tmp_path).scan(repo)
    assert result.files == []
    assert result.to_analyze == []


def test_missing_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        make_scanner(tmp_path).scan(tmp_path / "nope")


def test_duplicate_paths_via_symlink_are_reported_once(tmp_path: Path, repo: Path) -> None:
    original = write(repo, "a/Original.java")
    (repo / "b").mkdir()
    try:
        (repo / "b" / "Alias.java").symlink_to(original)
    except OSError:  # Windows without symlink privilege (developer mode / admin)
        pytest.skip("cannot create symlinks on this machine")
    result = make_scanner(tmp_path).scan(repo)
    assert len(result.files) == 1
    assert [s.reason for s in result.skipped] == ["duplicate_path"]


def test_invalid_java_is_still_recorded_not_parsed(tmp_path: Path, repo: Path) -> None:
    write(repo, "Broken.java", "public class { void ( {")
    result = make_scanner(tmp_path).scan(repo)
    assert [f.relative_path for f in result.files] == ["Broken.java"]


def test_non_utf8_source_is_flagged_and_scan_continues(tmp_path: Path, repo: Path) -> None:
    (repo / "Latin.java").write_bytes("package p;\n// caf\xe9\nclass L {}".encode("latin-1"))
    write(repo, "Good.java")
    result = make_scanner(tmp_path).scan(repo)
    flags = {f.relative_path: f.encoding_issue for f in result.files}
    assert flags == {"Latin.java": True, "Good.java": False}
    assert next(f for f in result.files if f.relative_path == "Latin.java").package == "p"


def test_incremental_scan_classifies_added_changed_unchanged_removed(
    tmp_path: Path, repo: Path
) -> None:
    scanner = make_scanner(tmp_path)
    write(repo, "Keep.java", "class Keep {}")
    write(repo, "Edit.java", "class Edit {}")
    write(repo, "Gone.java", "class Gone {}")
    first = scanner.scan(repo)
    assert sorted(first.added) == ["Edit.java", "Gone.java", "Keep.java"]

    second = scanner.scan(repo)
    assert second.added == [] and second.changed == [] and second.removed == []
    assert sorted(second.unchanged) == ["Edit.java", "Gone.java", "Keep.java"]
    assert second.to_analyze == []

    write(repo, "Edit.java", "class Edit { int x; }")
    (repo / "Gone.java").unlink()
    write(repo, "New.java", "class New {}")
    third = scanner.scan(repo)
    assert third.added == ["New.java"]
    assert third.changed == ["Edit.java"]
    assert third.unchanged == ["Keep.java"]
    assert third.removed == ["Gone.java"]
    assert third.to_analyze == ["Edit.java", "New.java"]

    fourth = scanner.scan(repo)
    assert fourth.changed == [] and fourth.removed == [] and fourth.added == []


def test_max_source_files_and_max_file_bytes(tmp_path: Path, repo: Path) -> None:
    for name in ("A", "B", "C"):
        write(repo, f"{name}.java")
    write(repo, "0Huge.java", "x" * 500)  # sorts first, so size is checked before the cap
    result = make_scanner(tmp_path, max_files=2, max_file_bytes=100).scan(repo)
    assert len(result.files) == 2
    assert {s.reason for s in result.skipped} == {"max_source_files", "too_large"}


def test_scan_logs_pipeline_events(
    tmp_path: Path, repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write(repo, "A.java")
    with caplog.at_level(logging.INFO, logger="app.scanner.scanner"):
        make_scanner(tmp_path).scan(repo)
    messages = [r.getMessage() for r in caplog.records]
    for event in (
        "repository_scan_started",
        "files_discovered",
        "files_changed",
        "files_skipped",
        "repository_scan_completed",
    ):
        assert event in messages
    assert "class A" not in " ".join(messages)  # source is never logged


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("package a.b.c;\nclass X {}", "a.b.c"),
        ("/* package fake; */\n// package nope;\npackage real.one;", "real.one"),
        ("@Deprecated package legacy;", "legacy"),
        ("class NoPackage {}", None),
    ],
)
def test_detect_package(text: str, expected: str | None) -> None:
    assert detect_package(text) == expected
