#!/usr/bin/env python3
"""Cross-platform task runner (Windows, macOS, Linux). Same tasks as the Makefile, no `make` needed.

    python run.py <task> [args...]        e.g.  python run.py check
                                                python run.py index C:\\path\\to\\java\\repo
Run `python run.py` to list tasks.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BIN = ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin")


def tool(name: str) -> str:
    exe = BIN / (name + (".exe" if os.name == "nt" else ""))
    return str(exe)


def run(cmd: list[str], cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(cmd), flush=True)
    full_env = {**os.environ, **(env or {})}
    # `npm`, `mvn` are .cmd shims on Windows and need the shell to resolve.
    code = subprocess.call(cmd, cwd=cwd, env=full_env, shell=(os.name == "nt"))
    if code != 0:
        raise SystemExit(code)


def cli(command: str, args: list[str]) -> None:
    run(
        [tool("python"), "-m", "app.cli", command, *args],
        env={"PYTHONPATH": str(ROOT / "backend")},
    )


def analyzer(_: list[str]) -> None:
    env = {}
    if os.environ.get("JDK17_HOME"):
        env["JAVA_HOME"] = os.environ["JDK17_HOME"]  # same override as `make analyzer JDK17_HOME=`
    run(["mvn", "-q", "-B", "package"], cwd=ROOT / "java-analyzer", env=env)


def check(_: list[str]) -> None:
    run([tool("ruff"), "check", "backend", "scripts"])
    run([tool("mypy")])
    run([tool("pytest")])


TASKS: dict[str, tuple[str, Callable[[list[str]], None]]] = {
    "install": (
        "uv sync and npm install",
        lambda _: (
            run(["uv", "sync", "--python", "3.12", "--group", "dev"]),
            run(["npm", "install"], cwd=ROOT / "frontend"),
        )[-1],
    ),
    "analyzer": ("build the Java analyzer jar (set JDK17_HOME to override)", analyzer),
    "test": ("pytest", lambda a: run([tool("pytest"), *a])),
    "lint": ("ruff", lambda _: run([tool("ruff"), "check", "backend", "scripts"])),
    "typecheck": ("mypy (strict)", lambda _: run([tool("mypy")])),
    "check": ("lint + typecheck + test", check),
    "backend": (
        "API on :8000",
        lambda _: run(
            [tool("uvicorn"), "app.main:app", "--app-dir", "backend", "--reload", "--port", "8000"]
        ),
    ),
    "frontend": ("UI on :5173", lambda _: run(["npm", "run", "dev"], cwd=ROOT / "frontend")),
    "frontend-test": (
        "frontend typecheck + vitest",
        lambda _: (
            run(["npm", "run", "typecheck"], cwd=ROOT / "frontend"),
            run(["npm", "test"], cwd=ROOT / "frontend"),
        )[-1],
    ),
    "cli": (
        "any CLI command: cli explain Class.method --path REPO",
        lambda a: cli(a[0], a[1:]) if a else print("usage: python run.py cli <command> [args]"),
    ),
    "scan": ("scan [PATH]", lambda a: cli("scan", a)),
    "index": ("index [PATH] [--force]", lambda a: cli("index", a)),
    "resolve": ("resolve [PATH]", lambda a: cli("resolve", a)),
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in TASKS:
        print(__doc__)
        for name, (help_text, _) in TASKS.items():
            print(f"  {name:<14}{help_text}")
        return 0 if not argv else 2
    TASKS[argv[0]][1](argv[1:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
