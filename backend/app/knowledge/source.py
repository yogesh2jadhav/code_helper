"""Read-only access to the scanned repository's source files.

Used wherever exact code is needed (retrieval documents, context for the LLM, the source viewer).
Paths are always relative to the repository root and cannot escape it.
"""

from __future__ import annotations

from pathlib import Path


class SourceReader:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    def path(self, relative: str) -> Path | None:
        candidate = (self.root / relative).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return None  # outside the repository: refuse
        return candidate if candidate.is_file() else None

    def text(self, relative: str) -> str | None:
        path = self.path(relative)
        if path is None:
            return None
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return path.read_text(encoding="latin-1")
        except OSError:
            return None

    def lines(self, relative: str, start: int, end: int) -> str | None:
        """Lines start..end (1-based, inclusive) or None if the file cannot be read."""
        text = self.text(relative)
        if text is None:
            return None
        rows = text.split("\n")
        return "\n".join(rows[max(0, start - 1) : end])
