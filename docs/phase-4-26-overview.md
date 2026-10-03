# Phases 4–26: knowledge engine, retrieval, LLM, UI, evaluation

How the pieces fit, and the rules each one keeps. Phase 3 (symbol resolution) is in
[phase-3-symbol-resolution.md](phase-3-symbol-resolution.md).

```
scan → AST (JavaParser sidecar) → symbol resolution → call graph / control flow / data flow
     → rule candidates + evidence → knowledge model (SQLite)
     → hybrid retrieval (BM25 + vectors) → context builder → explanation plan → LLM → answer
```

The LLM only *communicates* what the analysis established. Everything it is given is derived
deterministically, cited, and labelled fact / inference / unknown.

## Knowledge model (phases 4–9)

Per method: purpose (and whether it came from a comment, the name, or structure), inputs with where
their values come from (callers, tests), output, call graph edges (resolved / ambiguous / unresolved,
each non-resolved edge carries a reason), control-flow tree, data-flow edges and variable lifecycles,
**business-rule candidates**, evidence (comments, Javadoc, tests, README/docs), risks, unknowns.

- *Rule candidates* are patterns (threshold, validation, default, null handling, literal match, switch,
  filter, grouping, aggregation, ternary decisions). They are never called business rules; whether they
  are is stated as not established unless something author-written says so.
- *Unknowns* are first-class: "the repository does not establish why 30 was chosen".
- The model is stored as compressed JSON documents (`KNOWLEDGE_VERSION`) and rebuilt only when the
  fingerprint of the file analyses changes.

## Retrieval and context (phases 10–12)

Hybrid search fuses BM25 and vector rankings with reciprocal rank fusion. If vectors are unavailable
(Ollama or Chroma down) search degrades to BM25 and says so. The context builder fills a token budget
in fixed shares (`CONTEXT_BUDGET_*`): the target method first, then callees, flow, evidence; sections
are truncated, never silently dropped (what was omitted is reported). The *explanation plan* is the
deterministic skeleton of the answer: stages, rules, dependencies, unknowns, and numbered citations
`E1…En`.

## LLM (phases 13–16)

`OllamaClient` (chat model from config, typed errors for unreachable / timeout / model missing),
prompts that forbid inventing intent, and a parser that checks the nine required sections. After
generation: citation labels that do not exist are removed and reported; missing sections are reported.
If Ollama fails, the analysis-only answer (the same nine sections, built from the plan) is returned
with a note saying why. `trace` follows a variable upstream (callers, assignments), within the method,
and downstream (callees, returns) to a depth limit and states where the trace stops. `why` sorts the
evidence for selected lines into **confirmed** (the code, or an author-written comment/test/doc),
**likely** (labelled inference) and **unknown**.

## API and UI (phases 17–19)

See the README for the endpoint table. The UI is React + TypeScript; model output is rendered through a
small markdown subset that only produces React text nodes (no HTML injection). Source can only be read
through repository-relative paths that stay inside the repository root.

## Fixtures (phases 20–21)

`benchmarks/basic` has one small class per behaviour: A simple method with a comment, B nested
if/else, C for loop, D stream pipeline, E switch, F several methods (call graph), G data transformation,
H business-rule-like thresholds, I zero comments, J an unexplained magic number (the system must say
"not established"). `benchmarks/enterprise` is fully synthetic billing code with **no comments**: one
long method (loops, a stream with grouping and aggregation, nested conditions, a dozen helper calls,
several unexplained constants) plus three small rule-bearing classes. Nothing in them is taken from
any user code.

## Evaluation

`python -m app.cli evaluate benchmarks/<name> [--llm] [--json]`. Each benchmark is `expected.json` plus
a source tree. For every method the expectations were **written by hand from the code** (never from the
system's output): purpose words/basis/level, inputs, output type, top-level stages, rules,
dependencies, important data flow, and unknowns that must be stated.

| Metric | Definition |
|---|---|
| Purpose coverage | expected purpose words, basis and fact/inference level found |
| Structural coverage | expected inputs, output type and stages found (extra stages listed) |
| Data-flow coverage | expected "value reaches target" pairs reachable in the recorded data-flow edges (an argument also flows into the result of the call it is passed to) |
| Dependency coverage | expected callees *identified* (an unresolved call does not count); extra identified callees listed |
| Rule coverage | expected rule candidates (kind, literal or text) found; extra candidates counted |
| Unknowns correctly stated | expected statements of ignorance present in the analysis or answer |
| Unsupported claims | in the answer: backticked identifiers and numbers (list numbering and `L12` references excluded) that appear nowhere in the method source, the plan, the evidence or the callee/parameter names |
| Hallucination rate | unsupported claims ÷ claims checked |
| Invalid citations | citation labels that do not exist (removed from the final answer, counted from the warning) |
| Evidence coverage | bullet/numbered claim lines in the claim sections that carry a valid citation |

Reports show every metric per method and in total; **there is no combined score**. The deterministic
mode measures the analysis; `--llm` measures the model's wording on top of it.

Measured on the shipped benchmarks (analysis mode): structure, data flow, purpose and unknowns are 100%;
dependency coverage is 92% on `basic` (the stream lambda `o -> o.isCancelled()` stays unresolved because
lambda parameter types are not inferred) and rule coverage is 92% on `enterprise` (a comparison inside a
lambda, `c -> c.getAmount() > 14`, is not extracted as a threshold). Both are known gaps and are asserted
in `backend/tests/evaluation/test_evaluation.py`; closing them should change those assertions.

## Phases 23–26

- **Incremental analysis.** Files are keyed by content hash. Unchanged files skip parsing and never
  start the JVM; changed files are re-parsed; the knowledge model is rebuilt only if the set of
  analyses changed (v1 invalidation is file-level, and a rebuild is the unit). The retrieval index is
  updated for changed documents only. Reference: 1,704 files, cold index 8.6 s, warm pipeline 0.5 s.
- **Logging.** Structured `key=value` events for every stage: `repository_scan_started/completed`,
  `java_file_parsed`, `symbol_resolution_completed`, `call_graph_built`, `control_flow_built`,
  `data_flow_built`, `rule_candidates_extracted`, `knowledge_model_built`, `retrieval_started/completed`,
  `context_built`, `llm_request_started/completed`, `explanation_completed`, with repository, counts and
  duration. A test runs the whole path and fails if any event is missing, or if source text appears in
  any log record.
- **Error handling.** A malformed Java file is marked `parse_error` and the rest continue (tested through
  to the knowledge model); unresolved calls are stored with reasons; Ollama unavailable returns the
  analysis-only answer; Chroma/embedding failure degrades search to BM25; the context builder enforces
  limits for huge methods.
- **Privacy.** No telemetry (Chroma telemetry disabled), the only network peer is the configured
  Ollama host (a test asserts this), no source in logs, `DATA_ROOT` configurable, the analyzed
  repository is only read, and nothing modifies source code.
