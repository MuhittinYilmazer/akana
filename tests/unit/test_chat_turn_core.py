"""Unit tests for the shared non-streaming turn core (akana_server/api/routes/chat/turn_core.py).

These pin the safeguards the blocking POST /chat path gained by moving onto the shared
core — parity with the streaming producer:
  * empty-response auto-retry (once) before returning an empty outcome,
  * BreakerOpenError → TurnError(LLM_RATE_LIMITED),
  * LLMCallError → TurnError with the streaming error codes + on_active_run_reset.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from akana_server.api.routes import chat as chat_routes
from akana_server.api.routes.chat.turn_core import TurnError, run_nonstreaming_turn
from akana_server.network.breaker import BreakerOpenError
from akana_server.orchestrator.llm_dispatch import LLMCallError


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_returns_outcome_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(settings: Any, text: str, **kw: Any):
        return "hello", {
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "tool_calls": [{"name": "x"}],
            "agent_id": "ag-1",
        }

    monkeypatch.setattr(chat_routes, "complete_chat_with_usage", fake)
    out = _run(run_nonstreaming_turn(object(), "hi", conversation_id="c1"))
    assert out.text == "hello"
    assert out.tool_calls == [{"name": "x"}]
    assert out.agent_id == "ag-1"
    assert out.usage["prompt_tokens"] == 3


def test_empty_response_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty first response is retried exactly once; the second answer is returned."""
    calls: list[int] = []

    async def fake(settings: Any, text: str, **kw: Any):
        calls.append(1)
        if len(calls) == 1:
            return "   ", {"tool_calls": []}  # empty (whitespace only) + no tool call
        return "recovered", {"tool_calls": []}

    monkeypatch.setattr(chat_routes, "complete_chat_with_usage", fake)
    out = _run(run_nonstreaming_turn(object(), "hi", conversation_id="c1"))
    assert len(calls) == 2, "empty response should trigger exactly one retry"
    assert out.text == "recovered"


def test_second_empty_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """A persistently empty response is returned after the single retry (no infinite loop)."""
    calls: list[int] = []

    async def fake(settings: Any, text: str, **kw: Any):
        calls.append(1)
        return "", {"tool_calls": []}

    monkeypatch.setattr(chat_routes, "complete_chat_with_usage", fake)
    out = _run(run_nonstreaming_turn(object(), "hi", conversation_id="c1"))
    assert len(calls) == 2  # first + one retry, then give up
    assert out.text == ""


def test_tool_only_turn_is_not_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """No text but a tool call is a valid turn — it must NOT be retried as 'empty'."""
    calls: list[int] = []

    async def fake(settings: Any, text: str, **kw: Any):
        calls.append(1)
        return "", {"tool_calls": [{"name": "search"}]}

    monkeypatch.setattr(chat_routes, "complete_chat_with_usage", fake)
    out = _run(run_nonstreaming_turn(object(), "hi", conversation_id="c1"))
    assert len(calls) == 1
    assert out.tool_calls == [{"name": "search"}]


def test_breaker_open_maps_to_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(settings: Any, text: str, **kw: Any):
        raise BreakerOpenError("cursor", 12.0)

    monkeypatch.setattr(chat_routes, "complete_chat_with_usage", fake)
    with pytest.raises(TurnError) as ei:
        _run(run_nonstreaming_turn(object(), "hi", conversation_id="c1"))
    assert ei.value.code == "LLM_RATE_LIMITED"
    assert ei.value.status_code == 503


def test_llm_timeout_maps_to_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(settings: Any, text: str, **kw: Any):
        raise LLMCallError("LLM_TIMEOUT: took too long", status_code=504)

    monkeypatch.setattr(chat_routes, "complete_chat_with_usage", fake)
    with pytest.raises(TurnError) as ei:
        _run(run_nonstreaming_turn(object(), "hi", conversation_id="c1"))
    assert ei.value.code == "LLM_TIMEOUT"
    assert ei.value.status_code == 504


def test_bad_request_maps(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(settings: Any, text: str, **kw: Any):
        raise LLMCallError("bad input", status_code=400)

    monkeypatch.setattr(chat_routes, "complete_chat_with_usage", fake)
    with pytest.raises(TurnError) as ei:
        _run(run_nonstreaming_turn(object(), "hi", conversation_id="c1"))
    assert ei.value.code == "BAD_REQUEST"


def test_active_run_triggers_reset_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_calls: list[int] = []

    async def fake(settings: Any, text: str, **kw: Any):
        # bridge_pool._is_active_run_message matches this phrasing.
        raise LLMCallError("There is already an active run for this session", status_code=409)

    async def on_reset() -> None:
        reset_calls.append(1)

    monkeypatch.setattr(chat_routes, "complete_chat_with_usage", fake)
    with pytest.raises(TurnError):
        _run(
            run_nonstreaming_turn(
                object(), "hi", conversation_id="c1", on_active_run_reset=on_reset
            )
        )
    assert reset_calls == [1], "an active-run error must trigger the bridge-reset callback"
