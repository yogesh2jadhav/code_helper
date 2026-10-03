"""Data-flow model of a method: how values move between variables, calls and results.

Edges go `source -> target`. A *source* is the outermost value-producing expression inside a
source range (a variable, a field, a call result, an object creation or a literal); a *target* is a
variable, a callee parameter, the method's return value, or the result of a call. Everything is
derived from exact source positions plus symbol resolution, so there is no guessing about which
variable feeds which.

Per variable, the edges are also folded into a lifecycle: where it was created, where it was read
or modified, where it was passed (to which callee parameter), and which stream operations were
applied to it (filtered, grouped, collected, ...).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Literal

from pydantic import BaseModel, Field

from app.analyzer.ast_models import Expression, Method, Range, ValueHint
from app.analyzer.control_flow import _find_pipelines
from app.analyzer.mutators import is_mutator
from app.analyzer.resolution_models import Origin, Resolution, ResolutionStatus

VARIABLE_KINDS = {
    "local_variable": "local",
    "lambda_parameter": "local",
    "catch_parameter": "local",
    "pattern_variable": "local",
    "parameter": "parameter",
    "field": "field",
    "record_component": "field",
}
Via = Literal[
    "declare",
    "assign",
    "compound_assign",
    "argument",
    "return",
    "stream",
    "receiver",
    "mutation",
    "iterate",
    "lambda_param",
]
EventKind = Literal[
    "created",
    "read",
    "modified",
    "passed",
    "returned",
    "derived",
    "filtered",
    "mapped",
    "sorted",
    "grouped",
    "collected",
    "aggregated",
    "consumed",
    "iterated",
]
_LITERALS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r'^"(?:[^"\\]|\\.)*"$'), "String"),
    (re.compile(r"^'(?:[^'\\]|\\.)+'$"), "char"),
    (re.compile(r"^(?:true|false)$"), "boolean"),
    (re.compile(r"^[-+]?\d[\d_]*[lL]$"), "long"),
    (re.compile(r"^[-+]?\d[\d_]*$"), "int"),
    (re.compile(r"^[-+]?(?:\d[\d_]*\.\d*|\.\d+)[fF]$"), "float"),
    (re.compile(r"^[-+]?(?:\d[\d_]*\.\d*|\.\d+)(?:[dD])?$"), "double"),
)


def literal_type(text: str | None) -> str | None:
    """The type of `text` if it is exactly one literal (`"x"`, `42`, `true`), else None."""
    if not text:
        return None
    return next((kind for pattern, kind in _LITERALS if pattern.match(text.strip())), None)


_OP_EVENT = {
    "filter": "filtered",
    "map": "mapped",
    "sort": "sorted",
    "group": "grouped",
    "collect": "collected",
    "aggregate": "aggregated",
    "match": "consumed",
    "find": "consumed",
    "consume": "consumed",
}


class FlowRef(BaseModel):
    kind: Literal[
        "parameter",
        "local",
        "field",
        "constant",
        "call_result",
        "new",
        "literal",
        "return",
        "callee_parameter",
        "external_argument",
        "other",
    ]
    name: str
    symbol_id: str | None = None  # variable symbol id, or the callee method id for call results
    type: str | None = None
    text: str = ""
    line: int = 0  # where this reference appears
    decl_line: int | None = None  # where the variable is declared (variables only)


class DataFlowEdge(BaseModel):
    source: FlowRef
    target: FlowRef
    via: Via
    line: int
    expression_id: int
    callee_id: str | None = None  # argument edges to a project method
    param: str | None = None
    ops: list[str] = Field(default_factory=list)  # stream operations applied along the edge
    detail: str | None = None


class FlowEvent(BaseModel):
    kind: EventKind
    line: int
    column: int = 0
    detail: str = ""
    expression_id: int | None = None
    callee_id: str | None = None
    param: str | None = None


class VariableFlow(BaseModel):
    name: str
    kind: str  # parameter | local | field
    symbol_id: str
    type: str | None = None
    declared_line: int = 0
    created: str | None = (
        None  # how it came to exist: "parameter", "initialized from call f()", ...
    )
    events: list[FlowEvent] = Field(default_factory=list)

    def of_kind(self, *kinds: str) -> list[FlowEvent]:
        return [e for e in self.events if e.kind in kinds]

    def lifecycle(self) -> dict[str, list[str]]:
        """A compact summary: what happened to the variable, in order, without every read."""
        out: dict[str, list[str]] = {}
        for e in self.events:
            if e.kind == "read":
                continue
            out.setdefault(e.kind, []).append(f"L{e.line} {e.detail}".strip())
        return out


class MethodDataFlow(BaseModel):
    method_id: str
    variables: list[VariableFlow] = Field(default_factory=list)
    edges: list[DataFlowEdge] = Field(default_factory=list)

    def variable(self, name: str) -> VariableFlow | None:
        found = [v for v in self.variables if v.name == name]
        return found[-1] if found else None

    def edges_from(self, symbol_or_name: str) -> list[DataFlowEdge]:
        return [e for e in self.edges if symbol_or_name in (e.source.symbol_id, e.source.name)]

    def edges_to(self, symbol_or_name: str) -> list[DataFlowEdge]:
        return [e for e in self.edges if symbol_or_name in (e.target.symbol_id, e.target.name)]


ParamNames = Callable[[str], "list[str] | None"]


# ==== construction ===========================================================================


def build_data_flow(
    method: Method,
    method_id: str,
    refs: dict[int, Resolution],
    param_names: ParamNames | None = None,
) -> MethodDataFlow:
    return _Builder(method, method_id, refs, param_names or (lambda _id: None)).build()


class _Builder:
    def __init__(
        self, method: Method, method_id: str, refs: dict[int, Resolution], params_of: ParamNames
    ) -> None:
        self.m = method
        self.method_id = method_id
        self.refs = refs
        self.params_of = params_of
        self.exprs = method.expressions
        self.by_id = {e.id: e for e in self.exprs}
        self.stmts = {s.id: s for s in method.statements}
        self.edges: list[DataFlowEdge] = []
        self.variables: dict[str, VariableFlow] = {}
        self.lambdas = [e for e in self.exprs if e.kind == "lambda"]
        self.pipelines = _find_pipelines(self.exprs, refs)
        self.chain_calls = self._chain_call_ids()
        self._candidates = [e for e in self.exprs if self._source_ref(e) is not None]
        self._field_receivers = {
            x.receiver_expr_id
            for x in self._candidates
            if x.kind == "field_access" and x.receiver_expr_id is not None
        }

    # ---- driver ------------------------------------------------------------------------------

    def build(self) -> MethodDataFlow:
        self._parameters()
        for e in self.exprs:
            if e.kind == "variable_declaration":
                self._declaration(e)
            elif e.kind == "assignment":
                self._assignment(e)
            elif e.kind == "method_call":
                self._call(e)
            elif e.kind == "object_creation":
                self._arguments(e, callee_id=self._callee_id(e), owner=None)
            elif e.kind in ("name_ref", "field_access"):
                self._read(e)
        self._returns()
        self._pipelines()
        for v in self.variables.values():
            v.events.sort(key=lambda ev: (ev.line, ev.column, _EVENT_ORDER.get(ev.kind, 5)))
        self.edges.sort(key=lambda ed: (ed.line, ed.expression_id))
        ordered = sorted(self.variables.values(), key=lambda v: (v.declared_line, v.name))
        return MethodDataFlow(method_id=self.method_id, variables=ordered, edges=self.edges)

    # ---- references --------------------------------------------------------------------------

    def _source_ref(self, e: Expression) -> FlowRef | None:
        """The FlowRef an expression stands for as a *value*, or None if it is not one."""
        ref = self.refs.get(e.id)
        if e.kind == "name_ref" and ref is not None:
            kind = VARIABLE_KINDS.get(ref.kind)
            if kind and ref.symbol_id:
                return self._var_ref(e, ref, kind)
            return None
        if e.kind == "field_access" and ref is not None and ref.symbol_id:
            if ref.kind == "enum_constant":
                return FlowRef(
                    kind="constant",
                    name=e.text,
                    symbol_id=ref.symbol_id,
                    line=e.start_line,
                    text=e.text,
                    type=_type_text(ref),
                )
            kind = VARIABLE_KINDS.get(ref.kind)
            if kind:
                return self._var_ref(e, ref, kind)
            return None
        if e.kind == "method_call" and ref is not None:
            project = ref.status is ResolutionStatus.RESOLVED and ref.origin is Origin.PROJECT
            return FlowRef(
                kind="call_result",
                name=ref.name or e.name or "",
                symbol_id=ref.symbol_id if project else None,
                type=_type_text(ref),
                text=e.text,
                line=e.start_line,
            )
        if e.kind == "object_creation":
            return FlowRef(
                kind="new",
                name=e.name or e.type or "",
                symbol_id=ref.symbol_id if ref else None,
                type=e.type,
                text=e.text,
                line=e.start_line,
            )
        return None

    def _var_ref(self, e: Expression, ref: Resolution, kind: str) -> FlowRef:
        return FlowRef(
            kind=kind,  # type: ignore[arg-type]
            name=(e.name or e.text) if e.kind == "name_ref" else e.text,
            symbol_id=ref.symbol_id,
            type=_type_text(ref),
            text=e.text,
            line=e.start_line,
            decl_line=ref.line,
        )

    def _variable(
        self, symbol_id: str, name: str, kind: str, type_: str | None, line: int
    ) -> VariableFlow:
        v = self.variables.get(symbol_id)
        if v is None:
            v = VariableFlow(
                name=name, kind=kind, symbol_id=symbol_id, type=type_, declared_line=line
            )
            self.variables[symbol_id] = v
        return v

    def _event(
        self,
        ref: FlowRef | None,
        kind: EventKind,
        e: Expression,
        detail: str = "",
        *,
        line: int | None = None,
        callee_id: str | None = None,
        param: str | None = None,
    ) -> None:
        if ref is None or ref.symbol_id is None or ref.kind not in ("parameter", "local", "field"):
            return
        v = self._variable(ref.symbol_id, ref.name, ref.kind, ref.type, ref.decl_line or ref.line)
        v.events.append(
            FlowEvent(
                kind=kind,
                line=line if line is not None else e.start_line,
                column=e.start_column,
                detail=detail,
                expression_id=e.id if e.id > 0 else None,
                callee_id=callee_id,
                param=param,
            )
        )

    # ---- sources in a range ------------------------------------------------------------------

    def sources(
        self, rng: Range | None, exclude: set[int] | None = None
    ) -> list[tuple[Expression, FlowRef]]:
        """Outermost value-producing expressions inside `rng`, ignoring lambda bodies."""
        if rng is None:
            return []
        # lambda bodies are opaque: what flows through a lambda argument is not the argument
        opaque = [lam.range for lam in self.lambdas if rng.contains(lam.range)]
        inside = [
            e
            for e in self._candidates
            if rng.contains(e.range)
            and (exclude is None or e.id not in exclude)
            and not any(o.contains(e.range) for o in opaque)
        ]
        out: list[tuple[Expression, FlowRef]] = []
        for e in inside:
            nested = any(
                o is not e and o.range.contains(e.range) and o.range != e.range for o in inside
            )
            if not nested:
                ref = self._source_ref(e)
                if ref is not None:
                    out.append((e, ref))
        return sorted(out, key=lambda t: (t[0].start_line, t[0].start_column))

    def _value_sources(
        self, hint: ValueHint | None, rng: Range | None, exclude: set[int] | None = None
    ) -> list[tuple[Expression | None, FlowRef]]:
        found: list[tuple[Expression | None, FlowRef]] = list(self.sources(rng, exclude))
        if hint is not None and hint.kind == "literal" and hint.type and hint.type != "null":
            line = rng.start_line if rng else 0
            found.insert(
                0,
                (
                    None,
                    FlowRef(kind="literal", name=hint.type, text=f"{hint.type} literal", line=line),
                ),
            )
        return found

    # ---- edge builders -----------------------------------------------------------------------

    def _edge(
        self,
        src: FlowRef,
        dst: FlowRef,
        via: Via,
        e: Expression,
        *,
        callee_id: str | None = None,
        param: str | None = None,
        ops: list[str] | None = None,
        detail: str | None = None,
    ) -> None:
        self.edges.append(
            DataFlowEdge(
                source=src,
                target=dst,
                via=via,
                line=e.start_line,
                expression_id=e.id,
                callee_id=callee_id,
                param=param,
                ops=ops or [],
                detail=detail,
            )
        )

    def _parameters(self) -> None:
        for p in self.m.parameters:
            symbol = f"{self.method_id}${p.name}"
            v = self._variable(
                symbol,
                p.name,
                "parameter",
                p.type + ("..." if p.var_args else ""),
                self.m.start_line,
            )
            v.created = "parameter"
            v.events.append(
                FlowEvent(
                    kind="created", line=self.m.start_line, detail=f"parameter {p.type} {p.name}"
                )
            )

    def _declaration(self, e: Expression) -> None:
        ref = self.refs.get(e.id)
        if ref is None or not ref.symbol_id:
            return
        kind = "local"
        var = self._variable(ref.symbol_id, e.name or "", kind, e.type, e.start_line)
        target = FlowRef(
            kind="local", name=e.name or "", symbol_id=ref.symbol_id, type=e.type, line=e.start_line
        )

        if "foreach" in e.tags:
            stmt = self.stmts.get(e.statement_id or -1)
            srcs: Sequence[tuple[Expression | None, FlowRef]] = self.sources(
                stmt.header_range if stmt else None, {e.id}
            )
            for se, sref in srcs:
                self._edge(sref, target, "iterate", e, detail="each element")
                if se is not None:
                    self._event(sref, "iterated", se, f"into {e.name}")
            var.created = "element of " + (srcs[0][1].text if srcs else "iterable")
            var.events.append(
                FlowEvent(kind="created", line=e.start_line, detail=var.created, expression_id=e.id)
            )
            return
        if "lambda_param" in e.tags:
            self._lambda_param(e, target, var)
            return
        if "catch_param" in e.tags or "pattern" in e.tags or "param" in e.tags:
            var.created = "catch parameter" if "catch_param" in e.tags else "pattern binding"
            var.events.append(
                FlowEvent(kind="created", line=e.start_line, detail=var.created, expression_id=e.id)
            )
            return

        init = e.initializer
        srcs = self._value_sources(init, init.range if init else None, {e.id})
        for se, sref in srcs:
            self._edge(sref, target, "declare", e)
            if se is not None:
                self._event(sref, "derived", se, f"into {e.name}")
        var.created = _created_text(srcs, e.right)
        var.events.append(
            FlowEvent(
                kind="created",
                line=e.start_line,
                column=e.start_column,
                detail=var.created,
                expression_id=e.id,
            )
        )

    def _lambda_param(self, e: Expression, target: FlowRef, var: VariableFlow) -> None:
        origin = self._lambda_owner_source(e)
        var.created = f"lambda parameter over {origin.text}" if origin else "lambda parameter"
        var.events.append(
            FlowEvent(kind="created", line=e.start_line, detail=var.created, expression_id=e.id)
        )
        if origin is not None:
            self._edge(origin, target, "lambda_param", e, detail="each element")

    def _lambda_owner_source(self, param: Expression) -> FlowRef | None:
        """The value a lambda parameter ranges over: the root receiver of its enclosing call."""
        lam = next((x for x in self.lambdas if x.range.contains(param.range)), None)
        if lam is None:
            return None
        call = next(
            (
                c
                for c in self.exprs
                if c.kind == "method_call"
                and c.receiver_expr_id is not None
                and c.range.contains(lam.range)
                and c.range != lam.range
                and any(a.range and a.range.contains(lam.range) for a in (c.args or []))
            ),
            None,
        )
        return self._root_source(call) if call else None

    def _root_source(self, call: Expression) -> FlowRef | None:
        cur: Expression | None = call
        while cur is not None and cur.kind == "method_call" and cur.receiver_expr_id is not None:
            cur = self.by_id.get(cur.receiver_expr_id)
        return self._source_ref(cur) if cur is not None else None

    def _assignment(self, e: Expression) -> None:
        target_expr = self._assignment_target(e)
        target = self._source_ref(target_expr) if target_expr else None
        compound = e.operator not in ("=", None)
        op = e.operator or "="
        if op in ("++", "--"):
            self._event(target, "modified", e, op)
            if target:
                self._edge(target, target, "compound_assign", e, detail=op)
            return
        if target is None:  # `arr[i] = x`, `a.b().c = x`: record the container being changed
            container = self._container(e)
            target = container
        init = e.initializer
        srcs = self._value_sources(init, init.range if init else None)
        what = "write" if not compound else f"update ({op})"
        if target is not None:
            self._event(target, "modified", e, f"{what}: {e.right or ''}".strip())
            for se, sref in srcs:
                self._edge(sref, target, "compound_assign" if compound else "assign", e)
                if se is not None:
                    self._event(sref, "derived", se, f"into {target.name}")
            if compound:
                self._edge(target, target, "compound_assign", e, detail=op)

    def _assignment_target(self, e: Expression) -> Expression | None:
        for c in self.exprs:
            if (
                (c.start_line, c.start_column) == (e.start_line, e.start_column)
                and c.kind in ("name_ref", "field_access")
                and c.id != e.id
            ):
                if c.kind == "name_ref" and "assignment_target" not in c.tags:
                    continue
                return c
        return None

    def _container(self, e: Expression) -> FlowRef | None:
        for c in self.exprs:
            if (c.start_line, c.start_column) == (
                e.start_line,
                e.start_column,
            ) and c.kind == "name_ref":
                return self._source_ref(c)
        return None

    def _read(self, e: Expression) -> None:
        ref = self._source_ref(e)
        if ref is None or "assignment_target" in e.tags:
            return
        if e.kind == "name_ref" and e.id in self._field_receivers:
            return  # `owner.name`: the field access is the read, not `owner` itself
        self._event(ref, "read", e)

    # ---- calls -------------------------------------------------------------------------------

    def _callee_id(self, e: Expression) -> str | None:
        ref = self.refs.get(e.id)
        if ref and ref.status is ResolutionStatus.RESOLVED and ref.origin is Origin.PROJECT:
            return ref.symbol_id
        return None

    def _call(self, e: Expression) -> None:
        ref = self.refs.get(e.id)
        callee = self._callee_id(e)
        owner = ref.owner_type if ref else None
        self._arguments(e, callee, owner)
        if e.id in self.chain_calls or e.receiver_expr_id is None:
            return
        receiver = self.by_id.get(e.receiver_expr_id)
        recv_ref = self._source_ref(receiver) if receiver else None
        if recv_ref is None or receiver is None:
            return
        if is_mutator(e.name) and recv_ref.kind in ("parameter", "local", "field"):
            self._event(recv_ref, "modified", e, f"{e.name}({', '.join(e.arg_texts or [])})")
            for _arg_expr, sref in self._arg_sources(e):
                self._edge(sref, recv_ref, "mutation", e, detail=e.name)
            return
        result = self._source_ref(e)
        if result is not None:
            self._edge(recv_ref, result, "receiver", e, detail=e.name)

    def _arg_sources(self, e: Expression) -> list[tuple[Expression | None, FlowRef]]:
        out: list[tuple[Expression | None, FlowRef]] = []
        for hint in e.args or []:
            out.extend(self._value_sources(hint, hint.range))
        return out

    def _arguments(self, e: Expression, callee_id: str | None, owner: str | None) -> None:
        args = e.args or []
        if not args or e.id in self.chain_calls or "collector" in e.tags:
            return  # stream operations and Collectors.* are modeled by the pipeline's own edge
        names = self.params_of(callee_id) if callee_id else None
        mutating = e.kind == "method_call" and is_mutator(e.name)
        for i, hint in enumerate(args):
            if mutating and callee_id is None:
                continue  # modeled as flowing into the receiver
            if names:
                pname = names[i] if i < len(names) else names[-1]
                target = FlowRef(
                    kind="callee_parameter",
                    name=pname,
                    symbol_id=f"{callee_id}${pname}",
                    text=f"{callee_id}({pname})",
                    line=e.start_line,
                )
            else:
                label = (
                    f"{owner or '?'}.{e.name}#{i}"
                    if e.kind == "method_call"
                    else f"new {e.name}#{i}"
                )
                target = FlowRef(
                    kind="external_argument", name=label, text=label, line=e.start_line
                )
                pname = None
            for se, sref in self._value_sources(hint, hint.range):
                self._edge(
                    sref, target, "argument", e, callee_id=callee_id, param=pname, detail=e.name
                )
                if se is not None:
                    detail = f"to {e.name}({pname})" if pname else f"to {owner or '?'}.{e.name}"
                    self._event(sref, "passed", se, detail, callee_id=callee_id, param=pname)

    # ---- returns and pipelines ---------------------------------------------------------------

    def _returns(self) -> None:
        for s in self.m.statements:
            if s.kind != "return" or s.header_range is None:
                continue
            target = FlowRef(
                kind="return",
                name="return",
                symbol_id=f"{self.method_id}$return",
                line=s.start_line,
            )
            literal = literal_type(s.text)
            if literal is not None:  # `return "INVALID";` has no expression node, only the text
                lit = FlowRef(kind="literal", name=literal, text=s.text or "", line=s.start_line)
                self.edges.append(
                    DataFlowEdge(
                        source=lit, target=target, via="return", line=s.start_line, expression_id=0
                    )
                )
            for se, sref in self.sources(s.header_range):
                self.edges.append(
                    DataFlowEdge(
                        source=sref,
                        target=target,
                        via="return",
                        line=se.start_line,
                        expression_id=se.id,
                    )
                )
                self._event(sref, "returned", se, "returned")

    def _chain_call_ids(self) -> set[int]:
        ids: set[int] = set()
        for terminal, _ in _find_pipelines(self.exprs, self.refs):
            cur: Expression | None = terminal
            while cur is not None and cur.kind == "method_call":
                ids.add(cur.id)
                cur = self.by_id.get(cur.receiver_expr_id or -1)
                if cur is not None and not {"stream_source", "stream_op"} & set(cur.tags):
                    break
        return ids

    def _pipelines(self) -> None:
        for terminal, pipe in self.pipelines:
            chain: list[Expression] = []
            cur: Expression | None = terminal
            while (
                cur is not None
                and cur.kind == "method_call"
                and {"stream_source", "stream_op"} & set(cur.tags)
            ):
                chain.append(cur)
                cur = self.by_id.get(cur.receiver_expr_id or -1)
            chain.reverse()
            source_expr = self.by_id.get(chain[0].receiver_expr_id or -1) if chain else None
            src = self._source_ref(source_expr) if source_expr else None
            result = self._source_ref(terminal)
            ops = [o.name for o in pipe.ops]
            for o in pipe.ops:
                for c in o.collectors:
                    ops.append(c)
            if src is not None and result is not None and source_expr is not None:
                self._edge(src, result, "stream", terminal, ops=ops, detail=" > ".join(ops))
            for op in pipe.ops:
                kind = _OP_EVENT.get(op.category)
                if kind is None or src is None or source_expr is None:
                    continue
                detail = op.name + (f" ({', '.join(op.collectors)})" if op.collectors else "")
                if op.args:
                    detail += f": {', '.join(op.args)}"
                self._event(src, kind, source_expr, detail, line=op.line)  # type: ignore[arg-type]


_EVENT_ORDER = {"created": 0, "read": 1, "passed": 2, "derived": 3, "iterated": 3}


def _type_text(ref: Resolution) -> str | None:
    if ref.value_type is None:
        return None
    return ref.value_type.text


def _created_text(srcs: Sequence[tuple[Expression | None, FlowRef]], right: str | None) -> str:
    if not srcs:
        return f"initialized with {right}" if right else "declared"
    parts = []
    for _, s in srcs[:3]:
        if s.kind == "call_result":
            parts.append(f"call {s.name}()")
        elif s.kind == "new":
            parts.append(f"new {s.name}")
        elif s.kind == "literal":
            parts.append(s.text)
        else:
            parts.append(f"{s.kind} {s.name}")
    return "initialized from " + ", ".join(parts)


__all__ = [
    "DataFlowEdge",
    "FlowEvent",
    "FlowRef",
    "MethodDataFlow",
    "VariableFlow",
    "build_data_flow",
]
