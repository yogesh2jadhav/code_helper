"""Render an explanation plan as a developer-mentor style answer, without any LLM.

The same nine sections the LLM is asked for. Only what the plan contains is stated: facts as facts,
interpretations marked as inferred, and what is not established listed explicitly. Used when the
LLM is unavailable and as a baseline to compare LLM answers against.
"""

from __future__ import annotations

from app.context.explanation_plan import ExplanationPlan
from app.llm.prompts import EXPLAIN_SECTIONS


def _cites(refs: list[str]) -> str:
    return f" [{', '.join(refs)}]" if refs else ""


def render_mentor_answer(plan: ExplanationPlan, note: str | None = None) -> str:
    out: list[str] = []
    if note:
        out += [f"> {note}", ""]
    heads = dict(zip(EXPLAIN_SECTIONS, EXPLAIN_SECTIONS, strict=True))

    # 1. what it does
    out += [f"## {heads['What this method does']}", ""]
    p = plan.purpose
    if p.basis == "comment":
        out.append(f"{p.text} (stated in a comment{_cites(p.refs)}.)")
    else:
        out.append(
            f"Judging by its name and what it does, it {p.text}. This is an inference: no comment "
            f"states the purpose{_cites(p.refs)}."
        )
    out += [f"\n{plan.method_identity}", ""]

    # 2. flow
    out += [f"## {heads['High-level flow']}", ""]
    if plan.major_stages:
        for i, stage in enumerate(plan.major_stages):
            out.append(f"{stage.title}")
            if i < len(plan.major_stages) - 1:
                out.append("    ↓")
    out.append("")
    if plan.data_transformations:
        out += ["How the data moves:", *(f"- {c}" for c in plan.data_transformations), ""]

    # 3. inputs
    out += [f"## {heads['Inputs']}", "", *(f"- {i}" for i in plan.input_summary), ""]

    # 4. stages
    out += [f"## {heads['Major processing stages']}", ""]
    for i, s in enumerate(plan.major_stages, 1):
        out.append(
            f"{i}. **{s.title}** (L{s.start_line}-{s.end_line}): {s.description}{_cites(s.refs)}"
        )
    out.append("")

    # 5. rules
    out += [f"## {heads['Important rules']}", ""]
    if plan.business_rule_candidates:
        out.append(
            "These are patterns found in the code; whether they are business rules is not "
            "established."
        )
        for r in plan.business_rule_candidates:
            line = f"- L{r.lines}: {r.text}{_cites(r.refs)}"
            unexplained = [
                lit for lit in r.literals if any(f"why {lit} " in u for u in plan.unknowns)
            ]
            if unexplained:
                line += f" — the reason for {', '.join(unexplained)} is not established"
            out.append(line)
    else:
        out.append(
            "The analysis found no rule-like conditions, thresholds or defaults in this method."
        )
    out.append("")

    # 6. dependencies
    out += [f"## {heads['Important dependencies']}", ""]
    if plan.dependencies:
        for d in plan.dependencies:
            lines = f"L{', L'.join(map(str, d.lines))}"
            detail = f": {d.purpose}" if d.purpose else (f" ({d.note})" if d.note else "")
            out.append(f"- {d.name} [{d.kind}] at {lines}{detail}{_cites(d.refs)}")
    else:
        out.append("It does not call other methods that the analysis could identify.")
    out.append("")

    # 7. output
    refs = _cites(plan.output_refs)
    out += [f"## {heads['Output']}", "", *(f"- {o}{refs}" for o in plan.output_summary), ""]

    # 8. careful
    out += [f"## {heads['What to be careful about']}", ""]
    out += [f"- {r}" for r in plan.risks] or ["- The analysis flagged no specific risks."]
    out.append("")

    # 9. evidence
    out += [f"## {heads['Evidence and unknowns']}", ""]
    for level, label in (
        ("fact", "Established by the code"),
        ("inference", "Inferred"),
        ("unknown", "Not established"),
    ):
        claims = [c for c in plan.claims if c.level == level]
        if not claims:
            continue
        out.append(f"{label}:")
        out += [f"- {c.text}{_cites(c.refs)}" for c in claims]
        out.append("")
    out += [
        "Context from comments, tests and documentation:",
        *(f"- {h}" for h in plan.historical_context),
    ]
    return "\n".join(out).strip() + "\n"
