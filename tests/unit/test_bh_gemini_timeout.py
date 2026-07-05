"""BUGFIX regression: the gemini provider now enforces a wall-clock timeout on the
google-genai streaming/one-shot calls.

Before the fix ``client.aio.models.generate_content`` / ``generate_content_stream``
were awaited with NO ``asyncio.wait_for`` and the text client was built with no
``http_options`` timeout, so a stalled connection (Google holds the stream open
without the terminal chunk) hung the whole turn far past the user-configured LLM
ceiling that every other provider enforces (cursor via ``_total_timeout``,
ollama/openai via their driver timeout).

These tests drive a FAKE google-genai client whose ``generate_content`` /
``generate_content_stream`` NEVER return (they await an event that is never set).
With the wall-clock ceiling squeezed to a few milliseconds (patching the SAME
``base.total_timeout`` resolver the cursor path uses), the stalled call must
raise ``LLMCallError(status_code=504)`` via the ``asyncio.wait_for`` timeout —
mirroring the cursor path's «LLM_TIMEOUT» 504 contract — instead of hanging.

Hermetic: no real network/SDK; ``asyncio.run`` for
``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`` compatibility (matches test_gemini_provider)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from akana_server.orchestrator import base, gemini_provider
from akana_server.orchestrator.errors import LLMCallError


def _settings(tmp_path):
    # Same hermetic shape as test_gemini_provider._settings: an empty tmp_path (no
    # store file) → resolve_gemini_model_tag falls to the default model.
    return SimpleNamespace(data_dir=tmp_path, cursor_model="composer-2", gemini_model="")


class _StallingModels:
    """``client.aio.models`` twin whose calls NEVER return (simulate a stalled connection).

    Both ``generate_content`` and ``generate_content_stream`` await an ``asyncio.Event``
    that is never set → the awaiting coroutine hangs until ``asyncio.wait_for`` cancels
    it (exactly the stalled-Google scenario the timeout guards against)."""

    def __init__(self) -> None:
        self.started = 0

    async def generate_content_stream(self, *, model, contents, config):
        self.started += 1
        await asyncio.Event().wait()  # never returns

    async def generate_content(self, *, model, contents, config):
        self.started += 1
        await asyncio.Event().wait()  # never returns


class _StallingClient:
    def __init__(self, models: _StallingModels) -> None:
        self.aio = SimpleNamespace(models=models)


def _patch_stalling_client(monkeypatch) -> _StallingModels:
    models = _StallingModels()
    monkeypatch.setattr(
        gemini_provider, "make_client", lambda settings, **_kw: _StallingClient(models)
    )
    # Squeeze the SAME wall-clock ceiling the cursor path uses down to a few ms so the
    # never-returning call trips wait_for immediately (default is 1800s → the test would hang).
    # gemini resolves the ceiling via base.total_timeout (the shared resolver).
    monkeypatch.setattr(base, "total_timeout", lambda settings: 0.05)
    return models


def test_stream_user_chat_stalled_call_raises_504(tmp_path, monkeypatch) -> None:
    """A stalled ``generate_content_stream`` → LLMCallError(504) via wait_for (not a hang)."""
    models = _patch_stalling_client(monkeypatch)

    async def run():
        async for _ in gemini_provider.stream_user_chat(_settings(tmp_path), "selam"):
            pass

    with pytest.raises(LLMCallError) as ei:
        asyncio.run(asyncio.wait_for(run(), timeout=5))  # outer guard: never hang the suite
    assert ei.value.status_code == 504
    assert "timed out" in str(ei.value).lower()
    assert models.started == 1  # the call was actually attempted (then cancelled)


def test_complete_chat_stalled_call_raises_504(tmp_path, monkeypatch) -> None:
    """A stalled ``generate_content`` (one-shot) → LLMCallError(504) via wait_for."""
    models = _patch_stalling_client(monkeypatch)

    with pytest.raises(LLMCallError) as ei:
        asyncio.run(
            asyncio.wait_for(
                gemini_provider.complete_chat(_settings(tmp_path), "selam"), timeout=5
            )
        )
    assert ei.value.status_code == 504
    assert "timed out" in str(ei.value).lower()
    assert models.started == 1


def test_timeout_disabled_sentinel_does_not_wrap(tmp_path, monkeypatch) -> None:
    """The ``0 = disabled`` ceiling sentinel must NOT be passed to wait_for.

    ``combine_cap`` returns 0 when the ceiling is disabled; ``asyncio.wait_for(coro, 0)``
    would fire INSTANTLY (Bug: read_ndjson_line's timeout=0 pitfall). ``_with_timeout``
    must instead await unbounded when the timeout is non-positive — so a call that DOES
    return completes normally rather than dying on an instant false timeout."""
    monkeypatch.setattr(base, "total_timeout", lambda settings: 0.0)

    class _Resp:
        text = "ok"
        usage_metadata = None
        function_calls = None

    class _Models:
        async def generate_content(self, *, model, contents, config):
            return _Resp()

    monkeypatch.setattr(
        gemini_provider,
        "make_client",
        lambda settings, **_kw: SimpleNamespace(aio=SimpleNamespace(models=_Models())),
    )
    text, status, _usage = asyncio.run(
        gemini_provider.complete_chat(_settings(tmp_path), "selam")
    )
    assert text == "ok" and status == "finished"  # no instant false 504 at timeout=0
