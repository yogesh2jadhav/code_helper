# JDK 17 is pinned for building the analyzer; override with `make analyzer JDK17_HOME=...`
JDK17_HOME ?= /opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home
PY := .venv/bin

.PHONY: install analyzer test lint typecheck check backend frontend scan index

install:
	uv sync --python 3.12 --group dev
	cd frontend && npm install

analyzer:
	cd java-analyzer && JAVA_HOME=$(JDK17_HOME) mvn -q -B package

test:
	$(PY)/pytest

lint:
	$(PY)/ruff check backend scripts

typecheck:
	$(PY)/mypy

check: lint typecheck test

backend:
	$(PY)/uvicorn app.main:app --app-dir backend --reload --port 8000

frontend:
	cd frontend && npm run dev

# Usage: make index SRC=/path/to/java/repo   (SRC optional if SOURCE_ROOT is set in .env)
scan:
	PYTHONPATH=backend $(PY)/python -m app.cli scan $(SRC)

index:
	PYTHONPATH=backend $(PY)/python -m app.cli index $(SRC)
