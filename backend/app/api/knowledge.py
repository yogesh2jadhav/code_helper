"""REST API over the knowledge model: browse, inspect, explain, trace, why, chat, source."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.analyzer.rule_extractor import RuleCandidate
from app.config import Settings, get_settings
from app.explain.conversations import ConversationStore
from app.explain.explanation_service import (
    ChatResult,
    ExplainOptions,
    ExplainResult,
    ExplanationService,
    TraceExplanation,
    WhyExplanation,
)
from app.knowledge.models import ClassKnowledge, MethodKnowledge
from app.knowledge.source import SourceReader
from app.knowledge.store import ClassSummary, KnowledgeStore, MethodSummary
from app.llm.ollama_client import LLMClient, LLMError, OllamaClient
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.store import RetrievalStore
from app.scanner.store import ScanStore
from app.services.trace_service import VariableNotFoundError

router = APIRouter(prefix="/api")


# ==== dependencies (overridable in tests) =====================================


@lru_cache
def get_conversations() -> ConversationStore:
    return ConversationStore()


def get_llm(settings: Settings = Depends(get_settings)) -> LLMClient | None:
    return OllamaClient(
        settings.ollama_base_url,
        settings.ollama_chat_model,
        timeout=settings.ollama_timeout_seconds,
        temperature=settings.temperature,
        context_window=settings.context_window,
    )


def get_store(settings: Settings = Depends(get_settings)) -> KnowledgeStore:
    return KnowledgeStore(settings.db_path)


def _need_method(store: KnowledgeStore, method_id: str) -> tuple[MethodSummary, str]:
    summary = store.get_method_summary(method_id)
    repo = store.repository_of_method(method_id)
    if summary is None or repo is None:
        raise HTTPException(status_code=404, detail="unknown method")
    return summary, repo


def _reader(settings: Settings, repo: str) -> SourceReader | None:
    root = ScanStore(settings.db_path).repository_root(repo)
    return SourceReader(Path(root)) if root else None


def _explainer(
    settings: Settings,
    store: KnowledgeStore,
    llm: LLMClient | None,
    repo: str,
    conversations: ConversationStore,
) -> ExplanationService:
    retriever = HybridRetriever(RetrievalStore(settings.db_path))  # text search; vectors optional
    return ExplanationService(
        repo, store, _reader(settings, repo), settings, llm, retriever, conversations
    )


# ==== browsing ================================================================


class RepositoryStats(BaseModel):
    repository_id: str
    counts: dict[str, int]
    last_run: dict[str, object] | None = None


@router.get("/repositories/{repository_id}/stats")
def repository_stats(
    repository_id: str, store: KnowledgeStore = Depends(get_store)
) -> RepositoryStats:
    counts = store.counts(repository_id)
    if not counts["classes"]:
        raise HTTPException(
            status_code=404, detail="no knowledge model for this repository; run index"
        )
    run = store.last_successful_run(repository_id)
    return RepositoryStats(
        repository_id=repository_id, counts=counts, last_run=run.model_dump() if run else None
    )


@router.get("/repositories/{repository_id}/classes")
def list_classes(
    repository_id: str,
    q: str | None = None,
    include_tests: bool = True,
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    store: KnowledgeStore = Depends(get_store),
) -> list[ClassSummary]:
    return store.list_classes(
        repository_id, query=q, include_tests=include_tests, limit=limit, offset=offset
    )


@router.get("/repositories/{repository_id}/methods")
def search_methods(
    repository_id: str,
    q: str = Query(..., min_length=1),
    include_tests: bool = False,
    limit: int = Query(50, ge=1, le=200),
    store: KnowledgeStore = Depends(get_store),
) -> list[MethodSummary]:
    return store.search_methods(repository_id, q, include_tests=include_tests, limit=limit)


@router.get("/classes/{class_id}")
def get_class(class_id: str, store: KnowledgeStore = Depends(get_store)) -> ClassKnowledge:
    found = store.get_class(class_id)
    if found is None:
        raise HTTPException(status_code=404, detail="unknown class")
    return found


@router.get("/classes/{class_id}/methods")
def class_methods(class_id: str, store: KnowledgeStore = Depends(get_store)) -> list[MethodSummary]:
    if store.get_class(class_id) is None:
        raise HTTPException(status_code=404, detail="unknown class")
    return store.list_methods(class_id)


@router.get("/methods/{method_id}")
def get_method(method_id: str, store: KnowledgeStore = Depends(get_store)) -> MethodSummary:
    return _need_method(store, method_id)[0]


@router.get("/methods/{method_id}/knowledge")
def method_knowledge(method_id: str, store: KnowledgeStore = Depends(get_store)) -> MethodKnowledge:
    found = store.get_method(method_id)
    if found is None:
        raise HTTPException(status_code=404, detail="unknown method")
    return found


@router.get("/methods/{method_id}/rules")
def method_rules(method_id: str, store: KnowledgeStore = Depends(get_store)) -> list[RuleCandidate]:
    _need_method(store, method_id)
    return store.rules(method_id)


class Related(BaseModel):
    method: MethodSummary | None  # None when the target is outside the project
    method_id: str
    depth: int
    ambiguous: bool = False
    via_override: bool = False


def _related(
    method_id: str, direction: str, depth: int, overrides: bool, store: KnowledgeStore
) -> list[Related]:
    summary, repo = _need_method(store, method_id)
    reached = store.reach(
        repo, summary.method_id, depth, direction=direction, include_overrides=overrides
    )
    summaries = store.summaries_for(repo, [r.method_id for r in reached])
    return [
        Related(
            method=summaries.get(r.method_id),
            method_id=r.method_id,
            depth=r.depth,
            ambiguous=r.ambiguous,
            via_override=r.via_override,
        )
        for r in reached
    ]


@router.get("/methods/{method_id}/callers")
def method_callers(
    method_id: str, depth: int = Query(1, ge=1, le=5), store: KnowledgeStore = Depends(get_store)
) -> list[Related]:
    return _related(method_id, "callers", depth, False, store)


@router.get("/methods/{method_id}/callees")
def method_callees(
    method_id: str,
    depth: int = Query(1, ge=1, le=5),
    include_overrides: bool = False,
    store: KnowledgeStore = Depends(get_store),
) -> list[Related]:
    return _related(method_id, "callees", depth, include_overrides, store)


# ==== explain, trace, why, chat ===============================================


@router.post("/methods/{method_id}/explain")
def explain(
    method_id: str,
    options: ExplainOptions | None = None,
    settings: Settings = Depends(get_settings),
    store: KnowledgeStore = Depends(get_store),
    llm: LLMClient | None = Depends(get_llm),
    conversations: ConversationStore = Depends(get_conversations),
) -> ExplainResult:
    _, repo = _need_method(store, method_id)
    return _explainer(settings, store, llm, repo, conversations).explain(method_id, options)


class TraceRequest(BaseModel):
    variable: str
    depth: int = Field(default=2, ge=1, le=4)
    use_llm: bool = True


@router.post("/methods/{method_id}/trace")
def trace(
    method_id: str,
    request: TraceRequest,
    settings: Settings = Depends(get_settings),
    store: KnowledgeStore = Depends(get_store),
    llm: LLMClient | None = Depends(get_llm),
    conversations: ConversationStore = Depends(get_conversations),
) -> TraceExplanation:
    _, repo = _need_method(store, method_id)
    try:
        return _explainer(settings, store, llm, repo, conversations).trace(
            method_id, request.variable, request.depth, request.use_llm
        )
    except VariableNotFoundError as exc:
        raise HTTPException(
            status_code=400, detail={"message": str(exc), "available": exc.available}
        ) from exc


class WhyRequest(BaseModel):
    start_line: int | None = None
    end_line: int | None = None
    use_llm: bool = True


@router.post("/methods/{method_id}/why")
def why(
    method_id: str,
    request: WhyRequest,
    settings: Settings = Depends(get_settings),
    store: KnowledgeStore = Depends(get_store),
    llm: LLMClient | None = Depends(get_llm),
    conversations: ConversationStore = Depends(get_conversations),
) -> WhyExplanation:
    _, repo = _need_method(store, method_id)
    try:
        return _explainer(settings, store, llm, repo, conversations).why(
            method_id, request.start_line, request.end_line, request.use_llm
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


@router.post("/conversations/{conversation_id}/messages")
def chat(
    conversation_id: str,
    request: ChatRequest,
    settings: Settings = Depends(get_settings),
    store: KnowledgeStore = Depends(get_store),
    llm: LLMClient | None = Depends(get_llm),
    conversations: ConversationStore = Depends(get_conversations),
) -> ChatResult:
    conversation = conversations.get(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="unknown conversation")
    service = _explainer(settings, store, llm, conversation.repository_id, conversations)
    try:
        return service.chat(conversation_id, request.question)
    except LLMError as exc:  # no deterministic equivalent for free-form follow-ups
        raise HTTPException(status_code=503, detail=f"language model unavailable: {exc}") from exc


# ==== source and LLM status ===================================================


class SourceView(BaseModel):
    file_id: str
    path: str
    relative_path: str
    package: str | None = None
    language: str = "java"
    total_lines: int
    start_line: int
    end_line: int
    text: str


@router.get("/source/{file_id}")
def source(
    file_id: str,
    start: int | None = Query(None, ge=1),
    end: int | None = Query(None, ge=1),
    settings: Settings = Depends(get_settings),
) -> SourceView:
    scan = ScanStore(settings.db_path)
    record = scan.get_file(file_id)
    if record is None:
        raise HTTPException(status_code=404, detail="unknown file")
    root = scan.repository_root(record.repository_id)
    text = SourceReader(Path(root)).text(record.relative_path) if root else None
    if text is None:
        raise HTTPException(status_code=404, detail="the source file is no longer readable")
    lines = text.split("\n")
    first = start or 1
    last = min(end or len(lines), len(lines))
    if first > last:
        raise HTTPException(status_code=400, detail="start is after end")
    return SourceView(
        file_id=file_id,
        path=record.path,
        relative_path=record.relative_path,
        package=record.package,
        total_lines=len(lines),
        start_line=first,
        end_line=last,
        text="\n".join(lines[first - 1 : last]),
    )


class LLMStatus(BaseModel):
    model: str
    reachable: bool
    model_installed: bool
    installed_models: list[str] = Field(default_factory=list)
    error: str | None = None


@router.get("/llm/status")
def llm_status(
    settings: Settings = Depends(get_settings), llm: LLMClient | None = Depends(get_llm)
) -> LLMStatus:
    model = settings.ollama_chat_model
    if llm is None or not hasattr(llm, "available_models"):
        return LLMStatus(
            model=model,
            reachable=False,
            model_installed=False,
            error="no language model configured",
        )
    try:
        names = llm.available_models()
    except LLMError as exc:
        return LLMStatus(model=model, reachable=False, model_installed=False, error=str(exc))
    return LLMStatus(
        model=model,
        reachable=True,
        model_installed=model in names or f"{model}:latest" in names,
        installed_models=names,
    )
