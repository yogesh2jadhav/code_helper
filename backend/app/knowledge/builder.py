"""Builds the semantic code model from analysis results. Fully deterministic; no LLM involved."""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from app.analyzer.ast_models import Method, TypeDecl
from app.analyzer.call_graph import CallEdge, CallGraph, build_call_graph
from app.analyzer.control_flow import ControlFlow, build_control_flow
from app.analyzer.data_flow import FlowRef, MethodDataFlow, build_data_flow
from app.analyzer.resolution_models import (
    ClassResolution,
    FileResolution,
    MethodResolution,
    Origin,
    ResolutionStatus,
    TypeResolution,
)
from app.analyzer.rule_extractor import RuleCandidate, extract_rules
from app.analyzer.symbol_resolver import SymbolResolver
from app.analyzer.symbol_table import AnalyzedFile, ClassSymbol, SymbolTable
from app.knowledge.evidence import (
    DocIndex,
    Evidence,
    comment_evidence,
    doc_evidence,
    first_sentence,
    is_test_location,
    load_docs,
    make_evidence,
)
from app.knowledge.models import (
    CalleeInfo,
    CallerInfo,
    ClassKnowledge,
    Complexity,
    FieldInfo,
    KnowledgeStats,
    Level,
    MethodKnowledge,
    OutputInfo,
    ParameterInfo,
    Purpose,
    RepositoryKnowledge,
    Risk,
    Unknown,
    short_id,
)
from app.logging_setup import log_event

logger = logging.getLogger(__name__)

_VERBS = {
    "get": "returns",
    "is": "checks whether",
    "has": "checks whether",
    "can": "checks whether",
    "should": "checks whether",
    "set": "sets",
    "create": "creates",
    "build": "builds",
    "make": "creates",
    "new": "creates",
    "validate": "validates",
    "check": "checks",
    "verify": "verifies",
    "calculate": "calculates",
    "compute": "computes",
    "find": "finds",
    "fetch": "fetches",
    "load": "loads",
    "read": "reads",
    "save": "saves",
    "store": "stores",
    "write": "writes",
    "delete": "deletes",
    "remove": "removes",
    "update": "updates",
    "process": "processes",
    "handle": "handles",
    "convert": "converts",
    "to": "converts to",
    "add": "adds",
    "parse": "parses",
    "format": "formats",
    "apply": "applies",
    "init": "initializes",
    "initialize": "initializes",
    "generate": "generates",
    "map": "maps",
    "filter": "filters",
    "group": "groups",
    "sort": "sorts",
    "merge": "merges",
    "send": "sends",
    "execute": "executes",
    "run": "runs",
    "resolve": "resolves",
    "normalize": "normalizes",
    "extract": "extracts",
    "register": "registers",
    "match": "matches",
    "count": "counts",
    "select": "selects",
    "collect": "collects",
    "assign": "assigns",
    "reset": "resets",
    "clear": "clears",
    "close": "closes",
    "open": "opens",
    "start": "starts",
    "stop": "stops",
}
_VERBS.update(
    {
        "classify": "classifies",
        "determine": "determines",
        "evaluate": "evaluates",
        "compare": "compares",
        "combine": "combines",
        "join": "joins",
        "split": "splits",
        "transform": "transforms",
        "translate": "translates",
        "rank": "ranks",
        "notify": "notifies",
        "publish": "publishes",
        "schedule": "schedules",
        "authorize": "authorizes",
        "lookup": "looks up",
        "populate": "populates",
        "enrich": "enriches",
        "with": "returns a copy with",
        "of": "creates from",
        "from": "creates from",
    }
)
_TRIVIAL_LITERALS = {"0", "1", "-1", '""', "true", "false", "null", "0.0", "2"}
_BROAD_EXCEPTIONS = {"Exception", "Throwable", "RuntimeException"}
_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


@dataclass
class BuildOptions:
    docs_root: Path | None = None
    enable_tests: bool = True
    ignore_dirs: tuple[str, ...] = (".git", "node_modules", "target", "build")


@dataclass
class BuiltKnowledge:
    repository: RepositoryKnowledge
    graph: CallGraph


@dataclass
class _Draft:
    file: AnalyzedFile
    cls: ClassSymbol
    decl: TypeDecl
    method: Method
    resolution: MethodResolution
    class_id: str
    id: str
    flow: ControlFlow
    data: MethodDataFlow
    rules: list[RuleCandidate]
    evidence: dict[str, Evidence] = field(default_factory=dict)
    purpose: Purpose | None = None
    is_test: bool = False


class KnowledgeBuilder:
    def __init__(
        self,
        repository_id: str,
        files: list[AnalyzedFile],
        table: SymbolTable,
        resolver: SymbolResolver,
        resolutions: list[FileResolution],
        options: BuildOptions | None = None,
    ) -> None:
        self.repository_id = repository_id
        self.files = {f.file_id: f for f in files}
        self.table = table
        self.resolver = resolver
        self.resolutions = resolutions
        self.options = options or BuildOptions()

    # ==== orchestration ========================================================================

    def build(self) -> BuiltKnowledge:
        started = time.monotonic()
        graph = build_call_graph(self.table, self.resolver, self.resolutions)
        log_event(
            logger,
            "call_graph_built",
            methods=len(graph.nodes),
            edges=sum(len(e) for e in graph.edges_from.values()),
        )

        drafts = self._drafts()
        by_method_id = {d.resolution.method_id: d for d in drafts}
        incoming = self._incoming_arguments(drafts)
        self._add_test_evidence(drafts, graph, by_method_id)
        docs = self._doc_index()

        methods = [self._method(d, graph, by_method_id, incoming, docs) for d in drafts]
        classes = self._classes(drafts, methods, graph, docs)
        stats = KnowledgeStats(
            files=len(self.files),
            classes=len(classes),
            methods=len(methods),
            call_edges=sum(len(e) for e in graph.edges_from.values()),
            rule_candidates=sum(len(m.rule_candidates) for m in methods),
            evidence=sum(len(m.evidence) for m in methods) + sum(len(c.evidence) for c in classes),
            risks=sum(len(m.risks) for m in methods),
            unknowns=sum(len(m.unknowns) for m in methods),
        )
        log_event(
            logger,
            "knowledge_model_built",
            methods=stats.methods,
            classes=stats.classes,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        repo = RepositoryKnowledge(
            repository_id=self.repository_id, classes=classes, methods=methods, stats=stats
        )
        return BuiltKnowledge(repo, graph)

    # ==== pass 1: per-method facts =============================================================

    def _drafts(self) -> list[_Draft]:
        params_of = self._param_names()
        drafts: list[_Draft] = []
        ast_index = self._ast_index()
        for fr in self.resolutions:
            file = self.files[fr.file_id]
            for cr in fr.classes:
                cls = self._class_symbol(cr.fqn, fr.file_id)
                decl = self.table.declaration(cls)
                for mr in cr.methods:
                    method = ast_index.get((fr.file_id, cr.fqn, mr.signature))
                    if method is None:
                        continue
                    refs = {r.expression_id: r for r in mr.refs}
                    flow = build_control_flow(method, mr.method_id, refs)
                    data = build_data_flow(method, mr.method_id, refs, params_of.get)
                    rules = extract_rules(method, mr.method_id, flow, file.path)
                    annotations = [a.name for a in method.annotations]
                    draft = _Draft(
                        file=file,
                        cls=cls,
                        decl=decl,
                        method=method,
                        resolution=mr,
                        class_id=short_id("c", fr.file_id, cr.fqn),
                        id=short_id("m", fr.file_id, mr.method_id),
                        flow=flow,
                        data=data,
                        rules=rules,
                        is_test=is_test_location(file.path, cr.fqn, annotations),
                    )
                    self._own_evidence(draft)
                    draft.purpose = self._purpose(draft)
                    drafts.append(draft)
        log_event(logger, "control_flow_built", methods=len(drafts))
        log_event(logger, "data_flow_built", methods=len(drafts))
        log_event(logger, "rule_candidates_extracted", rules=sum(len(d.rules) for d in drafts))
        return drafts

    def _class_symbol(self, fqn: str, file_id: str) -> ClassSymbol:
        candidates = self.table.lookup(fqn)
        return next((c for c in candidates if c.file.file_id == file_id), candidates[0])

    def _ast_index(self) -> dict[tuple[str, str, str], Method]:
        index: dict[tuple[str, str, str], Method] = {}
        for f in self.files.values():
            stack = list(f.parsed.types)
            while stack:
                t = stack.pop()
                stack.extend(t.nested_types)
                for m in [*t.methods, *t.constructors]:
                    index[(f.file_id, t.qualified_name, m.signature)] = m
        return index

    def _param_names(self) -> dict[str, list[str]]:
        names: dict[str, list[str]] = {}
        for classes in self.table.classes.values():
            for cls in classes:
                for m in [*(x for ms in cls.methods.values() for x in ms), *cls.constructors]:
                    names[m.id] = m.param_names
        return names

    def _own_evidence(self, d: _Draft) -> None:
        m, file, cls = d.method, d.file.path, d.cls
        add = d.evidence.__setitem__
        impl = make_evidence(
            "source_code",
            file,
            m.start_line,
            m.end_line,
            m.source_text,
            "implements",
            class_name=cls.fqn,
            method=m.name,
            confidence="high",
        )
        add(impl.id, impl)
        for e in comment_evidence(m.comments, file, cls.fqn, m.name):
            add(e.id, e)
        for rule in d.rules:
            for ref in rule.evidence:
                e = make_evidence(
                    "source_code",
                    ref.file,
                    ref.start_line,
                    ref.end_line,
                    ref.snippet,
                    f"rule:{rule.kind}",
                    class_name=cls.fqn,
                    method=m.name,
                    confidence=rule.confidence,
                )
                add(e.id, e)

    # ==== purpose ===============================================================================

    def _purpose(self, d: _Draft) -> Purpose:
        structure = _structure(d.flow, d.rules, d.method)
        docs = [
            e
            for e in d.evidence.values()
            if e.source_type == "comment" and e.relation == "documents"
        ]
        if docs:
            first = min(docs, key=lambda e: e.start_line)
            sentence = first_sentence(first.snippet)
            if sentence:
                return Purpose(
                    text=sentence,
                    basis="comment",
                    level="fact",
                    structure=structure,
                    evidence_ids=[first.id],
                )
        phrase = describe_name(d.method.name, d.method.kind, d.cls.name, d.method.return_type)
        return Purpose(text=phrase, basis="name", level="inference", structure=structure)

    # ==== cross-method indexes =================================================================

    def _incoming_arguments(
        self, drafts: list[_Draft]
    ) -> dict[tuple[str, str], list[tuple[str, FlowRef, int]]]:
        incoming: dict[tuple[str, str], list[tuple[str, FlowRef, int]]] = defaultdict(list)
        for d in drafts:
            for edge in d.data.edges:
                if edge.via == "argument" and edge.callee_id and edge.param:
                    key = (edge.callee_id, edge.param)
                    incoming[key].append((d.resolution.method_id, edge.source, edge.line))
        return incoming

    def _add_test_evidence(
        self, drafts: list[_Draft], graph: CallGraph, by_method_id: dict[str, _Draft]
    ) -> None:
        if not self.options.enable_tests:
            return
        for test in drafts:
            if not test.is_test:
                continue
            for edge in graph.calls(test.resolution.method_id):
                for target in edge.targets:
                    subject = by_method_id.get(target)
                    if subject is None or subject.is_test:
                        continue
                    e = make_evidence(
                        "test",
                        test.file.path,
                        test.method.start_line,
                        test.method.end_line,
                        test.method.source_text,
                        "tested_by",
                        class_name=test.cls.fqn,
                        method=test.method.name,
                        confidence="medium"
                        if edge.status is ResolutionStatus.AMBIGUOUS
                        else "high",
                    )
                    subject.evidence[e.id] = e
        log_event(logger, "test_evidence_attached", tests=sum(1 for d in drafts if d.is_test))

    def _doc_index(self) -> DocIndex | None:
        root = self.options.docs_root
        if root is None:
            return None
        return DocIndex(load_docs(root, self.options.ignore_dirs))

    # ==== pass 2: assemble ======================================================================

    def _method(
        self,
        d: _Draft,
        graph: CallGraph,
        by_method_id: dict[str, _Draft],
        incoming: dict[tuple[str, str], list[tuple[str, FlowRef, int]]],
        docs: DocIndex | None,
    ) -> MethodKnowledge:
        m, mr = d.method, d.resolution
        method_id = mr.method_id
        if docs is not None and len(m.name) >= 3 and m.kind == "method":
            hits = docs.mentions_method(d.cls.name, m.name, qualified_only=len(m.name) < 5)
            for e in doc_evidence(hits, d.cls.fqn, m.name, "high"):
                d.evidence[e.id] = e

        callees = self._callees(method_id, graph, by_method_id)
        callers = self._callers(method_id, graph, by_method_id)
        risks = self._risks(d, graph, callers, by_method_id)
        unknowns = self._unknowns(d, callees)
        return MethodKnowledge(
            id=d.id,
            method_id=method_id,
            class_id=d.class_id,
            class_fqn=d.cls.fqn,
            name=m.name,
            signature=m.signature,
            kind=m.kind,
            file=d.file.path,
            file_id=d.file.file_id,
            start_line=m.start_line,
            end_line=m.end_line,
            visibility=m.visibility,
            modifiers=m.modifiers,
            annotations=[a.name for a in m.annotations],
            return_type=m.return_type,
            is_test=d.is_test,
            purpose=d.purpose or _empty_purpose(m),
            parameters=self._parameters(d, incoming, by_method_id),
            output=self._output(d),
            callees=callees,
            callers=callers,
            control_flow=d.flow,
            data_flow=d.data,
            rule_candidates=d.rules,
            evidence=sorted(d.evidence.values(), key=lambda e: (e.start_line, e.id)),
            risks=risks,
            unknowns=unknowns,
            complexity=Complexity(
                cyclomatic=m.cyclomatic_complexity,
                nesting_depth=m.max_nesting_depth,
                lines=m.end_line - m.start_line + 1,
                statements=len(m.statements),
                expressions=len(m.expressions),
            ),
        )

    def _parameters(
        self,
        d: _Draft,
        incoming: dict[tuple[str, str], list[tuple[str, FlowRef, int]]],
        by_method_id: dict[str, _Draft],
    ) -> list[ParameterInfo]:
        out: list[ParameterInfo] = []
        for p in d.method.parameters:
            sources = incoming.get((d.resolution.method_id, p.name), [])
            seen: dict[str, None] = {}
            for caller, ref, line in sources:
                caller_draft = by_method_id.get(caller)
                tag = " [test]" if caller_draft is not None and caller_draft.is_test else ""
                seen[f"{_short(caller)} L{line}: {describe_ref(ref)}{tag}"] = None
            comes_from = list(seen)[:6]
            if len(seen) > 6:
                comes_from.append(f"(+{len(seen) - 6} more call sites)")
            if not comes_from:
                comes_from = ["no project caller passes this (entry point or external caller)"]
            out.append(
                ParameterInfo(name=p.name, type=p.type, var_args=p.var_args, comes_from=comes_from)
            )
        return out

    def _output(self, d: _Draft) -> OutputInfo:
        returns: dict[str, None] = {}
        for e in d.data.edges:
            if e.via == "return":
                returns[describe_ref(e.source)] = None
        effects: dict[str, None] = {}
        for v in d.data.variables:
            if v.kind not in ("field", "parameter"):
                continue
            for ev in v.of_kind("modified"):
                effects[f"modifies {v.kind} {v.name} (L{ev.line}: {ev.detail})"] = None
        return OutputInfo(
            type=d.method.return_type, returns=list(returns)[:8], side_effects=list(effects)[:10]
        )

    def _callees(
        self, method_id: str, graph: CallGraph, by_method_id: dict[str, _Draft]
    ) -> list[CalleeInfo]:
        grouped: dict[tuple[str | None, str, str | None, str], list[CallEdge]] = {}
        for e in graph.calls(method_id):
            key = (e.callee_id, e.name, e.owner_type, ",".join(sorted(e.candidates)))
            grouped.setdefault(key, []).append(e)
        out: list[CalleeInfo] = []
        for (callee_id, name, owner, _), edges in grouped.items():
            first = edges[0]
            target = by_method_id.get(callee_id) if callee_id else None
            out.append(
                CalleeInfo(
                    method_id=callee_id,
                    name=name,
                    owner_type=owner,
                    status=first.status,
                    origin=first.origin,
                    site_lines=sorted({e.site_line for e in edges}),
                    candidates=first.candidates,
                    may_dispatch_to=graph.overrides.get(callee_id or "", []),
                    reason=first.reason,
                    purpose=target.purpose.text if target and target.purpose else None,
                )
            )
        return sorted(out, key=lambda c: (c.site_lines[0], c.name))

    def _callers(
        self, method_id: str, graph: CallGraph, by_method_id: dict[str, _Draft]
    ) -> list[CallerInfo]:
        grouped: dict[str, list[CallEdge]] = defaultdict(list)
        for e in graph.called_by(method_id):
            grouped[e.caller_id].append(e)
        out = []
        for caller_id, edges in grouped.items():
            target = by_method_id.get(caller_id)
            out.append(
                CallerInfo(
                    method_id=caller_id,
                    site_lines=sorted({e.site_line for e in edges}),
                    ambiguous=all(e.status is ResolutionStatus.AMBIGUOUS for e in edges),
                    purpose=target.purpose.text if target and target.purpose else None,
                )
            )
        return sorted(out, key=lambda c: c.method_id)

    # ==== risks and unknowns ====================================================================

    def _risks(
        self,
        d: _Draft,
        graph: CallGraph,
        callers: list[CallerInfo],
        by_method_id: dict[str, _Draft],
    ) -> list[Risk]:
        m, risks = d.method, []
        cc = m.cyclomatic_complexity
        if cc >= 15:
            risks.append(
                Risk(
                    kind="complexity",
                    level="high",
                    line=m.start_line,
                    message=f"cyclomatic complexity {cc}: many independent paths to test",
                )
            )
        elif cc >= 10:
            risks.append(
                Risk(
                    kind="complexity",
                    level="medium",
                    line=m.start_line,
                    message=f"cyclomatic complexity {cc}",
                )
            )
        if m.max_nesting_depth >= 4:
            risks.append(
                Risk(
                    kind="nesting",
                    level="medium",
                    line=m.start_line,
                    message=f"control structures nested {m.max_nesting_depth} deep",
                )
            )
        length = m.end_line - m.start_line + 1
        if length > 80:
            risks.append(
                Risk(
                    kind="length",
                    level="medium",
                    line=m.start_line,
                    message=f"{length} lines long; changes are hard to reason about",
                )
            )
        non_test = [
            c
            for c in callers
            if not (by_method_id.get(c.method_id) and by_method_id[c.method_id].is_test)
        ]
        if len(non_test) >= 5:
            level: Level = "high" if len(non_test) >= 15 else "medium"
            risks.append(
                Risk(
                    kind="wide_impact",
                    level=level,
                    line=m.start_line,
                    message=f"called from {len(non_test)} methods; a behaviour change "
                    "reaches all of them",
                )
            )
        risks.extend(self._state_risks(d))
        risks.extend(self._exception_risks(d))
        if graph.is_recursive(d.resolution.method_id):
            risks.append(
                Risk(
                    kind="recursion",
                    level="low",
                    line=m.start_line,
                    message="calls itself (directly or indirectly)",
                )
            )
        for r in d.flow.walk():
            if r.kind == "return" and r.text == "null":
                risks.append(
                    Risk(
                        kind="returns_null",
                        level="medium",
                        line=r.start_line,
                        message="can return null; callers must handle it",
                    )
                )
        return sorted(risks, key=lambda r: (r.line or 0, r.kind))

    def _state_risks(self, d: _Draft) -> list[Risk]:
        out: list[Risk] = []
        static_mutable = {
            f.name for f in d.decl.fields if "static" in f.modifiers and "final" not in f.modifiers
        }
        for v in d.data.variables:
            events = v.of_kind("modified")
            if not events:
                continue
            if v.kind == "field":
                shared = v.name in static_mutable
                out.append(
                    Risk(
                        kind="shared_state" if shared else "field_write",
                        level="high" if shared else "medium",
                        line=events[0].line,
                        message=(
                            f"writes {'static ' if shared else ''}field {v.name}; "
                            + ("shared across all callers" if shared else "changes object state")
                        ),
                    )
                )
            elif v.kind == "parameter":
                out.append(
                    Risk(
                        kind="mutates_argument",
                        level="medium",
                        line=events[0].line,
                        message=f"changes its argument {v.name}; callers observe the change",
                    )
                )
        return out

    def _exception_risks(self, d: _Draft) -> list[Risk]:
        out: list[Risk] = []
        for n in d.flow.walk():
            if n.kind != "catch":
                continue
            caught = (n.text or "").split()[0] if n.text else ""
            if any(b in caught.split("|") for b in _BROAD_EXCEPTIONS):
                out.append(
                    Risk(
                        kind="broad_catch",
                        level="medium",
                        line=n.start_line,
                        message=f"catches {caught}, which can hide unrelated failures",
                    )
                )
            if not n.children:
                out.append(
                    Risk(
                        kind="swallowed_exception",
                        level="medium",
                        line=n.start_line,
                        message="catch block does nothing; the failure is silently ignored",
                    )
                )
        return out

    def _unknowns(self, d: _Draft, callees: list[CalleeInfo]) -> list[Unknown]:
        out: list[Unknown] = []
        for c in callees:
            line = c.site_lines[0]
            if c.status is ResolutionStatus.UNRESOLVED:
                out.append(
                    Unknown(
                        kind="unresolved_call",
                        line=line,
                        message=f"cannot determine what {c.name}() refers to ({c.reason})",
                    )
                )
            elif c.status is ResolutionStatus.AMBIGUOUS:
                out.append(
                    Unknown(
                        kind="ambiguous_call",
                        line=line,
                        message=f"{c.name}() could be any of {len(c.candidates)} "
                        f"methods ({c.reason})",
                    )
                )
            elif c.may_dispatch_to:
                out.append(
                    Unknown(
                        kind="dynamic_dispatch",
                        line=line,
                        message=f"{c.name}() is resolved on its declared type; at runtime "
                        f"one of {len(c.may_dispatch_to)} overriding methods may run",
                    )
                )
        documented = {
            e.snippet for e in d.evidence.values() if e.source_type in ("comment", "test")
        }
        seen: set[str] = set()
        for rule in d.rules:
            for lit in rule.literals:
                if lit in _TRIVIAL_LITERALS or lit in seen or any(lit in s for s in documented):
                    continue
                seen.add(lit)
                out.append(
                    Unknown(
                        kind="unestablished_constant",
                        line=rule.start_line,
                        message=f"the repository does not establish why {lit} was "
                        "chosen (no comment or test mentions it)",
                    )
                )
        if d.purpose and d.purpose.basis != "comment":
            out.append(
                Unknown(
                    kind="purpose_not_documented",
                    line=d.method.start_line,
                    message="no comment states the purpose; it is inferred from the "
                    "name and the code's structure",
                )
            )
        return sorted(out, key=lambda u: (u.line or 0, u.kind))

    # ==== classes ===============================================================================

    def _classes(
        self,
        drafts: list[_Draft],
        methods: list[MethodKnowledge],
        graph: CallGraph,
        docs: DocIndex | None,
    ) -> list[ClassKnowledge]:
        by_class: dict[str, list[MethodKnowledge]] = defaultdict(list)
        for m in methods:
            by_class[m.class_id].append(m)
        resolution_of = self._class_resolutions()
        uses: dict[str, set[str]] = defaultdict(set)
        out: list[ClassKnowledge] = []
        for fr in self.resolutions:
            file = self.files[fr.file_id]
            for cr in fr.classes:
                cls = self._class_symbol(cr.fqn, fr.file_id)
                decl = self.table.declaration(cls)
                cid = short_id("c", fr.file_id, cr.fqn)
                members = sorted(by_class.get(cid, []), key=lambda x: (x.start_line, x.signature))
                deps = self._dependencies(cr, resolution_of, members, graph)
                uses[cr.fqn] = deps
                evidence = comment_evidence(decl.comments, file.path, cr.fqn, None)
                if docs is not None:
                    evidence += doc_evidence(docs.mentions_class(cls.name), cr.fqn, None, "medium")
                out.append(
                    ClassKnowledge(
                        id=cid,
                        fqn=cr.fqn,
                        name=cls.name,
                        kind=cls.kind,
                        package=cls.package,
                        file=file.path,
                        file_id=fr.file_id,
                        start_line=decl.start_line,
                        end_line=decl.end_line,
                        visibility=decl.visibility,
                        modifiers=decl.modifiers,
                        annotations=cls.annotations,
                        superclass=_type_name(cr.superclass) if cr.superclass else None,
                        interfaces=[_type_name(i) for i in cr.interfaces],
                        purpose=_class_purpose(cls, decl, members, evidence),
                        fields=[
                            FieldInfo(
                                name=f.name,
                                type=f.type,
                                visibility=f.visibility,
                                modifiers=f.modifiers,
                                line=f.start_line,
                            )
                            for f in decl.fields
                        ],
                        method_ids=[x.id for x in members],
                        dependencies=sorted(deps),
                        evidence=evidence,
                        is_test=is_test_location(file.path, cr.fqn, cls.annotations),
                    )
                )
        dependents: dict[str, set[str]] = defaultdict(set)
        for fqn, targets in uses.items():
            for t in targets:
                dependents[t].add(fqn)
        for c in out:
            c.dependents = sorted(dependents.get(c.fqn, set()))
        return out

    def _class_resolutions(self) -> dict[str, ClassResolution]:
        return {cr.fqn: cr for fr in self.resolutions for cr in fr.classes}

    def _dependencies(
        self,
        cr: ClassResolution,
        resolution_of: dict[str, ClassResolution],
        members: list[MethodKnowledge],
        graph: CallGraph,
    ) -> set[str]:
        project = self.table.classes
        found: set[str] = set()
        types = [t for t in [cr.superclass, *cr.interfaces, *(f.type for f in cr.fields)] if t]
        for mr in cr.methods:
            types.extend(mr.parameters)
            if mr.return_type:
                types.append(mr.return_type)
        stack = list(types)
        while stack:
            t = stack.pop()
            stack.extend(t.args)
            if t.origin is Origin.PROJECT and t.fqn:
                found.add(t.fqn)
        for m in members:
            for c in m.callees:
                node = graph.nodes.get(c.method_id or "")
                if node is not None:
                    found.add(node.class_fqn)
        found.discard(cr.fqn)
        return {f for f in found if f in project}


# ==== helpers =================================================================


def split_words(name: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(name)]


def describe_name(name: str, kind: str, class_name: str, return_type: str | None) -> str:
    """A plain-words guess at purpose from the name alone. Marked as inference by the caller."""
    if kind != "method":
        return f"constructs a {' '.join(split_words(class_name))}"
    words = split_words(name)
    if not words:
        return name
    verb = _VERBS.get(words[0])
    rest = " ".join(words[1:])
    if verb is None:
        return "performs " + " ".join(words)
    if words[0] == "get" and rest:
        return f"returns the {rest}"
    if words[0] in ("has", "can", "should") and rest:
        return f"checks whether it {words[0]} {rest}"
    if words[0] == "is" and rest:
        return f"checks whether {rest}"
    return f"{verb} {rest}".strip() if rest else verb


def _structure(flow: ControlFlow, rules: list[RuleCandidate], method: Method) -> str:
    s = flow.summary
    parts: list[str] = []
    if s.loops:
        parts.append(f"{s.loops} loop{'s' if s.loops > 1 else ''}")
    if s.decisions:
        parts.append(f"{s.decisions} decision{'s' if s.decisions > 1 else ''}")
    if s.switches:
        parts.append(f"{s.switches} switch{'es' if s.switches > 1 else ''}")
    streams = s.streams + sum(len(n.header_streams) for n in flow.walk())
    if streams:
        parts.append(f"{streams} stream pipeline{'s' if streams > 1 else ''}")
    if s.try_blocks:
        parts.append("exception handling")
    calls = [n.callee.name for n in flow.walk() if n.kind == "call" and n.callee]
    unique = list(dict.fromkeys(calls))
    if unique:
        shown = ", ".join(unique[:4]) + ("..." if len(unique) > 4 else "")
        parts.append(f"calls {shown}")
    if s.mutations:
        parts.append(f"{s.mutations} state change{'s' if s.mutations > 1 else ''}")
    validations = sum(1 for r in rules if r.kind == "validation")
    if validations:
        parts.append(f"{validations} validation{'s' if validations > 1 else ''}")
    ret = (
        f"returns {method.return_type}"
        if method.return_type and method.return_type != "void"
        else None
    )
    if ret:
        parts.append(ret)
    return ", ".join(parts) if parts else "trivial body"


def _empty_purpose(m: Method) -> Purpose:
    return Purpose(text=m.name, basis="name", level="inference")


def _class_purpose(
    cls: ClassSymbol, decl: TypeDecl, members: list[MethodKnowledge], evidence: list[Evidence]
) -> Purpose:
    docs = [e for e in evidence if e.source_type == "comment" and e.relation == "documents"]
    structure = f"{cls.kind} with {len(members)} method{'s' if len(members) != 1 else ''}"
    if cls.superclass:
        structure += f", extends {cls.superclass}"
    if cls.interfaces:
        structure += f", implements {', '.join(cls.interfaces)}"
    if docs:
        sentence = first_sentence(docs[0].snippet)
        if sentence:
            return Purpose(
                text=sentence,
                basis="comment",
                level="fact",
                structure=structure,
                evidence_ids=[docs[0].id],
            )
    words = " ".join(split_words(cls.name))
    return Purpose(
        text=f"{words} ({cls.kind})", basis="name", level="inference", structure=structure
    )


def describe_ref(ref: FlowRef) -> str:
    if ref.kind == "call_result":
        return f"result of {ref.name}()"
    if ref.kind == "new":
        return f"new {ref.name}"
    if ref.kind == "literal":
        return (
            f"literal {ref.name} ({ref.text})"
            if ref.text and ref.text != f"{ref.name} literal"
            else f"literal {ref.name}"
        )
    if ref.kind == "constant":
        return f"constant {ref.name}"
    return f"{ref.kind} {ref.name}"


def _short(method_id: str) -> str:
    cls, _, sig = method_id.partition("#")
    return f"{cls.rsplit('.', 1)[-1]}.{sig.split('(')[0]}()"


def _type_name(t: TypeResolution) -> str:
    return t.fqn or t.text
