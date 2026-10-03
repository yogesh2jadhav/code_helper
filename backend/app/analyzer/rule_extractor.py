"""Business-rule *candidates* extracted deterministically from code patterns.

A candidate is a pattern in the code that often encodes a business rule: a null guard, a threshold,
a match against a literal, a default value, a validation that throws, a grouping, an aggregation, a
`switch` decision, a stream filter. Finding one is a FACT about the code; whether it really is a
business rule, and why it exists, is not established here. Candidates therefore say what the code
does and carry the literals involved, so later stages can state "the repository does not establish
why 14 was chosen" instead of inventing a reason.
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, Field

from app.analyzer.ast_models import Expression, Method
from app.analyzer.control_flow import ControlFlow, FlowNode, StreamPipeline
from app.analyzer.data_flow import literal_type

RuleKind = Literal[
    "validation",
    "null_handling",
    "default_value",
    "threshold",
    "literal_match",
    "comparison",
    "decision",
    "filter",
    "grouping",
    "aggregation",
    "switch",
    "pipeline",
]

_NUMERIC = {"int", "long", "float", "double"}
_OPERATOR_PHRASE = {
    ">": "is greater than",
    ">=": "is at least",
    "<": "is less than",
    "<=": "is at most",
    "==": "equals",
    "!=": "does not equal",
}
_AGGREGATE_NAMES = {
    "min": "minimum",
    "max": "maximum",
    "sum": "sum",
    "count": "count",
    "average": "average",
    "reduce": "reduction",
    "counting": "count",
    "summingInt": "sum",
    "summingLong": "sum",
    "summingDouble": "sum",
    "averagingInt": "average",
    "averagingLong": "average",
    "averagingDouble": "average",
    "minBy": "minimum",
    "maxBy": "maximum",
    "reducing": "reduction",
}
_VALIDATION_CALLS = {
    "requireNonNull",
    "checkArgument",
    "checkState",
    "checkNotNull",
    "notNull",
    "isTrue",
    "assertTrue",
    "assertNotNull",
    "orElseThrow",
}
_DEFAULT_CALLS = {
    "orElse",
    "orElseGet",
    "requireNonNullElse",
    "requireNonNullElseGet",
    "getOrDefault",
}
_NULL = re.compile(r"^(?P<a>.+?)\s*(?P<op>==|!=)\s*null$|^null\s*(?P<op2>==|!=)\s*(?P<b>.+)$", re.S)
_COMPARE = re.compile(r"^(?P<l>.+?)\s*(?P<op>>=|<=|==|!=|>|<)\s*(?P<r>.+)$", re.S)
_CONSTANT = re.compile(r"^[A-Z][\w.]*\.[A-Z][A-Z0-9_]+$")
_GENERIC = re.compile(r"\b[A-Z][\w.]*<[\w\s,.?\[\]&<>]*>")  # List<String>, Map<K, List<V>>, Foo<>
_NON_COMPARISON = (">>>=", "<<=", ">>=", ">>>", ">>", "<<", "->")  # shifts and lambda arrows
_LAMBDA_START = re.compile(r"^\(?[\w\s,]*\)?\s*->")
_LAMBDA = re.compile(r"^\(?\s*\w+\s*\)?\s*->\s*(?P<body>.+)$", re.S)


class SourceRef(BaseModel):
    file: str
    start_line: int
    end_line: int
    snippet: str = ""


class Atom(BaseModel):
    """One indivisible condition, e.g. `weight > 30`."""

    text: str
    kind: Literal["null", "threshold", "literal_match", "comparison", "test"]
    meaning: str
    literals: list[str] = Field(default_factory=list)


class Branch(BaseModel):
    when: str
    action: str
    line: int


class PipelineStep(BaseModel):
    step: str  # filter | map | group | aggregate | sort | collect | ...
    description: str
    line: int


class RuleCandidate(BaseModel):
    id: str
    method_id: str
    kind: RuleKind
    start_line: int
    end_line: int
    condition: str | None = None  # the condition as written
    meaning: str | None = None  # the same condition in plain words, built mechanically
    atoms: list[Atom] = Field(default_factory=list)
    scope: str | None = None  # what the rule applies to (a collection, a grouping key, ...)
    calculation: str | None = None
    action: str | None = None  # what happens when the condition holds
    otherwise: str | None = None  # what happens when it does not (else branch), if any
    decision: str | None = None  # switch selector
    branches: list[Branch] = Field(default_factory=list)  # switch cases
    steps: list[PipelineStep] = Field(default_factory=list)  # semantic stream pipeline
    literals: list[str] = Field(default_factory=list)  # constants whose origin is not established
    confidence: Literal["high", "medium"] = "medium"
    evidence: list[SourceRef] = Field(default_factory=list)

    @property
    def summary(self) -> str:
        parts = [self.kind.replace("_", " ")]
        if self.meaning:
            parts.append(f"when {self.meaning}")
        if self.calculation:
            parts.append(self.calculation)
        if self.scope and not self.meaning:
            parts.append(f"over {self.scope}")
        if self.action:
            parts.append(f"-> {self.action}")
        return ": ".join([parts[0], " ".join(parts[1:])]) if len(parts) > 1 else parts[0]


# ==== public API ==============================================================


def extract_rules(
    method: Method, method_id: str, flow: ControlFlow, file: str
) -> list[RuleCandidate]:
    ctx = _Context(method, method_id, file)
    nodes = list(flow.walk())
    seen_pipelines: set[int] = set()
    for node in nodes:
        if node.kind in ("if", "else_if"):
            ctx.decision(node)
        elif node.kind == "switch":
            ctx.switch(node.text or "", node.branches, node)
        elif node.kind in ("case", "default"):
            continue
        for pipeline in [*([node.stream] if node.stream else []), *node.header_streams]:
            if id(pipeline) not in seen_pipelines:
                seen_pipelines.add(id(pipeline))
                ctx.pipeline(pipeline, node)
        if node.kind == "return" and node.text:
            ctx.return_value(node)
        if node.kind in ("return", "throw") or node.children:
            ctx.switch_expression(node)
    ctx.expressions()
    return sorted(ctx.rules, key=lambda r: (r.start_line, r.kind, r.id))


# ==== condition parsing =======================================================


def split_condition(text: str) -> list[tuple[str, str]]:
    """Split at top-level `&&` / `||`: [(operator-before, atom), ...]; first operator is ''."""
    parts: list[tuple[str, str]] = []
    depth = 0
    quote = ""
    start = 0
    op = ""
    i = 0
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0 and text[i : i + 2] in ("&&", "||"):
            parts.append((op, text[start:i].strip()))
            op = text[i : i + 2]
            i += 1
            start = i + 1
        i += 1
    parts.append((op, text[start:].strip()))
    return [(o, _strip_parens(a)) for o, a in parts]


def _strip_parens(text: str) -> str:
    text = text.strip()
    while text.startswith("(") and text.endswith(")") and _balanced(text[1:-1]):
        text = text[1:-1].strip()
    return text


def _balanced(text: str) -> bool:
    depth = 0
    for c in text:
        depth += {"(": 1, ")": -1}.get(c, 0)
        if depth < 0:
            return False
    return depth == 0


def parse_atom(text: str) -> Atom:
    """Classify one condition and describe it in plain words."""
    raw = text.strip()
    negated = raw.startswith("!") and not raw.startswith("!=")
    body = _strip_parens(raw[1:]) if negated else raw
    atom = _parse_positive(body)
    if negated:
        return Atom(
            text=raw, kind=atom.kind, meaning=f"not ({atom.meaning})", literals=atom.literals
        )
    return atom.model_copy(update={"text": raw})


def _parse_positive(text: str) -> Atom:
    if _LAMBDA_START.match(text):  # a function value, not a condition
        return Atom(text=text, kind="test", meaning=text)
    m = _NULL.match(text)
    if m:
        op = m.group("op") or m.group("op2")
        subject = (m.group("a") or m.group("b") or "").strip()
        state = "missing" if op == "==" else "present"
        return Atom(text=text, kind="null", meaning=f"{subject} is {state}")
    if _COMPARE.match(_top_level_only(text)):  # an operator outside any call or parenthesis
        return _comparison(text)
    call = split_call(text)
    if call is not None:
        atom = _method_condition(text, *call)
        if atom is not None:
            return atom
    return Atom(text=text, kind="test", meaning=text)


def split_call(text: str) -> tuple[str, str, str] | None:
    """`recv.name(args)` -> (recv, name, args) for the *last* call, using parenthesis matching."""
    text = text.strip()
    if not text.endswith(")"):
        return None
    depth = 0
    for i in range(len(text) - 1, -1, -1):
        depth += {")": 1, "(": -1}.get(text[i], 0)
        if depth == 0:
            head = text[:i]
            m = re.search(r"\.(\w+)$", head)
            if m is None:
                return None
            return head[: m.start()].strip(), m.group(1), text[i + 1 : -1].strip()
    return None


def _top_level_only(text: str) -> str:
    """The text with parenthesised parts blanked, so operators inside calls are not matched."""
    text = _GENERIC.sub(lambda m: "_" * len(m.group()), text)  # generics are not comparisons
    for token in _NON_COMPARISON:
        text = text.replace(token, "_" * len(token))
    depth = 0
    out: list[str] = []
    quote = ""
    for c in text:
        if quote:
            out.append("_")
            if c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
            out.append("_")
        elif c in "([{":
            depth += 1
            out.append("_")
        elif c in ")]}":
            depth -= 1
            out.append("_")
        else:
            out.append(c if depth == 0 else "_")
    return "".join(out)


def _comparison(text: str) -> Atom:
    masked = _top_level_only(text)
    m = _COMPARE.match(masked)
    assert m is not None
    left = text[: m.end("l")].strip()
    right = text[m.start("r") :].strip()
    op = m.group("op")
    literals = [x for x in (left, right) if _is_constant(x)]
    kinds = [literal_type(x) for x in (left, right)]
    if op in ("<", ">", "<=", ">=") and any(k in _NUMERIC for k in kinds):
        kind: Literal["null", "threshold", "literal_match", "comparison", "test"] = "threshold"
    elif op in ("==", "!=") and literals:
        kind = "literal_match"
    else:
        kind = "comparison"
    return Atom(
        text=text, kind=kind, meaning=f"{left} {_OPERATOR_PHRASE[op]} {right}", literals=literals
    )


def _is_constant(text: str) -> bool:
    t = text.strip()
    return literal_type(t) is not None or bool(_CONSTANT.match(t))


def _method_condition(text: str, recv: str, name: str, args: str) -> Atom | None:
    if name in ("equals", "equalsIgnoreCase"):
        literals = [x for x in (recv, args) if _is_constant(x)]
        word = "equals" if name == "equals" else "equals (ignoring case)"
        return Atom(
            text=text,
            kind="literal_match" if literals else "comparison",
            meaning=f"{recv} {word} {args}",
            literals=literals,
        )
    if name in ("isAfter", "isBefore", "isEqual"):
        word = {"isAfter": "is after", "isBefore": "is before", "isEqual": "is the same as"}[name]
        return Atom(text=text, kind="comparison", meaning=f"{recv} {word} {args}")
    if name in ("isEmpty", "isBlank") and not args:
        return Atom(text=text, kind="test", meaning=f"{recv} is {name[2:].lower()}")
    if name in ("contains", "startsWith", "endsWith", "matches"):
        literals = [x for x in (args,) if _is_constant(x)]
        word = {
            "contains": "contains",
            "startsWith": "starts with",
            "endsWith": "ends with",
            "matches": "matches",
        }[name]
        return Atom(
            text=text,
            kind="literal_match" if literals else "test",
            meaning=f"{recv} {word} {args}",
            literals=literals,
        )
    if name in ("compareTo", "compare"):
        return Atom(text=text, kind="comparison", meaning=f"compare {recv} with {args}")
    return None


def explain_condition(condition: str) -> tuple[list[Atom], str]:
    parts = split_condition(condition)
    atoms = [parse_atom(a) for _, a in parts]
    meaning = atoms[0].meaning
    for (op, _), atom in zip(parts[1:], atoms[1:], strict=True):
        meaning += f" {'AND' if op == '&&' else 'OR'} {atom.meaning}"
    return atoms, meaning


# ==== extraction ==============================================================


class _Context:
    def __init__(self, method: Method, method_id: str, file: str) -> None:
        self.method = method
        self.method_id = method_id
        self.file = file
        self.rules: list[RuleCandidate] = []
        self._lines = method.source_text.split("\n")

    def add(
        self, kind: RuleKind, start: int, end: int, key: str, **fields: object
    ) -> RuleCandidate:
        digest = hashlib.sha1(f"{self.method_id}|{kind}|{start}|{key}".encode()).hexdigest()[:12]
        rule = RuleCandidate(
            id=digest,
            method_id=self.method_id,
            kind=kind,
            start_line=start,
            end_line=end,
            evidence=[self.ref(start, end)],
            **fields,  # type: ignore[arg-type]
        )
        self.rules.append(rule)
        return rule

    def ref(self, start: int, end: int) -> SourceRef:
        offset = self.method.start_line
        lo = max(0, start - offset)
        hi = min(len(self._lines), end - offset + 1)
        snippet = "\n".join(self._lines[lo:hi]) if lo < hi else ""
        return SourceRef(file=self.file, start_line=start, end_line=end, snippet=snippet[:600])

    # ---- decisions ---------------------------------------------------------------------------

    def decision(self, node: FlowNode) -> None:
        condition = node.text or ""
        atoms, meaning = explain_condition(condition)
        action = describe_actions(node.children)
        otherwise = describe_actions(node.branches[0].children) if node.branches else None
        if node.branches and node.branches[0].kind == "else_if":
            otherwise = f"otherwise test: {node.branches[0].text}"
        throws = any(c.kind == "throw" for c in node.children)
        kind = _classify(atoms, throws=throws, defaults=_is_default_assignment(node, atoms))
        self._add_condition(
            kind, node.start_line, node.end_line, condition, meaning, atoms, action, otherwise
        )

    def boolean_value(self, e: Expression, text: str, action: str) -> None:
        """A comparison/logical expression used as a value: `return a > 5 && b != null;`."""
        atoms, meaning = explain_condition(text)
        if all(a.kind == "test" for a in atoms):
            return  # a plain call or identifier, not a rule-like expression
        kind = _classify(atoms, throws=False, defaults=False)
        self._add_condition(kind, e.start_line, e.end_line, text, meaning, atoms, action, None)

    def _add_condition(
        self,
        kind: RuleKind,
        start: int,
        end: int,
        condition: str,
        meaning: str,
        atoms: list[Atom],
        action: str | None,
        otherwise: str | None,
    ) -> None:
        literals = [x for a in atoms for x in a.literals]
        strong = kind in (
            "validation",
            "null_handling",
            "default_value",
            "threshold",
            "literal_match",
        )
        self.add(
            kind,
            start,
            end,
            condition,
            condition=condition,
            meaning=meaning,
            atoms=atoms,
            action=action,
            otherwise=otherwise,
            literals=literals,
            confidence="high" if strong else "medium",
        )

    def switch(self, selector: str, cases: list[FlowNode], node: FlowNode) -> None:
        branches = [
            Branch(
                when=c.text if c.kind == "case" and c.text else "default",
                action=describe_actions(c.children) or "falls through",
                line=c.start_line,
            )
            for c in cases
        ]
        literals = [c.text for c in cases if c.text and _is_constant(c.text)]
        self.add(
            "switch",
            node.start_line,
            node.end_line,
            selector,
            decision=selector,
            meaning=f"behaviour depends on {selector}",
            branches=branches,
            literals=literals,
            confidence="high",
        )

    def return_value(self, node: FlowNode) -> None:
        if node.text and "?" not in _top_level_only(node.text) and "->" not in node.text:
            expr = next(
                (
                    e
                    for e in self.method.expressions
                    if e.start_line == node.start_line and e.text == node.text
                ),
                None,
            )
            anchor = expr or Expression(
                id=-1,
                kind="logical",
                start_line=node.start_line,
                end_line=node.end_line,
                text=node.text,
            )
            self.boolean_value(anchor, node.text, "the method returns this result")

    def switch_expression(self, node: FlowNode) -> None:
        cases = [c for c in node.children if c.kind in ("case", "default")]
        if not cases:
            return
        match = re.search(r"switch\s*\((?P<sel>.+?)\)\s*\{", node.text or "", re.S)
        self.switch(match.group("sel") if match else "?", cases, node)

    # ---- pipelines ---------------------------------------------------------------------------

    def pipeline(self, p: StreamPipeline, node: FlowNode) -> None:
        steps: list[PipelineStep] = []
        for op in p.ops:
            step = describe_op(op.name, op.category, op.args, op.collectors)
            if step:
                steps.append(PipelineStep(step=op.category, description=step, line=op.line))
            if (
                op.category == "filter"
                and op.name in ("filter", "takeWhile", "dropWhile")
                and op.args
            ):
                condition = lambda_body(op.args[0])
                atoms, meaning = explain_condition(condition)
                self.add(
                    "filter",
                    op.line,
                    op.line,
                    f"{p.source}|{condition}",
                    scope=p.source,
                    condition=condition,
                    meaning=meaning,
                    atoms=atoms,
                    action=f"keep elements of {p.source} where this holds",
                    literals=[x for a in atoms for x in a.literals],
                    confidence="high",
                )
            if op.category == "match" and op.args:  # anyMatch/allMatch/noneMatch(predicate)
                condition = lambda_body(op.args[0])
                atoms, meaning = explain_condition(condition)
                quantifier = op.name.replace("Match", "").lower()
                self.add(
                    "filter",
                    op.line,
                    op.line,
                    f"{p.source}|{op.name}|{condition}",
                    scope=p.source,
                    condition=condition,
                    meaning=meaning,
                    atoms=atoms,
                    action=f"true if {quantifier} element of {p.source} satisfies this",
                    literals=[x for a in atoms for x in a.literals],
                    confidence="high",
                )
            if op.category == "group":
                keys = _grouping_keys(op.args)
                self.add(
                    "grouping",
                    op.line,
                    op.line,
                    f"{p.source}|{keys}",
                    scope=p.source,
                    calculation=f"group {p.source} by {keys}",
                    meaning=f"elements are grouped by {keys}",
                    confidence="high",
                )
            for aggregate in _aggregations(op.name, op.category, op.collectors, op.args):
                self.add(
                    "aggregation",
                    op.line,
                    op.line,
                    f"{p.source}|{aggregate}",
                    scope=p.source,
                    calculation=aggregate,
                    confidence="high",
                )
        if sum(1 for s in steps if s.step != "source") >= 2:
            self.add(
                "pipeline",
                node.start_line,
                node.end_line,
                p.text,
                scope=p.source,
                steps=steps,
                meaning=" -> ".join(s.description for s in steps),
                confidence="high",
            )

    # ---- single expressions ------------------------------------------------------------------

    def expressions(self) -> None:
        for e in self.method.expressions:
            if (
                e.kind in ("variable_declaration", "assignment")
                and e.right
                and not ({"foreach", "for_init", "resource"} & set(e.tags))
                and "?" not in _top_level_only(e.right)
                and "->" not in e.right
            ):
                self.boolean_value(e, e.right, f"the result is stored in {e.name or ''}")
            if e.kind == "ternary":
                self.ternary(e)
            elif e.kind == "method_call":
                self.call(e)

    def ternary(self, e: Expression) -> None:
        parts = split_ternary(e.text)
        if parts is None:
            return
        cond, a, b = parts
        atoms, meaning = explain_condition(cond)
        default = len(atoms) == 1 and atoms[0].kind == "null" and _is_default_choice(atoms[0], a, b)
        literals = [x for at in atoms for x in at.literals]
        if (
            default
        ):  # the fallback of a default is a business constant; plain ternary results are not
            literals += [x for x in (a, b) if literal_type(x) is not None]
        self.add(
            "default_value" if default else "decision",
            e.start_line,
            e.end_line,
            e.text,
            condition=cond,
            meaning=meaning,
            atoms=atoms,
            action=f"{a} when true, else {b}",
            literals=literals,
            confidence="high" if default else "medium",
        )

    def call(self, e: Expression) -> None:
        name = e.name or ""
        scope = (e.scope or "").strip()
        args = e.arg_texts or []
        if name in _VALIDATION_CALLS:
            self.add(
                "validation",
                e.start_line,
                e.end_line,
                e.text,
                condition=e.text,
                meaning=f"{name} check on {', '.join(args) or scope}",
                action="fails when the check does not hold",
                confidence="high",
            )
        elif name in _DEFAULT_CALLS and args:
            self.add(
                "default_value",
                e.start_line,
                e.end_line,
                e.text,
                scope=scope or None,
                calculation=f"falls back to {args[0]} when absent",
                literals=[args[0]] if _is_constant(args[0]) else [],
                confidence="high",
            )
        elif name in ("min", "max") and scope in (
            "Math",
            "Collections",
            "Integer",
            "Long",
            "Double",
        ):
            word = _AGGREGATE_NAMES[name]
            self.add(
                "aggregation",
                e.start_line,
                e.end_line,
                e.text,
                calculation=f"{word} of {', '.join(args)}",
                confidence="high",
            )


# ==== helpers =================================================================


def _classify(atoms: list[Atom], *, throws: bool, defaults: bool) -> RuleKind:
    kinds = {a.kind for a in atoms}
    if throws:
        return "validation"
    if defaults:
        return "default_value"
    if kinds == {"null"}:
        return "null_handling"
    if "threshold" in kinds:
        return "threshold"
    if "literal_match" in kinds:
        return "literal_match"
    if "comparison" in kinds:
        return "comparison"
    return "decision"


def describe_actions(nodes: list[FlowNode], limit: int = 3) -> str | None:
    """What a branch does, in a few short phrases (None when the branch is empty)."""
    phrases = [p for n in nodes if (p := _describe(n))]
    if not phrases:
        return None
    more = f" (+{len(phrases) - limit} more)" if len(phrases) > limit else ""
    return "; ".join(phrases[:limit]) + more


def _describe(n: FlowNode) -> str | None:
    if n.kind == "return":
        return f"return {n.text}" if n.text else "return"
    if n.kind == "throw":
        return f"throw {n.text}"
    if n.kind == "continue":
        return "skip to the next iteration"
    if n.kind == "break":
        return "stop the loop/switch"
    if n.kind == "mutation":
        if n.operator and n.operator[0].isalpha():
            return f"update {n.target} via {n.operator}()"
        return f"set {n.text}"
    if n.kind == "call" and n.callee:
        return f"call {n.callee.name}()"
    if n.kind == "stream":
        return "run a stream pipeline"
    if n.kind in ("if", "else_if"):
        return f"decide on {n.text}"
    if n.kind == "loop":
        return f"loop {n.text}"
    if n.kind == "try":
        return "try/catch block"
    return None


def _is_default_assignment(node: FlowNode, atoms: list[Atom]) -> bool:
    if len(atoms) != 1 or atoms[0].kind != "null" or "missing" not in atoms[0].meaning:
        return False
    subject = atoms[0].meaning.split(" is ")[0].strip()
    first = node.children[0] if node.children else None
    return bool(
        first and first.kind == "mutation" and first.operator == "=" and first.target == subject
    )


def split_ternary(text: str) -> tuple[str, str, str] | None:
    """`c ? a : b` -> (c, a, b), honouring nesting and string literals."""
    masked = _top_level_only(text)
    q = masked.find("?")
    if q < 0:
        return None
    colon = masked.find(":", q)
    if colon < 0:
        return None
    return text[:q].strip(), text[q + 1 : colon].strip(), text[colon + 1 :].strip()


def _is_default_choice(atom: Atom, a: str, b: str) -> bool:
    subject = atom.meaning.split(" is ")[0].strip()
    present = "present" in atom.meaning
    kept, fallback = (a, b) if present else (b, a)
    return kept == subject and kept != fallback


def lambda_body(arg: str) -> str:
    m = _LAMBDA.match(arg.strip())
    if m:
        return _strip_braces(m.group("body"))
    if "::" in arg:
        owner, _, name = arg.partition("::")
        if (owner.strip(), name.strip()) == ("Objects", "nonNull"):
            return "element != null"
        if (owner.strip(), name.strip()) == ("Objects", "isNull"):
            return "element == null"
        return f"{arg}(element)"
    return arg


def _strip_braces(body: str) -> str:
    body = body.strip()
    if body.startswith("{") and body.endswith("}"):
        inner = body[1:-1].strip()
        return re.sub(r"^return\s+|;$", "", inner).strip()
    return body


def _grouping_keys(args: list[str]) -> str:
    if not args:
        return "?"
    first = args[0]
    first = re.sub(r"^Collectors\.(groupingBy|partitioningBy)\(", "", first)
    return first.split(",")[0].rstrip(")").strip() or "?"


def _aggregations(name: str, category: str, collectors: list[str], args: list[str]) -> list[str]:
    out: list[str] = []
    if category == "aggregate" and name in _AGGREGATE_NAMES:
        out.append(f"{_AGGREGATE_NAMES[name]}" + (f" via {', '.join(args)}" if args else ""))
    for c in collectors:
        if c in _AGGREGATE_NAMES:
            out.append(f"{_AGGREGATE_NAMES[c]} (collector {c})")
    return out


def describe_op(name: str, category: str, args: list[str], collectors: list[str]) -> str | None:
    arg = args[0] if args else ""
    if category == "source":
        return "stream the elements" if name in ("stream", "parallelStream") else None
    if category == "filter" and name in ("filter", "takeWhile", "dropWhile"):
        return f"keep elements where {lambda_body(arg)}"
    if category == "filter":
        return f"{name} {arg}".strip()
    if category == "map":
        return f"transform each element via {arg}" if arg else "transform each element"
    if category == "sort":
        return f"sort by {arg}" if arg else "sort"
    if category == "group":
        return f"group by {_grouping_keys(args)}"
    if category == "aggregate":
        return f"{_AGGREGATE_NAMES.get(name, name)}" + (
            f" ({', '.join(collectors)})" if collectors else ""
        )
    if category == "collect":
        return "collect into " + (", ".join(collectors) if collectors else name)
    if category == "match":
        return f"check whether {name.replace('Match', '')} element matches {lambda_body(arg)}"
    if category == "find":
        return f"take {name.replace('find', '').lower()} element"
    if category == "consume":
        return f"apply {arg} to each element"
    return None
