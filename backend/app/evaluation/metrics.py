"""Metric computation. Each function answers one question and returns counts, not a verdict."""

from __future__ import annotations

import re
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field

from app.analyzer.rule_extractor import RuleCandidate
from app.context.citations import Citation
from app.evaluation.benchmark import ExpectedMethod, ExpectedRule
from app.explain.explanation_service import ExplainResult
from app.knowledge.models import MethodKnowledge


@dataclass
class Ratio:
    """`found` of `expected`. A benchmark with nothing expected is reported as not applicable."""

    found: int = 0
    expected: int = 0
    missing: list[str] = field(default_factory=list)

    @property
    def percent(self) -> float | None:
        return None if self.expected == 0 else 100.0 * self.found / self.expected


@dataclass
class MethodMetrics:
    method: str
    purpose: Ratio
    structure: Ratio  # inputs + output + stages
    structure_extra: list[str]  # stages the system reported that were not expected
    data_flow: Ratio
    dependencies: Ratio
    dependency_extra: list[str]  # resolved callees that were not expected
    rules: Ratio
    rule_extra: int  # rule candidates beyond the expected ones
    unknowns: Ratio
    unsupported_claims: list[str]  # claims in the answer with no support in the analysed material
    checked_claims: int
    invalid_citations: int
    evidence_cited: int  # claim lines with at least one valid citation
    evidence_lines: int  # claim lines
    answer_source: str  # llm | deterministic

    @property
    def hallucination_rate(self) -> float | None:
        if self.checked_claims == 0:
            return None
        return 100.0 * len(self.unsupported_claims) / self.checked_claims

    @property
    def evidence_coverage(self) -> float | None:
        return (
            None if self.evidence_lines == 0 else 100.0 * self.evidence_cited / self.evidence_lines
        )


# ---- structure ---------------------------------------------------------------------------------


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def purpose_ratio(m: MethodKnowledge, expected: ExpectedMethod) -> Ratio:
    want = expected.purpose
    text = m.purpose.text.lower()
    r = Ratio()
    for word in want.mentions:
        r.expected += 1
        if word.lower() in text:
            r.found += 1
        else:
            r.missing.append(f"purpose mentions {word!r}")
    for label, wanted, got in (
        ("basis", want.basis, m.purpose.basis),
        ("level", want.level, m.purpose.level),
    ):
        if wanted is not None:
            r.expected += 1
            if wanted == got:
                r.found += 1
            else:
                r.missing.append(f"purpose {label} {wanted!r} (got {got!r})")
    return r


def structure_ratio(m: MethodKnowledge, expected: ExpectedMethod) -> tuple[Ratio, list[str]]:
    r = Ratio()
    have_inputs = Counter(_norm(f"{p.type} {p.name}") for p in m.parameters)
    for item in expected.inputs:
        r.expected += 1
        if have_inputs[_norm(item)] > 0:
            have_inputs[_norm(item)] -= 1
            r.found += 1
        else:
            r.missing.append(f"input {item!r}")
    if expected.output is not None:
        r.expected += 1
        if m.return_type and _norm(m.return_type) == _norm(expected.output):
            r.found += 1
        else:
            r.missing.append(f"output {expected.output!r} (got {m.return_type!r})")
    have: Counter[str] = Counter(str(n.kind) for n in m.control_flow.nodes)
    for kind in expected.stages:
        r.expected += 1
        if have[kind] > 0:
            have[kind] -= 1
            r.found += 1
        else:
            r.missing.append(f"stage {kind!r}")
    extra = [f"stage {k!r}" for k, n in have.items() for _ in range(n)]
    return r, extra


# ---- dependencies ------------------------------------------------------------------------------


def _simple(name: str | None) -> str:
    return (name or "").rsplit(".", 1)[-1]


def dependency_ratio(m: MethodKnowledge, expected: ExpectedMethod) -> tuple[Ratio, list[str]]:
    identified: list[str] = []
    for c in m.callees:
        if c.status == "unresolved":
            continue  # a call the system could not identify is not an identified dependency
        owner = _simple(c.owner_type) or _simple((c.method_id or "").split("#")[0])
        identified.append(f"{owner}.{c.name}")
    r = Ratio()
    remaining = Counter(identified)
    for dep in expected.dependencies:
        r.expected += 1
        if remaining[dep] > 0:
            remaining[dep] -= 1
            r.found += 1
        else:
            r.missing.append(dep)
    extra = [d for d, n in remaining.items() for _ in range(n)]
    return r, extra


# ---- data flow ---------------------------------------------------------------------------------


def _flow_graph(m: MethodKnowledge) -> dict[str, set[str]]:
    """Name-level graph. An argument also flows into the result of the call it is passed to."""
    graph: dict[str, set[str]] = defaultdict(set)
    for e in m.data_flow.edges:
        graph[e.source.name].add(e.target.name)
        if e.via == "argument" and e.detail:
            graph[e.source.name].add(e.detail)
    return graph


def data_flow_ratio(m: MethodKnowledge, expected: ExpectedMethod) -> Ratio:
    graph = _flow_graph(m)
    r = Ratio()
    for source, target in expected.data_flow:
        r.expected += 1
        seen, queue = {source}, deque([source])
        while queue:
            for nxt in graph.get(queue.popleft(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        if target in seen and target != source:
            r.found += 1
        else:
            r.missing.append(f"{source} -> {target}")
    return r


# ---- rules -------------------------------------------------------------------------------------


def _rule_matches(rule: RuleCandidate, want: ExpectedRule) -> bool:
    if rule.kind != want.kind:
        return False
    if want.literal is not None and want.literal not in rule.literals:
        return False
    if want.contains is not None:
        text = " ".join(
            x for x in (rule.condition, rule.meaning, rule.calculation, rule.action) if x
        )
        return want.contains.lower() in text.lower()
    return True


def rule_ratio(m: MethodKnowledge, expected: ExpectedMethod) -> tuple[Ratio, int]:
    pool = list(m.rule_candidates)
    r = Ratio()
    for want in expected.rules:
        r.expected += 1
        hit = next((c for c in pool if _rule_matches(c, want)), None)
        if hit is not None:
            pool.remove(hit)
            r.found += 1
        else:
            detail = want.literal or want.contains or ""
            r.missing.append(f"rule {want.kind} {detail}".strip())
    return r, len(pool)


# ---- unknowns ----------------------------------------------------------------------------------


def unknowns_ratio(m: MethodKnowledge, result: ExplainResult, expected: ExpectedMethod) -> Ratio:
    stated = " ".join([*(u.message for u in m.unknowns), *result.unknowns, result.answer]).lower()
    r = Ratio()
    for text in expected.unknowns:
        r.expected += 1
        if text.lower() in stated:
            r.found += 1
        else:
            r.missing.append(text)
    return r


# ---- claims in the answer ----------------------------------------------------------------------

_BACKTICK = re.compile(r"`([^`\n]{1,80})`")
_NUMBER = re.compile(r"(?<![\w.#\[])(?:\d+\.\d+|\d+)(?![\w\]])")
_LINE_REF = re.compile(r"\bL\d+(?:-\d+)?\b")
_LIST_MARKER = re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE)
_CITE = re.compile(r"\[E\d+(?:\s*[,;]\s*E\d+)*\]")
_TOKEN = re.compile(r"[A-Za-z_][\w]*|\d+(?:\.\d+)?")
_CLAIM_SECTIONS = {
    "What this method does",
    "Major processing stages",
    "Important rules",
    "Important dependencies",
    "Output",
    "What to be careful about",
}


def support_tokens(m: MethodKnowledge, source: str, result: ExplainResult) -> set[str]:
    """Every identifier and number that appears in the material the answer was built from."""
    parts = [
        source,
        result.explanation_plan.model_dump_json(),
        *(e.snippet for e in m.evidence),
        *(c.name for c in m.callees),
        *(f"{p.type} {p.name}" for p in m.parameters),
        m.class_fqn,
        m.signature,
    ]
    return {t.lower() for part in parts for t in _TOKEN.findall(part)}


def _claim_lines(result: ExplainResult) -> list[str]:
    lines: list[str] = []
    for section in result.parsed.sections:
        if section.title not in _CLAIM_SECTIONS:
            continue
        for line in section.text.splitlines():
            stripped = line.strip()
            if re.match(r"^(?:[-*]|\d+[.)])\s+\S", stripped):
                lines.append(stripped)
    return lines


def claim_metrics(
    m: MethodKnowledge, source: str, result: ExplainResult
) -> tuple[list[str], int, int, int, int]:
    """(unsupported, checked, invalid_citations, cited_lines, claim_lines)."""
    support = support_tokens(m, source, result)
    valid = {c.label for c in result.evidence}
    unsupported: list[str] = []
    checked = 0
    for section in result.parsed.sections:
        if section.title in ("", "Evidence and unknowns"):
            continue  # preamble banners and the unknowns list restate the analysis
        text = _CITE.sub(" ", section.text)
        for token in _BACKTICK.findall(text):
            checked += 1
            words = [w.lower() for w in _TOKEN.findall(token)]
            if words and not all(w in support for w in words):
                unsupported.append(f"`{token}` ({section.title})")
        plain = _BACKTICK.sub(" ", _LINE_REF.sub(" ", _LIST_MARKER.sub("", text)))
        for number in _NUMBER.findall(plain):
            checked += 1
            if number.lower() not in support:
                unsupported.append(f"number {number} ({section.title})")
    invalid = sum(
        len(re.findall(r"E\d+", w.split(":", 1)[-1]))
        for w in result.warnings
        if w.startswith("removed citation labels")
    ) + len([c for c in result.parsed.invalid_citations if c not in valid])
    lines = _claim_lines(result)
    cited = sum(1 for line in lines if any(label in valid for label in re.findall(r"E\d+", line)))
    return unsupported, checked, invalid, cited, len(lines)


def evaluate_method(
    m: MethodKnowledge, source: str, result: ExplainResult, expected: ExpectedMethod
) -> MethodMetrics:
    structure, stage_extra = structure_ratio(m, expected)
    deps, dep_extra = dependency_ratio(m, expected)
    rules, rule_extra = rule_ratio(m, expected)
    unsupported, checked, invalid, cited, lines = claim_metrics(m, source, result)
    return MethodMetrics(
        method=m.method_id,
        purpose=purpose_ratio(m, expected),
        structure=structure,
        structure_extra=stage_extra,
        data_flow=data_flow_ratio(m, expected),
        dependencies=deps,
        dependency_extra=dep_extra,
        rules=rules,
        rule_extra=rule_extra,
        unknowns=unknowns_ratio(m, result, expected),
        unsupported_claims=unsupported,
        checked_claims=checked,
        invalid_citations=invalid,
        evidence_cited=cited,
        evidence_lines=lines,
        answer_source=result.answer_source,
    )


__all__ = ["Citation", "MethodMetrics", "Ratio", "evaluate_method"]
