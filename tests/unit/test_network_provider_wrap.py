"""NetworkEngine F0 — provider wrapping integration (behavior preservation + breaker).

NO real `claude`/`cursor` process: ``asyncio.create_subprocess_exec`` is replaced
with a fake proc backed by a StreamReader (the test_claude_provider.py pattern).
Verified:

* **Behavior preservation** — a successful stream produces the exact same event
  sequence, the process-wide breaker stays CLOSED (the happy path is unaffected by
  wrapping).
* **Circuit breaker** — once the consecutive-error threshold is exceeded the breaker
  opens and, WITHOUT the next spawn, raises :class:`BreakerOpenError` (Turkish).
* **Auth is not retried** — a claude auth error fails in a single attempt (the stream
  is already retry-free; the breaker counts the error).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from akana_server.config import Settings, load_settings
from akana_server.network.breaker import BreakerState
from akana_server.network.guard import (
    BreakerOpenError,
    global_registry,
    reset_global_registry,
)
from akana_server.orchestrator import claude_provider
from akana_server.orchestrator.llm_dispatch import LLMCallError


@pytest.fixture(autouse=True)
def _fresh_registry():
    """Every test starts with a fresh process-wide breaker registry (isolation)."""
    reset_global_registry()
    yield
    reset_global_registry()


class _FakeProc:
    pid = 4242
    returncode: int | None = 0

    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.stdin = None  # Windows cmd spill path checks proc.stdin (None → write skipped)

    def feed(self, *events: dict[str, Any]) -> None:
        for ev in events:
            self.stdout.feed_data((json.dumps(ev) + "\n").encode("utf-8"))
        self.stdout.feed_eof()

    async def wait(self) -> int:
        return self.returncode or 0

    def kill(self) -> None:  # pragma: no cover
        self.returncode = -9


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Settings:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    return load_settings()


def _patch_spawn(monkeypatch: pytest.MonkeyPatch, factory) -> dict[str, Any]:
    calls = {"n": 0}

    async def _fake_spawn(*cmd: str, **kwargs: Any):
        calls["n"] += 1
        return factory()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
    return calls


_INIT = {"type": "system", "subtype": "init", "session_id": "s1"}


def _delta(text: str) -> dict[str, Any]:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text},
        },
    }


_RESULT_OK = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "merhaba",
    "usage": {"input_tokens": 5, "output_tokens": 3},
    "session_id": "s1",
}
_RESULT_AUTH = {
    "type": "result",
    "subtype": "error",
    "is_error": True,
    "result": "Failed to authenticate",
    "api_error_status": 401,
    "session_id": "s1",
}
_RESULT_5XX = {
    "type": "result",
    "subtype": "error",
    "is_error": True,
    "result": "upstream 503 service unavailable",
    "api_error_status": 503,
    "session_id": "s1",
}


def test_success_stream_unchanged_breaker_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    def make_proc() -> _FakeProc:
        p = _FakeProc()
        p.feed(_INIT, _delta("mer"), _delta("haba"), _RESULT_OK)
        return p

    _patch_spawn(monkeypatch, make_proc)

    async def run() -> list[dict[str, Any]]:
        return [ev async for ev in claude_provider.stream_user_chat(settings, "selam")]

    events = asyncio.run(run())
    assert events[0] == {"agent_id": "s1"}
    assert [e["delta"] for e in events if "delta" in e] == ["mer", "haba"]
    final = events[-1]
    assert final["done"] is True
    assert final["text"] == "merhaba"
    # Behavior preservation: the process-wide breaker is still closed.
    assert global_registry().get("claude").state() == BreakerState.CLOSED


def test_repeated_5xx_opens_breaker(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)
    # Lower the threshold: the circuit should open after 2 errors.
    monkeypatch.setenv("AKANA_NETWORK_BREAKER_THRESHOLD", "2")
    monkeypatch.setenv("AKANA_NETWORK_BREAKER_COOLDOWN", "100")

    def make_failing() -> _FakeProc:
        p = _FakeProc()
        p.feed(_INIT, _RESULT_5XX)
        return p

    calls = _patch_spawn(monkeypatch, make_failing)

    async def one() -> None:
        async for _ in claude_provider.stream_user_chat(settings, "x"):
            pass

    # The first two calls fail with 5xx → the breaker opens.
    for _ in range(2):
        with pytest.raises(LLMCallError):
            asyncio.run(one())
    assert calls["n"] == 2
    assert global_registry().get("claude").state() == BreakerState.OPEN

    # Third call: breaker is open → NO spawn, fast-fail.
    with pytest.raises(BreakerOpenError):
        asyncio.run(one())
    assert calls["n"] == 2  # no new spawn


def test_auth_failure_not_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    def make_auth_fail() -> _FakeProc:
        p = _FakeProc()
        p.feed(_INIT, _RESULT_AUTH)
        return p

    calls = _patch_spawn(monkeypatch, make_auth_fail)

    async def one() -> None:
        async for _ in claude_provider.stream_user_chat(settings, "x"):
            pass

    with pytest.raises(LLMCallError) as ei:
        asyncio.run(one())
    # Auth → Turkish "claude login" message (the existing mapping is preserved), single attempt.
    assert "claude login" in str(ei.value)
    assert calls["n"] == 1
