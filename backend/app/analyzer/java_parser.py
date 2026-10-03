"""JavaParser-backed analyzer. Runs the java-analyzer jar as a subprocess (JDK 17+)."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from app.analyzer.ast_models import ParsedFile
from app.analyzer.base import AnalyzerUnavailableError
from app.logging_setup import log_event

logger = logging.getLogger(__name__)

MIN_JAVA_MAJOR = 17
_VERSION = re.compile(r'version "(\d+)(?:\.(\d+))?')


def resolve_java_bin(configured: str) -> str:
    """Use the configured binary; if it is the bare default, prefer $JAVA_HOME/bin/java."""
    if configured == "java" and os.environ.get("JAVA_HOME"):
        candidate = Path(os.environ["JAVA_HOME"], "bin", "java")
        if candidate.is_file():
            return str(candidate)
    return configured


def java_major_version(java_bin: str) -> int:
    try:
        proc = subprocess.run(
            [java_bin, "-version"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AnalyzerUnavailableError(f"cannot run '{java_bin}': {exc}") from exc
    match = _VERSION.search(proc.stderr + proc.stdout)
    if proc.returncode != 0 or not match:
        raise AnalyzerUnavailableError(
            f"'{java_bin} -version' failed (is a JDK installed? set JAVA_BIN): "
            f"{(proc.stderr or proc.stdout).strip()[:200]}"
        )
    return int(match.group(1))


class JavaParserAnalyzer:
    def __init__(self, java_bin: str, jar: Path, timeout_seconds: int = 300) -> None:
        self._java_bin = resolve_java_bin(java_bin)
        self._jar = jar
        self._timeout = timeout_seconds
        self._checked = False

    def _check_environment(self) -> None:
        if self._checked:
            return
        if not self._jar.is_file():
            raise AnalyzerUnavailableError(
                f"analyzer jar not found at {self._jar}; build it with `make analyzer`"
            )
        major = java_major_version(self._java_bin)
        if major < MIN_JAVA_MAJOR:
            raise AnalyzerUnavailableError(
                f"Java {MIN_JAVA_MAJOR}+ required, '{self._java_bin}' is Java {major}"
            )
        self._checked = True

    def analyze_files(self, paths: Sequence[Path]) -> list[ParsedFile]:
        if not paths:
            return []
        self._check_environment()
        started = time.monotonic()
        stdin = "\n".join(str(p.resolve()) for p in paths) + "\n"
        by_path: dict[str, ParsedFile] = {}
        failure: str | None = None

        try:
            proc = subprocess.run(
                [self._java_bin, "-jar", str(self._jar)],
                input=stdin, capture_output=True, text=True, encoding="utf-8",
                timeout=self._timeout, check=False,
            )
            stdout = proc.stdout
            if proc.returncode != 0:
                detail = proc.stderr.strip()[:300]
                failure = f"analyzer exited with code {proc.returncode}: {detail}"
        except subprocess.TimeoutExpired as exc:
            partial = exc.stdout  # bytes or str depending on platform; keep what was produced
            stdout = (
                partial.decode("utf-8", "replace") if isinstance(partial, bytes) else partial or ""
            )
            failure = f"analyzer timed out after {self._timeout}s"

        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                parsed = ParsedFile.model_validate(json.loads(line))
            except (json.JSONDecodeError, ValidationError) as exc:
                logger.warning("analyzer_output_invalid", extra={"error": str(exc)[:200]})
                continue
            by_path[parsed.path] = parsed

        results: list[ParsedFile] = []
        for path in paths:
            key = str(path.resolve())
            found = by_path.get(key) or by_path.get(str(path))
            parsed = found or ParsedFile(
                path=key, status="analyzer_error",
                errors=[failure or "analyzer produced no result for this file"],
            )
            log_event(logger, "java_file_parsed", file=path.name, status=parsed.status)
            results.append(parsed)

        ok = sum(1 for r in results if r.ok)
        log_event(
            logger, "java_analysis_completed", files=len(results), ok=ok,
            failed=len(results) - ok, duration_ms=int((time.monotonic() - started) * 1000),
        )
        if failure:
            logger.error("analyzer_failure", extra={"error": failure})
        return results
