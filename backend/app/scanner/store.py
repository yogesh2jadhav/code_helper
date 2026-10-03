"""SQLite persistence for repositories and the scanned file manifest."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from contextlib import closing
from datetime import datetime
from pathlib import Path

from app.scanner.models import Repository, SourceFile

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repositories (
    id TEXT PRIMARY KEY,
    root_path TEXT NOT NULL UNIQUE,
    last_scanned TEXT
);
CREATE TABLE IF NOT EXISTS source_files (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL REFERENCES repositories(id),
    path TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    hash TEXT NOT NULL,
    package TEXT,
    size INTEGER NOT NULL,
    language TEXT NOT NULL,
    last_scanned TEXT NOT NULL,
    encoding_issue INTEGER NOT NULL DEFAULT 0,
    UNIQUE (repository_id, relative_path)
);
"""


class ScanStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def known_files(self, repository_id: str) -> dict[str, SourceFile]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM source_files WHERE repository_id = ?", (repository_id,)
            ).fetchall()
        return {r["relative_path"]: _row_to_file(r) for r in rows}

    def save_scan(
        self,
        repository_id: str,
        root: str,
        files: Iterable[SourceFile],
        removed: Iterable[str],
        scanned_at: datetime,
    ) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO repositories (id, root_path, last_scanned) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET last_scanned = excluded.last_scanned",
                (repository_id, root, scanned_at.isoformat()),
            )
            conn.executemany(
                "DELETE FROM source_files WHERE repository_id = ? AND relative_path = ?",
                [(repository_id, rel) for rel in removed],
            )
            conn.executemany(
                "INSERT INTO source_files (id, repository_id, path, relative_path, hash, package,"
                " size, language, last_scanned, encoding_issue) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET hash=excluded.hash, package=excluded.package,"
                " size=excluded.size, last_scanned=excluded.last_scanned,"
                " encoding_issue=excluded.encoding_issue, path=excluded.path",
                [
                    (f.id, f.repository_id, f.path, f.relative_path, f.hash, f.package, f.size,
                     f.language, f.last_scanned.isoformat(), int(f.encoding_issue))
                    for f in files
                ],
            )

    def list_files(self, repository_id: str, limit: int, offset: int) -> list[SourceFile]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM source_files WHERE repository_id = ? ORDER BY relative_path"
                " LIMIT ? OFFSET ?",
                (repository_id, limit, offset),
            ).fetchall()
        return [_row_to_file(r) for r in rows]

    def count_files(self, repository_id: str) -> int:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM source_files WHERE repository_id = ?", (repository_id,)
            ).fetchone()
        return int(row[0])

    def has_repository(self, repository_id: str) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM repositories WHERE id = ?", (repository_id,)
            ).fetchone()
        return row is not None

    def list_repositories(self) -> list[Repository]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT r.id, r.root_path, r.last_scanned, COUNT(f.id) AS n FROM repositories r "
                "LEFT JOIN source_files f ON f.repository_id = r.id GROUP BY r.id"
            ).fetchall()
        return [
            Repository(
                id=r["id"],
                root_path=r["root_path"],
                last_scanned=(
                    datetime.fromisoformat(r["last_scanned"]) if r["last_scanned"] else None
                ),
                file_count=r["n"],
            )
            for r in rows
        ]


def _row_to_file(r: sqlite3.Row) -> SourceFile:
    return SourceFile(
        id=r["id"], repository_id=r["repository_id"], path=r["path"],
        relative_path=r["relative_path"], hash=r["hash"], package=r["package"], size=r["size"],
        language=r["language"], last_scanned=datetime.fromisoformat(r["last_scanned"]),
        encoding_issue=bool(r["encoding_issue"]),
    )
