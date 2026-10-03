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
| 4+ | Call graph, control/data flow, rules, knowledge model, retrieval, Ollama, UI | planned |

Design notes: [Phases 0–2 and the AST wire format](docs/phase-0-2.md), [Phase 3 symbol resolution](docs/phase-3-symbol-resolution.md).

## Requirements

| Tool | Version | Used for |
|---|---|---|
| Python | 3.12 | backend |
| [uv](https://github.com/astral-sh/uv) | recent | Python env and dependencies |
| **JDK** | **17 or newer** | running the analyzer; the jar is compiled for Java 17 |
| Maven | 3.9+ | building the analyzer jar |
| Node.js | 20+ | frontend |
| Ollama | any | LLM and embeddings (needed from the LLM phases onward, not for Phases 0–2) |

macOS with Homebrew: `brew install uv openjdk@17 maven node`.

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

   Edit `.env` and set at least `JAVA_BIN` to a JDK 17+ `java` binary, for example
   `/opt/homebrew/opt/openjdk@17/bin/java`. On macOS the bare `java` is often a stub that fails, which
   is why this is explicit. If `JAVA_BIN` is left as `java`, `$JAVA_HOME/bin/java` is used when
   `JAVA_HOME` is set. `.env` is git-ignored.

4. **Build the Java analyzer** (produces `java-analyzer/target/java-analyzer.jar`)

   ```bash
   make analyzer
   ```

   The Makefile pins JDK 17 for the build through `JDK17_HOME`, even if Maven's default JDK is newer.
   Override the location if yours differs:

   ```bash
   make analyzer JDK17_HOME=/path/to/jdk17/Contents/Home
   ```

5. **Install frontend dependencies**

   ```bash
   cd frontend && npm install && cd ..
   ```

6. **Verify the install**

   ```bash
   make check
   ```

   This runs ruff, mypy and pytest. Expect all green (133 tests).

## Run

Open two terminals.

```bash
make backend      # API on http://localhost:8000
make frontend     # UI  on http://localhost:5173
```

Check it works:

```bash
curl http://localhost:8000/health           # {"status":"healthy"}
curl http://localhost:5173/health           # same, through the Vite proxy
```

Interactive API docs are at http://localhost:8000/docs. The frontend proxies `/health` and `/api` to
the backend; point it elsewhere with `BACKEND_URL=http://host:port make frontend`.

## Use it

### Index a repository (recommended: CLI)

Indexing parses every Java file and stores the result, which takes a while on a large repository, so
it is a CLI command rather than a blocking HTTP call.

```bash
make index SRC=/absolute/path/to/your/java/repo
# or: PYTHONPATH=backend .venv/bin/python -m app.cli index /path/to/repo [--force] [--batch-size N]
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

Quick, analysis-free file discovery: `make scan SRC=/path/to/repo` (counts of added, changed,
unchanged, removed and skipped files).

### Resolve symbols

After indexing, resolve every reference (types, names, fields, calls, constructors) to its target:

```bash
make resolve SRC=/absolute/path/to/your/java/repo          # summary
PYTHONPATH=backend .venv/bin/python -m app.cli resolve /path/to/repo --examples 3   # + examples
```

Each reference ends up `resolved`, `ambiguous` (candidates listed) or `unresolved` (with a reason);
nothing is guessed. The report shows counts per kind and status, how calls split between project
code, external (JDK/library) types, ambiguous and unresolved, the unresolved reasons ranked, and
example source locations for each reason. About 1 s for 1,700 files. Rules, the reason catalogue and
the known limits are in [docs/phase-3-symbol-resolution.md](docs/phase-3-symbol-resolution.md).
`resolve` exits with `4` if the repository has not been indexed yet.

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

### Inspect the AST of one file

```bash
.venv/bin/python scripts/dump_ast.py path/to/File.java --no-source
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
| `CHROMA_PATH` | `$DATA_ROOT/indexes/chroma` | vector store (later phases) |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server (later phases) |
| `OLLAMA_CHAT_MODEL` | `qwen2.5-coder:7b` | chat model, never hard-coded in code |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | embedding model |
| `LOG_LEVEL` | `INFO` | log verbosity |
| `MAX_CONTEXT_TOKENS` | `8000` | prompt budget (later phases) |
| `MAX_SOURCE_FILES` | `20000` | scanner file cap |
| `MAX_CALL_DEPTH` | `2` | call-graph traversal depth (later phases) |
| `ENABLE_GIT_ANALYSIS` / `ENABLE_TEST_ANALYSIS` | `false` / `true` | feature flags |
| `IGNORE_DIRS` | `target,build,.git,node_modules,generated,out,.idea,.gradle` | comma-separated, skipped when scanning |
| `MAX_FILE_BYTES` | `2000000` | larger files are skipped |
| `INDEX_BATCH_SIZE` | `50` | files per analyzer call; progress is saved after each batch |
| `JAVA_BIN` | `java` | JDK 17+ binary used to run the analyzer |
| `ANALYZER_JAR` | `java-analyzer/target/java-analyzer.jar` | analyzer jar location |
| `ANALYZER_TIMEOUT_SECONDS` | `300` | per-batch analyzer timeout |

## Development

| Command | Does |
|---|---|
| `make install` | `uv sync` plus `npm install` |
| `make analyzer` | rebuild the Java analyzer jar (rerun after changing `java-analyzer/`) |
| `make test` | pytest |
| `make lint` | ruff |
| `make typecheck` | mypy (strict) |
| `make check` | lint + typecheck + test |
| `make backend` / `make frontend` | dev servers |
| `make index SRC=...` / `make scan SRC=...` | index or scan a repository from the CLI |
| `make resolve SRC=...` | resolve symbols over the indexed repository |

The analyzer tests run against the real jar and **fail, not skip,** if the jar or a JDK 17+ is
missing, so a broken setup is visible. Frontend production build: `cd frontend && npm run build`.

### Project workflow

Work proceeds phase by phase: implement, add tests, run `make check`, demonstrate output, review, then
move on. Tests are never weakened to make them pass.

## Layout

```
backend/app/
  config.py, logging_setup.py, main.py
  cli.py          scan / index commands
  api/            health, repositories (scan, index, files), jobs
  scanner/        file discovery, hashing, incremental manifest (SQLite)
  analyzer/       ast_models.py (Pydantic), base.py (interface), java_parser.py (adapter),
                  store.py (persisted per-file results), symbol_table.py, symbol_resolver.py,
                  type_parser.py, jdk_types.py, resolution_models.py
  services/       repository_service.py, indexing_service.py, jobs.py (background jobs),
                  resolution_service.py
backend/tests/    pytest; fixtures/java holds small synthetic Java sources
java-analyzer/    Java 17 + JavaParser sidecar: stdin paths -> stdout NDJSON, one object per file
frontend/         React + TypeScript + Vite
scripts/          dump_ast.py
docs/             design notes
data/             runtime storage (git-ignored)
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `analyzer jar not found ... make analyzer` | run `make analyzer` |
| `cannot run 'java'` or `Unable to locate a Java Runtime` | set `JAVA_BIN` in `.env` to a JDK 17+ binary |
| `Java 17+ required, ... is Java N` | point `JAVA_BIN` at a newer JDK |
| `make analyzer` builds with the wrong JDK or fails | pass `JDK17_HOME=...`; `/usr/libexec/java_home -V` lists installed JDKs |
| Scan/index returns HTTP 400 (or CLI exit 2) | the path is not a directory, or neither `path` nor `SOURCE_ROOT` was given |
| `409` when starting an index | that repository is already being indexed; poll the job id in the response |
| CLI exit code 1 | some files hit `analyzer_error`; re-run, they are retried automatically |
| `resolve` exits 4 | run `index` first; analyses written by an older analyzer version are redone by the next `index` |
| Frontend shows "Backend: unreachable" | start `make backend` first; check `BACKEND_URL` |
| `IGNORE_DIRS` seems ignored | it must be comma-separated, not JSON |

## Privacy

No telemetry. Ollama only, on your machine. Source code is never written to logs. The scanner and
analyzer are read-only against the target repository.
