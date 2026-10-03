"""Control-flow model of a method: structure only, no prose.

The tree mirrors how the method executes: decisions (with their else-if/else chain), loops,
switches, try/catch/finally, jumps (return/throw/break/continue), plus leaf steps for the things
that happen between them: mutations, calls into project code and stream pipelines.

Everything is derived from AST positions and symbol resolution; nothing is inferred by a model.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Literal

from pydantic import BaseModel, Field

from app.analyzer.ast_models import Expression, Method, Range, Statement
from app.analyzer.mutators import is_mutator
from app.analyzer.resolution_models import Origin, Resolution, ResolutionStatus

FlowKind = Literal[
    "if", "else_if", "else", "loop", "switch", "case", "default", "try", "catch", "finally",
    "return", "throw", "break", "continue", "stream", "mutation", "call",
]  # fmt: skip

_STATEMENT_KIND: dict[str, str] = {
    "for": "loop", "foreach": "loop", "while": "loop", "do": "loop",
}  # fmt: skip

STREAM_CATEGORY: dict[str, str] = {
    "stream": "source", "parallelStream": "source", "of": "source", "range": "source",
    "rangeClosed": "source", "iterate": "source", "generate": "source",
    "filter": "filter", "takeWhile": "filter", "dropWhile": "filter", "distinct": "filter",
    "limit": "filter", "skip": "filter",
    "map": "map", "mapToInt": "map", "mapToLong": "map", "mapToDouble": "map", "mapToObj": "map",
    "flatMap": "map", "boxed": "map", "peek": "map",
    "sorted": "sort",
    "collect": "collect", "toList": "collect", "toArray": "collect",
    "count": "aggregate", "sum": "aggregate", "min": "aggregate", "max": "aggregate",
    "average": "aggregate", "reduce": "aggregate", "summaryStatistics": "aggregate",
    "anyMatch": "match", "allMatch": "match", "noneMatch": "match",
    "findFirst": "find", "findAny": "find",
    "forEach": "consume", "forEachOrdered": "consume",
}  # fmt: skip
_GROUPING = {"groupingBy", "groupingByConcurrent", "partitioningBy"}
_AGGREGATING = {
    "counting", "summingInt", "summingLong", "summingDouble", "averagingInt", "averagingLong",
    "averagingDouble", "minBy", "maxBy", "reducing", "summarizingInt", "summarizingLong",
    "summarizingDouble",
}  # fmt: skip


class CallSummary(BaseModel):
    name: str
    status: ResolutionStatus
    callee_id: str | None = None  # the single resolved project target
    candidates: list[str] = Field(default_factory=list)
    owner_type: str | None = None  # external owner type
    line: int
    expression_id: int
    constructor: bool = False


class StreamOp(BaseModel):
    name: str
    category: (
        str  # source | filter | map | sort | group | aggregate | collect | match | find | consume
    )
    args: list[str] = Field(default_factory=list)
    collectors: list[str] = Field(default_factory=list)  # collect(...) only: Collectors.* used
    line: int


class StreamPipeline(BaseModel):
    source: str  # what is streamed, e.g. `visits` or `Stream.of(a, b)`
    ops: list[StreamOp]
    text: str
    calls: list[CallSummary] = Field(default_factory=list)  # project calls made inside the pipeline


class FlowNode(BaseModel):
    kind: FlowKind
    start_line: int
    end_line: int
    start_column: int = 0
    text: str | None = None  # condition / loop header / selector / value
    loop_kind: str | None = None  # for | foreach | while | do
    target: str | None = None  # mutation: what is assigned
    target_kind: str | None = None  # mutation: field | parameter | local_variable | ...
    operator: str | None = None  # mutation: = += ++ ...
    callee: CallSummary | None = None  # call leaf
    header_calls: list[CallSummary] = Field(default_factory=list)  # calls in the condition/header
    header_streams: list[StreamPipeline] = Field(default_factory=list)  # pipelines in the header
    stream: StreamPipeline | None = None
    in_lambda: bool = False
    children: list[FlowNode] = Field(default_factory=list)  # the sequential body
    branches: list[FlowNode] = Field(default_factory=list)  # else-if/else, catch/finally, cases

    def walk(self) -> Iterator[FlowNode]:
        yield self
        for child in [*self.children, *self.branches]:
            yield from child.walk()


class ControlFlowSummary(BaseModel):
    decisions: int = 0
    loops: int = 0
    switches: int = 0
    try_blocks: int = 0
    returns: int = 0
    throws: int = 0
    streams: int = 0
    mutations: int = 0
    calls: int = 0
    max_depth: int = 0


class ControlFlow(BaseModel):
    method_id: str
    nodes: list[FlowNode]
    summary: ControlFlowSummary

    def walk(self) -> Iterator[FlowNode]:
        for node in self.nodes:
            yield from node.walk()


# ==== construction ===========================================================================


def build_control_flow(
    method: Method, method_id: str, refs: dict[int, Resolution] | None = None
) -> ControlFlow:
    refs = refs or {}
    nodes: dict[int, FlowNode] = {s.id: _statement_node(s) for s in method.statements}
    roots: list[FlowNode] = []
    for s in method.statements:
        node = nodes[s.id]
        if s.parent_id is None:
            roots.append(node)
        else:
            _place(nodes[s.parent_id], node)

    exprs = method.expressions
    by_id = {e.id: e for e in exprs}
    pipelines = _find_pipelines(exprs, refs)
    pipeline_by_terminal = {p[0].id: p for p in pipelines}
    inside_pipeline = _covered(exprs, [p[0].range for p in pipelines])
    lambdas = [e.range for e in exprs if e.kind == "lambda"]
    stmts = {s.id: s for s in method.statements}

    for e in exprs:
        if e.kind not in ("assignment", "method_call", "object_creation"):
            continue
        owner_stmt = stmts.get(e.statement_id) if e.statement_id is not None else None
        owner = nodes[owner_stmt.id] if owner_stmt else None
        is_header = (
            owner_stmt is not None
            and owner_stmt.header_range is not None
            and owner_stmt.header_range.contains(e.range)
        )
        if e.id in inside_pipeline:
            continue  # part of a pipeline (or its lambdas) that is modeled by its own node
        terminal = pipeline_by_terminal.get(e.id)
        if terminal is not None:
            if is_header and owner is not None:  # `return xs.stream()...`, `if (xs.stream()...)`
                owner.header_streams.append(terminal[1])
                continue
            leaf = FlowNode(
                kind="stream", start_line=e.start_line, end_line=e.end_line,
                start_column=e.start_column, text=e.text, stream=terminal[1],
                in_lambda=_in_ranges(e, lambdas),
            )  # fmt: skip
        elif is_header:
            if e.kind != "assignment" and owner is not None:
                summary = _call_summary(e, refs)
                if summary is not None:
                    owner.header_calls.append(summary)
            continue
        elif e.kind == "assignment":
            leaf = _mutation_leaf(e, exprs, refs, _in_ranges(e, lambdas))
        else:
            summary = _call_summary(e, refs)
            mutated = _mutated_receiver(e, by_id, refs)
            if mutated is not None:  # `list.add(x)`, `dto.setName(y)`: the receiver changes
                receiver, receiver_ref = mutated
                leaf = FlowNode(
                    kind="mutation", start_line=e.start_line, end_line=e.end_line,
                    start_column=e.start_column, text=e.text, target=receiver.text,
                    target_kind=receiver_ref.kind if receiver_ref else None, operator=e.name,
                    callee=summary, in_lambda=_in_ranges(e, lambdas),
                )  # fmt: skip
            elif summary is None or not (
                summary.callee_id or summary.candidates
            ):  # keep project and ambiguous calls; external/unresolved live in the call graph
                continue
            else:
                leaf = FlowNode(
                    kind="call", start_line=e.start_line, end_line=e.end_line,
                    start_column=e.start_column, text=e.text, callee=summary,
                    in_lambda=_in_ranges(e, lambdas),
                )  # fmt: skip
        if owner is None:
            roots.append(leaf)
        else:
            owner.children.append(leaf)

    for node in [*roots, *nodes.values()]:
        _sort(node)
    roots.sort(key=_position)
    return ControlFlow(
        method_id=method_id, nodes=roots, summary=_summarize(roots, method.max_nesting_depth)
    )


def _statement_node(s: Statement) -> FlowNode:
    kind = _STATEMENT_KIND.get(s.kind, s.kind)
    return FlowNode(
        kind=kind,  # type: ignore[arg-type]
        start_line=s.start_line, end_line=s.end_line, start_column=s.start_column, text=s.text,
        loop_kind=s.kind if kind == "loop" else None,
    )  # fmt: skip


def _place(parent: FlowNode, node: FlowNode) -> None:
    alternative = (
        (node.kind in ("else_if", "else") and parent.kind in ("if", "else_if"))
        or (node.kind in ("catch", "finally") and parent.kind == "try")
        or (node.kind in ("case", "default") and parent.kind == "switch")
    )
    (parent.branches if alternative else parent.children).append(node)


def _position(n: FlowNode) -> tuple[int, int]:
    return (n.start_line, n.start_column)


def _sort(node: FlowNode) -> None:
    node.children.sort(key=_position)
    node.branches.sort(key=_position)


def _in_ranges(e: Expression, ranges: list[Range]) -> bool:
    return any(r.contains(e.range) and r != e.range for r in ranges)


def _covered(exprs: list[Expression], ranges: list[Range]) -> set[int]:
    return {e.id for e in exprs if _in_ranges(e, ranges)}


def _mutated_receiver(
    e: Expression, by_id: dict[int, Expression], refs: dict[int, Resolution]
) -> tuple[Expression, Resolution | None] | None:
    """The variable/field a mutating call changes, if the receiver is a plain variable or field."""
    if e.kind != "method_call" or not is_mutator(e.name) or e.receiver_expr_id is None:
        return None
    receiver = by_id.get(e.receiver_expr_id)
    if receiver is None or receiver.kind not in ("name_ref", "field_access"):
        return None
    return receiver, refs.get(receiver.id)


def _call_summary(e: Expression, refs: dict[int, Resolution]) -> CallSummary | None:
    ref = refs.get(e.id)
    if ref is None:
        return None
    resolved_project = ref.status is ResolutionStatus.RESOLVED and ref.origin is Origin.PROJECT
    return CallSummary(
        name=ref.name,
        status=ref.status,
        callee_id=ref.symbol_id if resolved_project else None,
        candidates=ref.candidates if ref.status is ResolutionStatus.AMBIGUOUS else [],
        owner_type=ref.owner_type if ref.origin is Origin.EXTERNAL else None,
        line=e.start_line,
        expression_id=e.id,
        constructor=e.kind == "object_creation",
    )


def _mutation_leaf(
    e: Expression, exprs: list[Expression], refs: dict[int, Resolution], in_lambda: bool
) -> FlowNode:
    target_kind: str | None = None
    for candidate in exprs:
        starts_here = (candidate.start_line, candidate.start_column) == (
            e.start_line,
            e.start_column,
        )
        if starts_here and candidate.kind in ("name_ref", "field_access"):
            ref = refs.get(candidate.id)
            target_kind = ref.kind if ref else None
            break
    return FlowNode(
        kind="mutation",
        start_line=e.start_line,
        end_line=e.end_line,
        start_column=e.start_column,
        text=e.text,
        target=e.name,
        target_kind=target_kind,
        operator=e.operator,
        in_lambda=in_lambda,
    )


# ==== stream pipelines =======================================================================


def _find_pipelines(
    exprs: list[Expression], refs: dict[int, Resolution]
) -> list[tuple[Expression, StreamPipeline]]:
    calls = {e.id: e for e in exprs if e.kind == "method_call"}
    stream_calls = {i: e for i, e in calls.items() if {"stream_source", "stream_op"} & set(e.tags)}
    inner = {
        e.receiver_expr_id for e in stream_calls.values() if e.receiver_expr_id in stream_calls
    }
    pipelines: list[tuple[Expression, StreamPipeline]] = []
    for terminal in (e for i, e in stream_calls.items() if i not in inner):
        chain: list[Expression] = []
        current: Expression | None = terminal
        while current is not None:
            chain.append(current)
            current = stream_calls.get(current.receiver_expr_id or -1)
        chain.reverse()
        pipelines.append((terminal, _pipeline(terminal, chain, exprs, refs)))
    return pipelines


def _pipeline(
    terminal: Expression,
    chain: list[Expression],
    exprs: list[Expression],
    refs: dict[int, Resolution],
) -> StreamPipeline:
    first = chain[0]
    source = (
        first.scope if first.name in ("stream", "parallelStream") and first.scope else first.text
    )
    collectors = [
        e for e in exprs
        if e.kind == "method_call" and "collector" in e.tags and terminal.range.contains(e.range)
    ]  # fmt: skip
    chain_ids = {c.id for c in chain} | {c.id for c in collectors}
    ops = [_op(c, collectors) for c in chain]
    calls = [
        s for e in exprs
        if e.kind in ("method_call", "object_creation")
        and e.id not in chain_ids
        and terminal.range.contains(e.range)
        and (s := _call_summary(e, refs)) is not None
        and (s.callee_id or s.candidates)
    ]  # fmt: skip
    return StreamPipeline(source=source or "", ops=ops, text=terminal.text, calls=calls)


def _op(call: Expression, collectors: list[Expression]) -> StreamOp:
    name = call.name or ""
    category = STREAM_CATEGORY.get(name, "other")
    names: list[str] = []
    if name == "collect":
        names = [c.name or "" for c in collectors if call.range.contains(c.range)]
        if any(n in _GROUPING for n in names):
            category = "group"
        elif any(n in _AGGREGATING for n in names):
            category = "aggregate"
    return StreamOp(
        name=name,
        category=category,
        args=call.arg_texts or [],
        collectors=names,
        line=call.start_line,
    )


# ==== summary and outline ====================================================================


def _summarize(roots: list[FlowNode], max_depth: int) -> ControlFlowSummary:
    summary = ControlFlowSummary(max_depth=max_depth)
    for root in roots:
        for n in root.walk():
            if n.kind in ("if", "else_if"):
                summary.decisions += 1
            elif n.kind == "loop":
                summary.loops += 1
            elif n.kind == "switch":
                summary.switches += 1
            elif n.kind == "try":
                summary.try_blocks += 1
            elif n.kind == "return":
                summary.returns += 1
            elif n.kind == "throw":
                summary.throws += 1
            elif n.kind == "stream":
                summary.streams += 1
            elif n.kind == "mutation":
                summary.mutations += 1
            elif n.kind == "call":
                summary.calls += 1
    return summary


def render_outline(flow: ControlFlow, *, lines: bool = False) -> str:
    """A compact indented outline, e.g. for golden-file tests and prompt context."""
    out: list[str] = []
    for node in flow.nodes:
        _render(node, 0, out, lines)
    return "\n".join(out)


def _label(n: FlowNode) -> str:
    text = n.text or ""
    if n.kind == "loop":
        return f"{n.loop_kind} {text}"
    if n.kind == "else_if":
        return f"else if {text}"
    if n.kind in ("else", "finally", "try", "default"):
        return n.kind
    if n.kind == "mutation":
        kind = n.target_kind or "?"
        if n.operator and n.operator[0].isalpha():  # a mutating call such as list.add(x)
            return f"mutate {n.target} via {n.operator}() [{kind}]"
        return f"set {n.target} {n.operator} [{kind}]"
    if n.kind == "call":
        callee = n.callee
        target = (callee.callee_id or "|".join(callee.candidates)) if callee else ""
        return f"call {n.text} -> {target}"
    if n.kind == "stream" and n.stream:
        return f"stream {_pipeline_label(n.stream)}"
    if n.kind in ("break", "continue"):
        return n.kind
    return f"{n.kind} {text}".rstrip()


def _pipeline_label(p: StreamPipeline) -> str:
    ops = " > ".join(
        o.name + (f"[{'+'.join(o.collectors)}]" if o.collectors else "") for o in p.ops
    )
    return f"{p.source}: {ops}"


def _render(n: FlowNode, depth: int, out: list[str], lines: bool) -> None:
    pad = "  " * depth
    suffix = f"  [{n.start_line}-{n.end_line}]" if lines else ""
    calls = f"  (calls {', '.join(c.name for c in n.header_calls)})" if n.header_calls else ""
    out.append(f"{pad}{_label(n)}{calls}{suffix}")
    for pipeline in n.header_streams:
        out.append(f"{pad}  ~ stream {_pipeline_label(pipeline)}")
    for child in n.children:
        _render(child, depth + 1, out, lines)
    # alternatives of an if/try sit beside the node; cases sit inside their switch
    branch_depth = depth + 1 if n.kind == "switch" else depth
    for branch in n.branches:
        _render(branch, branch_depth, out, lines)
