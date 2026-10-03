"""Run symbol resolution over a repository's stored analysis."""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from app.analyzer.ast_models import Expression, Method, TypeDecl
from app.analyzer.resolution_models import FileResolution, ResolutionStatus, ResolutionSummary
from app.analyzer.store import AnalysisStore
from app.analyzer.symbol_resolver import SymbolResolver
from app.analyzer.symbol_table import AnalyzedFile, SymbolTable
from app.config import Settings
from app.scanner.scanner import repository_id_for
from app.scanner.store import ScanStore


class NotIndexedError(RuntimeError):
    """The repository has no stored analysis yet; run the index command first."""


class Example(BaseModel):
    file: str
    line: int
    kind: str
    name: str
    text: str
    candidates: list[str] = []


@dataclass
class ResolutionRun:
    repository_id: str
    files: list[AnalyzedFile]
    resolutions: list[FileResolution]
    summary: ResolutionSummary
    duration_ms: int


class ResolutionService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._scan_store = ScanStore(settings.db_path)
        self._analysis_store = AnalysisStore(settings.db_path)

    def resolve(self, root: Path | None = None) -> ResolutionRun:
        started = time.monotonic()
        target = root or self._settings.source_root
        if target is None:
            raise ValueError("no repository path given and SOURCE_ROOT is not configured")
        repo_id = repository_id_for(target.expanduser().resolve())
        paths = {f.id: f.relative_path for f in self._scan_store.known_files(repo_id).values()}
        files = [
            AnalyzedFile(file_id, paths.get(file_id, parsed.path), parsed)
            for file_id, parsed in self._analysis_store.load_all(repo_id)
        ]
        if not files:
            raise NotIndexedError(
                f"no analysis stored for {target}; run `index` first (or re-run it after upgrading,"
                " since older analysis results are re-generated automatically)"
            )
        resolver = SymbolResolver(SymbolTable(files))
        resolutions = resolver.resolve_all(files)
        return ResolutionRun(
            repo_id,
            files,
            resolutions,
            resolver.summarize(resolutions),
            int((time.monotonic() - started) * 1000),
        )

    @staticmethod
    def examples(run: ResolutionRun, per_reason: int = 3) -> dict[str, list[Example]]:
        """A few concrete unresolved/ambiguous references per reason, for triage."""
        exprs: dict[tuple[str, str, str], dict[int, Expression]] = {}
        for f in run.files:
            for t in _walk_types(f.parsed.types):
                for m in [*t.methods, *t.constructors]:
                    exprs[(f.file_id, t.qualified_name, m.signature)] = _by_id(m)
        out: dict[str, list[Example]] = {}
        for fr in run.resolutions:
            for cr in fr.classes:
                for mr in cr.methods:
                    table = exprs.get((fr.file_id, cr.fqn, mr.signature), {})
                    for ref in mr.refs:
                        if (
                            ref.status is ResolutionStatus.RESOLVED
                            or ref.expression_id not in table
                        ):
                            continue
                        bucket = out.setdefault(ref.reason or "unspecified", [])
                        if len(bucket) < per_reason:
                            bucket.append(
                                Example(
                                    file=fr.path,
                                    line=ref.site_line,
                                    kind=ref.kind,
                                    name=ref.name,
                                    text=table[ref.expression_id].text[:120],
                                    candidates=ref.candidates[:4],
                                )
                            )
        return out


def _by_id(method: Method) -> dict[int, Expression]:
    return {e.id: e for e in method.expressions}


def _walk_types(types: list[TypeDecl]) -> Iterator[TypeDecl]:
    for t in types:
        yield t
        yield from _walk_types(t.nested_types)
