"""Analyzer abstraction: swap JavaParser for another parser (e.g. Eclipse JDT) behind this."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from app.analyzer.ast_models import ParsedFile


class AnalyzerUnavailableError(RuntimeError):
    """The analyzer cannot run at all (missing JDK/jar, wrong version). Not a per-file failure."""


class JavaAnalyzer(Protocol):
    def analyze_files(self, paths: Sequence[Path]) -> list[ParsedFile]:
        """Parse files, returning exactly one ParsedFile per input path, in input order.

        Per-file problems are reported via ParsedFile.status, never by raising.
        """
        ...
