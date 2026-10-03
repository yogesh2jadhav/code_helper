"""Citations: short labels (E1, E2, ...) for source locations, shared by the context sent to the
model, the explanation plan and the final answer, so every claim can be traced to code."""

from __future__ import annotations

from pydantic import BaseModel


class Citation(BaseModel):
    label: str  # E1, E2, ...
    file: str
    start_line: int
    end_line: int
    source_type: str  # source_code | comment | test | readme | documentation | retrieved
    note: str = ""

    @property
    def location(self) -> str:
        span = (
            str(self.start_line)
            if self.start_line == self.end_line
            else f"{self.start_line}-{self.end_line}"
        )
        return f"{self.file}:{span}"


class CitationBook:
    """Assigns one label per distinct (file, range, type); asking again returns the same label."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, int, int, str], Citation] = {}

    def cite(
        self,
        file: str,
        start_line: int,
        end_line: int,
        source_type: str = "source_code",
        note: str = "",
    ) -> Citation:
        key = (file, start_line, end_line, source_type)
        found = self._by_key.get(key)
        if found is None:
            found = Citation(
                label=f"E{len(self._by_key) + 1}",
                file=file,
                start_line=start_line,
                end_line=end_line,
                source_type=source_type,
                note=note,
            )
            self._by_key[key] = found
        return found

    @classmethod
    def from_citations(cls, citations: list[Citation]) -> CitationBook:
        """Rebuild a book that keeps the given labels (so the plan matches the prompt context)."""
        book = cls()
        for c in citations:
            book._by_key[(c.file, c.start_line, c.end_line, c.source_type)] = c
        return book

    @property
    def citations(self) -> list[Citation]:
        return list(self._by_key.values())

    def get(self, label: str) -> Citation | None:
        return next((c for c in self._by_key.values() if c.label == label), None)
