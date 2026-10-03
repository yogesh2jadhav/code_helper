"""Trace a variable's data across methods: where it comes from, what happens to it, where it goes.

Entirely deterministic, built from the stored data-flow model. The LLM only explains the result.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.analyzer.data_flow import FlowRef, VariableFlow
from app.context.citations import Citation, CitationBook
from app.knowledge.models import MethodKnowledge
from app.knowledge.store import KnowledgeStore

Direction = Literal["upstream", "within", "downstream"]


class VariableNotFoundError(LookupError):
    def __init__(self, name: str, available: list[str]) -> None:
        super().__init__(f"no variable '{name}' in this method; available: {', '.join(available)}")
        self.available = available


class TraceStep(BaseModel):
    direction: Direction
    kind: str
    depth: int  # 0 = the traced method; 1 = one call away, ...
    method_id: str
    text: str
    file: str | None = None
    line: int | None = None
    label: str | None = None  # citation label


class TraceResult(BaseModel):
    method_id: str
    variable: str
    symbol_id: str
    variable_kind: str
    type: str | None = None
    steps: list[TraceStep] = Field(default_factory=list)
    stops: list[str] = Field(default_factory=list)  # where and why the trace ends
    citations: list[Citation] = Field(default_factory=list)

    def as_text(self) -> str:
        out = [
            f"Subject: {self.variable_kind} `{self.variable}` ({self.type or 'type unknown'}) "
            f"in {self.method_id}"
        ]
        for direction, title in (
            ("upstream", "Where it comes from"),
            ("within", "Inside the method"),
            ("downstream", "Where it goes"),
        ):
            steps = [s for s in self.steps if s.direction == direction]
            if not steps:
                continue
            out.append(f"\n{title}:")
            for s in steps:
                tag = f" [{s.label}]" if s.label else ""
                out.append(f"{'  ' * (s.depth + 1)}- {s.text}{tag}")
        if self.stops:
            out += ["\nThe trace stops here:", *(f"  - {x}" for x in self.stops)]
        return "\n".join(out)


def short(method_id: str) -> str:
    cls, _, sig = method_id.partition("#")
    return f"{cls.rsplit('.', 1)[-1]}.{sig}"


def article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def describe_source(ref: FlowRef) -> str:
    if ref.kind == "call_result":
        return f"the result of {ref.name}()"
    if ref.kind == "literal":
        return f"{article(ref.name)} {ref.name} literal"
    if ref.kind == "new":
        return f"{article(ref.name)} new {ref.name}"
    return f"{ref.kind} `{ref.name}`"


class TraceService:
    def __init__(self, store: KnowledgeStore) -> None:
        self._store = store

    def trace(
        self, method_pk: str, variable: str, depth: int = 2, max_breadth: int = 5
    ) -> TraceResult:
        method = self._store.get_method(method_pk)
        if method is None:
            raise LookupError(f"unknown method {method_pk}")
        repo = self._store.repository_of_method(method_pk) or ""
        var = self._pick(method, variable)
        run = _Run(self._store, repo, depth, max_breadth, CitationBook())
        result = TraceResult(
            method_id=method.method_id,
            variable=var.name,
            symbol_id=var.symbol_id,
            variable_kind=var.kind,
            type=var.type,
        )
        run.upstream(method, var, 0)
        run.within(method, var, 0)
        run.downstream(method, var, 0)
        result.steps = run.steps
        result.stops = list(dict.fromkeys(run.stops))
        result.citations = run.book.citations
        return result

    @staticmethod
    def _pick(method: MethodKnowledge, name: str) -> VariableFlow:
        matches = [v for v in method.data_flow.variables if v.symbol_id == name or v.name == name]
        if not matches:
            raise VariableNotFoundError(name, sorted({v.name for v in method.data_flow.variables}))
        return matches[-1]  # the innermost/latest declaration if a name is reused


class _Run:
    def __init__(
        self, store: KnowledgeStore, repo: str, depth: int, breadth: int, book: CitationBook
    ) -> None:
        self.store, self.repo, self.max_depth, self.breadth, self.book = (
            store,
            repo,
            depth,
            breadth,
            book,
        )
        self.steps: list[TraceStep] = []
        self.stops: list[str] = []
        self._seen: set[tuple[str, str, str]] = set()

    # ---- helpers -----------------------------------------------------------------------------

    def add(
        self,
        direction: Direction,
        kind: str,
        depth: int,
        m: MethodKnowledge | None,
        method_id: str,
        text: str,
        line: int | None = None,
    ) -> None:
        label = None
        file = m.file if m else None
        if m and line:
            label = self.book.cite(m.file, line, line, "source_code", m.name).label
        self.steps.append(
            TraceStep(
                direction=direction,
                kind=kind,
                depth=depth,
                method_id=method_id,
                text=text,
                file=file,
                line=line,
                label=label,
            )
        )

    def method(self, method_id: str) -> MethodKnowledge | None:
        found = self.store.find_method(self.repo, method_id)
        return found[0] if found else None

    def first(self, key: tuple[str, str, str]) -> bool:
        if key in self._seen:
            return False
        self._seen.add(key)
        return True

    # ---- within ------------------------------------------------------------------------------

    def within(self, m: MethodKnowledge, var: VariableFlow, depth: int) -> None:
        for e in var.events:
            if e.kind == "read":
                continue
            text = {
                "created": f"created: {e.detail}",
                "returned": "returned from the method",
                "passed": f"passed {e.detail}",
                "derived": f"flows {e.detail}",
            }.get(e.kind, f"{e.kind}: {e.detail}")
            self.add("within", e.kind, depth, m, m.method_id, text, e.line)

    # ---- upstream ----------------------------------------------------------------------------

    def upstream(self, m: MethodKnowledge, var: VariableFlow, depth: int) -> None:
        if not self.first((m.method_id, var.symbol_id, "up")):
            return
        if var.kind == "parameter":
            self._from_callers(m, var, depth)
        elif var.kind == "field":
            self._from_writers(m, var, depth)
        else:
            self._from_edges(m, var, depth)

    def _from_callers(self, m: MethodKnowledge, var: VariableFlow, depth: int) -> None:
        rows = self.store.argument_sources(m.method_id, var.name)
        if not rows:
            self.stops.append(
                f"{short(m.method_id)}: no project caller passes `{var.name}` "
                "(entry point or external caller)"
            )
            return
        for row in rows[: self.breadth]:
            caller = self.method(row["caller_id"])
            src = describe_source(
                FlowRef(kind=row["source_kind"], name=row["source_name"], text="")
            )
            self.add(
                "upstream",
                "passed_in",
                depth,
                caller,
                row["caller_id"],
                f"{short(row['caller_id'])} passes {src} as `{var.name}`",
                row["line"],
            )
            if caller is not None and depth + 1 <= self.max_depth - 1 and row["source_symbol"]:
                next_var = next(
                    (v for v in caller.data_flow.variables if v.symbol_id == row["source_symbol"]),
                    None,
                )
                if next_var is not None:
                    self.upstream(caller, next_var, depth + 1)
        if len(rows) > self.breadth:
            self.stops.append(
                f"{short(m.method_id)}: {len(rows) - self.breadth} more call sites not shown"
            )
        if depth >= self.max_depth - 1:
            self.stops.append(f"depth limit ({self.max_depth}) reached above {short(m.method_id)}")

    def _from_writers(self, m: MethodKnowledge, var: VariableFlow, depth: int) -> None:
        writers = [w for w in self.store.variable_writers(self.repo, var.symbol_id)]
        if not writers:
            self.stops.append(
                f"field `{var.name}` is never written by an analysed method "
                "(constant or set elsewhere)"
            )
        for w in writers[: self.breadth]:
            self.add(
                "upstream",
                "written_in",
                depth,
                self.method(w["method_id"]),
                w["method_id"],
                f"field `{var.name}` is modified in {short(w['method_id'])}",
            )

    def _from_edges(self, m: MethodKnowledge, var: VariableFlow, depth: int) -> None:
        incoming = [
            e
            for e in m.data_flow.edges
            if e.target.symbol_id == var.symbol_id and e.via != "return"
        ]
        if not incoming and var.created:
            self.add(
                "upstream",
                "created",
                depth,
                m,
                m.method_id,
                f"created: {var.created}",
                var.declared_line,
            )
        for e in incoming[: self.breadth * 2]:
            self.add(
                "upstream",
                e.via,
                depth,
                m,
                m.method_id,
                f"{e.via}: from {describe_source(e.source)}",
                e.line,
            )
            self._follow_source(m, e.source, depth)

    def _follow_source(self, m: MethodKnowledge, src: FlowRef, depth: int) -> None:
        if depth + 1 > self.max_depth - 1:
            if src.kind in ("parameter", "call_result"):
                self.stops.append(
                    f"depth limit ({self.max_depth}) reached at {describe_source(src)}"
                )
            return
        if src.kind in ("parameter", "local", "field") and src.symbol_id:
            nxt = next((v for v in m.data_flow.variables if v.symbol_id == src.symbol_id), None)
            if nxt is not None:
                self.upstream(m, nxt, depth + 1)
        elif src.kind == "call_result" and src.symbol_id:
            callee = self.method(src.symbol_id)
            if callee is None:
                return
            self.add(
                "upstream",
                "callee_returns",
                depth + 1,
                callee,
                callee.method_id,
                f"{short(callee.method_id)} returns: "
                + ("; ".join(callee.output.returns[:4]) or "-"),
                callee.start_line,
            )
            returned = {
                e.source.symbol_id
                for e in callee.data_flow.edges
                if e.via == "return" and e.source.symbol_id
            }
            for v in [v for v in callee.data_flow.variables if v.symbol_id in returned][
                : self.breadth
            ]:
                self.upstream(callee, v, depth + 1)
        elif src.kind == "call_result":
            self.stops.append(f"{describe_source(src)} comes from library/JDK code with no source")

    # ---- downstream --------------------------------------------------------------------------

    def downstream(self, m: MethodKnowledge, var: VariableFlow, depth: int) -> None:
        if not self.first((m.method_id, var.symbol_id, "down")):
            return
        for e in var.events:
            if e.kind == "passed":
                self._passed(m, e, depth)
            elif e.kind == "derived":
                target = e.detail.replace("into ", "").strip()
                nxt = next(
                    (
                        v
                        for v in m.data_flow.variables
                        if v.name == target and v.declared_line >= var.declared_line
                    ),
                    None,
                )
                self.add(
                    "downstream", "derived", depth, m, m.method_id, f"feeds `{target}`", e.line
                )
                if nxt is not None and nxt.symbol_id != var.symbol_id:
                    self.downstream(m, nxt, depth)
            elif e.kind == "returned":
                self._returned(m, depth)

    def _passed(self, m: MethodKnowledge, e, depth: int) -> None:  # type: ignore[no-untyped-def]
        if not e.callee_id:
            self.add(
                "downstream", "passed_external", depth, m, m.method_id, f"passed {e.detail}", e.line
            )
            self.stops.append(f"passed to library/JDK code: {e.detail} (no source available)")
            return
        self.add(
            "downstream",
            "passed",
            depth,
            m,
            m.method_id,
            f"passed {e.detail} ({short(e.callee_id)})",
            e.line,
        )
        if depth + 1 > self.max_depth - 1:
            self.stops.append(f"depth limit ({self.max_depth}) reached inside {short(e.callee_id)}")
            return
        callee = self.method(e.callee_id)
        param = (
            next(
                (
                    v
                    for v in callee.data_flow.variables
                    if v.name == e.param and v.kind == "parameter"
                ),
                None,
            )
            if callee
            else None
        )
        if callee is not None and param is not None:
            for ev in param.events:
                if ev.kind != "read" and ev.kind != "created":
                    self.add(
                        "downstream",
                        ev.kind,
                        depth + 1,
                        callee,
                        callee.method_id,
                        f"in {short(callee.method_id)} `{param.name}`: "
                        f"{ev.kind} {ev.detail}".strip(),
                        ev.line,
                    )
            self.downstream(callee, param, depth + 1)

    def _returned(self, m: MethodKnowledge, depth: int) -> None:
        self.add("downstream", "returned", depth, m, m.method_id, "returned to the caller", None)
        if depth + 1 > self.max_depth - 1:
            self.stops.append(f"depth limit ({self.max_depth}) reached above {short(m.method_id)}")
            return
        uses = self.store.result_uses(self.repo, m.method_id)
        if not uses:
            self.stops.append(f"{short(m.method_id)} has no project caller that uses its result")
        for row in uses[: self.breadth]:
            caller = self.method(row["caller_id"])
            what = {
                "return": "returns it onward",
                "argument": f"passes it on ({row['target_name']})",
                "receiver": f"calls {row['target_name']}() on it",
                "declare": f"stores it in `{row['target_name']}`",
                "assign": f"stores it in `{row['target_name']}`",
            }.get(row["via"], f"{row['via']} into `{row['target_name']}`")
            self.add(
                "downstream",
                "used_by_caller",
                depth + 1,
                caller,
                row["caller_id"],
                f"{short(row['caller_id'])} {what}",
                row["line"],
            )
            if caller is not None and row["target_symbol"] and row["via"] in ("declare", "assign"):
                nxt = next(
                    (v for v in caller.data_flow.variables if v.symbol_id == row["target_symbol"]),
                    None,
                )
                if nxt is not None:
                    self.downstream(caller, nxt, depth + 1)
