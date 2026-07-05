"""OllamaDriver: NDJSON line parsing + stream mapping, hermetic (no HTTP).

The HTTP IO seam (``_post_stream``) is monkeypatched to yield canned NDJSON, so
no httpx transport or running Ollama is needed. Parsing is exercised directly.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from akana.driver.base import DriverError, DriverUnavailable, Message
from akana.driver.ollama import OllamaDriver, _chat_timeout, _parse_ollama_line


async def _drain(agen):
    return [chunk async for chunk in agen]


def test_parse_line_delta_done_and_garbage():
    assert _parse_ollama_line("not json") is None
    assert _parse_ollama_line(json.dumps({"message": {"content": "hi"}, "done": False})).delta == "hi"
    assert _parse_ollama_line(json.dumps({"message": {"content": ""}, "done": False})) is None
    done = _parse_ollama_line(json.dumps({"done": True, "prompt_eval_count": 3, "eval_count": 4}))
    assert done.done is True
    assert done.usage == {"prompt_tokens": 3, "completion_tokens": 4}


def test_parse_line_error_raises():
    with pytest.raises(DriverError):
        _parse_ollama_line(json.dumps({"error": "model 'x' not found"}))


def test_stream_maps_content_and_usage(monkeypatch):
    drv = OllamaDriver()
    captured: dict = {}

    async def fake_post_stream(body):
        captured["body"] = body
        yield json.dumps({"message": {"role": "assistant", "content": "Mer"}, "done": False})
        yield json.dumps({"message": {"role": "assistant", "content": "haba"}, "done": False})
        yield json.dumps({"done": True, "prompt_eval_count": 5, "eval_count": 2})

    monkeypatch.setattr(drv, "_post_stream", fake_post_stream)

    chunks = asyncio.run(
        _drain(drv.stream_chat([Message("system", "p"), Message("user", "selam")]))
    )

    assert "".join(c.delta for c in chunks if c.delta) == "Merhaba"
    assert chunks[-1].done is True
    assert chunks[-1].usage["completion_tokens"] == 2
    # system flows natively; messages pass through unchanged; stream requested
    assert captured["body"]["messages"][0] == {"role": "system", "content": "p"}
    assert captured["body"]["stream"] is True


def test_complete_drains_stream(monkeypatch):
    drv = OllamaDriver()

    async def fake_post_stream(body):
        yield json.dumps({"message": {"content": "pong"}, "done": False})
        yield json.dumps({"done": True, "prompt_eval_count": 1, "eval_count": 1})

    monkeypatch.setattr(drv, "_post_stream", fake_post_stream)

    result = asyncio.run(drv.complete([Message("user", "ping")]))

    assert result.text == "pong"
    assert result.model == "ollama"


def test_error_line_propagates_through_stream(monkeypatch):
    drv = OllamaDriver()

    async def fake_post_stream(body):
        yield json.dumps({"error": "model 'nope' not found"})

    monkeypatch.setattr(drv, "_post_stream", fake_post_stream)

    with pytest.raises(DriverError):
        asyncio.run(_drain(drv.stream_chat([Message("user", "hi")])))


# -- model listing (/api/tags) — feeds the Ollama model dropdown in the switcher ----


def test_list_models_returns_installed_names():
    import httpx

    def handler(request):
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "llama3.1:latest"},
                    {"name": "qwen2.5:7b"},
                    {"model": "gemma2"},  # some versions use the 'model' field
                ]
            },
        )

    drv = OllamaDriver(transport=httpx.MockTransport(handler))
    assert asyncio.run(drv.list_models()) == ["llama3.1:latest", "qwen2.5:7b", "gemma2"]


def test_list_models_unreachable_raises_unavailable():
    import httpx

    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    drv = OllamaDriver(transport=httpx.MockTransport(handler))
    with pytest.raises(DriverUnavailable):
        asyncio.run(drv.list_models())


def test_list_models_empty_when_none_installed():
    import httpx

    drv = OllamaDriver(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"models": []}))
    )
    assert asyncio.run(drv.list_models()) == []


def test_chat_timeout_positive_is_a_uniform_ceiling():
    """A positive timeout keeps the historical uniform read/connect ceiling."""
    t = _chat_timeout(120.0)
    assert t.read == 120.0
    assert t.connect == 120.0


@pytest.mark.parametrize("disabled", [0.0, -1.0])
def test_chat_timeout_disabled_removes_only_the_read_ceiling(disabled):
    """<=0 = no generation ceiling: read is unbounded, connect stays finite so an
    unreachable server still fails fast."""
    t = _chat_timeout(disabled)
    assert t.read is None
    assert t.connect is not None and t.connect > 0


def test_list_models_disabled_timeout_does_not_zero_the_listing_call():
    """With the generation timeout disabled (0), list_models must still use a real
    (10 s) timeout, not min(0, 10)=0 which httpx treats as an instant timeout."""
    import httpx

    drv = OllamaDriver(
        timeout=0.0,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"models": [{"name": "llama3.1"}]})
        ),
    )
    assert asyncio.run(drv.list_models()) == ["llama3.1"]
