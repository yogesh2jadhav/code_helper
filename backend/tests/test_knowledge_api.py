"""The REST API over the knowledge model, end to end on a really indexed repository."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.knowledge import get_conversations, get_llm
from app.config import Settings, get_settings
from app.llm.ollama_client import (
    GenerationResult,
    LLMTimeoutError,
    LLMUnavailableError,
    Message,
)
from app.llm.prompts import EXPLAIN_SECTIONS
from app.main import create_app
from app.scanner.scanner import repository_id_for
from app.services.pipeline_service import PipelineService
from tests.shop_repo import World

GOOD = "\n\n".join(f"## {s}\nText for {s}. [E1]" for s in EXPLAIN_SECTIONS)


class FakeLLM:
    model = "fake-model"

    def __init__(
        self, text: str = GOOD, error: Exception | None = None, models: list[str] | None = None
    ) -> None:
        self.text, self.error, self.models = (
            text,
            error,
            models or ["fake-model:latest", "other:1b"],
        )

    def _go(self) -> GenerationResult:
        if self.error:
            raise self.error
        return GenerationResult(text=self.text, model=self.model, duration_ms=1)

    def generate(self, prompt: str, **kw: Any) -> GenerationResult:
        return self._go()

    def chat(self, messages: list[Message], **kw: Any) -> GenerationResult:
        return self._go()

    def stream_chat(self, messages: list[Message], **kw: Any):  # type: ignore[no-untyped-def]
        yield self._go().text

    def available_models(self) -> list[str]:
        if self.error:
            raise self.error
        return self.models


class Api:
    def __init__(self, client: TestClient, repo: str, world: World) -> None:
        self.client, self.repo, self.world = client, repo, world

    def get(self, url: str, **kw: Any) -> Any:
        return self.client.get(url, **kw)

    def post(self, url: str, json: dict[str, Any] | None = None) -> Any:
        return self.client.post(url, json=json)

    def method(self, name: str) -> dict[str, Any]:
        found = self.get(f"/api/repositories/{self.repo}/methods", params={"q": name}).json()
        exact = [m for m in found if m["name"] == name]
        assert len(exact) == 1, (name, [m["method_id"] for m in found])
        return exact[0]


@pytest.fixture
def api(isolated_settings: Settings, java_analyzer: object, shop: World) -> Api:
    PipelineService(isolated_settings).run(shop.root)
    get_conversations.cache_clear()
    app = create_app()
    app.dependency_overrides[get_llm] = lambda: FakeLLM()
    return Api(TestClient(app), repository_id_for(shop.root.resolve()), shop)


def use_llm(api: Api, llm: FakeLLM | None) -> None:
    api.client.app.dependency_overrides[get_llm] = lambda: llm  # type: ignore[attr-defined]


# ---- browsing ---------------------------------------------------------------------------------


def test_repository_stats_and_classes(api: Api) -> None:
    stats = api.get(f"/api/repositories/{api.repo}/stats").json()
    assert stats["counts"]["classes"] == 3 and stats["counts"]["methods"] == 4
    assert stats["last_run"]["status"] == "succeeded"
    classes = api.get(f"/api/repositories/{api.repo}/classes").json()
    assert [c["fqn"] for c in classes] == [
        "shop.InvoicePrinter",
        "shop.ShippingCalculator",
        "shop.ShippingCalculatorTest",
    ]
    assert [
        c["fqn"]
        for c in api.get(
            f"/api/repositories/{api.repo}/classes", params={"include_tests": "false"}
        ).json()
    ] == ["shop.InvoicePrinter", "shop.ShippingCalculator"]
    assert (
        len(
            api.get(
                f"/api/repositories/{api.repo}/classes", params={"q": "Ship", "limit": 1}
            ).json()
        )
        == 1
    )


def test_class_detail_and_methods(api: Api) -> None:
    cls = next(
        c
        for c in api.get(f"/api/repositories/{api.repo}/classes").json()
        if c["fqn"] == "shop.ShippingCalculator"
    )
    detail = api.get(f"/api/classes/{cls['id']}").json()
    assert detail["purpose"] == {
        **detail["purpose"],
        "text": "Computes shipping cost for parcels.",
        "basis": "comment",
    }
    methods = api.get(f"/api/classes/{cls['id']}/methods").json()
    assert [m["name"] for m in methods] == ["shippingFee", "loyaltyDiscount"]
    assert methods[0]["purpose"] == "Returns the shipping fee; heavy parcels cost extra."
    assert (methods[0]["start_line"], methods[0]["end_line"]) == (6, 12)


def test_method_summary_knowledge_and_search(api: Api) -> None:
    fee = api.method("shippingFee")
    assert (
        api.get(f"/api/methods/{fee['id']}").json()["method_id"]
        == "shop.ShippingCalculator#shippingFee(double,boolean)"
    )
    knowledge = api.get(f"/api/methods/{fee['id']}/knowledge").json()
    assert knowledge["purpose"]["level"] == "fact" and knowledge["callers"][0][
        "method_id"
    ].endswith("heavyParcelsCostExtra()")
    assert {"control_flow", "data_flow", "rule_candidates", "evidence", "risks", "unknowns"} <= set(
        knowledge
    )
    assert [
        m["name"]
        for m in api.get(f"/api/repositories/{api.repo}/methods", params={"q": "fee"}).json()
    ] == ["shippingFee"]
    with_tests = api.get(
        f"/api/repositories/{api.repo}/methods", params={"q": "Heavy", "include_tests": "true"}
    ).json()
    assert [m["name"] for m in with_tests] == ["heavyParcelsCostExtra"]


def test_rules_callers_and_callees(api: Api) -> None:
    fee = api.method("shippingFee")
    rules = api.get(f"/api/methods/{fee['id']}/rules").json()
    assert [(r["kind"], r["start_line"]) for r in rules] == [("threshold", 8), ("decision", 11)]
    assert rules[0]["literals"] == ["30"] and rules[0]["meaning"] == "weightKg is greater than 30"
    callers = api.get(f"/api/methods/{fee['id']}/callers").json()
    assert [(c["method"]["name"], c["depth"]) for c in callers] == [("heavyParcelsCostExtra", 1)]
    callees = api.get(f"/api/methods/{callers[0]['method']['id']}/callees").json()
    assert [c["method"]["name"] for c in callees if c["method"]] == ["shippingFee"]
    assert api.get(f"/api/methods/{fee['id']}/callees").json() == []


def test_unknown_ids_are_404(api: Api) -> None:
    for url in (
        "/api/methods/m_x",
        "/api/methods/m_x/knowledge",
        "/api/methods/m_x/rules",
        "/api/methods/m_x/callers",
        "/api/methods/m_x/callees",
        "/api/classes/c_x",
        "/api/classes/c_x/methods",
        "/api/source/nope",
        "/api/repositories/none/stats",
    ):
        assert api.get(url).status_code == 404, url
    for url, body in (
        ("/api/methods/m_x/explain", {}),
        ("/api/methods/m_x/trace", {"variable": "a"}),
        ("/api/methods/m_x/why", {}),
        ("/api/conversations/none/messages", {"question": "hi"}),
    ):
        assert api.post(url, body).status_code == 404, url


# ---- explain, trace, why, chat ----------------------------------------------------------------


def test_explain_returns_plan_answer_evidence_and_unknowns(api: Api) -> None:
    fee = api.method("shippingFee")
    r = api.post(
        f"/api/methods/{fee['id']}/explain",
        {"depth": 2, "include_tests": True, "include_docs": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert (
        body["answer"] == GOOD and body["answer_source"] == "llm" and body["model"] == "fake-model"
    )
    assert body["explanation_plan"]["purpose"]["basis"] == "comment"
    assert body["method"]["name"] == "shippingFee" and body["conversation_id"]
    assert body["evidence"][0]["label"] == "E1" and body["context_tokens"] > 0
    assert body["unknowns"][0].startswith("the repository does not establish why 30 was chosen")
    assert body["parsed"]["missing_sections"] == [] and body["warnings"] == []


def test_explain_falls_back_when_the_llm_is_down(api: Api) -> None:
    use_llm(
        api, FakeLLM(error=LLMUnavailableError("cannot reach Ollama at http://localhost:11434"))
    )
    r = api.post(f"/api/methods/{api.method('shippingFee')['id']}/explain", {})
    assert r.status_code == 200  # the analysis is still delivered
    body = r.json()
    assert body["answer_source"] == "deterministic" and "cannot reach Ollama" in body["llm_error"]
    assert (
        body["answer"].startswith("> LLM generation unavailable")
        and body["explanation_plan"]["major_stages"]
    )
    use_llm(api, None)
    assert (
        api.post(f"/api/methods/{api.method('shippingFee')['id']}/explain", {}).json()[
            "answer_source"
        ]
        == "deterministic"
    )


def test_explain_validates_options(api: Api) -> None:
    assert (
        api.post(
            f"/api/methods/{api.method('shippingFee')['id']}/explain", {"depth": 9}
        ).status_code
        == 422
    )


def test_trace(api: Api) -> None:
    fee = api.method("shippingFee")
    r = api.post(f"/api/methods/{fee['id']}/trace", {"variable": "fee", "depth": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["trace"]["variable"] == "fee" and body["trace"]["variable_kind"] == "local"
    assert (
        any(s["direction"] == "within" for s in body["trace"]["steps"])
        and body["answer_source"] == "llm"
    )
    bad = api.post(f"/api/methods/{fee['id']}/trace", {"variable": "nope"})
    assert bad.status_code == 400
    assert bad.json()["detail"]["available"] == ["express", "fee", "weightKg"]


def test_why(api: Api) -> None:
    fee = api.method("shippingFee")
    r = api.post(f"/api/methods/{fee['id']}/why", {"start_line": 8, "end_line": 10})
    assert r.status_code == 200
    why = r.json()["why"]
    assert why["start_line"] == 8 and why["unknown"][0].startswith("why 30 was chosen")
    assert [p["text"] for p in why["confirmed"]][
        0
    ] == "At L8 the code applies: weightKg is greater than 30; then set fee += 20"
    assert (
        api.post(f"/api/methods/{fee['id']}/why", {"start_line": 500, "end_line": 600}).status_code
        == 400
    )
    assert (
        api.post(f"/api/methods/{fee['id']}/why", {"use_llm": False}).json()["answer_source"]
        == "deterministic"
    )


def test_follow_up_chat(api: Api) -> None:
    fee = api.method("shippingFee")
    cid = api.post(f"/api/methods/{fee['id']}/explain", {}).json()["conversation_id"]
    use_llm(api, FakeLLM("Express doubles it [E1], see [E77]."))
    r = api.post(f"/api/conversations/{cid}/messages", {"question": "What about express?"})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "Express doubles it [E1], see ." and body["conversation_id"] == cid
    assert body["warnings"] == ["removed citation labels that do not exist: E77"]
    assert api.post(f"/api/conversations/{cid}/messages", {"question": ""}).status_code == 422
    use_llm(api, FakeLLM(error=LLMTimeoutError("slow")))
    down = api.post(f"/api/conversations/{cid}/messages", {"question": "again?"})
    assert down.status_code == 503 and "language model unavailable" in down.json()["detail"]


# ---- source and status ------------------------------------------------------------------------


def test_source_viewer(api: Api) -> None:
    files = api.get(f"/api/repositories/{api.repo}/files", params={"limit": 10}).json()["files"]
    calc = next(f for f in files if f["relative_path"].endswith("ShippingCalculator.java"))
    whole = api.get(f"/api/source/{calc['id']}").json()
    assert (
        whole["relative_path"] == "src/main/shop/ShippingCalculator.java"
        and whole["package"] == "shop"
    )
    assert (
        whole["start_line"] == 1
        and whole["end_line"] == whole["total_lines"]
        and "public class ShippingCalculator" in whole["text"]
    )
    part = api.get(f"/api/source/{calc['id']}", params={"start": 8, "end": 10}).json()
    assert part["text"] == "        if (weightKg > 30) {\n            fee += 20;\n        }"
    assert (part["start_line"], part["end_line"]) == (8, 10)
    assert api.get(f"/api/source/{calc['id']}", params={"start": 20, "end": 10}).status_code == 400
    assert api.get(f"/api/source/{calc['id']}", params={"start": 0}).status_code == 422
    assert (
        api.get(f"/api/source/{calc['id']}", params={"start": 5, "end": 9999}).json()["end_line"]
        == whole["total_lines"]
    )


def test_source_by_repository_relative_path(api: Api) -> None:
    ok = api.get(
        f"/api/repositories/{api.repo}/source",
        params={"path": "src/main/shop/InvoicePrinter.java", "start": 4, "end": 4},
    )
    assert (
        ok.status_code == 200
        and ok.json()["text"] == "    public String render(String customer, double total) {"
    )
    assert (
        api.get(
            f"/api/repositories/{api.repo}/source", params={"path": "../../etc/passwd"}
        ).status_code
        == 404
    )
    assert (
        api.get(f"/api/repositories/{api.repo}/source", params={"path": "README.md"}).status_code
        == 404
    )  # not a scanned .java file
    assert api.get("/api/repositories/nope/source", params={"path": "x"}).status_code == 404


def test_source_for_a_deleted_file_is_404(api: Api) -> None:
    files = api.get(f"/api/repositories/{api.repo}/files").json()["files"]
    inv = next(f for f in files if f["relative_path"].endswith("InvoicePrinter.java"))
    (api.world.root / inv["relative_path"]).rename(api.world.root / "moved.txt")
    try:
        assert api.get(f"/api/source/{inv['id']}").status_code == 404
    finally:
        (api.world.root / "moved.txt").rename(api.world.root / inv["relative_path"])


def test_llm_status(api: Api) -> None:
    ok = api.get("/api/llm/status").json()
    assert (
        ok["reachable"] is True and ok["model_installed"] is False
    )  # configured model is qwen..., fake lists others
    assert ok["installed_models"] == ["fake-model:latest", "other:1b"] and ok["error"] is None
    use_llm(api, FakeLLM(models=[get_settings().ollama_chat_model]))
    assert api.get("/api/llm/status").json()["model_installed"] is True
    use_llm(api, FakeLLM(error=LLMUnavailableError("nope")))
    down = api.get("/api/llm/status").json()
    assert down["reachable"] is False and down["error"] == "nope"
    use_llm(api, None)
    assert api.get("/api/llm/status").json()["error"] == "no language model configured"
