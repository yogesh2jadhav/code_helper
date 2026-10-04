# Code Helper

Local developer knowledge-transfer system for Java repositories. A **code-understanding engine**
(static analysis → semantic model) feeds evidence to a **local LLM** (Ollama) that explains code
like an experienced maintainer. The LLM is the communication layer, not the source of truth.

Everything runs locally: no telemetry, no external API calls, source text is never logged, and the
scanned repository is only read.

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Bootstrap: config, logging, FastAPI skeleton, frontend skeleton, tests, lint | done |
| 1 | Repository scanner with incremental scanning | done |
| 2 | Java AST analysis (JavaParser, Java 17 sidecar) | done |
| 3 | Symbol resolution (types, identifiers, fields, calls, constructors, overloads) | done |
| 4–9 | Call graph, control flow, data flow, business-rule candidates, evidence, knowledge model | done |
| 10–12 | Hybrid retrieval (BM25 + vectors), context builder, explanation plan | done |
| 13–16 | Ollama client, prompts, response parser, explain / trace / why / follow-up chat | done |
| 17–19 | REST API, Code Explorer UI, Knowledge Model UI | done |
| 20–22 | Benchmark fixtures, enterprise fixture, evaluation framework | done |
| 23–26 | Incremental analysis, logging, error handling, privacy | done (see [what is enforced](docs/phase-4-26-overview.md#phases-23-26)) |

Design notes: [Phases 0–2 and the AST wire format](docs/phase-0-2.md), [Phase 3 symbol resolution](docs/phase-3-symbol-resolution.md), [Phases 4–26: knowledge engine, retrieval, LLM, UI, evaluation](docs/phase-4-26-overview.md).

## Requirements

| Tool | Version | Used for |
|---|---|---|
| Python | 3.12 | backend |
| [uv](https://github.com/astral-sh/uv) | recent | Python env and dependencies |
| **JDK** | **17 or newer** | running the analyzer; the jar is compiled for Java 17 |
| Maven | 3.9+ | building the analyzer jar |
| Node.js | 20+ | frontend |
| Ollama | any | LLM and embeddings (needed from the LLM phases onward, not for Phases 0–2) |

Install the tools:

- **macOS (Homebrew):** `brew install uv openjdk@17 maven node`
- **Windows (PowerShell):**
  ```powershell
  winget install --id=astral-sh.uv -e
  winget install EclipseAdoptium.Temurin.17.JDK
  winget install Apache.Maven
  winget install OpenJS.NodeJS.LTS
  ```
  Then **close and reopen PowerShell** so the new commands are on your PATH.

Ollama (for the LLM phases): install from https://ollama.com (Windows: `winget install Ollama.Ollama`),
then `ollama pull qwen2.5-coder:7b` and `ollama pull nomic-embed-text`.

All commands below use `python run.py <task>`, a small task runner that works the same on Windows,
macOS and Linux. No `make` is needed (see [Task runner](#task-runner)).

## Setup from a fresh clone

1. **Clone**

   ```bash
   git clone https://github.com/yogesh2jadhav/code_helper.git
   cd code_helper
   ```

2. **Install Python dependencies** (creates `.venv`)

   ```bash
   uv sync --python 3.12 --group dev
   ```

3. **Create your local config**

   ```bash
   cp .env.example .env
   ```

   (Windows PowerShell: `Copy-Item .env.example .env`.)

   Edit `.env` and set at least `JAVA_BIN` to a JDK 17+ `java` binary, for example
   `/opt/homebrew/opt/openjdk@17/bin/java` on macOS or
   `C:\Program Files\Eclipse Adoptium\jdk-17.0.x-hotspot\bin\java.exe` on Windows. On macOS the bare `java` is often a stub that fails, which
   is why this is explicit. If `JAVA_BIN` is left as `java`, `$JAVA_HOME/bin/java` is used when
   `JAVA_HOME` is set. `.env` is git-ignored.

4. **Build the Java analyzer** (produces `java-analyzer/target/java-analyzer.jar`)

   ```bash
   python run.py analyzer
   ```

   Maven uses whatever JDK `JAVA_HOME` points to. To build with a specific JDK 17 (for example when your
   default is newer), set `JDK17_HOME`:

   ```bash
   JDK17_HOME=/path/to/jdk17/Contents/Home python run.py analyzer     # macOS / Linux
   ```

   ```powershell
   $env:JDK17_HOME = "C:\Program Files\Eclipse Adoptium\jdk-17.0.x-hotspot"
   python run.py analyzer
   ```

5. **Install frontend dependencies**

   ```bash
   cd frontend && npm install && cd ..      # PowerShell: cd frontend; npm install; cd ..
   ```

6. **Verify the install**

   ```bash
   python run.py check
   ```

   This runs ruff, mypy and pytest. Expect all green (490+ tests).

## Run

Open two terminals.

```bash
python run.py backend      # API on http://localhost:8000
python run.py frontend     # UI  on http://localhost:5173
```

Check it works:

```bash
curl http://localhost:8000/health           # {"status":"healthy"}
curl http://localhost:5173/health           # same, through the Vite proxy
```

(PowerShell: `Invoke-RestMethod http://localhost:8000/health`.)

Interactive API docs are at http://localhost:8000/docs. The frontend proxies `/health` and `/api` to
the backend; point it elsewhere with `BACKEND_URL=http://host:port python run.py frontend` (PowerShell: `$env:BACKEND_URL="http://host:port"; python run.py frontend`).

## Use it

### Index a repository (recommended: CLI)

Indexing parses every Java file and stores the result, which takes a while on a large repository, so
it is a CLI command rather than a blocking HTTP call.

```bash
python run.py index /absolute/path/to/your/java/repo [--force] [--batch-size N]
# Windows path:  python run.py index C:\code\my-java-repo
```

It prints a live `[analyze] 840/1700 files` counter and a JSON summary. It is **incremental and
resumable**: results are saved after every batch and keyed by file hash, so

- re-running on an unchanged repo does no analysis and never starts the JVM (about 0.3 s for 1,700 files),
- after edits, only new/changed files are re-analyzed,
- if a run is interrupted (Ctrl+C, crash), the next run continues with the remaining files,
- files that failed because the analyzer itself broke (`analyzer_error`) are retried; genuine Java
  syntax errors (`parse_error`) are a stable result and are not.

`--force` re-analyzes everything. Exit codes: `0` ok, `1` some files hit analyzer errors, `2` bad
path/config, `3` analyzer unavailable (no jar or JDK < 17). Reference timing: 1,700 generated classes
indexed cold in about 9 s on a laptop, producing a ~17 MB SQLite database.

Quick, analysis-free file discovery: `python run.py scan /path/to/repo` (counts of added, changed,
unchanged, removed and skipped files).

### Resolve symbols

After indexing, resolve every reference (types, names, fields, calls, constructors) to its target:

```bash
python run.py resolve /absolute/path/to/your/java/repo          # summary
python run.py resolve /path/to/repo --examples 3             # + examples
```

Each reference ends up `resolved`, `ambiguous` (candidates listed) or `unresolved` (with a reason);
nothing is guessed. The report shows counts per kind and status, how calls split between project
code, external (JDK/library) types, ambiguous and unresolved, the unresolved reasons ranked, and
example source locations for each reason. About 1 s for 1,700 files. Rules, the reason catalogue and
the known limits are in [docs/phase-3-symbol-resolution.md](docs/phase-3-symbol-resolution.md).
`resolve` exits with `4` if the repository has not been indexed yet.

### Use the web UI

`python run.py backend` and `python run.py frontend`, then open http://localhost:5173.

- **Repository** page: start an index job (path, optional re-analyze), watch progress, see counts and
  whether the local model is ready.
- **Code Explorer**: class tree (filter, expand to methods) | source viewer (click a line, shift-click for
  a range) | Developer Mentor with tabs *Explain*, *Trace Data*, *Business Rules* (plus "why does this
  code do this?" for the selected lines), *Dependencies*, *Knowledge* (the raw model: purpose, inputs,
  outputs, control flow, data flow, rules, callers/callees, risks, unknowns, evidence). Citations in
  answers are links that jump to the cited source. Follow-up questions keep the method context.

Frontend checks: `python run.py frontend-test` (typecheck + vitest).

### Index through the REST API (background job)

The API starts the same work in the background and returns immediately; poll the job for progress.

```bash
# 1. start (returns 202 with a job id; 409 if this repo is already being indexed)
curl -X POST http://localhost:8000/api/repositories/index \
  -H 'content-type: application/json' -d '{"path": "/absolute/path/to/your/java/repo"}'

# 2. poll: state is running | succeeded | failed; stage/done/total give progress
curl http://localhost:8000/api/jobs/<job-id>
```

| Endpoint | Purpose |
|---|---|
| `POST /api/repositories/index` | start indexing (`path`, `force`, `batch_size`; empty body uses `SOURCE_ROOT`) |
| `GET /api/jobs/{id}`, `GET /api/jobs` | job progress, result summary or error |
| `POST /api/repositories/scan` | fast discovery, returns **counts only** |
| `GET /api/repositories` | scanned repositories |
| `GET /api/repositories/{id}/files?limit=100&offset=0` | paginated file list (limit up to 1000) |

Jobs are held in memory and disappear when the server restarts; the indexing itself is resumable, so
nothing but the progress display is lost.

### Build the knowledge model (part of `index`)

`index` now also builds the **knowledge model**: call graph, control flow, data flow, rule
candidates, evidence (comments, Javadoc, tests, README/docs) and risks/unknowns per method. It is
skipped when no file changed (fingerprint of the analyses), so re-indexing an unchanged repository is
instant. `--no-knowledge` stops after the AST stage.

### Search, explain, trace, why (CLI)

```bash
python run.py cli embed  --path /path/to/repo               # BM25 + embeddings (needs Ollama for vectors; --bm25-only otherwise)
python run.py cli search "free shipping threshold" --path /path/to/repo
python run.py cli explain ShippingCalculator.shippingFee --path /path/to/repo           # Developer Mentor answer
python run.py cli explain ShippingCalculator.shippingFee --path /path/to/repo --no-llm  # analysis-only, no Ollama
python run.py cli trace   ShippingCalculator.shippingFee --var fee --path /path/to/repo # where a value comes from / goes
python run.py cli why     ShippingCalculator.shippingFee --lines 8-10 --path /path/to/repo
```

`explain`, `trace` and `why` need `index` first. They talk to Ollama (`OLLAMA_CHAT_MODEL`); if it is
unreachable or the model is missing they print the analysis-only answer and say why. Answers carry
citations such as `[E3]`; labels the model invents are removed and reported as warnings. Facts,
inferences and "not established" statements are kept apart: the system will say *"the repository does
not establish why 30 was chosen"* rather than invent a reason.

### Evaluate quality

```bash
python run.py cli evaluate benchmarks/basic            # deterministic analysis
python run.py cli evaluate benchmarks/enterprise
python run.py cli evaluate benchmarks/basic --llm      # also score the model's answers
```

Each method is scored on purpose, structure, data flow, dependency and rule coverage, correctly
stated unknowns, unsupported claims, hallucination rate and evidence coverage. They are reported
separately; there is deliberately no single quality score. Details: [evaluation](docs/phase-4-26-overview.md#evaluation).

### REST API

Besides `/health` and the indexing endpoints above (`POST /api/repositories/index`,
`GET /api/jobs/{id}`), the knowledge API serves the UI (interactive docs at `/docs`):

| Endpoint | Returns |
|---|---|
| `GET /api/repositories`, `/{id}/stats`, `/{id}/classes`, `/{id}/methods?q=`, `/{id}/source?path=` | browsing |
| `GET /api/classes/{id}`, `/api/classes/{id}/methods` | class and its methods |
| `GET /api/methods/{id}`, `/knowledge`, `/rules`, `/callers`, `/callees` | method knowledge model |
| `POST /api/methods/{id}/explain` `{depth, include_tests, include_docs, use_llm}` | mentor explanation, evidence, unknowns |
| `POST /api/methods/{id}/trace` `{variable, depth, use_llm}` | data trace |
| `POST /api/methods/{id}/why` `{start_line, end_line, use_llm}` | confirmed / likely / unknown |
| `POST /api/conversations/{id}/messages` `{question}` | follow-up answer |
| `GET /api/source/{file_id}?start=&end=`, `/api/llm/status` | source lines, Ollama readiness |

### Inspect the AST of one file

```bash
python scripts/dump_ast.py path/to/File.java --no-source
```

Drop `--no-source` to include each method's full source text. The output is JSON with classes,
methods, statements, expressions, complexity and comments. Try it on the bundled fixtures in
`backend/tests/fixtures/java/`.

### Use the analyzer from Python

```python
from pathlib import Path
from app.analyzer import JavaParserAnalyzer
from app.config import get_settings

s = get_settings()
analyzer = JavaParserAnalyzer(s.java_bin, s.analyzer_jar, s.analyzer_timeout_seconds)
for parsed in analyzer.analyze_files([Path("Foo.java")]):
    print(parsed.status, [m.signature for t in parsed.types for m in t.methods])
```

Run it from the repo root with `PYTHONPATH=backend`. One result is returned per input file, in
order. A broken file gets `status="parse_error"` and never stops the others.

## Configuration

All settings come from environment variables or `.env`. Nothing machine-specific is hard-coded.

| Variable | Default | Meaning |
|---|---|---|
| `SOURCE_ROOT` | empty | default repository to scan |
| `DATA_ROOT` | `./data` | SQLite and index storage |
| `CHROMA_PATH` | `$DATA_ROOT/indexes/chroma` | vector store |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server |
| `OLLAMA_CHAT_MODEL` | `qwen2.5-coder:7b` | chat model, never hard-coded in code |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | embedding model |
| `LOG_LEVEL` | `INFO` | log verbosity |
| `MAX_CONTEXT_TOKENS` | `8000` | prompt context budget |
| `CONTEXT_BUDGET_TARGET/CALLEES/FLOW/EVIDENCE` | `0.35/0.25/0.20/0.20` | how that budget is shared |
| `EMBEDDING_PROVIDER` | `ollama` | `ollama` or `hash` (offline, deterministic) |
| `EMBED_BATCH_SIZE` / `RETRIEVAL_TOP_K` | `32` / `10` | embedding batch size, search results |
| `OLLAMA_TIMEOUT_SECONDS` / `TEMPERATURE` / `CONTEXT_WINDOW` | `120` / `0.2` / `8192` | generation settings |
| `MAX_SOURCE_FILES` | `20000` | scanner file cap |
| `MAX_CALL_DEPTH` | `2` | call-graph traversal depth |
| `ENABLE_GIT_ANALYSIS` / `ENABLE_TEST_ANALYSIS` | `false` / `true` | feature flags |
| `IGNORE_DIRS` | `target,build,.git,node_modules,generated,out,.idea,.gradle` | comma-separated, skipped when scanning |
| `MAX_FILE_BYTES` | `2000000` | larger files are skipped |
| `INDEX_BATCH_SIZE` | `50` | files per analyzer call; progress is saved after each batch |
| `JAVA_BIN` | `java` | JDK 17+ binary used to run the analyzer |
| `ANALYZER_JAR` | `java-analyzer/target/java-analyzer.jar` | analyzer jar location |
| `ANALYZER_TIMEOUT_SECONDS` | `300` | per-batch analyzer timeout |

## Development

### Task runner

`python run.py` lists the tasks (`python run.py <task> [args]`). On Windows the virtualenv tools live in
`.venv\Scripts` rather than `.venv/bin`; the runner handles that.

| Command | Does |
|---|---|
| `python run.py install` | `uv sync` plus `npm install` |
| `python run.py analyzer` | rebuild the Java analyzer jar (rerun after changing `java-analyzer/`) |
| `python run.py test` | pytest |
| `python run.py lint` | ruff |
| `python run.py typecheck` | mypy (strict) |
| `python run.py check` | lint + typecheck + test |
| `python run.py backend` / `frontend` | dev servers |
| `python run.py index PATH` / `scan PATH` / `resolve PATH` | index, scan or resolve a repository |
| `python run.py cli <command> ...` | any CLI command, e.g. `cli explain Class.method --path REPO` |
| `python run.py frontend-test` | frontend typecheck + vitest |

The analyzer tests run against the real jar and **fail, not skip,** if the jar or a JDK 17+ is
missing, so a broken setup is visible. Frontend production build: `cd frontend && npm run build`.

### Project workflow

Work proceeds phase by phase: implement, add tests, run `python run.py check`, demonstrate output, review, then
move on. Tests are never weakened to make them pass.

## Layout

```
backend/app/
  config.py, logging_setup.py, main.py
  cli.py          scan / index / resolve / embed / search / explain / trace / why / evaluate
  api/            health, repositories (scan, index, files), jobs, knowledge (browse, explain, trace, why, chat)
  scanner/        file discovery, hashing, incremental manifest (SQLite)
  analyzer/       ast_models.py (Pydantic), base.py (interface), java_parser.py (adapter),
                  store.py (persisted per-file results), symbol_table.py, symbol_resolver.py,
                  type_parser.py, jdk_types.py, resolution_models.py
  knowledge/      builder (call graph + flows + rules + evidence -> model), store, source reader
  retrieval/      BM25, embeddings, Chroma, hybrid search (reciprocal rank fusion), indexer
  context/        token-budgeted context builder, citations, explanation plan
  llm/            Ollama client, prompts, response parser
  explain/        explanation service (explain/trace/why/chat), mentor answer format
  evaluation/     benchmark format, metrics, runner, reports
  services/       repository, indexing, jobs (background), resolution, knowledge, pipeline,
                  retrieval, trace, why
backend/tests/    pytest; fixtures/java and fixtures/project hold small synthetic Java sources
benchmarks/       evaluation datasets: basic (fixtures A-J), enterprise (synthetic, no comments)
java-analyzer/    Java 17 + JavaParser sidecar: stdin paths -> stdout NDJSON, one object per file
frontend/         React + TypeScript + Vite
scripts/          dump_ast.py
docs/             design notes
data/             runtime storage (git-ignored)
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `analyzer jar not found ... run.py analyzer` | run `python run.py analyzer` |
| `uv` / `mvn` / `npm` "not recognized" (Windows) | install it (see Requirements) and reopen PowerShell so PATH refreshes |
| `cannot run 'java'` or `Unable to locate a Java Runtime` | set `JAVA_BIN` in `.env` to a JDK 17+ binary |
| `Java 17+ required, ... is Java N` | point `JAVA_BIN` at a newer JDK |
| `python run.py analyzer` builds with the wrong JDK or fails | pass `JDK17_HOME=...`; `/usr/libexec/java_home -V` lists installed JDKs |
| Scan/index returns HTTP 400 (or CLI exit 2) | the path is not a directory, or neither `path` nor `SOURCE_ROOT` was given |
| `409` when starting an index | that repository is already being indexed; poll the job id in the response |
| CLI exit code 1 | some files hit `analyzer_error`; re-run, they are retried automatically |
| `resolve` exits 4 | run `index` first; analyses written by an older analyzer version are redone by the next `index` |
| Frontend shows "backend unreachable" | start `python run.py backend` first; check `BACKEND_URL` |
| Explanations say "LLM generation unavailable" | start Ollama and `ollama pull` the models named in `.env`; the Repository page shows the status |
| `explain` exits 4 | run `index` first; `explain` takes `Class.method`, `Class#method(sig)` or a unique method name |
| Search says vector search is not configured | run `embed`; with Ollama down, `--bm25-only` still works |
| `IGNORE_DIRS` seems ignored | it must be comma-separated, not JSON |

## Privacy

No telemetry. Ollama only, on your machine. Source code is never written to logs. The scanner and
analyzer are read-only against the target repository.
