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
| 3+ | Symbol resolution, call graph, control/data flow, rules, knowledge model, retrieval, Ollama, UI | planned |

Design notes and the AST wire format: [docs/phase-0-2.md](docs/phase-0-2.md).

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

   This runs ruff, mypy and pytest. Expect all green (58 tests).

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

### Scan a Java repository

Pass a path in the request, or set `SOURCE_ROOT` in `.env` and send an empty body.

```bash
curl -X POST http://localhost:8000/api/repositories/scan \
  -H 'content-type: application/json' \
  -d '{"path": "/absolute/path/to/your/java/repo"}'
```

The response lists discovered files (path, hash, package, size) and what changed since the last
scan: `added`, `changed`, `unchanged`, `removed`, `skipped`. Scan again after editing code and only
the changed files are reported as needing re-analysis. List scanned repositories:

```bash
curl http://localhost:8000/api/repositories
```

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

The analyzer tests run against the real jar and **fail, not skip,** if the jar or a JDK 17+ is
missing, so a broken setup is visible. Frontend production build: `cd frontend && npm run build`.

### Project workflow

Work proceeds phase by phase: implement, add tests, run `make check`, demonstrate output, review, then
move on. Tests are never weakened to make them pass.

## Layout

```
backend/app/
  config.py, logging_setup.py, main.py
  api/            health, repositories (scan / list)
  scanner/        file discovery, hashing, incremental manifest (SQLite)
  analyzer/       ast_models.py (Pydantic), base.py (analyzer interface), java_parser.py (adapter)
  services/       repository_service.py
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
| Scan returns HTTP 400 | the path is not a directory, or neither `path` nor `SOURCE_ROOT` was given |
| Frontend shows "Backend: unreachable" | start `make backend` first; check `BACKEND_URL` |
| `IGNORE_DIRS` seems ignored | it must be comma-separated, not JSON |

## Privacy

No telemetry. Ollama only, on your machine. Source code is never written to logs. The scanner and
analyzer are read-only against the target repository.
