# JDK 17 is pinned for building the analyzer; override with `make analyzer JDK17_HOME=...`
JDK17_HOME ?= /opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home
PY := .venv/bin

.PHONY: install analyzer test lint typecheck check backend frontend

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
