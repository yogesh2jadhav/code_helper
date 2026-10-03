"""Plain-text and JSON reports. Metrics stay separate; nothing is averaged into a single score."""

from __future__ import annotations

import json
from dataclasses import asdict

from app.evaluation.metrics import MethodMetrics, Ratio
from app.evaluation.runner import EvaluationRun


def _pct(r: Ratio) -> str:
    return "n/a" if r.percent is None else f"{r.percent:.0f}%"


def _opt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f}%"


def _row(label: str, r: Ratio, extra: str = "") -> str:
    return f"  {label + ':':<27}{_pct(r):>5}   ({r.found}/{r.expected}){extra}"


def _extra(prefix: str, items: list[str]) -> str:
    return f"   {prefix}: {', '.join(items)}" if items else ""


def method_report(m: MethodMetrics) -> str:
    cited = f"({m.evidence_cited}/{m.evidence_lines} claim lines cited)"
    lines = [
        f"Method: {m.method}   (answer: {m.answer_source})",
        "",
        _row("Purpose coverage", m.purpose),
        _row("Structural coverage", m.structure, _extra("extra", m.structure_extra)),
        _row("Data-flow coverage", m.data_flow),
        _row("Dependency coverage", m.dependencies, _extra("extra", m.dependency_extra)),
        _row(
            "Rule coverage",
            m.rules,
            f"   extra candidates: {m.rule_extra}" if m.rule_extra else "",
        ),
        f"  {'Unknowns correctly stated:':<27}{m.unknowns.found}/{m.unknowns.expected}",
        f"  {'Unsupported claims:':<27}{len(m.unsupported_claims):>5}"
        f"   (of {m.checked_claims} checked)",
        f"  {'Hallucination rate:':<27}{_opt(m.hallucination_rate):>5}",
        f"  {'Invalid citations:':<27}{m.invalid_citations:>5}",
        f"  {'Evidence coverage:':<27}{_opt(m.evidence_coverage):>5}   {cited}",
    ]
    gaps = [
        *m.purpose.missing,
        *m.structure.missing,
        *m.data_flow.missing,
        *m.dependencies.missing,
        *m.rules.missing,
        *(f"unknown not stated: {u}" for u in m.unknowns.missing),
    ]
    if gaps:
        lines.append("  Missed:")
        lines += [f"    - {g}" for g in gaps]
    if m.unsupported_claims:
        lines.append("  Unsupported:")
        lines += [f"    - {c}" for c in m.unsupported_claims]
    return "\n".join(lines)


def _total(rs: list[Ratio]) -> Ratio:
    return Ratio(sum(r.found for r in rs), sum(r.expected for r in rs))


def summary(run: EvaluationRun) -> str:
    ms = run.methods
    checked = sum(m.checked_claims for m in ms)
    unsupported = sum(len(m.unsupported_claims) for m in ms)
    cited = sum(m.evidence_cited for m in ms)
    claim_lines = sum(m.evidence_lines for m in ms)
    rows = [
        ("Purpose coverage", _pct(_total([m.purpose for m in ms]))),
        ("Structural coverage", _pct(_total([m.structure for m in ms]))),
        ("Data-flow coverage", _pct(_total([m.data_flow for m in ms]))),
        ("Dependency coverage", _pct(_total([m.dependencies for m in ms]))),
        ("Rule coverage", _pct(_total([m.rules for m in ms]))),
        ("Unknowns correctly stated", _fraction(_total([m.unknowns for m in ms]))),
        ("Unsupported claims", f"{unsupported} of {checked} checked"),
        ("Hallucination rate", _opt(None if checked == 0 else 100.0 * unsupported / checked)),
        ("Invalid citations", str(sum(m.invalid_citations for m in ms))),
        ("Evidence coverage", _opt(None if claim_lines == 0 else 100.0 * cited / claim_lines)),
    ]
    head = f"Benchmark: {run.benchmark}   mode: {run.mode}   methods: {len(ms)}"
    body = "\n".join(f"  {name + ':':<28}{value}" for name, value in rows)
    note = "\n  Not found: " + ", ".join(run.not_found) if run.not_found else ""
    return f"{head}\n{body}{note}"


def _fraction(r: Ratio) -> str:
    return f"{r.found}/{r.expected}"


def full_report(run: EvaluationRun) -> str:
    parts = [method_report(m) for m in run.methods]
    return "\n\n".join(parts) + "\n\n" + "=" * 72 + "\n" + summary(run)


def to_json(run: EvaluationRun) -> str:
    def encode(m: MethodMetrics) -> dict[str, object]:
        data = asdict(m)
        data["hallucination_rate"] = m.hallucination_rate
        data["evidence_coverage"] = m.evidence_coverage
        for key in ("purpose", "structure", "data_flow", "dependencies", "rules", "unknowns"):
            data[key]["percent"] = getattr(m, key).percent
        return data

    return json.dumps(
        {
            "benchmark": run.benchmark,
            "mode": run.mode,
            "not_found": run.not_found,
            "methods": [encode(m) for m in run.methods],
        },
        indent=2,
    )
