"""Run a benchmark: index its source, explain each expected method, measure the result."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.config import Settings
from app.evaluation.benchmark import Benchmark
from app.evaluation.metrics import MethodMetrics, evaluate_method
from app.explain.explanation_service import ExplainOptions, ExplanationService
from app.knowledge.source import SourceReader
from app.knowledge.store import KnowledgeStore
from app.llm.ollama_client import LLMClient
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.store import RetrievalStore
from app.scanner.scanner import repository_id_for
from app.services.pipeline_service import PipelineService


@dataclass
class EvaluationRun:
    benchmark: str
    mode: str  # "analysis" (deterministic) or the model name
    methods: list[MethodMetrics] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)  # expected methods the system never found


def run_benchmark(
    path: Path, settings: Settings, llm: LLMClient | None = None, *, use_llm: bool = False
) -> EvaluationRun:
    benchmark = Benchmark.load(path)
    root = benchmark.source_root(path)
    PipelineService(settings).run(root)
    store = KnowledgeStore(settings.db_path)
    repo = repository_id_for(root)
    by_id = {m.method_id: m.id for m in store.iter_methods(repo)}
    reader = SourceReader(root)
    service = ExplanationService(
        repo,
        store,
        reader,
        settings,
        llm if use_llm else None,
        HybridRetriever(RetrievalStore(settings.db_path)),
    )
    mode = getattr(llm, "model", "llm") if use_llm and llm else "analysis"
    run = EvaluationRun(benchmark=benchmark.name, mode=str(mode))
    for expected in benchmark.methods:
        pk = by_id.get(expected.method)
        knowledge = store.get_method(pk) if pk else None
        if pk is None or knowledge is None:
            run.not_found.append(expected.method)
            continue
        result = service.explain(pk, ExplainOptions(use_llm=use_llm))
        source = reader.lines(knowledge.file, knowledge.start_line, knowledge.end_line) or ""
        run.methods.append(evaluate_method(knowledge, source, result, expected))
    return run
