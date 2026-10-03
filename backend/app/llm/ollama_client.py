"""Local LLM access behind a provider-neutral interface, with an Ollama implementation.

Nothing in the application depends on Ollama directly: it uses `LLMClient`. The model name always
comes from configuration. Prompt and response text are never logged, only sizes and timings.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterator
from typing import Any, Protocol

import httpx
from pydantic import BaseModel

from app.logging_setup import log_event

logger = logging.getLogger(__name__)
_THINK = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


class LLMError(RuntimeError):
    """Base class: generation failed."""


class LLMUnavailableError(LLMError):
    """The server cannot be reached."""


class LLMTimeoutError(LLMError):
    """The request took longer than the configured timeout."""


class LLMModelNotFoundError(LLMError):
    """The configured model is not installed on the server."""


class LLMResponseError(LLMError):
    """The server answered, but not with usable text (malformed or empty)."""


class Message(BaseModel):
    role: str  # system | user | assistant
    content: str


class GenerationResult(BaseModel):
    text: str
    model: str
    duration_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class LLMClient(Protocol):
    model: str

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> GenerationResult: ...

    def chat(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> GenerationResult: ...

    def stream_chat(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> Iterator[str]: ...


def strip_reasoning(text: str) -> str:
    """Remove `<think>...</think>` blocks that reasoning models emit before the answer."""
    cleaned = _THINK.sub("", text)
    if "<think>" in cleaned.lower():  # an unterminated block: drop from the tag onwards
        cleaned = re.split(r"<think>", cleaned, flags=re.IGNORECASE)[0]
    return cleaned.strip()


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout: float = 120.0,
        temperature: float = 0.2,
        context_window: int = 8192,
        client: httpx.Client | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self.model = model
        self._timeout = timeout
        self._defaults: dict[str, Any] = {"temperature": temperature, "num_ctx": context_window}
        self._client = client  # injectable for tests

    # ==== public ================================================================================

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> GenerationResult:
        body: dict[str, Any] = {
            "model": model or self.model,
            "prompt": prompt,
            "stream": False,
            "options": {**self._defaults, **(options or {})},
        }
        if system:
            body["system"] = system
        data, ms = self._post("/api/generate", body, len(prompt) + len(system or ""))
        return self._result(data, "response", body["model"], ms)

    def chat(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> GenerationResult:
        body = self._chat_body(messages, model, options, stream=False)
        size = sum(len(m.content) for m in messages)
        data, ms = self._post("/api/chat", body, size)
        message = data.get("message") if isinstance(data, dict) else None
        data = {
            **data,
            "_text": (message or {}).get("content") if isinstance(message, dict) else None,
        }
        return self._result(data, "_text", body["model"], ms)

    def stream_chat(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        """Yield text pieces as they arrive. Raises the same LLMError subclasses."""
        body = self._chat_body(messages, model, options, stream=True)
        started = time.monotonic()
        log_event(
            logger,
            "llm_request_started",
            model=body["model"],
            endpoint="chat",
            stream=True,
            prompt_chars=sum(len(m.content) for m in messages),
        )
        produced = 0
        try:
            with (
                self._http() as client,
                client.stream(
                    "POST", self._base + "/api/chat", json=body, timeout=self._timeout
                ) as response,
            ):
                self._check_status(response, body["model"], read=True)
                for line in response.iter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except ValueError as exc:
                        raise LLMResponseError("malformed streaming response") from exc
                    piece = (chunk.get("message") or {}).get("content", "")
                    if piece:
                        produced += len(piece)
                        yield piece
                    if chunk.get("done"):
                        break
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"no response from Ollama within {self._timeout}s") from exc
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(f"cannot reach Ollama at {self._base}: {exc}") from exc
        if produced == 0:
            raise LLMResponseError("the model returned an empty response")
        log_event(
            logger,
            "llm_request_completed",
            model=body["model"],
            stream=True,
            response_chars=produced,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def available_models(self) -> list[str]:
        """Names of installed models; raises LLMUnavailableError if the server is down."""
        try:
            with self._http() as client:
                response = client.get(self._base + "/api/tags", timeout=min(self._timeout, 10))
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(f"cannot reach Ollama at {self._base}: {exc}") from exc
        try:
            return [m["name"] for m in response.json().get("models", [])]
        except (ValueError, KeyError, AttributeError) as exc:
            raise LLMResponseError("malformed model list") from exc

    # ==== internals =============================================================================

    def _http(self) -> httpx.Client:
        if self._client is not None:
            return _Borrowed(self._client)  # type: ignore[return-value]
        return httpx.Client()

    def _chat_body(
        self,
        messages: list[Message],
        model: str | None,
        options: dict[str, Any] | None,
        stream: bool,
    ) -> dict[str, Any]:
        return {
            "model": model or self.model,
            "messages": [m.model_dump() for m in messages],
            "stream": stream,
            "options": {**self._defaults, **(options or {})},
        }

    def _post(
        self, path: str, body: dict[str, Any], prompt_chars: int
    ) -> tuple[dict[str, Any], int]:
        started = time.monotonic()
        log_event(
            logger,
            "llm_request_started",
            model=body["model"],
            endpoint=path,
            prompt_chars=prompt_chars,
        )
        try:
            with self._http() as client:
                response = client.post(self._base + path, json=body, timeout=self._timeout)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"no response from Ollama within {self._timeout}s") from exc
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(f"cannot reach Ollama at {self._base}: {exc}") from exc
        self._check_status(response, body["model"])
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMResponseError("Ollama returned malformed JSON") from exc
        if not isinstance(data, dict):
            raise LLMResponseError("Ollama returned an unexpected payload")
        return data, int((time.monotonic() - started) * 1000)

    @staticmethod
    def _check_status(response: httpx.Response, model: str, read: bool = False) -> None:
        if response.status_code == 200:
            return
        if read:
            response.read()
        detail = response.text[:200] if response.text else ""
        if response.status_code == 404 or "not found" in detail.lower():
            raise LLMModelNotFoundError(
                f"model '{model}' is not available on the server; run `ollama pull {model}`"
            )
        raise LLMError(f"Ollama returned HTTP {response.status_code}: {detail}")

    def _result(self, data: dict[str, Any], key: str, model: str, ms: int) -> GenerationResult:
        raw = data.get(key)
        if not isinstance(raw, str):
            raise LLMResponseError("Ollama response has no text")
        text = strip_reasoning(raw)
        if not text:
            raise LLMResponseError("the model returned an empty response")
        log_event(
            logger, "llm_request_completed", model=model, response_chars=len(text), duration_ms=ms
        )
        return GenerationResult(
            text=text,
            model=model,
            duration_ms=ms,
            prompt_tokens=data.get("prompt_eval_count"),
            completion_tokens=data.get("eval_count"),
        )


class _Borrowed:
    """Use an injected client as a context manager without closing it."""

    def __init__(self, client: httpx.Client) -> None:
        self._c = client

    def __enter__(self) -> httpx.Client:
        return self._c

    def __exit__(self, *exc: object) -> None:
        return None
