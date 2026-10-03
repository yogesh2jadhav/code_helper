"""SQLite store of retrieval documents (text + metadata), the source for BM25 and result display."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing
from pathlib import Path

from app.retrieval.documents import DocType, Document

_SCHEMA = """
CREATE TABLE IF NOT EXISTS retrieval_docs (
    id TEXT PRIMARY KEY, repository_id TEXT NOT NULL, type TEXT NOT NULL, text TEXT NOT NULL,
    hash TEXT NOT NULL, file_path TEXT NOT NULL, class_name TEXT NOT NULL DEFAULT '',
    method_name TEXT NOT NULL DEFAULT '', line_start INTEGER, line_end INTEGER,
    symbol_ids TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_rdocs_repo ON retrieval_docs (repository_id, type);
CREATE INDEX IF NOT EXISTS idx_rdocs_class ON retrieval_docs (repository_id, class_name);
CREATE INDEX IF NOT EXISTS idx_rdocs_file ON retrieval_docs (repository_id, file_path);
"""


class RetrievalStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def hashes(self, repository_id: str) -> dict[str, str]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, hash FROM retrieval_docs WHERE repository_id = ?", (repository_id,)
            ).fetchall()
        return {r["id"]: r["hash"] for r in rows}

    def upsert(self, docs: Iterable[Document]) -> int:
        rows = [
            (
                d.id,
                d.repository_id,
                d.type,
                d.text,
                d.hash,
                d.file_path,
                d.class_name,
                d.method_name,
                d.line_start,
                d.line_end,
                json.dumps(d.symbol_ids),
            )
            for d in docs
        ]
        with closing(self._connect()) as conn, conn:
            conn.executemany(
                "INSERT INTO retrieval_docs (id, repository_id, type, text, hash, file_path,"
                " class_name, method_name, line_start, line_end, symbol_ids)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET type=excluded.type,"
                " text=excluded.text, hash=excluded.hash, file_path=excluded.file_path,"
                " class_name=excluded.class_name, method_name=excluded.method_name,"
                " line_start=excluded.line_start, line_end=excluded.line_end,"
                " symbol_ids=excluded.symbol_ids",
                rows,
            )
        return len(rows)

    def delete(self, ids: list[str]) -> None:
        with closing(self._connect()) as conn, conn:
            conn.executemany("DELETE FROM retrieval_docs WHERE id = ?", [(i,) for i in ids])

    def get_many(self, ids: list[str]) -> dict[str, Document]:
        out: dict[str, Document] = {}
        with closing(self._connect()) as conn:
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                marks = ",".join("?" * len(chunk))
                for r in conn.execute(f"SELECT * FROM retrieval_docs WHERE id IN ({marks})", chunk):
                    out[r["id"]] = _doc(r)
        return out

    def iter_docs(self, repository_id: str) -> Iterator[Document]:
        with closing(self._connect()) as conn:
            for r in conn.execute(
                "SELECT * FROM retrieval_docs WHERE repository_id = ? ORDER BY id", (repository_id,)
            ):
                yield _doc(r)

    def filter_ids(
        self,
        repository_id: str,
        *,
        types: Iterable[DocType] | None = None,
        class_name: str | None = None,
        method_name: str | None = None,
        file_path: str | None = None,
        symbol_id: str | None = None,
    ) -> set[str]:
        """Ids matching the metadata filter; applied before any ranking."""
        sql = "SELECT id FROM retrieval_docs WHERE repository_id = ?"
        args: list[object] = [repository_id]
        if types:
            kinds = list(types)
            sql += f" AND type IN ({','.join('?' * len(kinds))})"
            args += kinds
        for column, value in (
            ("class_name", class_name),
            ("method_name", method_name),
            ("file_path", file_path),
        ):
            if value:
                sql += f" AND {column} = ?"
                args.append(value)
        if symbol_id:
            sql += " AND symbol_ids LIKE ?"
            args.append(f'%"{symbol_id}"%')
        with closing(self._connect()) as conn:
            return {r["id"] for r in conn.execute(sql, args)}

    def count(self, repository_id: str) -> int:
        with closing(self._connect()) as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM retrieval_docs WHERE repository_id = ?", (repository_id,)
                ).fetchone()[0]
            )


def _doc(r: sqlite3.Row) -> Document:
    return Document(
        id=r["id"],
        type=r["type"],
        text=r["text"],
        repository_id=r["repository_id"],
        file_path=r["file_path"],
        class_name=r["class_name"],
        method_name=r["method_name"],
        line_start=r["line_start"] or 0,
        line_end=r["line_end"] or 0,
        symbol_ids=json.loads(r["symbol_ids"]),
    )
