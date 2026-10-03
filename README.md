# Code Helper

Local developer knowledge-transfer system for Java repositories. A **code-understanding engine**
(static analysis → semantic model) feeds evidence to a **local LLM** (Ollama) that explains code
like an experienced maintainer. The LLM is the communication layer, not the source of truth.

Status: **Phases 0–2** (bootstrap, repository scanner, Java AST analysis). See
[docs/phase-0-2.md](docs/phase-0-2.md) for design notes and the AST wire format.

## Requirements

- Python 3.12 and [uv](https://github.com/astral-sh/uv)
- **JDK 17+** (the analyzer is built for and runs on Java 17) and Maven 3.9+
- Node 20+ (frontend)
- Ollama (later phases)

## Setup

```bash
uv sync --python 3.12 --group dev      # backend deps into .venv
cp .env.example .env                   # then edit: set JAVA_BIN to a JDK 17+ binary
make analyzer                          # builds java-analyzer/target/java-analyzer.jar with JDK 17
cd frontend && npm install
```

`make analyzer` pins JDK 17 through `JDK17_HOME` (default is the Homebrew `openjdk@17` path); override
with `make analyzer JDK17_HOME=/path/to/jdk17`. `JAVA_BIN` in `.env` tells the Python side which
`java` to run the jar with (a bare `java` falls back to `$JAVA_HOME/bin/java`).

## Run

```bash
make backend     # http://localhost:8000  (GET /health)
make frontend    # http://localhost:5173  (proxies /health and /api to the backend)
```

## Quality

```bash
make check       # ruff + mypy + pytest
```

Analyzer tests run against the real jar and **fail** (not skip) if it or a JDK 17+ is missing.

## Inspect the AST of one file

```bash
.venv/bin/python scripts/dump_ast.py path/to/File.java --no-source
```

## Layout

```
backend/app/
  config.py, logging_setup.py, main.py
  api/            health, repositories (scan / list)
  scanner/        file discovery, hashing, incremental manifest (SQLite)
  analyzer/       ast_models.py (Pydantic), base.py (analyzer interface), java_parser.py (adapter)
  services/       repository_service.py
java-analyzer/    Java 17 + JavaParser sidecar; stdin paths -> stdout NDJSON (one object per file)
frontend/         React + TypeScript + Vite
backend/tests/    pytest; fixtures/java holds small synthetic Java sources
```

Privacy: no telemetry, no external calls, source text is never logged, the repository is only read.
