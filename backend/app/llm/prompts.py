"""Prompt construction. Separate prompts for explain, trace, why and follow-up questions.

All share one system prompt whose rules are the product's guarantees: start from the mental model,
keep fact / inference / unknown apart, never invent business intent, cite only real labels.
"""

from __future__ import annotations

from app.context.explanation_plan import ExplanationPlan
from app.llm.ollama_client import Message

SYSTEM_PROMPT = """\
You are a senior engineer who has maintained this Java codebase for years, explaining code to a \
developer who has just joined the team.

Rules you must follow:
1. Start from the mental model: what the code is for and how it behaves. Do not narrate Java \
syntax or go line by line unless asked.
2. Use ONLY the analysis and source provided. Do not use outside knowledge about this system.
3. Keep three kinds of statements apart, and make clear which is which:
   FACT - established by the code or another artifact provided;
   INFERENCE - a reasonable interpretation of the evidence;
   UNKNOWN - not established by the material.
4. If evidence does not establish why something exists, say that the reason is not established. \
Never invent business intent, history, requirements or the meaning of a constant.
5. Cite sources with the labels in square brackets, for example [E3]. Only use labels that appear \
in the material. Never make up a label.
6. Be concise and concrete. Quote conditions and constants exactly as given.\
"""

EXPLAIN_SECTIONS = [
    "What this method does",
    "High-level flow",
    "Inputs",
    "Major processing stages",
    "Important rules",
    "Important dependencies",
    "Output",
    "What to be careful about",
    "Evidence and unknowns",
]


def render_plan_text(plan: ExplanationPlan) -> str:
    """The plan as compact text. Claims carry their level so the model keeps them apart."""
    lines = [
        f"Method: {plan.method_identity}",
        f"Purpose [{plan.purpose.level.upper()}, {plan.purpose.basis}]: {plan.purpose.text} "
        f"[{', '.join(plan.purpose.refs)}]",
        "Inputs:",
        *(f"  - {i}" for i in plan.input_summary),
        "Output:",
        *(f"  - {o}" for o in plan.output_summary),
        "Stages (in order):",
    ]
    for i, s in enumerate(plan.major_stages, 1):
        lines.append(
            f"  {i}. L{s.start_line}-{s.end_line} {s.title} -- {s.description}"
            f" [{', '.join(s.refs)}]"
        )
    if plan.data_transformations:
        lines += ["How values move:", *(f"  - {c}" for c in plan.data_transformations)]
    if plan.business_rule_candidates:
        lines.append("Business-rule candidates (patterns in the code, not confirmed rules):")
        lines += [
            f"  - L{r.lines} {r.text}"
            + (f" (constants: {', '.join(r.literals)})" if r.literals else "")
            + f" [{', '.join(r.refs)}]"
            for r in plan.business_rule_candidates
        ]
    if plan.dependencies:
        lines.append("Dependencies:")
        for d in plan.dependencies:
            extra = f" -- {d.purpose}" if d.purpose else (f" -- {d.note}" if d.note else "")
            lines.append(f"  - {d.name} ({d.kind}, L{', L'.join(map(str, d.lines))}){extra}")
    lines += [
        "Historical context (comments, tests, docs):",
        *(f"  - {h}" for h in plan.historical_context),
    ]
    if plan.risks:
        lines += ["Risks:", *(f"  - {r}" for r in plan.risks)]
    lines.append("Claims by evidence level:")
    for c in plan.claims:
        lines.append(f"  [{c.level.upper()}] {c.text} [{', '.join(c.refs)}]")
    return "\n".join(lines)


def explain_prompt(plan: ExplanationPlan, context: str) -> str:
    sections = "\n".join(f"{i}. {s}" for i, s in enumerate(EXPLAIN_SECTIONS, 1))
    return f"""\
Explain this method to a new teammate.

# Analysis (derived by static analysis, not by you)
{render_plan_text(plan)}

# Source and supporting material
{context}

# Answer format
Use exactly these section headings, in this order, as markdown headings:
{sections}

Guidance:
- "What this method does": two or three sentences of mental model, in business terms where the \
evidence supports it. Mark it as inference if it rests only on names and structure.
- "High-level flow": a short arrow diagram (one step per line, ↓ between steps).
- "Important rules": each rule with its condition, its constants, and where it is (cite). For \
every constant or condition whose origin the material does not explain, say its reason is not \
established.
- "Evidence and unknowns": state what the code establishes, what you inferred, and what the \
material does not establish.
"""


def trace_prompt(trace_text: str, context: str, subject: str) -> str:
    return f"""\
Explain how the data `{subject}` moves through the code, from where it comes to where it ends up.

# Deterministic trace (derived by static analysis)
{trace_text}

# Source and supporting material
{context}

# Answer format
Use these markdown headings in order: "Where it comes from", "How it changes", "Where it goes", \
"What to be careful about", "Evidence and unknowns".
Describe each step in terms of what it means for the data, not Java syntax. Cite with [E#] labels \
only. Say plainly where the trace stops and why (for example an external library call).
"""


def why_prompt(selection: str, evidence_text: str, context: str) -> str:
    return f"""\
A developer selected this code and asks: why does it do this?

# Selected code
{selection}

# Evidence gathered by static analysis (current method, callers, callees, tests, comments, docs)
{evidence_text}

# Source and supporting material
{context}

# Answer format
Use exactly three markdown headings in this order: "Confirmed", "Likely interpretation", "Unknown".
- Confirmed: what the code and the evidence establish, including any comment or test that states a \
reason. Cite each point.
- Likely interpretation: reasonable readings, each labelled as inference with the evidence \
behind it.
- Unknown: what the repository does not establish (for example why a specific constant was \
chosen).
If no comment, test or document states a reason, the reason is not established; say so. Do not \
invent a historical or business reason.
"""


def followup_messages(
    history: list[Message], question: str, context: str, summary: str
) -> list[Message]:
    """System prompt, then the conversation, with the current method context kept in view."""
    anchor = (
        "You are continuing a conversation about ONE method. The current material is below; "
        "answer from it, keep the same rules, and say so if the question needs something that "
        "is not in it.\n\n"
        f"# Current method\n{summary}\n\n# Source and supporting material\n{context}"
    )
    return [
        Message(role="system", content=SYSTEM_PROMPT),
        Message(role="user", content=anchor),
        Message(role="assistant", content="Understood. I will answer from this material only."),
        *history,
        Message(role="user", content=question),
    ]
