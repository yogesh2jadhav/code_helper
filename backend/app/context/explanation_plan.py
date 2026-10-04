"""The explanation plan: a structured, mostly deterministic account of a method.

The LLM receives this plan (plus selected source) and turns it into prose; it does not have to
discover structure itself. Every claim is classified:

* fact: directly established by the code or another authoritative artifact;
* inference: a reasonable interpretation (e.g. from names or patterns);
* unknown: the repository does not establish it.
"""

from __future__ import annotations

import re
from collections import deque
from typing import Literal

from pydantic import BaseModel, Field

from app.analyzer.control_flow import FlowNode, StreamPipeline
from app.analyzer.data_flow import DataFlowEdge, FlowRef
from app.analyzer.resolution_models import ResolutionStatus
from app.analyzer.rule_extractor import describe_actions, describe_op
from app.context.citations import Citation, CitationBook
from app.knowledge.models import MethodKnowledge

Level = Literal["fact", "inference", "unknown"]
MAX_STAGES = 12
MAX_CLAIMS = 30


class Claim(BaseModel):
    text: str
    level: Level
    refs: list[str] = Field(default_factory=list)  # citation labels


StageKind = Literal["setup", "loop", "decision", "switch", "guarded", "pipeline", "return", "other"]


class Stage(BaseModel):
    kind: StageKind
    title: str
    description: str
    start_line: int
    end_line: int
    refs: list[str] = Field(default_factory=list)


class RuleNote(BaseModel):
    kind: str
    text: str
    lines: str
    literals: list[str] = Field(default_factory=list)
    refs: list[str] = Field(default_factory=list)


class Dependency(BaseModel):
    name: str
    method_id: str | None = None
    purpose: str | None = None
    lines: list[int] = Field(default_factory=list)
    kind: Literal["project", "ambiguous", "external"] = "project"
    note: str | None = None
    refs: list[str] = Field(default_factory=list)


class PlanPurpose(BaseModel):
    text: str
    basis: str
    level: Level
    refs: list[str] = Field(default_factory=list)


class ExplanationPlan(BaseModel):
    method_id: str
    method_identity: str
    purpose: PlanPurpose
    input_summary: list[str]
    output_summary: list[str]
    output_refs: list[str] = Field(default_factory=list)
    major_stages: list[Stage]
    data_transformations: list[str]
    business_rule_candidates: list[RuleNote]
    dependencies: list[Dependency]
    historical_context: list[str]
    risks: list[str]
    unknowns: list[str]
    claims: list[Claim]
    citations: list[Citation]


def build_plan(method: MethodKnowledge, citations: list[Citation] | None = None) -> ExplanationPlan:
    book = CitationBook.from_citations(citations or [])
    cite = _Cite(book, method)
    stages = _stages(method, cite)
    plan = ExplanationPlan(
        method_id=method.method_id,
        method_identity=(
            f"{method.class_fqn}.{method.signature} [{method.visibility} {method.kind}] "
            f"in {method.file}:{method.start_line}-{method.end_line}"
        ),
        purpose=_purpose(method, cite),
        input_summary=_inputs(method),
        output_summary=_outputs(method),
        output_refs=_output_refs(method, cite),
        major_stages=stages,
        data_transformations=_chains(method),
        business_rule_candidates=_rules(method, cite),
        dependencies=_dependencies(method, cite),
        historical_context=_history(method, cite),
        risks=[
            f"[{r.level}] {r.message}" + (f" (L{r.line})" if r.line else "") for r in method.risks
        ],
        unknowns=[u.message + (f" (L{u.line})" if u.line else "") for u in method.unknowns],
        claims=[],
        citations=[],
    )
    plan.claims = _claims(method, plan, cite)
    plan.citations = book.citations
    return plan


class _Cite:
    def __init__(self, book: CitationBook, method: MethodKnowledge) -> None:
        self.book, self.method = book, method

    def lines(self, start: int, end: int | None = None) -> str:
        m = self.method
        return self.book.cite(m.file, start, end or start, "source_code", m.name).label

    def evidence(self, source_type: str, file: str, start: int, end: int) -> str:
        return self.book.cite(file, start, end, source_type).label


# ==== purpose, inputs, outputs ================================================


def _purpose(m: MethodKnowledge, cite: _Cite) -> PlanPurpose:
    p = m.purpose
    refs: list[str] = []
    for e in m.evidence:
        if e.id in p.evidence_ids:
            refs.append(cite.evidence(e.source_type, e.file, e.start_line, e.end_line))
    refs = refs or [cite.lines(m.start_line, m.end_line)]
    return PlanPurpose(
        text=p.text, basis=p.basis, level="fact" if p.level == "fact" else "inference", refs=refs
    )


def _inputs(m: MethodKnowledge) -> list[str]:
    if not m.parameters:
        return ["takes no parameters"]
    return [f"{p.type} {p.name}: " + "; ".join(p.comes_from[:3]) for p in m.parameters]


def _outputs(m: MethodKnowledge) -> list[str]:
    out = [
        f"returns {m.output.type or 'void'}"
        + (": " + "; ".join(m.output.returns[:5]) if m.output.returns else "")
    ]
    out += m.output.side_effects[:6]
    return out


# ==== stages ==================================================================


def _stages(m: MethodKnowledge, cite: _Cite) -> list[Stage]:
    if not m.control_flow.nodes:
        what = (
            "has no body (abstract or interface method)"
            if not _has_body(m)
            else "has an empty body"
        )
        return [
            Stage(
                kind="other",
                title="No steps",
                description=what,
                start_line=m.start_line,
                end_line=m.end_line,
                refs=[cite.lines(m.start_line, m.end_line)],
            )
        ]
    stages = [_stage(n, m, cite) for n in m.control_flow.nodes]
    return _merge(stages)


def _has_body(m: MethodKnowledge) -> bool:
    return "abstract" not in m.modifiers and bool(m.complexity.lines > 1)


def _stage(n: FlowNode, m: MethodKnowledge, cite: _Cite) -> Stage:
    ref = cite.lines(n.start_line, n.end_line)

    def make(kind: StageKind, title: str, description: str) -> Stage:
        return Stage(
            kind=kind,
            title=title,
            description=description,
            start_line=n.start_line,
            end_line=n.end_line,
            refs=[ref],
        )

    if n.kind == "loop":
        inner = describe_actions(n.children, limit=4) or "nothing notable"
        return make("loop", f"Loop ({n.loop_kind}) {n.text}", f"on each pass: {inner}")
    if n.kind in ("if", "else_if"):
        return make("decision", f"Decide on {n.text}", _chain(n))
    if n.kind == "switch" or any(c.kind in ("case", "default") for c in n.children):
        cases = n.branches or [c for c in n.children if c.kind in ("case", "default")]
        labels = ", ".join(c.text or "default" for c in cases)
        return make("switch", f"Branch on {n.text or 'the selector'}", f"cases: {labels}")
    if n.kind == "try":
        handlers = "; ".join(
            f"{b.kind} {b.text or ''}".strip() + f": {describe_actions(b.children) or 'nothing'}"
            for b in n.branches
        )
        body = describe_actions(n.children) or "nothing"
        return make("guarded", "Guarded section", f"does: {body}; {handlers}")
    if n.kind == "stream" and n.stream:
        return make("pipeline", f"Pipeline over {n.stream.source}", _pipeline_text(n.stream))
    if n.kind == "return":
        pipe = n.header_streams[0] if n.header_streams else None
        text = f"returns {n.text}"
        if pipe is not None:
            text += f" (computed by a pipeline over {pipe.source}: {_pipeline_text(pipe)})"
        return make("return", "Produce the result", text)
    if n.kind == "throw":
        return make("return", "Fail", f"throws {n.text}")
    if n.kind == "call" and n.callee:
        purpose = _callee_purpose(m, n.callee.name)
        return make("setup", f"Call {n.callee.name}()", purpose or n.text or "")
    if n.kind == "mutation":
        is_call = bool(n.operator) and (n.operator or "x")[0].isalpha()
        verb = f"update {n.target} via {n.operator}()" if is_call else f"set {n.text}"
        return make("setup", verb.capitalize(), n.text or "")
    return make("other", n.kind, n.text or "")


def _chain(n: FlowNode) -> str:
    parts = [f"if {n.text}: {describe_actions(n.children) or 'nothing'}"]
    cur = n
    while cur.branches:
        nxt = cur.branches[0]
        if nxt.kind == "else_if":
            parts.append(f"else if {nxt.text}: {describe_actions(nxt.children) or 'nothing'}")
        else:
            parts.append(f"else: {describe_actions(nxt.children) or 'nothing'}")
        cur = nxt
    return "; ".join(parts)


def _pipeline_text(p: StreamPipeline) -> str:
    steps = [describe_op(o.name, o.category, o.args, o.collectors) for o in p.ops]
    return " -> ".join(s for s in steps if s) or "streams the elements"


def _callee_purpose(m: MethodKnowledge, name: str) -> str | None:
    return next((c.purpose for c in m.callees if c.name == name and c.purpose), None)


def _merge(stages: list[Stage]) -> list[Stage]:
    """Fold consecutive small setup steps together, then cap the total number of stages."""
    merged: list[Stage] = []
    for s in stages:
        if merged and s.kind == "setup" and merged[-1].kind == "setup":
            last = merged[-1]
            merged[-1] = Stage(
                kind="setup",
                title=f"{last.title}; {s.title}"[:120],
                description="; ".join(x for x in (last.description, s.description) if x)[:300],
                start_line=last.start_line,
                end_line=s.end_line,
                refs=[*last.refs, *s.refs],
            )
        else:
            merged.append(s)
    while len(merged) > MAX_STAGES:
        tail = merged[MAX_STAGES - 1 :]
        merged = [
            *merged[: MAX_STAGES - 1],
            Stage(
                kind="other",
                title=f"{len(tail)} further steps",
                description="; ".join(t.title for t in tail)[:300],
                start_line=tail[0].start_line,
                end_line=tail[-1].end_line,
                refs=[r for t in tail for r in t.refs][:6],
            ),
        ]
    return merged


# ==== data transformations ====================================================


def _label(ref: FlowRef) -> str:
    if ref.kind == "call_result":
        return f"{ref.name}()"
    if ref.kind == "literal":
        return f"{ref.name} literal"
    if ref.kind == "new":
        return f"new {ref.name}"
    if ref.kind == "return":
        return "return value"
    return ref.name


def _node_key(ref: FlowRef, edge_line: int) -> str:
    # call results are distinguished by where they happen; variables by their identity
    return ref.symbol_id or (
        f"{ref.kind}:{ref.name}@{edge_line}"
        if ref.kind in ("call_result", "new")
        else f"{ref.kind}:{ref.name}"
    )


def _chains(m: MethodKnowledge) -> list[str]:
    edges = m.data_flow.edges
    graph: dict[str, list[tuple[str, DataFlowEdge]]] = {}
    labels: dict[str, str] = {}
    for e in edges:
        src, dst = _node_key(e.source, e.line), _node_key(e.target, e.line)
        if src == dst:
            continue
        labels[src], labels[dst] = _label(e.source), _label(e.target)
        graph.setdefault(src, []).append((dst, e))
    chains: list[str] = []
    starts = [(v.symbol_id, v.name) for v in m.data_flow.variables if v.kind == "parameter"]
    goal = f"{m.method_id}$return"
    for start, name in starts:
        path = _shortest(graph, start, goal)
        if path is None:
            continue
        text = name
        for node, edge in path:
            via = edge.via + (f": {', '.join(edge.ops[:4])}" if edge.ops else "")
            text += f" -[{via}]-> {labels.get(node, node)}"
        chains.append(text)
    if not chains:  # no parameter reaches the result: summarise the interesting variables instead
        for v in m.data_flow.variables:
            life = v.lifecycle()
            kinds = [
                k
                for k in (
                    "created",
                    "iterated",
                    "filtered",
                    "mapped",
                    "grouped",
                    "collected",
                    "modified",
                    "passed",
                    "returned",
                )
                if k in life
            ]
            if len(kinds) >= 2:
                chains.append(f"{v.name}: " + " -> ".join(kinds))
    return list(dict.fromkeys(chains))[:6]


def _shortest(
    graph: dict[str, list[tuple[str, DataFlowEdge]]], start: str, goal: str
) -> list[tuple[str, DataFlowEdge]] | None:
    queue: deque[tuple[str, list[tuple[str, DataFlowEdge]]]] = deque([(start, [])])
    seen = {start}
    while queue:
        node, path = queue.popleft()
        if node == goal:
            return path
        for nxt, edge in graph.get(node, []):
            if nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, [*path, (nxt, edge)]))
    return None


# ==== rules, dependencies, history ============================================


def _rules(m: MethodKnowledge, cite: _Cite) -> list[RuleNote]:
    return [
        RuleNote(
            kind=r.kind,
            text=r.summary,
            literals=r.literals,
            refs=[cite.lines(r.start_line, r.end_line)],
            lines=f"{r.start_line}-{r.end_line}"
            if r.end_line != r.start_line
            else str(r.start_line),
        )
        for r in m.rule_candidates
    ]


def _output_refs(m: MethodKnowledge, cite: _Cite) -> list[str]:
    lines = sorted({n.start_line for n in m.control_flow.walk() if n.kind == "return"})
    return [cite.lines(n) for n in lines[:4]] or [cite.lines(m.start_line)]


def _dependencies(m: MethodKnowledge, cite: _Cite) -> list[Dependency]:
    out: list[Dependency] = []
    externals: dict[str, list[int]] = {}
    for c in m.callees:
        if c.status is ResolutionStatus.RESOLVED and c.method_id:
            note = (
                f"may dispatch to {len(c.may_dispatch_to)} override(s)"
                if c.may_dispatch_to
                else None
            )
            out.append(
                Dependency(
                    name=_dependency_name(c.method_id, c.name),
                    method_id=c.method_id,
                    purpose=c.purpose,
                    lines=c.site_lines,
                    note=note,
                )
            )
        elif c.status is ResolutionStatus.AMBIGUOUS:
            out.append(
                Dependency(
                    name=c.name,
                    kind="ambiguous",
                    lines=c.site_lines,
                    note=f"one of {len(c.candidates)} candidates ({c.reason})",
                )
            )
        elif c.status is ResolutionStatus.RESOLVED and c.owner_type:
            externals.setdefault(c.owner_type, []).extend(c.site_lines)
    for owner, lines in sorted(externals.items()):
        out.append(
            Dependency(
                name=owner,
                kind="external",
                lines=sorted(set(lines)),
                note="library/JDK type; members not verified",
            )
        )
    for dep in out:
        dep.refs = [cite.lines(n) for n in dep.lines[:3]]
    return out


def _dependency_name(method_id: str, name: str) -> str:
    owner = method_id.split("#")[0].rsplit(".", 1)[-1]
    return (
        f"new {owner}" if owner == name else f"{owner}.{name}"
    )  # a constructor shares its class name


def _history(m: MethodKnowledge, cite: _Cite) -> list[str]:
    out: list[str] = []
    for e in m.evidence:
        ref = cite.evidence(e.source_type, e.file, e.start_line, e.end_line)
        if e.source_type == "comment" and e.relation != "commented_code":
            out.append(f'comment ({e.relation}) at L{e.start_line}: "{_clip(e.snippet)}" [{ref}]')
        elif e.source_type == "test":
            words = " ".join(re.findall(r"[A-Z]?[a-z]+|\d+", e.method or "")).lower()
            owner = (e.class_name or "").rsplit(".", 1)[-1]
            out.append(f'tested by {owner}.{e.method} ("{words}") [{ref}]')
        elif e.source_type in ("readme", "documentation"):
            first = e.snippet.split("\n")[0]
            out.append(f'{e.file} section "{first}" mentions it [{ref}]')
    return out or [
        "no comment, test or documentation mentions this method (git history is not analyzed)"
    ]


def _clip(text: str, n: int = 160) -> str:
    one = " ".join(text.split())
    return one if len(one) <= n else one[: n - 1] + "…"


# ==== claims ==================================================================


def _claims(m: MethodKnowledge, plan: ExplanationPlan, cite: _Cite) -> list[Claim]:
    claims: list[Claim] = []
    sig_ref = [cite.lines(m.start_line, m.end_line)]
    params = ", ".join(f"{p.type} {p.name}" for p in m.parameters) or "no parameters"
    claims.append(
        Claim(
            text=f"{m.name} takes {params} and returns {m.output.type or 'void'}.",
            level="fact",
            refs=sig_ref,
        )
    )
    if m.purpose.basis == "comment":
        claims.append(
            Claim(
                text=f"A comment states its purpose: {m.purpose.text}",
                level="fact",
                refs=plan.purpose.refs,
            )
        )
    else:
        claims.append(
            Claim(
                text=f"Judging by its name and structure, it {m.purpose.text}.",
                level="inference",
                refs=sig_ref,
            )
        )
    for note, r in zip(plan.business_rule_candidates, m.rule_candidates, strict=True):
        what = r.meaning or r.calculation or r.summary
        claims.append(
            Claim(
                text=f"At L{r.start_line} the code applies this check or calculation: {what}.",
                level="fact",
                refs=note.refs,
            )
        )
        if r.kind in ("threshold", "literal_match", "validation", "default_value", "null_handling"):
            claims.append(
                Claim(
                    text=f"L{r.start_line} looks like a {r.kind.replace('_', ' ')} business rule.",
                    level="inference",
                    refs=note.refs,
                )
            )
    for d in plan.dependencies:
        if d.kind == "project":
            claims.append(
                Claim(
                    text=f"It calls {d.name} (L{', L'.join(map(str, d.lines))}).",
                    level="fact",
                    refs=sig_ref,
                )
            )
    for effect in m.output.side_effects[:4]:
        claims.append(Claim(text=f"It {effect}.", level="fact", refs=sig_ref))
    for u in m.unknowns:
        claims.append(
            Claim(text=u.message[0].upper() + u.message[1:] + ".", level="unknown", refs=sig_ref)
        )
    return claims[:MAX_CLAIMS]
