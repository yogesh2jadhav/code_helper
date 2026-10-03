"""Phase 24 (every stage logs) and Phase 26 (no source in logs, no external calls)."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.explain.explanation_service import ExplanationService
from app.knowledge.source import SourceReader
from app.knowledge.store import KnowledgeStore
from app.llm.ollama_client import OllamaClient
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.store import RetrievalStore
from app.scanner.scanner import repository_id_for
from app.services.pipeline_service import PipelineService

REQUIRED = [
    "repository_scan_started",
    "repository_scan_completed",
    "java_file_parsed",
    "symbol_resolution_completed",
    "call_graph_built",
    "control_flow_built",
    "data_flow_built",
    "rule_candidates_extracted",
    "knowledge_model_built",
    "retrieval_started",
    "retrieval_completed",
    "context_built",
    "llm_request_started",
    "llm_request_completed",
    "explanation_completed",
]

# Distinctive text that exists only in the analysed source. If it shows up in a log line, source leaked.
CODE_MARKER = "zq_confidential_rate"
COMMENT_MARKER = "quarterly-embargo-policy"
SRC = f"""package p;
public class Ledger {{
    /** Applies the {COMMENT_MARKER}. */
    public int settle(int amount) {{
        int {CODE_MARKER} = 17;
        if (amount > 90210) {{ return amount - {CODE_MARKER}; }}
        return amount;
    }}
}}
"""


def test_every_stage_logs_and_no_source_leaks(
    isolated_settings: Settings,
    java_analyzer: object,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "repo"
    (root / "src/main/p").mkdir(parents=True)
    (root / "src/main/p/Ledger.java").write_text(SRC)

    seen_hosts: list[str] = []

    def ollama(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host)
        return httpx.Response(
            200, json={"response": "## What this method does\nSettles. [E1]", "eval_count": 3}
        )

    llm = OllamaClient(
        "http://ollama.local:11434",
        "m",
        client=httpx.Client(transport=httpx.MockTransport(ollama)),
    )

    with caplog.at_level(logging.DEBUG):
        PipelineService(isolated_settings).run(root)
        store = KnowledgeStore(isolated_settings.db_path)
        repo = repository_id_for(root.resolve())
        method = next(m for m in store.iter_methods(repo) if m.name == "settle")
        ExplanationService(
            repo,
            store,
            SourceReader(root),
            isolated_settings,
            llm,
            HybridRetriever(RetrievalStore(isolated_settings.db_path)),
        ).explain(method.id)

    events = {r.getMessage() for r in caplog.records}
    missing = [e for e in REQUIRED if e not in events]
    assert missing == [], f"events never logged: {missing}"

    for record in caplog.records:
        rendered = " ".join([record.getMessage(), *(str(v) for v in record.__dict__.values())])
        assert CODE_MARKER not in rendered and COMMENT_MARKER not in rendered, record.getMessage()
        assert "90210" not in rendered, record.getMessage()

    assert seen_hosts and set(seen_hosts) == {"ollama.local"}  # the only network peer is the LLM


def test_events_carry_repository_and_duration(
    isolated_settings: Settings,
    java_analyzer: object,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "repo"
    (root / "src/main/p").mkdir(parents=True)
    (root / "src/main/p/Ledger.java").write_text(SRC)
    with caplog.at_level(logging.INFO):
        PipelineService(isolated_settings).run(root)
    done = next(r for r in caplog.records if r.getMessage() == "repository_scan_completed")
    assert done.__dict__["repository"] == repository_id_for(root.resolve())
    assert isinstance(done.__dict__["duration_ms"], int | float)
    built = next(r for r in caplog.records if r.getMessage() == "knowledge_model_built")
    assert built.__dict__.get("methods") == 1
