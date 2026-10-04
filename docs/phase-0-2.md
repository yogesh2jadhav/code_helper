# Phases 0–2: design notes

## Why a Java sidecar

The spec asks for JavaParser, which is a Java library, while the application layer is Python. The
parser therefore runs as a small **Java 17** process (`java-analyzer/`) that reads absolute file
paths from stdin and writes **one JSON object per file** (NDJSON) to stdout. The Python adapter
(`analyzer/java_parser.py`) batches many files into a single JVM launch, then validates each line
against the Pydantic models in `analyzer/ast_models.py`.

* A parse failure in one file yields `status: "parse_error"` for that file only. Partial ASTs from
  recovered parses are deliberately discarded so they are never mistaken for facts.
* A crash/timeout of the JVM marks every file without a result `analyzer_error`; the batch call
  itself never raises for per-file problems. Only "cannot run at all" (missing jar, JDK < 17) raises
  `AnalyzerUnavailableError`.
* `analyzer/base.py` defines the `JavaAnalyzer` protocol so Eclipse JDT (or anything else) can be
  swapped in later without touching callers.

## What the AST contains (Phase 2)

Per file: package, imports, and types (class / interface / enum / record / annotation) with
superclass, interfaces, annotations, type parameters, enum constants, record components, fields,
constructors (incl. compact record constructors), methods and nested types.

Per method: name, signature (`name(Type,Type...)`), visibility, static/final/abstract, return type,
parameters, annotations, throws, start/end line, exact `source_text` (whole lines), cyclomatic
complexity, max nesting depth, comments (as evidence), and two flat lists linked by ids:

* `statements`: `if else_if else switch case default for foreach while do try catch finally return
  throw break continue`. `parent_id` gives the tree; `depth` is the number of enclosing control
  structures. else-if chains stay at the depth of the first `if`.
* `expressions`: `method_call field_access assignment object_creation lambda method_ref ternary
  comparison null_check logical switch_expr variable_declaration`, each tied to its nearest
  enclosing recorded statement (`statement_id`).

Lists are emitted in AST traversal order, which is *not* always source order (JavaParser visits a
`for`'s update clause before its body). Use `start_line` / `parent_id` for ordering decisions.

### Tags are syntactic heuristics

`stream_source`, `stream_op`, `collector`, `optional`, `predicate`, `comparison`, `null_check`,
`anonymous_class`, `initialized` come from call-chain shape and well-known names, not from types.
For example `x.map(...)` is only tagged `optional` when the chain starts at `Optional.*`. They stay
syntactic hints even after Phase 3: symbol resolution (see
[phase-3-symbol-resolution.md](phase-3-symbol-resolution.md)) resolves call *targets*, but it has
no JDK member signatures, so it cannot turn these tags into type facts.

## Known limits (deliberate, for later phases)

* Symbol resolution happens after parsing, in Python: see [phase-3-symbol-resolution.md](phase-3-symbol-resolution.md).
* Methods of anonymous/local classes are folded into the enclosing method's statements/expressions,
  not extracted as separate methods. Enum-constant class bodies and initializer blocks are skipped.
* Operators `!`, casts, `instanceof` and array access are not recorded as expressions yet.
* Parse results are persisted as raw AST JSON in `file_analysis` (keyed by file hash) so indexing is
  incremental and resumable. The normalized knowledge-model tables arrive with Phase 10.
  Invalidation is file-level only; dependent-file invalidation comes later.
