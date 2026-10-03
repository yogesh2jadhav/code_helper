"""Call graph over resolved references, in both directions, plus override relationships.

Edges come straight from symbol resolution, so they inherit its honesty: a call that resolved to
one project method is an edge to it; an ambiguous call keeps all its candidates; an external call
records the owner type; an unresolved call keeps its reason. Nothing is dropped and nothing is
invented.

Dynamic dispatch is modeled separately from the edges: `overrides` maps a method to the methods
that override it in subtypes, and traversals can opt in to following them
(`include_overrides=True`), because a call resolved to an interface or superclass method may run
any of those at runtime.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, Field

from app.analyzer.resolution_models import FileResolution, Origin, ResolutionStatus
from app.analyzer.symbol_resolver import SymbolResolver, erasure
from app.analyzer.symbol_table import MethodSymbol, SymbolTable

CALL_KINDS = {"method": "call", "constructor": "constructor", "method_ref": "method_ref"}


class MethodNode(BaseModel):
    id: str
    class_fqn: str
    name: str
    signature: str
    kind: str  # method | constructor
    file: str
    file_id: str
    start_line: int
    end_line: int
    is_static: bool = False
    is_abstract: bool = False
    visibility: str = "package"
    implicit: bool = False  # compiler-provided (default constructor, record accessor)


class CallEdge(BaseModel):
    caller_id: str
    caller_file: str
    expression_id: int
    kind: Literal["call", "constructor", "method_ref"]
    name: str
    status: ResolutionStatus
    callee_id: str | None = None  # the single resolved project target
    candidates: list[str] = Field(default_factory=list)  # ambiguous project targets
    owner_type: str | None = None  # owning type for external targets
    origin: Origin | None = None
    site_line: int
    reason: str | None = None
    implicit: bool = False

    @property
    def targets(self) -> list[str]:
        """Every project method this edge may reach (one if resolved, several if ambiguous)."""
        if self.callee_id:
            return [self.callee_id]
        return list(self.candidates)


class Reach(BaseModel):
    """A method reached by a traversal, with how far away it is and the edge that led to it."""

    method_id: str
    depth: int
    edge: CallEdge
    via_override: bool = False  # reached by following an override rather than a direct call
    ambiguous: bool = False  # reached through an ambiguous edge, i.e. only one candidate of several


class CallGraph:
    def __init__(self) -> None:
        self.nodes: dict[str, MethodNode] = {}
        self.edges_from: dict[str, list[CallEdge]] = {}
        self.edges_to: dict[str, list[CallEdge]] = {}
        self.overrides: dict[str, list[str]] = {}  # method id -> ids of overriding methods
        self.overridden: dict[str, list[str]] = {}  # method id -> ids of methods it overrides

    # ---- construction ------------------------------------------------------------------------

    def add_node(self, node: MethodNode) -> None:
        self.nodes.setdefault(node.id, node)

    def add_edge(self, edge: CallEdge) -> None:
        self.edges_from.setdefault(edge.caller_id, []).append(edge)
        for target in edge.targets:
            self.edges_to.setdefault(target, []).append(edge)

    def add_override(self, base_id: str, override_id: str) -> None:
        self.overrides.setdefault(base_id, []).append(override_id)
        self.overridden.setdefault(override_id, []).append(base_id)

    # ---- queries -----------------------------------------------------------------------------

    def calls(self, method_id: str) -> list[CallEdge]:
        """Direct outgoing edges (every call site), by line, then outer call before inner."""
        return sorted(
            self.edges_from.get(method_id, []), key=lambda e: (e.site_line, e.expression_id)
        )

    def called_by(self, method_id: str) -> list[CallEdge]:
        """Direct incoming edges (every call site that resolves, or may resolve, to the method)."""
        return sorted(self.edges_to.get(method_id, []), key=lambda e: (e.caller_id, e.site_line))

    def callees(
        self,
        method_id: str,
        depth: int = 1,
        *,
        include_candidates: bool = True,
        include_overrides: bool = False,
    ) -> list[Reach]:
        """Methods reachable by following calls up to `depth` levels (breadth first)."""
        return self._walk(
            method_id,
            depth,
            forward=True,
            include_candidates=include_candidates,
            include_overrides=include_overrides,
        )

    def callers(
        self, method_id: str, depth: int = 1, *, include_candidates: bool = True
    ) -> list[Reach]:
        """Methods that can reach `method_id` within `depth` calls."""
        return self._walk(
            method_id,
            depth,
            forward=False,
            include_candidates=include_candidates,
            include_overrides=False,
        )

    def _walk(
        self,
        start: str,
        depth: int,
        *,
        forward: bool,
        include_candidates: bool,
        include_overrides: bool,
    ) -> list[Reach]:
        reached: dict[str, Reach] = {}
        queue: deque[tuple[str, int]] = deque([(start, 0)])
        visited = {start}  # cycle and recursion safe: each method is expanded once
        while queue:
            current, level = queue.popleft()
            if level >= depth:
                continue
            for edge, target, via_override in self._neighbours(
                current, forward, include_candidates, include_overrides
            ):
                if target == start or target in visited:
                    continue
                visited.add(target)
                reached[target] = Reach(
                    method_id=target,
                    depth=level + 1,
                    edge=edge,
                    via_override=via_override,
                    ambiguous=edge.status is ResolutionStatus.AMBIGUOUS,
                )
                queue.append((target, level + 1))
        return sorted(reached.values(), key=lambda r: (r.depth, r.method_id))

    def _neighbours(
        self, method_id: str, forward: bool, include_candidates: bool, include_overrides: bool
    ) -> Iterable[tuple[CallEdge, str, bool]]:
        if forward:
            for edge in self.calls(method_id):
                targets = (
                    edge.targets
                    if include_candidates
                    else ([edge.callee_id] if edge.callee_id else [])
                )
                for target in targets:
                    yield edge, target, False
                    if include_overrides:
                        for impl in self.overrides.get(target, []):
                            yield edge, impl, True
        else:
            for edge in self.called_by(method_id):
                if edge.status is ResolutionStatus.AMBIGUOUS and not include_candidates:
                    continue
                yield edge, edge.caller_id, False

    def unresolved_calls(self, method_id: str) -> list[CallEdge]:
        return [e for e in self.calls(method_id) if e.status is ResolutionStatus.UNRESOLVED]

    def external_calls(self, method_id: str) -> list[CallEdge]:
        return [e for e in self.calls(method_id) if e.origin is Origin.EXTERNAL]

    def is_recursive(self, method_id: str) -> bool:
        """True if the method can reach itself through calls."""
        seen: set[str] = set()
        stack = [t for e in self.calls(method_id) for t in e.targets]
        while stack:
            current = stack.pop()
            if current == method_id:
                return True
            if current in seen:
                continue
            seen.add(current)
            stack.extend(t for e in self.calls(current) for t in e.targets)
        return False


def build_call_graph(
    table: SymbolTable, resolver: SymbolResolver, resolutions: Iterable[FileResolution]
) -> CallGraph:
    graph = CallGraph()
    for classes in table.classes.values():
        for cls in classes:
            symbols = [m for ms in cls.methods.values() for m in ms] + list(cls.constructors)
            for m in symbols:
                graph.add_node(_node(m))

    for fr in resolutions:
        for cr in fr.classes:
            for mr in cr.methods:
                for ref in mr.refs:
                    kind = CALL_KINDS.get(ref.kind)
                    if kind is None:
                        continue
                    graph.add_edge(
                        CallEdge(
                            caller_id=mr.method_id,
                            caller_file=fr.path,
                            expression_id=ref.expression_id,
                            kind=kind,  # type: ignore[arg-type]
                            name=ref.name,
                            status=ref.status,
                            callee_id=ref.symbol_id
                            if ref.status is ResolutionStatus.RESOLVED
                            else None,
                            candidates=ref.candidates
                            if ref.status is ResolutionStatus.AMBIGUOUS
                            else [],
                            owner_type=ref.owner_type if ref.origin is Origin.EXTERNAL else None,
                            origin=ref.origin,
                            site_line=ref.site_line,
                            reason=ref.reason,
                            implicit=ref.implicit,
                        )
                    )

    _add_overrides(graph, table, resolver)
    return graph


def _node(m: MethodSymbol) -> MethodNode:
    return MethodNode(
        id=m.id,
        class_fqn=m.owner.fqn,
        name=m.name,
        signature=m.signature,
        kind=m.kind,
        file=m.owner.file.path,
        file_id=m.owner.file.file_id,
        start_line=m.start_line,
        end_line=m.end_line,
        is_static=m.is_static,
        is_abstract=m.is_abstract,
        visibility=m.visibility,
        implicit=m.implicit,
    )


def _add_overrides(graph: CallGraph, table: SymbolTable, resolver: SymbolResolver) -> None:
    """For each instance method, find the same-signature methods it overrides in supertypes."""
    for classes in table.classes.values():
        for cls in classes:
            supers = resolver.hierarchy(cls).classes[1:]
            if not supers:
                continue
            for name, methods in cls.methods.items():
                for m in methods:
                    if m.is_static or m.implicit:
                        continue
                    signature = tuple(erasure(t) for t in m.param_types)
                    for sup in supers:
                        for base in sup.methods.get(name, []):
                            if (
                                not base.is_static
                                and tuple(erasure(t) for t in base.param_types) == signature
                            ):
                                graph.add_override(base.id, m.id)
