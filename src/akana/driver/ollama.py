"""Ollama backend — local models over HTTP (the secondary driver).

Talks to ``{url}/api/chat`` with ``stream:true`` and maps Ollama's NDJSON onto
neutral :class:`ChatChunk`s. The system message flows through natively (Ollama's
chat endpoint accepts a ``system`` role in ``messages``).

Function-calling + thinking PARITY (mirrors gemini/openai): Ollama's ``/api/chat``
accepts OpenAI-style ``tools=[...]`` and ``think: true``. The driver stays
PROVIDER-NEUTRAL — it does NOT run the tool loop or know the tool schemas; it only
(a) forwards ``tools``/``think`` into the request body and (b) parses the model's
``message.tool_calls`` and ``message.thinking`` out of the stream, parking them in
:attr:`ChatChunk.raw` (alongside the usual ``delta``/``done``). The actual
function-calling LOOP (dispatch + append tool messages + re-call) lives one layer
up in ``akana_server.orchestrator.ollama_provider`` (mirroring ``gemini_provider``),
because tool-call and tool-result turns need richer message shapes than the
neutral :class:`Message` (role-only/content-only) can carry. Existing text-only
behavior is unchanged: ``stream_chat(list[Message])`` and the ``delta``/``done``
chunk shapes are byte-for-byte the same when no tools/think are requested.

``httpx`` is imported lazily inside the IO seam so importing the driver layer
costs nothing until a local call actually runs. Parsing is a pure function so it
can be tested without any HTTP at all.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from akana.driver.base import (
    ChatChunk,
    Driver,
    DriverError,
    DriverUnavailable,
    Message,
)


#: Connect/write/pool ceiling used when the generation (read) timeout is disabled.
#: Only the READ (inter-token) timeout is unbounded in that mode — the handshake
#: still fails fast so an unreachable server raises promptly instead of hanging.
_CONNECT_TIMEOUT_S = 30.0


def _chat_timeout(timeout: float) -> Any:
    """Build the httpx timeout for a streaming chat call.

    ``timeout > 0`` → a uniform ceiling (connect/read/write/pool all = ``timeout``),
    the historical behavior. ``timeout <= 0`` → the generation (read / inter-token)
    ceiling is DISABLED (``read=None``) so a slow or cold-loading model that takes a
    long time before the next token is NEVER cut off, while connect/write/pool keep a
    finite :data:`_CONNECT_TIMEOUT_S` so an unreachable server still fails fast. The
    non-positive = "no ceiling" convention mirrors the orchestrator's idle/total
    timeout knobs (``combine_cap`` → 0 = disabled)."""
    import httpx

    if timeout and timeout > 0:
        return httpx.Timeout(timeout)
    return httpx.Timeout(_CONNECT_TIMEOUT_S, read=None)


def _message_extras(message: dict[str, Any]) -> dict[str, Any] | None:
    """Pull thinking text + tool_calls out of one ``message`` object → ``raw`` extras.

    Ollama (thinking models) puts reasoning in ``message.thinking`` (separate from
    ``message.content``) and native function-calls in ``message.tool_calls`` (the
    OpenAI shape: ``[{"function": {"name", "arguments"}}]``). We DON'T interpret
    them here — we hand them to the provider via :attr:`ChatChunk.raw` so the
    text answer (``content``) and the side-channel (thinking/tools) stay cleanly
    separated. Returns ``None`` when neither is present (so a pure-empty line still
    maps to ``None`` — existing behavior preserved)."""
    if not isinstance(message, dict):
        return None
    extras: dict[str, Any] = {}
    thinking = message.get("thinking")
    if isinstance(thinking, str) and thinking:
        extras["thinking"] = thinking
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        extras["tool_calls"] = tool_calls
    return extras or None


def _parse_ollama_line(line: str) -> ChatChunk | None:
    """One NDJSON line -> a :class:`ChatChunk`, or ``None`` to skip.

    Raises :class:`DriverError` on an error line (e.g. unknown model returned
    inside a 200 body). When the ``message`` carries ``thinking`` and/or
    ``tool_calls``, they are parked in :attr:`ChatChunk.raw` (under those keys) so
    the provider's function-calling loop can read them; a line with ONLY those
    (no ``content``) still yields a chunk (``delta=""``) rather than ``None`` so
    the side-channel is not dropped. A line with neither content nor extras maps
    to ``None`` (unchanged).
    """
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(ev, dict):
        return None
    if ev.get("error"):
        raise DriverError(
            f"ollama error: {ev['error']}", kind="provider_error", provider="ollama"
        )
    if ev.get("done"):
        usage = {
            "prompt_tokens": int(ev.get("prompt_eval_count") or 0),
            "completion_tokens": int(ev.get("eval_count") or 0),
        }
        return ChatChunk(done=True, usage=usage, raw=ev)
    message = ev.get("message") or {}
    delta = str(message.get("content") or "")
    extras = _message_extras(message)
    if delta or extras:
        return ChatChunk(delta=delta, raw=extras)
    return None


class OllamaDriver(Driver):
    """Secondary backend: a local Ollama server."""

    name = "ollama"

    def __init__(
        self,
        *,
        url: str = "http://localhost:11434",
        model: str = "llama3.1",
        timeout: float = 300.0,
        transport: Any = None,  # httpx transport injection (tests / custom HTTP)
    ) -> None:
        self._url = url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._transport = transport

    async def _post_stream(self, body: dict[str, Any]) -> AsyncIterator[str]:
        """IO seam: POST and yield raw NDJSON lines. Maps transport faults to DriverError."""
        import httpx

        client_kwargs: dict[str, Any] = {"timeout": _chat_timeout(self._timeout)}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                async with client.stream(
                    "POST", f"{self._url}/api/chat", json=body
                ) as resp:
                    if resp.status_code >= 400:
                        text = (await resp.aread()).decode("utf-8", "replace")
                        raise DriverError(
                            f"ollama http {resp.status_code}: {text[:200]}",
                            kind="provider_error",
                            provider="ollama",
                            status_code=resp.status_code,
                        )
                    async for line in resp.aiter_lines():
                        if line.strip():
                            yield line
        except httpx.ConnectError as exc:
            raise DriverUnavailable(
                f"ollama not reachable at {self._url}",
                kind="unavailable",
                retryable=True,
                provider="ollama",
            ) from exc
        except httpx.TimeoutException as exc:
            raise DriverError(
                "ollama request timed out",
                kind="timeout",
                retryable=True,
                provider="ollama",
            ) from exc
        except httpx.HTTPError as exc:
            raise DriverError(
                f"ollama transport error: {exc}",
                kind="provider_error",
                provider="ollama",
            ) from exc

    async def _stream_body(self, body: dict[str, Any]) -> AsyncIterator[ChatChunk]:
        """POST ``body`` and yield parsed :class:`ChatChunk`s (skipping ``None`` lines)."""
        async for line in self._post_stream(body):
            chunk = _parse_ollama_line(line)
            if chunk is not None:
                yield chunk

    async def stream_chat(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
    ) -> AsyncIterator[ChatChunk]:
        body = {
            "model": model or self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": True,
        }
        async for chunk in self._stream_body(body):
            yield chunk

    async def stream_chat_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        think: bool | None = None,
    ) -> AsyncIterator[ChatChunk]:
        """Stream from RAW OpenAI-style message dicts, with optional ``tools``/``think``.

        Unlike :meth:`stream_chat` (which takes neutral :class:`Message`s), this
        takes pre-built message dicts so the provider can include assistant
        ``tool_calls`` turns and ``{"role": "tool", ...}`` results during a
        function-calling loop. ``tools`` (OpenAI tools JSON-schema) and ``think``
        are added to the body ONLY when provided/truthy — when both are omitted the
        request is identical to a plain chat call (no behavior change for callers
        that don't use tools/thinking). Parsed ``message.thinking`` / ``tool_calls``
        surface via :attr:`ChatChunk.raw`."""
        body: dict[str, Any] = {
            "model": model or self._model,
            "messages": list(messages),
            "stream": True,
        }
        if tools:
            body["tools"] = tools
        if think:
            body["think"] = True
        async for chunk in self._stream_body(body):
            yield chunk

    async def list_models(self) -> list[str]:
        """Names of installed models (``GET /api/tags``).

        If the daemon is unreachable, raise :class:`DriverUnavailable` (so the
        caller can treat it as 'Ollama is not running'); if there are no models,
        return an empty list. Listing must be fast → the timeout is capped at
        10 s (even if the chat timeout is long, or DISABLED via ``<=0`` — which
        for a stream means 'no read ceiling' but for listing means a flat 10 s)."""
        import httpx

        list_timeout = min(self._timeout, 10.0) if self._timeout and self._timeout > 0 else 10.0
        client_kwargs: dict[str, Any] = {"timeout": list_timeout}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                resp = await client.get(f"{self._url}/api/tags")
                if resp.status_code >= 400:
                    raise DriverError(
                        f"ollama http {resp.status_code}",
                        kind="provider_error",
                        provider="ollama",
                        status_code=resp.status_code,
                    )
                data = resp.json()
        except httpx.ConnectError as exc:
            raise DriverUnavailable(
                f"ollama not reachable at {self._url}",
                kind="unavailable",
                retryable=True,
                provider="ollama",
            ) from exc
        except httpx.TimeoutException as exc:
            raise DriverError(
                "ollama /api/tags timed out",
                kind="timeout",
                retryable=True,
                provider="ollama",
            ) from exc
        except httpx.HTTPError as exc:
            raise DriverError(
                f"ollama transport error: {exc}",
                kind="provider_error",
                provider="ollama",
            ) from exc
        rows = data.get("models") if isinstance(data, dict) else None
        out: list[str] = []
        for m in rows or []:
            name = (m.get("name") or m.get("model")) if isinstance(m, dict) else None
            if name:
                out.append(str(name))
        return out
