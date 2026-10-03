"""Command line interface: `python -m app.cli scan|index [PATH]`.

Indexing is the long-running operation (thousands of files), so it lives here with a progress
display rather than behind a blocking HTTP request.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.analyzer.base import AnalyzerUnavailableError
from app.config import get_settings
from app.explain.explanation_service import AnswerInfo, ExplainOptions, ExplanationService
from app.knowledge.source import SourceReader
from app.knowledge.store import KnowledgeStore
from app.llm.ollama_client import OllamaClient
from app.logging_setup import configure_logging
from app.services.indexing_service import Progress, ProgressFn
from app.services.pipeline_service import PipelineService
from app.services.repository_service import RepositoryService
from app.services.resolution_service import NotIndexedError, ResolutionService
from app.services.retrieval_service import RetrievalService
from app.services.trace_service import VariableNotFoundError


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
    index.add_argument(
        "--no-knowledge",
        action="store_true",
        help="stop after analysis; skip building the knowledge model",
    )

    resolve = sub.add_parser(
        "resolve", help="resolve symbols over the indexed repository and report the outcome"
    )
    resolve.add_argument(
        "path", nargs="?", type=Path, help="repository root (default: SOURCE_ROOT)"
    )
    resolve.add_argument(
        "--examples",
        type=int,
        default=0,
        metavar="N",
        help="also show N example references per unresolved/ambiguous reason",
    )

    embed = sub.add_parser(
        "embed", help="build the search index (BM25 + embeddings); needs `index` first"
    )
    embed.add_argument("path", nargs="?", type=Path, help="repository root (default: SOURCE_ROOT)")
    embed.add_argument(
        "--bm25-only", action="store_true", help="skip embeddings (no Ollama needed)"
    )

    search = sub.add_parser("search", help="hybrid search over the indexed repository")
    search.add_argument("query")
    search.add_argument("--path", type=Path, help="repository root (default: SOURCE_ROOT)")
    search.add_argument("-k", type=int, default=0, help="number of results (default: config)")
    search.add_argument("--type", action="append", dest="types", help="restrict to a document type")
    search.add_argument("--class", dest="class_name", help="restrict to a class (fully qualified)")
    search.add_argument("--bm25-only", action="store_true", help="do not use vector search")

    for name, help_text in (
        ("explain", "explain a method like a senior teammate (local LLM, analysis fallback)"),
        ("trace", "trace how a variable's data moves through the code"),
        ("why", "why does this code do what it does? (confirmed / likely / unknown)"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("method", help="Class.method, Class#method(sig), a method name, or an id")
        p.add_argument("--path", type=Path, help="repository root (default: SOURCE_ROOT)")
        p.add_argument("--no-llm", action="store_true", help="print the analysis-only answer")
        p.add_argument("--json", action="store_true", dest="as_json", help="print the full result")
        if name == "explain":
            p.add_argument("--depth", type=int, default=1, help="how many call levels to include")
        if name == "trace":
            p.add_argument("--var", required=True, help="variable, parameter or field to trace")
            p.add_argument("--depth", type=int, default=2)
        if name == "why":
            p.add_argument("--lines", help="selection as START-END (default: the whole method)")
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

        if args.command == "embed":
            service = RetrievalService(settings, use_vectors=not args.bm25_only)
            built = service.index(
                args.path,
                lambda stage, done, total: print(
                    f"\r[{stage}] {done}/{total}",
                    end="" if done < total else "\n",
                    file=sys.stderr,
                    flush=True,
                ),
            )
            print(built.model_dump_json(indent=2))
            return 1 if built.vector_error else 0

        if args.command == "search":
            service = RetrievalService(settings, use_vectors=not args.bm25_only)
            found = service.retriever().search(
                service.repository_id(args.path),
                args.query,
                k=args.k or settings.retrieval_top_k,
                types=args.types,
                class_name=args.class_name,
            )
            print(
                json.dumps(
                    {
                        "degraded": found.degraded,
                        "degraded_reason": found.degraded_reason,
                        "hits": [
                            {
                                "score": round(h.score, 4),
                                "type": h.doc.type,
                                "file": h.doc.file_path,
                                "lines": f"{h.doc.line_start}-{h.doc.line_end}",
                                "class": h.doc.class_name,
                                "method": h.doc.method_name,
                                "bm25_rank": h.bm25_rank,
                                "vector_rank": h.vector_rank,
                                "text": h.doc.text[:200],
                            }
                            for h in found.hits
                        ],
                    },
                    indent=2,
                )
            )
            return 0

        if args.command in ("explain", "trace", "why"):
            return _run_explain(args, settings)

        if args.command == "resolve":
            run = ResolutionService(settings).resolve(args.path)
            report: dict[str, object] = {
                "repository_id": run.repository_id,
                "duration_ms": run.duration_ms,
                "summary": run.summary.model_dump(),
            }
            if args.examples:
                report["examples"] = {
                    reason: [e.model_dump() for e in items]
                    for reason, items in ResolutionService.examples(run, args.examples).items()
                }
            print(json.dumps(report, indent=2))
            return 0

        result = PipelineService(settings).run(
            args.path,
            force=args.force,
            batch_size=args.batch_size,
            knowledge=not args.no_knowledge,
            on_progress=_progress_printer(),
        )
    except (ValueError, NotADirectoryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    except NotIndexedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    except AnalyzerUnavailableError as exc:
        print(f"error: Java analyzer unavailable: {exc}", file=sys.stderr)
        return 3

    print(result.model_dump_json(indent=2))
    return 1 if result.index.analyzer_errors else 0


def _run_explain(args: argparse.Namespace, settings: object) -> int:
    """explain / trace / why against the stored knowledge model."""
    from app.config import Settings

    assert isinstance(settings, Settings)
    root = args.path or settings.source_root
    if root is None:
        print("error: no repository path given and SOURCE_ROOT is not configured", file=sys.stderr)
        return 2
    repo = RetrievalService(settings, use_vectors=False).repository_id(root)
    store = KnowledgeStore(settings.db_path)
    matches = store.resolve_methods(repo, args.method)
    if not matches:
        print(f"error: no method matches '{args.method}' (run `index` first?)", file=sys.stderr)
        return 4
    if len(matches) > 1:
        print(f"'{args.method}' is ambiguous; be more specific:", file=sys.stderr)
        for m in matches[:15]:
            print(f"  {m.method_id}  ({m.file}:{m.start_line})", file=sys.stderr)
        return 2
    llm = (
        None
        if args.no_llm
        else OllamaClient(
            settings.ollama_base_url,
            settings.ollama_chat_model,
            timeout=settings.ollama_timeout_seconds,
            temperature=settings.temperature,
            context_window=settings.context_window,
        )
    )
    service = ExplanationService(
        repo,
        store,
        SourceReader(Path(root)),
        settings,
        llm,
        RetrievalService(settings, use_vectors=False).retriever(),
    )
    target = matches[0].id
    result: AnswerInfo
    try:
        if args.command == "explain":
            result = service.explain(
                target, ExplainOptions(depth=args.depth, use_llm=llm is not None)
            )
        elif args.command == "trace":
            result = service.trace(target, args.var, args.depth, use_llm=llm is not None)
        else:
            first, _, last = (args.lines or "").partition("-")
            result = service.why(
                target,
                int(first) if first else None,
                int(last) if last else None,
                use_llm=llm is not None,
            )
    except VariableNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.as_json:
        print(result.model_dump_json(indent=2))
        return 0
    print(result.answer)
    if result.warnings:
        print("\n[warnings] " + "; ".join(result.warnings), file=sys.stderr)
    if result.llm_error:
        print(f"\n[llm] {result.llm_error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
