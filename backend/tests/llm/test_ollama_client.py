from __future__ import annotations

import json
import logging
from collections.abc import Callable

import httpx
import pytest

from app.llm.ollama_client import (
    LLMError,
    LLMModelNotFoundError,
    LLMResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
    Message,
    OllamaClient,
    strip_reasoning,
)

Handler = Callable[[httpx.Request], httpx.Response]


def client_with(handler: Handler, **kw: object) -> tuple[OllamaClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = httpx.Client(transport=httpx.MockTransport(recording))
    return OllamaClient("http://ollama:11434/", "qwen-test", client=http, **kw), seen  # type: ignore[arg-type]


def body_of(request: httpx.Request) -> dict[str, object]:
    return json.loads(request.content)  # type: ignore[no-any-return]


# ---- generate and chat ------------------------------------------------------------------------


def test_generate_sends_the_configured_model_and_options() -> None:
    llm, seen = client_with(
        lambda r: httpx.Response(
            200, json={"response": "Hello there", "prompt_eval_count": 12, "eval_count": 3}
        ),
        temperature=0.1,
        context_window=4096,
    )
    result = llm.generate("say hi", system="be brief")
    assert (result.text, result.model, result.prompt_tokens, result.completion_tokens) == (
        "Hello there",
        "qwen-test",
        12,
        3,
    )
    assert result.duration_ms >= 0
    (request,) = seen
    assert str(request.url) == "http://ollama:11434/api/generate"
    assert body_of(request) == {
        "model": "qwen-test",
        "prompt": "say hi",
        "stream": False,
        "system": "be brief",
        "options": {"temperature": 0.1, "num_ctx": 4096},
    }


def test_model_and_options_can_be_overridden_per_call() -> None:
    llm, seen = client_with(lambda r: httpx.Response(200, json={"response": "ok"}))
    llm.generate("x", model="other-model", options={"temperature": 0.9, "num_predict": 50})
    assert body_of(seen[0])["model"] == "other-model"
    assert body_of(seen[0])["options"] == {"temperature": 0.9, "num_ctx": 8192, "num_predict": 50}
    assert "system" not in body_of(seen[0])


def test_chat_passes_the_conversation_through() -> None:
    llm, seen = client_with(
        lambda r: httpx.Response(
            200, json={"message": {"role": "assistant", "content": "Because."}}
        )
    )
    history = [Message(role="system", content="s"), Message(role="user", content="why?")]
    assert llm.chat(history).text == "Because."
    assert str(seen[0].url).endswith("/api/chat")
    assert body_of(seen[0])["messages"] == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "why?"},
    ]


def test_reasoning_blocks_are_removed_from_answers() -> None:
    text = "<think>let me ponder\nlonger</think>\n\nThe answer is 42."
    llm, _ = client_with(lambda r: httpx.Response(200, json={"response": text}))
    assert llm.generate("q").text == "The answer is 42."
    assert strip_reasoning("<THINK>a</THINK>x<think>b</think>y") == "xy"
    assert strip_reasoning("answer<think>never closed") == "answer"
    assert strip_reasoning("plain") == "plain"


# ---- streaming --------------------------------------------------------------------------------


def ndjson(*chunks: dict[str, object]) -> bytes:
    return ("\n".join(json.dumps(c) for c in chunks) + "\n").encode()


def test_stream_yields_pieces_until_done() -> None:
    payload = ndjson(
        {"message": {"content": "Hel"}},
        {"message": {"content": "lo"}},
        {"message": {"content": ""}, "done": True},
    )
    llm, seen = client_with(lambda r: httpx.Response(200, content=payload))
    assert list(llm.stream_chat([Message(role="user", content="hi")])) == ["Hel", "lo"]
    assert body_of(seen[0])["stream"] is True


def test_stream_errors_are_typed() -> None:
    llm, _ = client_with(
        lambda r: httpx.Response(200, content=ndjson({"message": {"content": ""}, "done": True}))
    )
    with pytest.raises(LLMResponseError, match="empty"):
        list(llm.stream_chat([Message(role="user", content="hi")]))
    bad, _ = client_with(lambda r: httpx.Response(200, content=b"not json\n"))
    with pytest.raises(LLMResponseError, match="malformed"):
        list(bad.stream_chat([Message(role="user", content="hi")]))
    missing, _ = client_with(lambda r: httpx.Response(404, json={"error": "model 'x' not found"}))
    with pytest.raises(LLMModelNotFoundError):
        list(missing.stream_chat([Message(role="user", content="hi")]))


# ---- failures ---------------------------------------------------------------------------------


def raising(exc: Exception) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


@pytest.mark.parametrize(
    ("handler", "error", "message"),
    [
        (
            raising(httpx.ConnectError("refused")),
            LLMUnavailableError,
            "cannot reach Ollama at http://ollama:11434",
        ),
        (raising(httpx.ReadTimeout("slow")), LLMTimeoutError, "within 120"),
        (
            lambda r: httpx.Response(404, json={"error": "model 'qwen-test' not found"}),
            LLMModelNotFoundError,
            "ollama pull qwen-test",
        ),
        (lambda r: httpx.Response(500, text="boom"), LLMError, "HTTP 500: boom"),
        (lambda r: httpx.Response(200, text="<html>"), LLMResponseError, "malformed JSON"),
        (lambda r: httpx.Response(200, json=["list"]), LLMResponseError, "unexpected payload"),
        (lambda r: httpx.Response(200, json={"other": 1}), LLMResponseError, "no text"),
        (lambda r: httpx.Response(200, json={"response": "   "}), LLMResponseError, "empty"),
        (
            lambda r: httpx.Response(200, json={"response": "<think>only thoughts</think>"}),
            LLMResponseError,
            "empty",
        ),
    ],
)
def test_every_failure_mode_has_a_distinct_clear_error(
    handler: Handler, error: type[Exception], message: str
) -> None:
    llm, _ = client_with(handler)
    with pytest.raises(error, match=message):
        llm.generate("q")
    if error is not LLMModelNotFoundError:  # the chat path shares the same handling
        with pytest.raises(error):
            llm.chat([Message(role="user", content="q")])


def test_failure_classes_share_a_base() -> None:
    for cls in (LLMUnavailableError, LLMTimeoutError, LLMModelNotFoundError, LLMResponseError):
        assert issubclass(cls, LLMError)


def test_chat_response_without_a_message_is_rejected() -> None:
    llm, _ = client_with(lambda r: httpx.Response(200, json={"done": True}))
    with pytest.raises(LLMResponseError, match="no text"):
        llm.chat([Message(role="user", content="q")])


# ---- model list and privacy -------------------------------------------------------------------


def test_available_models() -> None:
    llm, seen = client_with(
        lambda r: httpx.Response(200, json={"models": [{"name": "a:1"}, {"name": "b:2"}]})
    )
    assert llm.available_models() == ["a:1", "b:2"] and str(seen[0].url).endswith("/api/tags")
    down, _ = client_with(raising(httpx.ConnectError("no")))
    with pytest.raises(LLMUnavailableError):
        down.available_models()
    bad, _ = client_with(lambda r: httpx.Response(200, json={"models": [{"id": "x"}]}))
    with pytest.raises(LLMResponseError):
        bad.available_models()


def test_prompts_and_answers_are_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    secret_prompt = "SECRET-SOURCE-CODE class Payroll { }"
    secret_answer = "SECRET-ANSWER salary table"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/chat":
            return httpx.Response(
                200, json={"message": {"role": "assistant", "content": secret_answer}}
            )
        return httpx.Response(200, json={"response": secret_answer})

    llm, _ = client_with(handler)
    with caplog.at_level(logging.DEBUG, logger="app.llm.ollama_client"):
        llm.generate(secret_prompt, system="SECRET-SYSTEM")
        llm.chat([Message(role="user", content=secret_prompt)])
    text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "llm_request_started" in text and "llm_request_completed" in text
    assert "SECRET" not in text
    assert "model" in text and "duration_ms" in text
