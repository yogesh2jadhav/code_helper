"""SQLite persistence of the knowledge model.

Two layers: relational tables for querying (calls, rules, evidence, variables, flow nodes and
edges, ...) and one compressed JSON document per method and class, which is what reads return so
the full model round-trips exactly. A repository's knowledge is replaced as a whole in one
transaction, so readers never see a half-written model.
"""

from __future__ import annotations

import json
import sqlite3
import zlib
from collections import deque
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from app.analyzer.call_graph import CallGraph
from app.analyzer.control_flow import FlowNode
from app.analyzer.resolution_models import ResolutionStatus
from app.analyzer.rule_extractor import RuleCandidate
from app.knowledge.evidence import Evidence
from app.knowledge.models import ClassKnowledge, MethodKnowledge, RepositoryKnowledge

KNOWLEDGE_VERSION = 1  # bump when the model or its derivation changes

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    knowledge_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    files INTEGER, classes INTEGER, methods INTEGER, call_edges INTEGER,
    rule_candidates INTEGER, evidence INTEGER, risks INTEGER, unknowns INTEGER
);
CREATE TABLE IF NOT EXISTS classes (
    id TEXT PRIMARY KEY, repository_id TEXT NOT NULL, file_id TEXT NOT NULL, fqn TEXT NOT NULL,
    name TEXT NOT NULL, kind TEXT NOT NULL, package TEXT, file TEXT NOT NULL,
    start_line INTEGER, end_line INTEGER, superclass TEXT, is_test INTEGER NOT NULL DEFAULT 0,
    method_count INTEGER NOT NULL DEFAULT 0, doc BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_classes_repo ON classes (repository_id, fqn);
CREATE TABLE IF NOT EXISTS methods (
    id TEXT PRIMARY KEY, repository_id TEXT NOT NULL, class_id TEXT NOT NULL,
    file_id TEXT NOT NULL, method_id TEXT NOT NULL, class_fqn TEXT NOT NULL, name TEXT NOT NULL,
    signature TEXT NOT NULL, kind TEXT NOT NULL, visibility TEXT, file TEXT NOT NULL,
    start_line INTEGER, end_line INTEGER, is_test INTEGER NOT NULL DEFAULT 0,
    complexity INTEGER, nesting INTEGER, purpose TEXT, purpose_basis TEXT,
    callers_count INTEGER NOT NULL DEFAULT 0, callees_count INTEGER NOT NULL DEFAULT 0,
    rules_count INTEGER NOT NULL DEFAULT 0, risks_count INTEGER NOT NULL DEFAULT 0,
    unknowns_count INTEGER NOT NULL DEFAULT 0, doc BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_methods_repo ON methods (repository_id, method_id);
CREATE INDEX IF NOT EXISTS idx_methods_class ON methods (class_id);
CREATE INDEX IF NOT EXISTS idx_methods_name ON methods (repository_id, name);
CREATE TABLE IF NOT EXISTS parameters (
    method_pk TEXT NOT NULL, position INTEGER NOT NULL, name TEXT NOT NULL, type TEXT NOT NULL,
    var_args INTEGER NOT NULL DEFAULT 0, comes_from TEXT
);
CREATE INDEX IF NOT EXISTS idx_parameters_method ON parameters (method_pk);
CREATE TABLE IF NOT EXISTS fields (
    class_id TEXT NOT NULL, name TEXT NOT NULL, type TEXT NOT NULL, visibility TEXT,
    modifiers TEXT, line INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fields_class ON fields (class_id);
CREATE TABLE IF NOT EXISTS method_calls (
    repository_id TEXT NOT NULL, caller_pk TEXT, caller_id TEXT NOT NULL, callee_id TEXT,
    name TEXT NOT NULL, owner_type TEXT, status TEXT NOT NULL, ambiguous INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL, site_line INTEGER, expression_id INTEGER, reason TEXT,
    implicit INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_calls_caller ON method_calls (repository_id, caller_id);
CREATE INDEX IF NOT EXISTS idx_calls_callee ON method_calls (repository_id, callee_id);
CREATE VIEW IF NOT EXISTS method_callers AS
    SELECT repository_id, callee_id AS method_id, caller_id, site_line, ambiguous
    FROM method_calls WHERE callee_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS method_overrides (
    repository_id TEXT NOT NULL, base_id TEXT NOT NULL, override_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_overrides_base ON method_overrides (repository_id, base_id);
CREATE TABLE IF NOT EXISTS variables (
    method_pk TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, type TEXT,
    symbol_id TEXT NOT NULL, declared_line INTEGER, created TEXT, lifecycle TEXT
);
CREATE INDEX IF NOT EXISTS idx_variables_method ON variables (method_pk);
CREATE TABLE IF NOT EXISTS control_flow_nodes (
    method_pk TEXT NOT NULL, ord INTEGER NOT NULL, parent_ord INTEGER, kind TEXT NOT NULL,
    text TEXT, start_line INTEGER, end_line INTEGER, depth INTEGER
);
CREATE INDEX IF NOT EXISTS idx_flow_method ON control_flow_nodes (method_pk);
CREATE TABLE IF NOT EXISTS data_flow_edges (
    method_pk TEXT NOT NULL, ord INTEGER NOT NULL, source_kind TEXT, source_name TEXT,
    source_symbol TEXT, target_kind TEXT, target_name TEXT, target_symbol TEXT, via TEXT NOT NULL,
    line INTEGER, callee_id TEXT, param TEXT, ops TEXT
);
CREATE INDEX IF NOT EXISTS idx_dataflow_method ON data_flow_edges (method_pk);
CREATE INDEX IF NOT EXISTS idx_dataflow_callee ON data_flow_edges (callee_id);
CREATE TABLE IF NOT EXISTS rule_candidates (
    id TEXT NOT NULL, method_pk TEXT NOT NULL, kind TEXT NOT NULL, start_line INTEGER,
    end_line INTEGER, condition TEXT, meaning TEXT, scope TEXT, calculation TEXT, action TEXT,
    confidence TEXT, doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rules_method ON rule_candidates (method_pk);
CREATE TABLE IF NOT EXISTS evidence (
    id TEXT NOT NULL, repository_id TEXT NOT NULL, method_pk TEXT, class_id TEXT,
    source_type TEXT NOT NULL, file TEXT NOT NULL, class_name TEXT, method TEXT,
    start_line INTEGER, end_line INTEGER, snippet TEXT, confidence TEXT, relation TEXT
);
CREATE INDEX IF NOT EXISTS idx_evidence_id ON evidence (id);
CREATE INDEX IF NOT EXISTS idx_evidence_method ON evidence (method_pk);
CREATE INDEX IF NOT EXISTS idx_evidence_class ON evidence (class_id);
"""

_CHILD_TABLES = (
    "parameters",
    "variables",
    "control_flow_nodes",
    "data_flow_edges",
    "rule_candidates",
    "evidence",
)


class RunInfo(BaseModel):
    id: int
    repository_id: str
    fingerprint: str
    knowledge_version: int
    status: str
    started_at: str
    finished_at: str | None = None
    files: int | None = None
    classes: int | None = None
    methods: int | None = None
    call_edges: int | None = None
    rule_candidates: int | None = None
    evidence: int | None = None
    risks: int | None = None
    unknowns: int | None = None


class ClassSummary(BaseModel):
    id: str
    fqn: str
    name: str
    kind: str
    package: str | None = None
    file: str
    file_id: str
    start_line: int
    end_line: int
    is_test: bool = False
    method_count: int = 0


class MethodSummary(BaseModel):
    id: str
    method_id: str
    class_id: str
    class_fqn: str
    name: str
    signature: str
    kind: str
    visibility: str | None = None
    file: str
    file_id: str
    start_line: int
    end_line: int
    is_test: bool = False
    complexity: int | None = None
    purpose: str | None = None
    purpose_basis: str | None = None
    callers_count: int = 0
    callees_count: int = 0
    rules_count: int = 0
    risks_count: int = 0
    unknowns_count: int = 0


class StoredCall(BaseModel):
    caller_id: str
    callee_id: str | None = None
    name: str
    owner_type: str | None = None
    status: str
    ambiguous: bool = False
    kind: str
    site_line: int | None = None
    reason: str | None = None


class Reach(BaseModel):
    method_id: str
    depth: int
    via_override: bool = False
    ambiguous: bool = False


def _pack(model: BaseModel) -> bytes:
    return zlib.compress(model.model_dump_json().encode("utf-8"), level=6)


def _unpack(blob: bytes) -> str:
    return zlib.decompress(blob).decode("utf-8")


class KnowledgeStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # ==== runs ==================================================================================

    def last_successful_run(self, repository_id: str) -> RunInfo | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM analysis_runs WHERE repository_id = ? AND status = 'succeeded'"
                " ORDER BY id DESC LIMIT 1",
                (repository_id,),
            ).fetchone()
        return RunInfo(**dict(row)) if row else None

    def runs(self, repository_id: str, limit: int = 10) -> list[RunInfo]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM analysis_runs WHERE repository_id = ? ORDER BY id DESC LIMIT ?",
                (repository_id, limit),
            ).fetchall()
        return [RunInfo(**dict(r)) for r in rows]

    # ==== writing ===============================================================================

    def replace(
        self, knowledge: RepositoryKnowledge, graph: CallGraph, fingerprint: str
    ) -> RunInfo:
        """Replace the repository's whole knowledge model atomically and record the run."""
        repo = knowledge.repository_id
        started = datetime.now(UTC).isoformat()
        with self._conn() as conn:
            try:
                conn.execute("BEGIN")
                self._delete_repository(conn, repo)
                self._insert_classes(conn, repo, knowledge.classes)
                self._insert_methods(conn, repo, knowledge.methods)
                self._insert_graph(conn, repo, knowledge.methods, graph)
                s = knowledge.stats
                cur = conn.execute(
                    "INSERT INTO analysis_runs (repository_id, fingerprint, knowledge_version,"
                    " status, started_at, finished_at, files, classes, methods, call_edges,"
                    " rule_candidates, evidence, risks, unknowns)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        repo,
                        fingerprint,
                        KNOWLEDGE_VERSION,
                        "succeeded",
                        started,
                        datetime.now(UTC).isoformat(),
                        s.files,
                        s.classes,
                        s.methods,
                        s.call_edges,
                        s.rule_candidates,
                        s.evidence,
                        s.risks,
                        s.unknowns,
                    ),
                )
                conn.commit()
                run_id = cur.lastrowid
            except Exception:
                conn.rollback()
                raise
        run = self.last_successful_run(repo)
        assert run is not None and run.id == run_id
        return run

    def _delete_repository(self, conn: sqlite3.Connection, repo: str) -> None:
        method_pks = "(SELECT id FROM methods WHERE repository_id = ?)"
        class_ids = "(SELECT id FROM classes WHERE repository_id = ?)"
        for table in (
            "parameters",
            "variables",
            "control_flow_nodes",
            "data_flow_edges",
            "rule_candidates",
        ):
            conn.execute(f"DELETE FROM {table} WHERE method_pk IN {method_pks}", (repo,))
        conn.execute(f"DELETE FROM fields WHERE class_id IN {class_ids}", (repo,))
        for table in ("evidence", "method_calls", "method_overrides", "methods", "classes"):
            conn.execute(f"DELETE FROM {table} WHERE repository_id = ?", (repo,))

    def _insert_classes(
        self, conn: sqlite3.Connection, repo: str, classes: list[ClassKnowledge]
    ) -> None:
        conn.executemany(
            "INSERT INTO classes (id, repository_id, file_id, fqn, name, kind, package, file,"
            " start_line, end_line, superclass, is_test, method_count, doc)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    c.id,
                    repo,
                    c.file_id,
                    c.fqn,
                    c.name,
                    c.kind,
                    c.package,
                    c.file,
                    c.start_line,
                    c.end_line,
                    c.superclass,
                    int(c.is_test),
                    len(c.method_ids),
                    _pack(c),
                )
                for c in classes
            ],
        )
        conn.executemany(
            "INSERT INTO fields (class_id, name, type, visibility, modifiers, line)"
            " VALUES (?,?,?,?,?,?)",
            [
                (c.id, f.name, f.type, f.visibility, json.dumps(f.modifiers), f.line)
                for c in classes
                for f in c.fields
            ],
        )
        self._insert_evidence(conn, repo, None, [(c.id, e) for c in classes for e in c.evidence])

    def _insert_methods(
        self, conn: sqlite3.Connection, repo: str, methods: list[MethodKnowledge]
    ) -> None:
        conn.executemany(
            "INSERT INTO methods (id, repository_id, class_id, file_id, method_id, class_fqn,"
            " name, signature, kind, visibility, file, start_line, end_line, is_test, complexity,"
            " nesting, purpose, purpose_basis, callers_count, callees_count, rules_count,"
            " risks_count, unknowns_count, doc) VALUES (" + ",".join("?" * 24) + ")",
            [
                (
                    m.id,
                    repo,
                    m.class_id,
                    m.file_id,
                    m.method_id,
                    m.class_fqn,
                    m.name,
                    m.signature,
                    m.kind,
                    m.visibility,
                    m.file,
                    m.start_line,
                    m.end_line,
                    int(m.is_test),
                    m.complexity.cyclomatic,
                    m.complexity.nesting_depth,
                    m.purpose.text,
                    m.purpose.basis,
                    len(m.callers),
                    len(m.callees),
                    len(m.rule_candidates),
                    len(m.risks),
                    len(m.unknowns),
                    _pack(m),
                )
                for m in methods
            ],
        )
        conn.executemany(
            "INSERT INTO parameters (method_pk, position, name, type, var_args, comes_from)"
            " VALUES (?,?,?,?,?,?)",
            [
                (m.id, i, p.name, p.type, int(p.var_args), json.dumps(p.comes_from))
                for m in methods
                for i, p in enumerate(m.parameters)
            ],
        )
        conn.executemany(
            "INSERT INTO variables (method_pk, name, kind, type, symbol_id, declared_line, created,"
            " lifecycle) VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    m.id,
                    v.name,
                    v.kind,
                    v.type,
                    v.symbol_id,
                    v.declared_line,
                    v.created,
                    json.dumps(v.lifecycle()),
                )
                for m in methods
                for v in m.data_flow.variables
            ],
        )
        conn.executemany(
            "INSERT INTO control_flow_nodes (method_pk, ord, parent_ord, kind, text, start_line,"
            " end_line, depth) VALUES (?,?,?,?,?,?,?,?)",
            [row for m in methods for row in _flow_rows(m)],
        )
        conn.executemany(
            "INSERT INTO data_flow_edges (method_pk, ord, source_kind, source_name, source_symbol,"
            " target_kind, target_name, target_symbol, via, line, callee_id, param, ops)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    m.id,
                    i,
                    e.source.kind,
                    e.source.name,
                    e.source.symbol_id,
                    e.target.kind,
                    e.target.name,
                    e.target.symbol_id,
                    e.via,
                    e.line,
                    e.callee_id,
                    e.param,
                    json.dumps(e.ops),
                )
                for m in methods
                for i, e in enumerate(m.data_flow.edges)
            ],
        )
        conn.executemany(
            "INSERT INTO rule_candidates (id, method_pk, kind, start_line, end_line, condition,"
            " meaning, scope, calculation, action, confidence, doc)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    r.id,
                    m.id,
                    r.kind,
                    r.start_line,
                    r.end_line,
                    r.condition,
                    r.meaning,
                    r.scope,
                    r.calculation,
                    r.action,
                    r.confidence,
                    r.model_dump_json(),
                )
                for m in methods
                for r in m.rule_candidates
            ],
        )
        self._insert_evidence(conn, repo, methods, [])

    def _insert_evidence(
        self,
        conn: sqlite3.Connection,
        repo: str,
        methods: list[MethodKnowledge] | None,
        class_pairs: list[tuple[str, Evidence]],
    ) -> None:
        rows: list[tuple[object, ...]] = [
            (
                e.id,
                repo,
                None,
                cid,
                e.source_type,
                e.file,
                e.class_name,
                e.method,
                e.start_line,
                e.end_line,
                e.snippet,
                e.confidence,
                e.relation,
            )
            for cid, e in class_pairs
        ]
        for m in methods or []:
            rows.extend(
                (
                    e.id,
                    repo,
                    m.id,
                    None,
                    e.source_type,
                    e.file,
                    e.class_name,
                    e.method,
                    e.start_line,
                    e.end_line,
                    e.snippet,
                    e.confidence,
                    e.relation,
                )
                for e in m.evidence
            )
        conn.executemany(
            "INSERT INTO evidence (id, repository_id, method_pk, class_id, source_type, file,"
            " class_name, method, start_line, end_line, snippet, confidence, relation)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )

    def _insert_graph(
        self,
        conn: sqlite3.Connection,
        repo: str,
        methods: list[MethodKnowledge],
        graph: CallGraph,
    ) -> None:
        pk_of = {m.method_id: m.id for m in methods}
        rows: list[tuple[object, ...]] = []
        for caller, edges in graph.edges_from.items():
            for e in edges:
                targets: list[str | None] = [*e.targets] or [None]
                for target in targets:
                    rows.append(
                        (
                            repo,
                            pk_of.get(caller),
                            caller,
                            target,
                            e.name,
                            e.owner_type,
                            e.status.value,
                            int(e.status is ResolutionStatus.AMBIGUOUS),
                            e.kind,
                            e.site_line,
                            e.expression_id,
                            e.reason,
                            int(e.implicit),
                        )
                    )
        conn.executemany(
            "INSERT INTO method_calls (repository_id, caller_pk, caller_id, callee_id, name,"
            " owner_type, status, ambiguous, kind, site_line, expression_id, reason, implicit)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.executemany(
            "INSERT INTO method_overrides (repository_id, base_id, override_id) VALUES (?,?,?)",
            [(repo, base, o) for base, overrides in graph.overrides.items() for o in overrides],
        )

    # ==== reading: classes and methods ==========================================================

    def list_classes(
        self,
        repository_id: str,
        *,
        query: str | None = None,
        include_tests: bool = True,
        limit: int = 500,
        offset: int = 0,
    ) -> list[ClassSummary]:
        sql = (
            "SELECT id, fqn, name, kind, package, file, file_id, start_line, end_line, is_test,"
            " method_count FROM classes WHERE repository_id = ?"
        )
        args: list[object] = [repository_id]
        if query:
            sql += " AND (fqn LIKE ? OR name LIKE ?)"
            args += [f"%{query}%", f"%{query}%"]
        if not include_tests:
            sql += " AND is_test = 0"
        sql += " ORDER BY fqn, file LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [ClassSummary(**{**dict(r), "is_test": bool(r["is_test"])}) for r in rows]

    def count_classes(self, repository_id: str) -> int:
        with self._conn() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM classes WHERE repository_id = ?", (repository_id,)
                ).fetchone()[0]
            )

    def get_class(self, class_id: str) -> ClassKnowledge | None:
        with self._conn() as conn:
            row = conn.execute("SELECT doc FROM classes WHERE id = ?", (class_id,)).fetchone()
        return ClassKnowledge.model_validate_json(_unpack(row["doc"])) if row else None

    def find_class(self, repository_id: str, fqn: str) -> list[ClassKnowledge]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT doc FROM classes WHERE repository_id = ? AND fqn = ? ORDER BY file",
                (repository_id, fqn),
            ).fetchall()
        return [ClassKnowledge.model_validate_json(_unpack(r["doc"])) for r in rows]

    def list_methods(self, class_id: str) -> list[MethodSummary]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM methods WHERE class_id = ? ORDER BY start_line, signature",
                (class_id,),
            ).fetchall()
        return [_method_summary(r) for r in rows]

    def search_methods(
        self, repository_id: str, query: str, *, include_tests: bool = False, limit: int = 50
    ) -> list[MethodSummary]:
        like = f"%{query}%"
        sql = (
            "SELECT * FROM methods WHERE repository_id = ? AND (name LIKE ? OR class_fqn LIKE ?"
            " OR method_id LIKE ?)"
        )
        if not include_tests:
            sql += " AND is_test = 0"
        sql += " ORDER BY (name = ?) DESC, length(name), class_fqn LIMIT ?"
        with self._conn() as conn:
            rows = conn.execute(sql, (repository_id, like, like, like, query, limit)).fetchall()
        return [_method_summary(r) for r in rows]

    def summaries_for(self, repository_id: str, method_ids: list[str]) -> dict[str, MethodSummary]:
        """method_id -> summary for the ids that exist (first match when a class is duplicated)."""
        out: dict[str, MethodSummary] = {}
        with self._conn() as conn:
            for start in range(0, len(method_ids), 500):
                chunk = method_ids[start : start + 500]
                marks = ",".join("?" * len(chunk))
                for row in conn.execute(
                    f"SELECT * FROM methods WHERE repository_id = ? AND method_id IN ({marks})"
                    " ORDER BY file",
                    [repository_id, *chunk],
                ):
                    out.setdefault(row["method_id"], _method_summary(row))
        return out

    def get_method_summary(self, method_pk: str) -> MethodSummary | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM methods WHERE id = ?", (method_pk,)).fetchone()
        return _method_summary(row) if row else None

    def resolve_methods(self, repository_id: str, query: str) -> list[MethodSummary]:
        """Methods matching a human reference: a full id, `Class.method`, `Class#method`, or a name.

        Exact matches win over partial ones; tests are excluded unless nothing else matches.
        """
        q = query.strip()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM methods WHERE repository_id = ? AND (method_id = ? OR id = ?)",
                (repository_id, q, q),
            ).fetchall()
            if not rows:
                owner, sep, name = q.replace("#", ".").rpartition(".")
                if sep:
                    rows = conn.execute(
                        "SELECT * FROM methods WHERE repository_id = ? AND name = ?"
                        " AND (class_fqn = ? OR class_fqn LIKE ?) ORDER BY class_fqn, start_line",
                        (repository_id, name.split("(")[0], owner, f"%.{owner}"),
                    ).fetchall()
            if not rows:
                rows = conn.execute(
                    "SELECT * FROM methods WHERE repository_id = ? AND name = ?"
                    " ORDER BY is_test, class_fqn, start_line",
                    (repository_id, q.split("(")[0]),
                ).fetchall()
        found = [_method_summary(r) for r in rows]
        production = [m for m in found if not m.is_test]
        return production or found

    def get_method(self, method_pk: str) -> MethodKnowledge | None:
        with self._conn() as conn:
            row = conn.execute("SELECT doc FROM methods WHERE id = ?", (method_pk,)).fetchone()
        return MethodKnowledge.model_validate_json(_unpack(row["doc"])) if row else None

    def find_method(self, repository_id: str, method_id: str) -> list[MethodKnowledge]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT doc FROM methods WHERE repository_id = ? AND method_id = ? ORDER BY file",
                (repository_id, method_id),
            ).fetchall()
        return [MethodKnowledge.model_validate_json(_unpack(r["doc"])) for r in rows]

    def iter_methods(self, repository_id: str) -> Iterator[MethodKnowledge]:
        with self._conn() as conn:
            for row in conn.execute(
                "SELECT doc FROM methods WHERE repository_id = ? ORDER BY class_fqn, start_line",
                (repository_id,),
            ):
                yield MethodKnowledge.model_validate_json(_unpack(row["doc"]))

    def iter_classes(self, repository_id: str) -> Iterator[ClassKnowledge]:
        with self._conn() as conn:
            for row in conn.execute(
                "SELECT doc FROM classes WHERE repository_id = ? ORDER BY fqn, file",
                (repository_id,),
            ):
                yield ClassKnowledge.model_validate_json(_unpack(row["doc"]))

    # ==== reading: graph, rules, evidence =======================================================

    def calls_from(self, repository_id: str, caller_id: str) -> list[StoredCall]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM method_calls WHERE repository_id = ? AND caller_id = ?"
                " ORDER BY site_line, expression_id",
                (repository_id, caller_id),
            ).fetchall()
        return [_stored_call(r) for r in rows]

    def calls_to(self, repository_id: str, callee_id: str) -> list[StoredCall]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM method_calls WHERE repository_id = ? AND callee_id = ?"
                " ORDER BY caller_id, site_line",
                (repository_id, callee_id),
            ).fetchall()
        return [_stored_call(r) for r in rows]

    def overrides_of(self, repository_id: str, base_id: str) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT override_id FROM method_overrides WHERE repository_id = ? AND base_id = ?"
                " ORDER BY override_id",
                (repository_id, base_id),
            ).fetchall()
        return [r[0] for r in rows]

    def reach(
        self,
        repository_id: str,
        method_id: str,
        depth: int = 1,
        *,
        direction: str = "callees",
        include_candidates: bool = True,
        include_overrides: bool = False,
    ) -> list[Reach]:
        """Breadth-first traversal of the stored call graph (cycle safe)."""
        out: dict[str, Reach] = {}
        visited = {method_id}
        queue: deque[tuple[str, int]] = deque([(method_id, 0)])
        while queue:
            current, level = queue.popleft()
            if level >= depth:
                continue
            if direction == "callees":
                neighbours = [
                    (c.callee_id, c.ambiguous, False)
                    for c in self.calls_from(repository_id, current)
                    if c.callee_id and (include_candidates or not c.ambiguous)
                ]
                if include_overrides:
                    neighbours += [
                        (o, False, True)
                        for target, _, _ in list(neighbours)
                        for o in self.overrides_of(repository_id, target or "")
                    ]
            else:
                neighbours = [
                    (c.caller_id, c.ambiguous, False)
                    for c in self.calls_to(repository_id, current)
                    if include_candidates or not c.ambiguous
                ]
            for target, ambiguous, via_override in neighbours:
                if not target or target in visited:
                    continue
                visited.add(target)
                out[target] = Reach(
                    method_id=target,
                    depth=level + 1,
                    ambiguous=ambiguous,
                    via_override=via_override,
                )
                queue.append((target, level + 1))
        return sorted(out.values(), key=lambda r: (r.depth, r.method_id))

    def rules(self, method_pk: str) -> list[RuleCandidate]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT doc FROM rule_candidates WHERE method_pk = ? ORDER BY start_line, kind",
                (method_pk,),
            ).fetchall()
        return [RuleCandidate.model_validate_json(r["doc"]) for r in rows]

    def evidence_by_id(self, evidence_id: str) -> Evidence | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM evidence WHERE id = ? LIMIT 1", (evidence_id,)
            ).fetchone()
        return _evidence(row) if row else None

    def evidence_for_method(self, method_pk: str) -> list[Evidence]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM evidence WHERE method_pk = ? ORDER BY start_line, id", (method_pk,)
            ).fetchall()
        return [_evidence(r) for r in rows]

    def argument_sources(self, callee_id: str, param: str) -> list[sqlite3.Row]:
        """Data-flow edges that pass a value into `param` of `callee_id` (across all callers)."""
        with self._conn() as conn:
            return conn.execute(
                "SELECT e.*, m.method_id AS caller_id FROM data_flow_edges e"
                " JOIN methods m ON m.id = e.method_pk"
                " WHERE e.via = 'argument' AND e.callee_id = ? AND e.param = ?"
                " ORDER BY m.method_id, e.line",
                (callee_id, param),
            ).fetchall()

    def repository_of_method(self, method_pk: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT repository_id FROM methods WHERE id = ?", (method_pk,)
            ).fetchone()
        return row["repository_id"] if row else None

    def repository_of_class(self, class_id: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT repository_id FROM classes WHERE id = ?", (class_id,)
            ).fetchone()
        return row["repository_id"] if row else None

    def result_uses(self, repository_id: str, method_id: str) -> list[sqlite3.Row]:
        """Data-flow edges in callers where the result of `method_id` is consumed."""
        with self._conn() as conn:
            return conn.execute(
                "SELECT e.target_kind, e.target_name, e.target_symbol, e.via, e.line,"
                " m.method_id AS caller_id, m.id AS caller_pk"
                " FROM data_flow_edges e JOIN methods m ON m.id = e.method_pk"
                " WHERE m.repository_id = ? AND e.source_kind = 'call_result'"
                " AND e.source_symbol = ? ORDER BY m.method_id, e.line",
                (repository_id, method_id),
            ).fetchall()

    def variable_writers(self, repository_id: str, symbol_id: str) -> list[sqlite3.Row]:
        """Methods whose lifecycle for the variable/field `symbol_id` includes a modification."""
        with self._conn() as conn:
            return conn.execute(
                "SELECT m.method_id, m.id AS method_pk, v.lifecycle FROM variables v"
                " JOIN methods m ON m.id = v.method_pk"
                " WHERE m.repository_id = ? AND v.symbol_id = ? AND v.lifecycle LIKE '%modified%'"
                " ORDER BY m.method_id",
                (repository_id, symbol_id),
            ).fetchall()

    def counts(self, repository_id: str) -> dict[str, int]:
        with self._conn() as conn:

            def one(sql: str) -> int:
                return int(conn.execute(sql, (repository_id,)).fetchone()[0])

            return {
                "classes": one("SELECT COUNT(*) FROM classes WHERE repository_id = ?"),
                "methods": one("SELECT COUNT(*) FROM methods WHERE repository_id = ?"),
                "calls": one("SELECT COUNT(*) FROM method_calls WHERE repository_id = ?"),
                "evidence": one("SELECT COUNT(*) FROM evidence WHERE repository_id = ?"),
                "rules": one(
                    "SELECT COUNT(*) FROM rule_candidates WHERE method_pk IN"
                    " (SELECT id FROM methods WHERE repository_id = ?)"
                ),
            }


# ==== row helpers =============================================================


def _method_summary(r: sqlite3.Row) -> MethodSummary:
    return MethodSummary(
        id=r["id"],
        method_id=r["method_id"],
        class_id=r["class_id"],
        class_fqn=r["class_fqn"],
        name=r["name"],
        signature=r["signature"],
        kind=r["kind"],
        visibility=r["visibility"],
        file=r["file"],
        file_id=r["file_id"],
        start_line=r["start_line"],
        end_line=r["end_line"],
        is_test=bool(r["is_test"]),
        complexity=r["complexity"],
        purpose=r["purpose"],
        purpose_basis=r["purpose_basis"],
        callers_count=r["callers_count"],
        callees_count=r["callees_count"],
        rules_count=r["rules_count"],
        risks_count=r["risks_count"],
        unknowns_count=r["unknowns_count"],
    )


def _stored_call(r: sqlite3.Row) -> StoredCall:
    return StoredCall(
        caller_id=r["caller_id"],
        callee_id=r["callee_id"],
        name=r["name"],
        owner_type=r["owner_type"],
        status=r["status"],
        ambiguous=bool(r["ambiguous"]),
        kind=r["kind"],
        site_line=r["site_line"],
        reason=r["reason"],
    )


def _evidence(r: sqlite3.Row) -> Evidence:
    return Evidence(
        id=r["id"],
        source_type=r["source_type"],
        file=r["file"],
        class_name=r["class_name"],
        method=r["method"],
        start_line=r["start_line"],
        end_line=r["end_line"],
        snippet=r["snippet"] or "",
        confidence=r["confidence"],
        relation=r["relation"],
    )


def _flow_rows(m: MethodKnowledge) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    counter = 0

    def visit(node: FlowNode, parent: int | None, depth: int) -> None:
        nonlocal counter
        order = counter
        counter += 1
        rows.append(
            (m.id, order, parent, node.kind, node.text, node.start_line, node.end_line, depth)
        )
        for child in [*node.children, *node.branches]:
            visit(child, order, depth + 1)

    for root in m.control_flow.nodes:
        visit(root, None, 0)
    return rows
