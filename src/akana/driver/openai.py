"""OpenAI backend — Chat Completions over raw HTTP (the twin of ``ollama.py``).

Talks to ``{base_url}/chat/completions``; ``Authorization: Bearer {key}``.
An exact mirror of the ``ollama.py`` pattern: raw ``httpx`` (NO openai SDK), the
``_post_stream`` IO seam, PURE parse functions (testable without HTTP),
``transport`` injection (tests / custom HTTP). The only difference from Ollama is
the wire shape: Ollama emits NDJSON, while OpenAI emits ``data: {json}\\n\\n`` SSE +
a ``[DONE]`` sentinel and carries native function-calling (``tool_calls``).

``httpx`` is imported LAZILY inside the IO seam → importing the driver layer is
free until a real call runs (the Ollama pattern). Because parsing is a PURE
function, it can be tested without any HTTP at all.

VISION/images: this driver is TEXT-ONLY (like ollama). ``file_ids`` / image input
is OUT OF SCOPE — the message list carries plain ``[{role,content}]``.
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

#: SSE data-line prefix and end-of-stream sentinel (OpenAI Chat Completions).
_SSE_DATA_PREFIX = "data:"
_SSE_DONE = "[DONE]"


def _parse_openai_sse_line(line: str) -> dict[str, Any] | None:
    """One SSE line → a raw ``delta`` dict, or ``None`` to skip.

    The OpenAI stream emits ``data: {json}`` lines + a ``data: [DONE]`` sentinel;
    comment lines (starting with ``:``) and blanks are skipped. ``[DONE]`` returns a
    special marker (``{"__done__": True}``) → the caller closes the stream. Malformed
    JSON (a partial frame) is silently skipped (like the defensiveness of the ollama
    parser).

    The return is the raw ``choices[0].delta`` dict (which may contain ``content``
    and/or ``tool_calls`` and/or ``reasoning*``); the upper layer (``stream_chat``)
    reduces it to a ``ChatChunk``. ``usage`` (which may arrive in the final frame via
    stream_options) surfaces as ``{"__usage__": {...}}``."""
    text = line.strip()
    if not text or text.startswith(":"):
        return None
    if not text.startswith(_SSE_DATA_PREFIX):
        return None
    payload = text[len(_SSE_DATA_PREFIX) :].strip()
    if payload == _SSE_DONE:
        return {"__done__": True}
    try:
        ev = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(ev, dict):
        return None
    # OpenAI can carry an error inside a 200 body too ({"error": {...}}).
    if ev.get("error"):
        err = ev["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise DriverError(
            f"openai error: {msg}", kind="provider_error", provider="openai"
        )
    out: dict[str, Any] = {}
    choices = ev.get("choices")
    if isinstance(choices, list) and choices:
        choice0 = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice0.get("delta")
        if isinstance(delta, dict):
            out["delta"] = delta
        fr = choice0.get("finish_reason")
        if fr:
            out["finish_reason"] = fr
    usage = ev.get("usage")
    if isinstance(usage, dict):
        out["__usage__"] = usage
    return out or None


def _usage_from(usage: dict[str, Any] | None) -> dict[str, Any]:
    """OpenAI ``usage`` → the Akana token dict (the ollama shape: prompt/completion)."""
    usage = usage or {}
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
    }


def _reasoning_text(delta: dict[str, Any]) -> str:
    """Extract the thinking/reasoning text from a stream delta ('' if absent).

    OpenAI-compatible servers surface reasoning under different field names:
    ``reasoning_content`` (DeepSeek/compatible), ``reasoning`` (some gateways). We
    return the first non-empty one; '' if none."""
    for key in ("reasoning_content", "reasoning"):
        val = delta.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


class _ToolCallAccumulator:
    """Merge the piecewise ``tool_calls`` frames in the stream, keyed by index.

    OpenAI streams function arguments token by token: each frame carries ``index``;
    ``id``/``function.name`` arrive in the first frame, ``function.arguments`` are
    appended in pieces. On completion, ``finalize`` reduces each call to a single
    ``{"id","name","arguments"}`` dict (arguments is a raw JSON string)."""

    def __init__(self) -> None:
        self._by_index: dict[int, dict[str, Any]] = {}

    def add(self, frames: list[Any]) -> None:
        for frame in frames or []:
            if not isinstance(frame, dict):
                continue
            idx = frame.get("index")
            if idx is None:
                idx = len(self._by_index)
            slot = self._by_index.setdefault(
                int(idx), {"id": None, "name": "", "arguments": ""}
            )
            if frame.get("id"):
                slot["id"] = frame["id"]
            fn = frame.get("function")
            if isinstance(fn, dict):
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]

    def has_calls(self) -> bool:
        return bool(self._by_index)

    def finalize(self) -> list[dict[str, Any]]:
        return [self._by_index[i] for i in sorted(self._by_index)]


def _flatten_tool_calls(raw: Any) -> list[dict[str, Any]]:
    """Reduce a one-shot ``message.tool_calls`` to the stream ``finalize()`` shape.

    Non-stream response tool_calls are in OpenAI's NESTED shape
    (``{"id","type","function":{"name","arguments"}}``); but the provider loop
    (``openai_provider``) expects the FLAT ``{"id","name","arguments"}`` on both the
    stream and the one-shot path (the same contract as
    ``_ToolCallAccumulator.finalize``). If the two paths don't return the same shape,
    one-shot FC silently dispatches with an empty name/arguments. This helper equalizes
    the shapes; it defensively skips malformed/incomplete inputs (never raises)."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for tc in raw:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function")
        fn = fn if isinstance(fn, dict) else {}
        out.append(
            {
                "id": tc.get("id"),
                "name": fn.get("name") or "",
                "arguments": fn.get("arguments") or "",
            }
        )
    return out


class OpenAIDriver(Driver):
    """OpenAI Chat Completions backend (raw httpx; the twin of ``ollama``).

    Chat messages are already in the OpenAI shape (``role``/``content``); so raw
    message dicts carrying ``tool``/``tool_calls`` are also accepted directly (via
    ``extra``). The driver runs a SINGLE turn — the function-calling LOOP runs one
    layer up (``openai_provider``): when it sees a tool turn it dispatches and makes a
    new ``stream_chat``/``complete`` call (the gemini_provider pattern)."""

    name = "openai"

    def __init__(
        self,
        *,
        base_url: str = "https://api.openai.com/v1",
        api_key: str | None = None,
        model: str = "gpt-5.4",
        timeout: float = 300.0,
        transport: Any = None,  # httpx transport injection (tests / custom HTTP)
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def _post_stream(self, body: dict[str, Any]) -> AsyncIterator[str]:
        """IO seam: POST and yield raw SSE lines. Map transport faults onto the
        DriverError taxonomy (the mirror of ollama's ``_post_stream``)."""
        import httpx

        client_kwargs: dict[str, Any] = {"timeout": self._timeout}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                async with client.stream(
                    "POST",
                    f"{self._base_url}/chat/completions",
                    json=body,
                    headers=self._headers(),
                ) as resp:
                    if resp.status_code >= 400:
                        text = (await resp.aread()).decode("utf-8", "replace")
                        raise DriverError(
                            f"openai http {resp.status_code}: {text[:200]}",
                            kind="provider_error",
                            provider="openai",
                            status_code=resp.status_code,
                        )
                    async for line in resp.aiter_lines():
                        if line.strip():
                            yield line
        except httpx.ConnectError as exc:
            raise DriverUnavailable(
                f"openai not reachable at {self._base_url}",
                kind="unavailable",
                retryable=True,
                provider="openai",
            ) from exc
        except httpx.TimeoutException as exc:
            raise DriverError(
                "openai request timed out",
                kind="timeout",
                retryable=True,
                provider="openai",
            ) from exc
        except httpx.HTTPError as exc:
            raise DriverError(
                f"openai transport error: {exc}",
                kind="provider_error",
                provider="openai",
            ) from exc

    async def _post_json(self, body: dict[str, Any]) -> dict[str, Any]:
        """IO seam: one-shot (non-stream) POST → the parsed JSON body.

        ``complete`` (one-shot completion) uses this — instead of draining the stream,
        it reads ``choices[0].message`` directly (including tool_calls)."""
        import httpx

        client_kwargs: dict[str, Any] = {"timeout": self._timeout}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                resp = await client.post(
                    f"{self._base_url}/chat/completions",
                    json=body,
                    headers=self._headers(),
                )
                if resp.status_code >= 400:
                    text = resp.text
                    raise DriverError(
                        f"openai http {resp.status_code}: {text[:200]}",
                        kind="provider_error",
                        provider="openai",
                        status_code=resp.status_code,
                    )
                # A 200 body from a compatible/proxy gateway may still be non-JSON
                # (an HTML error page, a truncated frame). Map the decode failure onto
                # the DriverError taxonomy so callers never see a raw JSONDecodeError.
                try:
                    return resp.json()
                except ValueError as exc:
                    raise DriverError(
                        f"openai: invalid JSON response: {exc}",
                        kind="provider_error",
                        provider="openai",
                        status_code=resp.status_code,
                    ) from exc
        except httpx.ConnectError as exc:
            raise DriverUnavailable(
                f"openai not reachable at {self._base_url}",
                kind="unavailable",
                retryable=True,
                provider="openai",
            ) from exc
        except httpx.TimeoutException as exc:
            raise DriverError(
                "openai request timed out",
                kind="timeout",
                retryable=True,
                provider="openai",
            ) from exc
        except httpx.HTTPError as exc:
            raise DriverError(
                f"openai transport error: {exc}",
                kind="provider_error",
                provider="openai",
            ) from exc

    def _build_body(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None,
        stream: bool,
        tools: list[dict[str, Any]] | None,
        reasoning_effort: str | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model or self._model,
            "messages": messages,
            "stream": stream,
        }
        if stream:
            # Carry usage in the final frame (for token counting); a compatible server ignores it.
            body["stream_options"] = {"include_usage": True}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        return body

    @staticmethod
    def _to_message_dicts(messages: list[Message]) -> list[dict[str, Any]]:
        """A list of ``Message`` → OpenAI message dicts ([{role,content}, ...])."""
        return [{"role": m.role, "content": m.content} for m in messages]

    async def stream_chat(
        self,
        messages: list[Message] | list[dict[str, Any]],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[ChatChunk]:
        """Stream a SINGLE turn → ``ChatChunk``s (the final chunk has ``done=True``).

        ``messages`` may be either a neutral ``Message`` list or raw OpenAI message
        dicts (the provider loop feeds tool-call turns as raw dicts). The text delta
        goes to ``ChatChunk.delta``; the reasoning text to ``raw={"reasoning":...}``; the
        accumulated tool calls go to the FINAL ``done`` chunk's ``raw={"tool_calls":[...]}``
        field (the provider sees this and dispatches)."""
        msg_dicts = (
            messages
            if (messages and isinstance(messages[0], dict))
            else self._to_message_dicts(messages)  # type: ignore[arg-type]
        )
        body = self._build_body(
            msg_dicts,
            model=model,
            stream=True,
            tools=tools,
            reasoning_effort=reasoning_effort,
        )
        accum = _ToolCallAccumulator()
        usage: dict[str, Any] | None = None
        finish_reason: str | None = None
        async for line in self._post_stream(body):
            parsed = _parse_openai_sse_line(line)
            if parsed is None:
                continue
            if parsed.get("__done__"):
                break
            if "__usage__" in parsed:
                usage = parsed["__usage__"]
            if parsed.get("finish_reason"):
                finish_reason = parsed["finish_reason"]
            delta = parsed.get("delta")
            if isinstance(delta, dict):
                tcs = delta.get("tool_calls")
                if isinstance(tcs, list) and tcs:
                    accum.add(tcs)
                reasoning = _reasoning_text(delta)
                if reasoning:
                    yield ChatChunk(delta="", raw={"reasoning": reasoning})
                content = delta.get("content")
                if isinstance(content, str) and content:
                    yield ChatChunk(delta=content)
        raw_done: dict[str, Any] = {"finish_reason": finish_reason}
        if accum.has_calls():
            raw_done["tool_calls"] = accum.finalize()
        yield ChatChunk(done=True, usage=_usage_from(usage), raw=raw_done)

    async def complete_once(
        self,
        messages: list[Message] | list[dict[str, Any]],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """A ONE-SHOT (non-stream) turn → a raw ``{text, tool_calls, usage}`` dict.

        ``complete`` (the Driver contract) drains the stream; but the function-calling
        loop needs to see tool_calls clearly → this helper reads ``choices[0].message``
        directly. ``provider.complete_chat`` uses this."""
        msg_dicts = (
            messages
            if (messages and isinstance(messages[0], dict))
            else self._to_message_dicts(messages)  # type: ignore[arg-type]
        )
        body = self._build_body(
            msg_dicts,
            model=model,
            stream=False,
            tools=tools,
            reasoning_effort=reasoning_effort,
        )
        data = await self._post_json(body)
        choices = data.get("choices") if isinstance(data, dict) else None
        message: dict[str, Any] = {}
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            msg = choices[0].get("message")
            if isinstance(msg, dict):
                message = msg
        return {
            "text": str(message.get("content") or ""),
            "message": message,
            # Reduce to the FLAT shape ({"id","name","arguments"}) — the provider loop
            # expects the same contract as the stream's ``finalize()`` (else it dispatches with an empty name/arguments).
            "tool_calls": _flatten_tool_calls(message.get("tool_calls")),
            "usage": _usage_from(data.get("usage") if isinstance(data, dict) else None),
        }


__all__ = [
    "OpenAIDriver",
    "_parse_openai_sse_line",
    "_ToolCallAccumulator",
    "_flatten_tool_calls",
]
