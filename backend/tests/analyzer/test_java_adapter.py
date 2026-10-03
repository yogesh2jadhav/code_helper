"""Failure handling and Java 17 syntax coverage for the JavaParser adapter."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from app.analyzer import AnalyzerUnavailableError, JavaParserAnalyzer
from app.analyzer.java_parser import java_major_version, resolve_java_bin
from app.config import get_settings


def fake_java(tmp_path: Path, version: str, run_body: str = "exit 0") -> str:
    script = tmp_path / "fake-java"
    script.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "-version" ]; then echo \'openjdk version "{version}"\' >&2; exit 0; fi\n'
        f"{run_body}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


@pytest.fixture
def jar(tmp_path: Path) -> Path:
    path = tmp_path / "analyzer.jar"
    path.write_bytes(b"not a real jar")
    return path


def test_missing_jar_is_unavailable_not_a_crash(tmp_path: Path) -> None:
    analyzer = JavaParserAnalyzer("java", tmp_path / "missing.jar")
    with pytest.raises(AnalyzerUnavailableError, match="make analyzer"):
        analyzer.analyze_files([tmp_path / "A.java"])


def test_unrunnable_java_binary(tmp_path: Path, jar: Path) -> None:
    analyzer = JavaParserAnalyzer(str(tmp_path / "no-such-java"), jar)
    with pytest.raises(AnalyzerUnavailableError, match="cannot run"):
        analyzer.analyze_files([tmp_path / "A.java"])


def test_old_java_is_rejected(tmp_path: Path, jar: Path) -> None:
    analyzer = JavaParserAnalyzer(fake_java(tmp_path, "11.0.2"), jar)
    with pytest.raises(AnalyzerUnavailableError, match="Java 17\\+ required"):
        analyzer.analyze_files([tmp_path / "A.java"])


@pytest.mark.parametrize(("version", "major"), [("17.0.20.1", 17), ("21", 21), ("1.8.0_292", 1)])
def test_java_version_parsing(tmp_path: Path, version: str, major: int) -> None:
    assert java_major_version(fake_java(tmp_path, version)) == major


def test_crashing_analyzer_marks_every_file_failed(tmp_path: Path, jar: Path) -> None:
    analyzer = JavaParserAnalyzer(fake_java(tmp_path, "17.0.1", "echo boom >&2; exit 3"), jar)
    results = analyzer.analyze_files([tmp_path / "A.java", tmp_path / "B.java"])
    assert [r.status for r in results] == ["analyzer_error", "analyzer_error"]
    assert "exited with code 3" in results[0].errors[0] and "boom" in results[0].errors[0]


def test_timeout_marks_files_failed(tmp_path: Path, jar: Path) -> None:
    analyzer = JavaParserAnalyzer(
        fake_java(tmp_path, "17.0.1", "exec sleep 30"), jar, timeout_seconds=1)
    (result,) = analyzer.analyze_files([tmp_path / "A.java"])
    assert result.status == "analyzer_error" and "timed out" in result.errors[0]


def test_malformed_output_lines_are_ignored(tmp_path: Path, jar: Path) -> None:
    analyzer = JavaParserAnalyzer(fake_java(tmp_path, "17.0.1", "echo 'not json'; echo '{}'"), jar)
    (result,) = analyzer.analyze_files([tmp_path / "A.java"])
    assert result.status == "analyzer_error"
    assert "no result" in result.errors[0]


def test_empty_input_needs_no_jvm(tmp_path: Path) -> None:
    assert JavaParserAnalyzer("definitely-not-java", tmp_path / "x.jar").analyze_files([]) == []


def test_resolve_java_bin_prefers_java_home_only_for_bare_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "bin").mkdir()
    java = tmp_path / "bin" / "java"
    java.write_text("")
    monkeypatch.setenv("JAVA_HOME", str(tmp_path))
    assert resolve_java_bin("java") == str(java)
    assert resolve_java_bin("/custom/java") == "/custom/java"


# ---- real analyzer --------------------------------------------------------------------------


def test_results_match_input_order_and_missing_file_is_a_per_file_error(
    java_analyzer: JavaParserAnalyzer, tmp_path: Path
) -> None:
    good = tmp_path / "Good.java"
    good.write_text("package p; class Good { void m() {} }")
    gone = tmp_path / "Gone.java"
    results = java_analyzer.analyze_files([gone, good])
    assert [Path(r.path).name for r in results] == ["Gone.java", "Good.java"]
    assert results[0].status == "analyzer_error" and results[0].errors
    assert results[1].ok and results[1].types[0].methods[0].signature == "m()"


def test_latin1_source_still_parses(java_analyzer: JavaParserAnalyzer, tmp_path: Path) -> None:
    path = tmp_path / "Latin.java"
    path.write_bytes('class L { String s = "caf\xe9"; }'.encode("latin-1"))
    (result,) = java_analyzer.analyze_files([path])
    assert result.ok and result.types[0].fields[0].initializer == '"café"'


def test_java_17_syntax(java_analyzer: JavaParserAnalyzer, tmp_path: Path) -> None:
    path = tmp_path / "Modern.java"
    path.write_text(
        'sealed interface Shape permits Circle, Square {}\n'
        'record Circle(double r) implements Shape {}\n'
        'record Square(double s) implements Shape {}\n'
        'class Modern {\n'
        '    String describe(Object o) {\n'
        '        var text = """\n'
        '            hello\n'
        '            """;\n'
        '        if (o instanceof String s && !s.isEmpty()) {\n'
        '            return s + text;\n'
        '        }\n'
        '        return switch (o) { default -> "other"; };\n'
        '    }\n'
        '}\n'
    )
    (result,) = java_analyzer.analyze_files([path])
    assert result.ok, result.errors
    assert [t.name for t in result.types] == ["Shape", "Circle", "Square", "Modern"]
    describe = result.types[3].methods[0]
    assert describe.cyclomatic_complexity == 3  # 1 + if + `&&`; a lone default adds nothing
    assert any(e.kind == "variable_declaration" and e.type == "var" for e in describe.expressions)


def test_settings_point_at_a_working_jdk() -> None:
    s = get_settings()
    assert java_major_version(resolve_java_bin(s.java_bin)) >= 17
