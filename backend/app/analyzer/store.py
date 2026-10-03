"""SQLite persistence of per-file analysis results, keyed by the file hash they were built from."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from app.analyzer.ast_models import ParsedFile

_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_analysis (
    file_id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL,
    hash TEXT NOT NULL,
    status TEXT NOT NULL,
    errors TEXT NOT NULL,
    ast_json TEXT NOT NULL,
    analyzed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_file_analysis_repo ON file_analysis (repository_id);
"""


class AnalysisStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.executescript(_SCHEMA)

    def states(self, repository_id: str) -> dict[str, tuple[str, str]]:
        """file_id -> (hash analysed, status)."""
        with closing(sqlite3.connect(self._db_path)) as conn:
            rows = conn.execute(
                "SELECT file_id, hash, status FROM file_analysis WHERE repository_id = ?",
                (repository_id,),
            ).fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}

    def save_batch(self, repository_id: str, items: Iterable[tuple[str, str, ParsedFile]]) -> None:
        """Persist (file_id, hash, result) tuples in one transaction."""
        now = datetime.now(UTC).isoformat()
        with closing(sqlite3.connect(self._db_path)) as conn, conn:
            conn.executemany(
                "INSERT INTO file_analysis (file_id, repository_id, hash, status, errors, ast_json,"
                " analyzed_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(file_id) DO UPDATE SET"
                " hash=excluded.hash, status=excluded.status, errors=excluded.errors,"
                " ast_json=excluded.ast_json, analyzed_at=excluded.analyzed_at",
                [
                    (file_id, repository_id, digest, parsed.status, json.dumps(parsed.errors),
                     parsed.model_dump_json(by_alias=True), now)
                    for file_id, digest, parsed in items
                ],
            )

    def load(self, file_id: str) -> ParsedFile | None:
        with closing(sqlite3.connect(self._db_path)) as conn:
            row = conn.execute(
                "SELECT ast_json FROM file_analysis WHERE file_id = ?", (file_id,)
            ).fetchone()
        return ParsedFile.model_validate_json(row[0]) if row else None

    def prune(self, repository_id: str) -> int:
        """Drop results for files no longer present in source_files. Returns rows removed."""
        with closing(sqlite3.connect(self._db_path)) as conn, conn:
            cur = conn.execute(
                "DELETE FROM file_analysis WHERE repository_id = ? AND file_id NOT IN"
                " (SELECT id FROM source_files WHERE repository_id = ?)",
                (repository_id, repository_id),
            )
            return cur.rowcount

    def count(self, repository_id: str) -> int:
        with closing(sqlite3.connect(self._db_path)) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM file_analysis WHERE repository_id = ?", (repository_id,)
            ).fetchone()
        return int(row[0])
