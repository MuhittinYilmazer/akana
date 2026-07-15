"""OpenAIDriver: SSE line parsing + stream/complete mapping, hermetic (no network).

Twin of the ``ollama`` driver test: raw parse tested directly as a PURE function;
the HTTP seam is driven with ``httpx.MockTransport`` (NO real OpenAI / network).
For ``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`` compatibility, uses ``asyncio.run`` (so
async tests still run without pytest-asyncio — the same pattern as the
gemini/ollama tests)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from akana.driver.base import DriverError, DriverUnavailable, Message
from akana.driver.openai import (
    OpenAIDriver,
    _parse_openai_sse_line,
    _ToolCallAccumulator,
)


async def _drain(agen):
    return [chunk async for chunk in agen]


def _sse(*objs) -> str:
    """OpenAI SSE body: each object as ``data: {json}\\n\\n``, then ``data: [DONE]``."""
    lines = "".join("data: " + json.dumps(o) + "\n\n" for o in objs)
    return lines + "data: [DONE]\n\n"


def _delta(content=None, tool_calls=None, finish_reason=None, usage=None) -> dict:
    """Build a single SSE frame (choices[0].delta)."""
    d: dict = {}
    if content is not None or tool_calls is not None:
        delta: dict = {}
        if content is not None:
            delta["content"] = content
        if tool_calls is not None:
            delta["tool_calls"] = tool_calls
        d["choices"] = [{"delta": delta, "finish_reason": finish_reason}]
    elif finish_reason is not None:
        d["choices"] = [{"delta": {}, "finish_reason": finish_reason}]
    if usage is not None:
        d["usage"] = usage
    return d


# --- PURE parse tests (no HTTP) ----------------------------------------


def test_parse_line_delta_done_and_garbage():
    assert _parse_openai_sse_line("") is None
    assert _parse_openai_sse_line(": keep-alive comment") is None
    assert _parse_openai_sse_line("event: ping") is None  # no data: prefix
    assert _parse_openai_sse_line("data: not-json") is None
    assert _parse_openai_sse_line("data: [DONE]") == {"__done__": True}
    parsed = _parse_openai_sse_line(
        'data: {"choices":[{"delta":{"content":"hi"}}]}'
    )
    assert parsed["delta"] == {"content": "hi"}


def test_parse_line_usage_and_finish_reason():
    parsed = _parse_openai_sse_line(
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":3,"completion_tokens":5}}'
    )
    assert parsed["finish_reason"] == "stop"
    assert parsed["__usage__"] == {"prompt_tokens": 3, "completion_tokens": 5}


def test_parse_line_error_raises():
    with pytest.raises(DriverError):
        _parse_openai_sse_line('data: {"error":{"message":"bad key"}}')


def test_tool_call_accumulator_merges_streamed_fragments():
    """Arguments stream token-by-token; the accumulator merges them by index."""
    accum = _ToolCallAccumulator()
    accum.add([{"index": 0, "id": "call_1", "function": {"name": "memory_search", "arguments": '{"qu'}}])
    accum.add([{"index": 0, "function": {"arguments": 'ery":"kahve"}'}}])
    assert accum.has_calls() is True
    calls = accum.finalize()
    assert calls == [{"id": "call_1", "name": "memory_search", "arguments": '{"query":"kahve"}'}]


# --- stream_chat (MockTransport) ------------------------------------------


def test_stream_maps_content_and_usage():
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        captured["auth"] = req.headers.get("authorization")
        return httpx.Response(
            200,
            text=_sse(
                _delta(content="Mer"),
                _delta(content="haba"),
                _delta(finish_reason="stop", usage={"prompt_tokens": 5, "completion_tokens": 2}),
            ),
        )

    drv = OpenAIDriver(api_key="sk-test", model="gpt-5.4", transport=httpx.MockTransport(handler))
    chunks = asyncio.run(_drain(drv.stream_chat([Message("system", "p"), Message("user", "selam")])))

    assert "".join(c.delta for c in chunks if c.delta) == "Merhaba"
    assert chunks[-1].done is True
    assert chunks[-1].usage["completion_tokens"] == 2
    # messages pass through; stream + auth header set
    assert captured["body"]["messages"][0] == {"role": "system", "content": "p"}
    assert captured["body"]["stream"] is True
    assert captured["auth"] == "Bearer sk-test"


def test_stream_surfaces_reasoning_in_raw():
    """A delta containing reasoning → ChatChunk.raw['reasoning'] (content empty)."""
    def handler(_req):
        return httpx.Response(
            200,
            text=_sse(
                {"choices": [{"delta": {"reasoning_content": "düşünüyorum..."}}]},
                _delta(content="cevap", finish_reason="stop"),
            ),
        )

    drv = OpenAIDriver(api_key="k", transport=httpx.MockTransport(handler))
    chunks = asyncio.run(_drain(drv.stream_chat([Message("user", "x")])))
    reasonings = [c.raw["reasoning"] for c in chunks if c.raw and c.raw.get("reasoning")]
    assert reasonings == ["düşünüyorum..."]
    assert "".join(c.delta for c in chunks if c.delta) == "cevap"


def test_stream_accumulates_tool_calls_onto_done_chunk():
    """tool_calls frames in the stream accumulate onto the LAST done chunk's raw."""
    def handler(_req):
        return httpx.Response(
            200,
            text=_sse(
                _delta(tool_calls=[{"index": 0, "id": "t1", "function": {"name": "save_memory", "arguments": '{"text":'}}]),
                _delta(tool_calls=[{"index": 0, "function": {"arguments": '"kahve"}'}}]),
                _delta(finish_reason="tool_calls"),
            ),
        )

    drv = OpenAIDriver(api_key="k", transport=httpx.MockTransport(handler))
    chunks = asyncio.run(_drain(drv.stream_chat([Message("user", "x")], tools=[{"type": "function", "function": {"name": "save_memory"}}])))
    done = chunks[-1]
    assert done.done is True
    assert done.raw["tool_calls"] == [{"id": "t1", "name": "save_memory", "arguments": '{"text":"kahve"}'}]


def test_build_body_includes_tools_and_reasoning():
    captured: dict = {}

    def handler(req):
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, text=_sse(_delta(content="ok", finish_reason="stop")))

    drv = OpenAIDriver(api_key="k", transport=httpx.MockTransport(handler))
    asyncio.run(_drain(drv.stream_chat(
        [Message("user", "x")],
        tools=[{"type": "function", "function": {"name": "memory_search"}}],
        reasoning_effort="high",
    )))
    assert captured["body"]["tool_choice"] == "auto"
    assert captured["body"]["reasoning_effort"] == "high"
    assert captured["body"]["tools"][0]["function"]["name"] == "memory_search"


def test_complete_once_reads_message_and_tool_calls():
    """Non-stream one-shot: text + FLAT tool_calls are read (same shape as the stream's finalize()).

    OpenAI non-stream tool_calls come back NESTED (``function.name``); but the provider
    loop expects FLAT ``{"id","name","arguments"}`` on both the stream and one-shot paths.
    ``complete_once`` reconciles this via ``_flatten_tool_calls`` — this verifies that
    contract (regression: without flattening, dispatch happens with an empty name/arguments)."""
    def handler(_req):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "tam yanıt",
                            "tool_calls": [
                                {"id": "t1", "type": "function", "function": {"name": "memory_search", "arguments": '{"query":"x"}'}}
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 6},
            },
        )

    drv = OpenAIDriver(api_key="k", transport=httpx.MockTransport(handler))
    result = asyncio.run(drv.complete_once([Message("user", "x")]))
    assert result["text"] == "tam yanıt"
    tc = result["tool_calls"][0]
    assert tc == {"id": "t1", "name": "memory_search", "arguments": '{"query":"x"}'}
    assert result["usage"]["completion_tokens"] == 6


def test_http_error_maps_to_driver_error():
    def handler(_req):
        return httpx.Response(401, text="invalid api key")

    drv = OpenAIDriver(api_key="bad", transport=httpx.MockTransport(handler))
    with pytest.raises(DriverError) as ei:
        asyncio.run(_drain(drv.stream_chat([Message("user", "x")])))
    assert ei.value.status_code == 401


def test_connect_error_maps_to_unavailable():
    def handler(req):
        raise httpx.ConnectError("connection refused", request=req)

    drv = OpenAIDriver(api_key="k", transport=httpx.MockTransport(handler))
    with pytest.raises(DriverUnavailable):
        asyncio.run(_drain(drv.stream_chat([Message("user", "x")])))


# -- generation-timeout knob (the ollama _chat_timeout twin) --------------------


def test_chat_timeout_positive_is_a_uniform_ceiling():
    """timeout > 0 → the historical behavior: connect/read/write/pool all bounded."""
    from akana.driver.openai import _chat_timeout

    t = _chat_timeout(45.0)
    assert t.connect == 45.0
    assert t.read == 45.0
    assert t.write == 45.0
    assert t.pool == 45.0


def test_chat_timeout_zero_disables_read_but_keeps_connect_finite():
    """timeout <= 0 → the inter-token (read) ceiling is disabled so a slow reply is
    never cut off, while the handshake still fails fast (an unreachable endpoint must
    not hang forever)."""
    from akana.driver.openai import _CONNECT_TIMEOUT_S, _chat_timeout

    t = _chat_timeout(0.0)
    assert t.read is None
    assert t.connect == _CONNECT_TIMEOUT_S
