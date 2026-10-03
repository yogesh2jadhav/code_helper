"""Command line interface: `python -m app.cli scan|index [PATH]`.

Indexing is the long-running operation (thousands of files), so it lives here with a progress
display rather than behind a blocking HTTP request.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.analyzer.base import AnalyzerUnavailableError
from app.config import get_settings
from app.logging_setup import configure_logging
from app.services.indexing_service import IndexingService, Progress, ProgressFn
from app.services.repository_service import RepositoryService


def _progress_printer() -> ProgressFn:
    last = {"stage": ""}

    def show(p: Progress) -> None:
        if p.stage == "analyze" and p.total:
            end = "\n" if p.done >= p.total else ""
            print(f"\r[analyze] {p.done}/{p.total} files", end=end, file=sys.stderr, flush=True)
        elif p.stage != last["stage"] and p.stage != "analyze":
            print(f"[{p.stage}]", file=sys.stderr, flush=True)
        last["stage"] = p.stage

    return show


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="code-helper", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="discover Java files and report what changed (fast)")
    scan.add_argument("path", nargs="?", type=Path, help="repository root (default: SOURCE_ROOT)")

    index = sub.add_parser("index", help="scan, then analyze new/changed files (resumable)")
    index.add_argument("path", nargs="?", type=Path, help="repository root (default: SOURCE_ROOT)")
    index.add_argument("--force", action="store_true", help="re-analyze every file")
    index.add_argument("--batch-size", type=int, help="files per analyzer call (default: config)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging("WARNING" if settings.log_level.upper() == "INFO" else settings.log_level)

    try:
        if args.command == "scan":
            summary = RepositoryService(settings).scan(args.path).summary()
            print(summary.model_dump_json(indent=2))
            return 0

        result = IndexingService(settings).index(
            args.path, force=args.force, batch_size=args.batch_size,
            on_progress=_progress_printer(),
        )
    except (ValueError, NotADirectoryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except AnalyzerUnavailableError as exc:
        print(f"error: Java analyzer unavailable: {exc}", file=sys.stderr)
        return 3

    print(result.model_dump_json(indent=2))
    return 1 if result.analyzer_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
