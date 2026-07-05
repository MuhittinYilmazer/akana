"""Stream chat SSE tests — done.text fallback, normal deltas, persistence."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app
from akana_server.orchestrator.llm_dispatch import LLMCallError


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8766")
    monkeypatch.setenv("CURSOR_API_KEY", "x")
    # Post-turn LLM memory capture now runs IN THE BACKGROUND; since it is on by
    # default it would spawn real `claude -p` subprocesses in these tests (which can
    # outlive teardown and leak, slowing the test down). Disable capture here — that
    # capture is moved to the background is separately verified in
    # test_stream_done_not_blocked_by_memory_capture (by mocking propose_memory_captures).
    monkeypatch.setenv("AKANA_MEMORY_LLM_CAPTURE", "0")
    app = create_app()
    with TestClient(app) as c:
        yield c


def _parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse SSE body into [(event_name, data_dict), ...]."""
    events: list[tuple[str, dict[str, Any]]] = []
    event_name = "message"
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
        if data_lines:
            try:
                payload = json.loads("\n".join(data_lines))
            except json.JSONDecodeError:
                payload = {"raw": "\n".join(data_lines)}
            events.append((event_name, payload))
    return events


def _events_of(events: list[tuple[str, dict[str, Any]]], name: str) -> list[dict[str, Any]]:
    return [p for n, p in events if n == name]


def test_stream_persists_when_bridge_only_sends_done_text(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: bridge skipped deltas and only emitted done.text — assistant must still persist."""

    async def _mock_stream(*_args, **_kwargs) -> AsyncIterator[dict[str, Any]]:
        yield {
            "done": True,
            "text": "Tek seferde gelen tam yanıt.",
            "usage": {"prompt_tokens": 3, "completion_tokens": 7, "tool_calls": []},
            "status": "finished",
            "tool_calls": [],
        }

    monkeypatch.setattr(
        "akana_server.api.routes.chat.stream_user_chat",
        _mock_stream,
    )

    created = client.post("/api/v1/conversations", json={"title": "Stream fallback"})
    cid = created.json()["id"]

    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"text": "merhaba", "conversation_id": cid},
    ) as response:
        assert response.status_code == 200
        body = response.read().decode("utf-8")

    events = _parse_sse(body)
    delta_texts = [p.get("text", "") for p in _events_of(events, "delta")]
    assert "".join(delta_texts) == "Tek seferde gelen tam yanıt."

    done_events = _events_of(events, "done")
    assert done_events, "stream did not emit a done event"
    assert done_events[-1]["text"] == "Tek seferde gelen tam yanıt."

    messages = client.get(f"/api/v1/conversations/{cid}/messages").json()["messages"]
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant"], f"got roles {roles}"
    assert messages[1]["content"] == "Tek seferde gelen tam yanıt."


def test_tts_end_sent_even_without_tts(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even on a NON-voice turn (no tts query param → tts_active=false) the backend
    must send tts_end. Otherwise the voice-mode client stayed STUCK on
    ttsStreamOpen=true, freezing for ~10s on 'responding' (user bug). tts_end = the
    'turn over, reopen the mic' signal — sent whether or not there is audio."""

    async def _mock_stream(*_args, **_kwargs) -> AsyncIterator[dict[str, Any]]:
        yield {
            "done": True,
            "text": "Kısa yanıt.",
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "tool_calls": []},
            "status": "finished",
            "tool_calls": [],
        }

    monkeypatch.setattr("akana_server.api.routes.chat.stream_user_chat", _mock_stream)
    cid = client.post("/api/v1/conversations", json={"title": "tts_end"}).json()["id"]

    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"text": "merhaba", "conversation_id": cid},  # NO tts query param
    ) as response:
        assert response.status_code == 200
        body = response.read().decode("utf-8")

    events = _parse_sse(body)
    tts_end = _events_of(events, "tts_end")
    assert tts_end, "tts_end must be sent even on a non-voice turn (so 'responding' does not freeze)"
    assert tts_end[-1].get("tts_active") is False  # no audio produced but the signal arrived


def test_stream_tool_calls_collapse_start_end_into_one(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (two-chats-at-once bug): the orchestrator emits TWO events per tool
    (phase=start, then end, same id). The streamed events must go to the client AS-IS
    (for live status) but the ``tool_calls`` in the done payload must be ONE record per
    tool — otherwise the "N tools" header (raw length) shows twice the deduplicated
    cards ("4 tools" but 2 cards)."""

    async def _mock_stream(*_args, **_kwargs) -> AsyncIterator[dict[str, Any]]:
        yield {"tool_call": {"id": "t1", "name": "memory_recall", "phase": "start", "args": {"q": "x"}, "result": None, "status": None}}
        yield {"tool_call": {"id": "t1", "name": "memory_recall", "phase": "end", "args": None, "result": "11 sonuç", "status": "ok"}}
        yield {"tool_call": {"id": "t2", "name": "task", "phase": "start", "args": None, "result": None, "status": None}}
        yield {"tool_call": {"id": "t2", "name": "task", "phase": "end", "args": None, "result": "0 kayıt", "status": "ok"}}
        yield {"delta": "tamam", "done": False}
        yield {
            "done": True,
            "text": "tamam",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "tool_calls": []},
            "status": "finished",
            "tool_calls": [],
        }

    monkeypatch.setattr(
        "akana_server.api.routes.chat.stream_user_chat",
        _mock_stream,
    )

    created = client.post("/api/v1/conversations", json={"title": "Tool dedupe"})
    cid = created.json()["id"]

    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"text": "ara", "conversation_id": cid},
    ) as response:
        assert response.status_code == 200
        body = response.read().decode("utf-8")

    events = _parse_sse(body)
    # Live: BOTH the start and end events must stream to the client (status update).
    assert len(_events_of(events, "tool_call")) == 4, "start+end events must stream to the client as-is"

    # done: ONE record per tool (dedup) — 2 tools, not 4.
    done_events = _events_of(events, "done")
    assert done_events, "stream did not emit a done event"
    calls = done_events[-1]["tool_calls"]
    assert len(calls) == 2, f"done.tool_calls must have one record per tool, got {len(calls)}"
    by_id = {c["id"]: c for c in calls}
    assert sorted(by_id) == ["t1", "t2"]
    # the end event must update the start record IN PLACE (result/status filled, args preserved).
    assert by_id["t1"]["result"] == "11 sonuç"
    assert by_id["t1"]["status"] == "ok"
    assert by_id["t1"]["args"] == {"q": "x"}
    assert by_id["t2"]["result"] == "0 kayıt"


def test_stream_persists_partial_when_bridge_errors_mid_stream(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Partial-save: bridge fails after some deltas — what we got must persist."""

    async def _mock_stream(*_args, **_kwargs) -> AsyncIterator[dict[str, Any]]:
        yield {"delta": "Yarım ", "done": False}
        yield {"delta": "yanıt", "done": False}
        raise LLMCallError("bridge crashed mid-stream", status_code=503)

    monkeypatch.setattr(
        "akana_server.api.routes.chat.stream_user_chat",
        _mock_stream,
    )

    created = client.post("/api/v1/conversations", json={"title": "Partial save"})
    cid = created.json()["id"]

    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"text": "merhaba", "conversation_id": cid},
    ) as response:
        assert response.status_code == 200
        body = response.read().decode("utf-8")

    events = _parse_sse(body)
    error_events = _events_of(events, "error")
    assert error_events, "expected an error SSE event"
    assert error_events[0]["partial_text"] == "Yarım yanıt"

    messages = client.get(f"/api/v1/conversations/{cid}/messages").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "Yarım yanıt"


def test_stream_persists_error_turn_when_bridge_fails_before_any_delta(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (Issue 3): if the LLM fails BEFORE the first delta (LLM_UNAVAILABLE),
    the error card lived only in the client's localStorage (``_localError``) → it
    disappeared after F5 or a model switch. Now the failed turn persists ON THE SERVER
    as a ``role="error"`` row → on a /messages reload the error card comes back like
    any other message. Since the user turn is already persisted before the LLM call,
    the roles are ["user", "error"]."""

    unavailable = "Claude Fable 5 is currently unavailable. Learn more: https://x/news"

    async def _mock_stream(*_args, **_kwargs) -> AsyncIterator[dict[str, Any]]:
        raise LLMCallError(unavailable, status_code=503)
        yield  # pragma: no cover - needed so this is a generator (unreachable)

    monkeypatch.setattr(
        "akana_server.api.routes.chat.stream_user_chat",
        _mock_stream,
    )

    created = client.post("/api/v1/conversations", json={"title": "Err persist"})
    cid = created.json()["id"]

    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"text": "merhaba", "conversation_id": cid},
    ) as response:
        assert response.status_code == 200
        body = response.read().decode("utf-8")

    events = _parse_sse(body)
    error_events = _events_of(events, "error")
    assert error_events, "expected an error SSE event"
    assert error_events[0]["code"] == "LLM_UNAVAILABLE"
    assert error_events[0]["partial_text"] == ""

    # F5 simulation: reload from the server → the error row persists, its content same as the SSE message.
    messages = client.get(f"/api/v1/conversations/{cid}/messages").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "error"], f"got {messages}"
    assert messages[1]["content"] == unavailable


def test_stream_persists_error_turn_on_empty_response(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (Issue 3, symmetric): when the model returns neither text nor a
    tool call (EMPTY_RESPONSE) the failed turn also persists on the server as
    ``role="error"`` → the empty-response card comes back after F5."""

    async def _mock_stream(*_args, **_kwargs) -> AsyncIterator[dict[str, Any]]:
        yield {
            "done": True,
            "text": "",
            "usage": {"prompt_tokens": 1, "completion_tokens": 0, "tool_calls": []},
            "status": "finished",
            "tool_calls": [],
        }

    monkeypatch.setattr(
        "akana_server.api.routes.chat.stream_user_chat",
        _mock_stream,
    )

    created = client.post("/api/v1/conversations", json={"title": "Empty persist"})
    cid = created.json()["id"]

    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"text": "merhaba", "conversation_id": cid},
    ) as response:
        assert response.status_code == 200
        body = response.read().decode("utf-8")

    events = _parse_sse(body)
    error_events = _events_of(events, "error")
    assert error_events, "expected an error SSE event"
    assert error_events[0]["code"] == "EMPTY_RESPONSE"

    messages = client.get(f"/api/v1/conversations/{cid}/messages").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "error"], f"got {messages}"
    # The persisted content is the SAME as the SSE error message (the card shows the same text).
    assert messages[1]["content"] == error_events[0]["message"]


def test_stream_retries_once_on_empty_then_succeeds(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auto-retry: a TRANSIENT empty response (no text, no tool call) is retried ONCE;
    the second attempt's real text is delivered with NO EMPTY_RESPONSE error and the turn
    persists as a normal assistant message. ("sometimes it errors" → silent recovery.)"""
    calls = {"n": 0}

    async def _mock_stream(*_args, **_kwargs) -> AsyncIterator[dict[str, Any]]:
        calls["n"] += 1
        if calls["n"] == 1:
            # First attempt: empty (neither text nor tool call) → must trigger a retry.
            yield {
                "done": True,
                "text": "",
                "usage": {"prompt_tokens": 1, "completion_tokens": 0, "tool_calls": []},
                "status": "finished",
                "tool_calls": [],
            }
        else:
            # Second attempt: a real answer.
            yield {"delta": "İkinci denemede geldi.", "done": False}
            yield {
                "done": True,
                "text": "İkinci denemede geldi.",
                "usage": {"prompt_tokens": 1, "completion_tokens": 3, "tool_calls": []},
                "status": "finished",
                "tool_calls": [],
            }

    monkeypatch.setattr("akana_server.api.routes.chat.stream_user_chat", _mock_stream)

    cid = client.post("/api/v1/conversations", json={"title": "Empty retry"}).json()["id"]

    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"text": "merhaba", "conversation_id": cid},
    ) as response:
        assert response.status_code == 200
        body = response.read().decode("utf-8")

    events = _parse_sse(body)
    assert calls["n"] == 2, "the empty first attempt must be retried exactly once"
    assert not _events_of(events, "error"), "retry succeeded → no EMPTY_RESPONSE error"
    done = _events_of(events, "done")
    assert done and done[0]["text"] == "İkinci denemede geldi."

    messages = client.get(f"/api/v1/conversations/{cid}/messages").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"], f"got {messages}"
    assert messages[1]["content"] == "İkinci denemede geldi."


def test_stream_skips_auto_capture_when_memory_remember_called(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Double-write fix: if the turn called the memory_remember tool, the background
    llm_capture is NOT SPAWNED — so the same info is not saved twice, once by the tool
    and once by the automatic capture."""
    spawned: list[Any] = []

    def _fake_spawn(_app: Any, coro: Any) -> None:
        # The LLM chat-titler also spawns via _spawn_background; it is not under
        # test here — close it WITHOUT recording so only the auto-capture coro is
        # counted by the assertions below.
        if getattr(getattr(coro, "cr_code", None), "co_name", "") == "maybe_title_conversation":
            coro.close()
            return
        spawned.append(coro)
        coro.close()  # record the spawn; don't actually run the capture

    monkeypatch.setattr(
        "akana_server.api.routes.chat.chat_producer._spawn_background", _fake_spawn
    )

    async def _mock_stream(*_args, **_kwargs) -> AsyncIterator[dict[str, Any]]:
        yield {"tool_call": {"id": "t1", "name": "akana_memory/memory_remember", "phase": "start", "args": {"text": "ad: Alex"}, "result": None, "status": None}}
        yield {"tool_call": {"id": "t1", "name": "akana_memory/memory_remember", "phase": "end", "args": None, "result": "ok", "status": "ok"}}
        yield {"delta": "Not ettim Alex.", "done": False}
        yield {
            "done": True,
            "text": "Not ettim Alex.",
            "usage": {"prompt_tokens": 1, "completion_tokens": 3, "tool_calls": []},
            "status": "finished",
            "tool_calls": [],
        }

    monkeypatch.setattr("akana_server.api.routes.chat.stream_user_chat", _mock_stream)
    cid = client.post("/api/v1/conversations", json={"title": "Dup capture"}).json()["id"]
    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"text": "benim adım alex", "conversation_id": cid},
    ) as response:
        assert response.status_code == 200
        response.read()

    assert spawned == [], "background capture must not be spawned when memory_remember was called"


def test_stream_skips_auto_capture_when_save_memory_called(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Double-write fix (gemini/openai): the local memory-write tool is named ``save_memory``
    (NOT memory_remember as in MCP). The automatic capture must be SKIPPED for this one too —
    otherwise "my name is X" was saved TWICE, once by the tool (user_name) and once by the
    automatic capture (name). Regression of the reported bug."""
    spawned: list[Any] = []

    def _fake_spawn(_app: Any, coro: Any) -> None:
        # Ignore the chat-titler's background spawn (not under test here).
        if getattr(getattr(coro, "cr_code", None), "co_name", "") == "maybe_title_conversation":
            coro.close()
            return
        spawned.append(coro)
        coro.close()

    monkeypatch.setattr(
        "akana_server.api.routes.chat.chat_producer._spawn_background", _fake_spawn
    )

    async def _mock_stream(*_args, **_kwargs) -> AsyncIterator[dict[str, Any]]:
        yield {"tool_call": {"id": "t1", "name": "save_memory", "phase": "start", "args": {"text": "benim adım Alice"}, "result": None, "status": None}}
        yield {"tool_call": {"id": "t1", "name": "save_memory", "phase": "end", "args": None, "result": "staged", "status": "ok"}}
        yield {"delta": "Not ettim Alice.", "done": False}
        yield {
            "done": True,
            "text": "Not ettim Alice.",
            "usage": {"prompt_tokens": 1, "completion_tokens": 3, "tool_calls": []},
            "status": "finished",
            "tool_calls": [],
        }

    monkeypatch.setattr("akana_server.api.routes.chat.stream_user_chat", _mock_stream)
    cid = client.post("/api/v1/conversations", json={"title": "Dup capture save"}).json()["id"]
    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"text": "benim adım alice", "conversation_id": cid},
    ) as response:
        assert response.status_code == 200
        response.read()

    assert spawned == [], "background capture must not be spawned when save_memory was called (gemini/openai)"


def test_stream_runs_auto_capture_without_memory_tool(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control (not a regression): WHEN there is no memory write tool, the background capture is STILL spawned."""
    spawned: list[Any] = []

    def _fake_spawn(_app: Any, coro: Any) -> None:
        # Ignore the chat-titler's background spawn (not under test here).
        if getattr(getattr(coro, "cr_code", None), "co_name", "") == "maybe_title_conversation":
            coro.close()
            return
        spawned.append(coro)
        coro.close()

    monkeypatch.setattr(
        "akana_server.api.routes.chat.chat_producer._spawn_background", _fake_spawn
    )
    # Hermetic: a prior test may leave the cursor breaker OPEN (integration tests don't
    # auto-reset the global breaker registry), which would suppress the capture spawn and
    # make this control test order-dependent. Force the breaker-closed path.
    monkeypatch.setattr(
        "akana_server.api.routes.chat.chat_producer._cursor_breaker_open", lambda _s: False
    )

    async def _mock_stream(*_args, **_kwargs) -> AsyncIterator[dict[str, Any]]:
        yield {"delta": "Merhaba.", "done": False}
        yield {
            "done": True,
            "text": "Merhaba.",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "tool_calls": []},
            "status": "finished",
            "tool_calls": [],
        }

    monkeypatch.setattr("akana_server.api.routes.chat.stream_user_chat", _mock_stream)
    cid = client.post("/api/v1/conversations", json={"title": "Capture on"}).json()["id"]
    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"text": "selam", "conversation_id": cid},
    ) as response:
        assert response.status_code == 200
        response.read()

    assert len(spawned) == 1, "background capture must be spawned when there is no tool (regression)"


def test_stream_llm_timeout_surfaces_distinct_code_not_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (HIGH): an LLM timeout (504 + ``LLM_TIMEOUT`` token) looked the SAME
    to the client as a generic ``LLM_UNAVAILABLE`` — the token was buried in the
    message, a separate ``code`` was never emitted (only a metric counter incremented).

    Now the stream's ``error`` SSE event carries a distinct ``code == "LLM_TIMEOUT"``
    (BAD_REQUEST on 400, LLM_UNAVAILABLE preserved otherwise) and the message stays
    informative. This is deliberately SEPARATE from ``LLM_UNAVAILABLE``: a real timeout
    is not a "stuck-active-turn", so it must not trigger the front-end's auto-cancel
    recovery that hangs off it."""

    async def _mock_stream(*_args, **_kwargs) -> AsyncIterator[dict[str, Any]]:
        raise LLMCallError("LLM_TIMEOUT: cursor bridge timed out", status_code=504)
        yield  # pragma: no cover - needed so this is a generator (unreachable)

    monkeypatch.setattr(
        "akana_server.api.routes.chat.stream_user_chat",
        _mock_stream,
    )

    created = client.post("/api/v1/conversations", json={"title": "Timeout"})
    cid = created.json()["id"]

    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"text": "merhaba", "conversation_id": cid},
    ) as response:
        assert response.status_code == 200
        body = response.read().decode("utf-8")

    events = _parse_sse(body)
    error_events = _events_of(events, "error")
    assert error_events, "expected an error SSE event"
    assert error_events[0]["code"] == "LLM_TIMEOUT"
    # A timeout must not be confused with the generic "unavailable" (the actual bug).
    assert error_events[0]["code"] != "LLM_UNAVAILABLE"
    # The message stays informative (it carries the token).
    assert "LLM_TIMEOUT" in (error_events[0]["message"] or "")


def test_stream_non_timeout_504_stays_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Edge case: a 504 WITHOUT the ``LLM_TIMEOUT`` token (e.g. an upstream gateway
    error) must not count as a timeout — it stays ``LLM_UNAVAILABLE``. Only the
    token+504 combination earns the new code (no false positives)."""

    async def _mock_stream(*_args, **_kwargs) -> AsyncIterator[dict[str, Any]]:
        raise LLMCallError("gateway exploded", status_code=504)
        yield  # pragma: no cover

    monkeypatch.setattr(
        "akana_server.api.routes.chat.stream_user_chat",
        _mock_stream,
    )

    cid = client.post("/api/v1/conversations", json={"title": "Gateway504"}).json()["id"]

    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"text": "merhaba", "conversation_id": cid},
    ) as response:
        assert response.status_code == 200
        body = response.read().decode("utf-8")

    events = _parse_sse(body)
    error_events = _events_of(events, "error")
    assert error_events, "expected an error SSE event"
    assert error_events[0]["code"] == "LLM_UNAVAILABLE"
    assert error_events[0]["code"] != "LLM_TIMEOUT"


def test_stream_breaker_open_surfaces_rate_limit_not_stream_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (live bug): when parallel detached turns opened the LLM circuit
    breaker, a raw ``BreakerOpenError`` fell into the generic ``except Exception`` and
    became "STREAM_ERROR" → the user thought "the server is gone / the screen broke".
    Now it is caught specifically → ``LLM_RATE_LIMITED`` + a clear message with retry_after."""
    from akana_server.network.breaker import BreakerOpenError

    async def _mock_stream(*_args, **_kwargs) -> AsyncIterator[dict[str, Any]]:
        raise BreakerOpenError("cursor", 12.0)
        yield  # pragma: no cover - needed so this is a generator (unreachable)

    monkeypatch.setattr(
        "akana_server.api.routes.chat.stream_user_chat",
        _mock_stream,
    )

    created = client.post("/api/v1/conversations", json={"title": "Breaker"})
    cid = created.json()["id"]

    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"text": "merhaba", "conversation_id": cid},
    ) as response:
        assert response.status_code == 200
        body = response.read().decode("utf-8")

    events = _parse_sse(body)
    error_events = _events_of(events, "error")
    assert error_events, "expected an error SSE event"
    assert error_events[0]["code"] == "LLM_RATE_LIMITED"
    assert error_events[0]["code"] != "STREAM_ERROR"


def test_stream_memory_question_flows_through_normal_llm_path(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (live bug): "benim adım ne?" was killing the SSE stream.

    Old behavior: classify_intent put this pattern into "memory_recall",
    `_handle_memory_recall` made a blocking one-shot recall LLM call BEFORE the
    first SSE byte → the UI saw no events ("the message isn't sending").
    New behavior: the intent stays "chat", the normal stream path runs (the LLM
    calls the memory_search MCP tool itself), and deltas flow.
    """
    stream_called = {"n": 0}

    async def _mock_stream(*_args, **_kwargs) -> AsyncIterator[dict[str, Any]]:
        stream_called["n"] += 1
        yield {"delta": "Adın ", "done": False}
        yield {"delta": "Alice.", "done": False}
        yield {
            "done": True,
            "text": "Adın Alice.",
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "tool_calls": []},
            "status": "finished",
            "tool_calls": [],
        }

    monkeypatch.setattr(
        "akana_server.api.routes.chat.stream_user_chat",
        _mock_stream,
    )

    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"text": "benim adım ne?"},
    ) as response:
        assert response.status_code == 200
        body = response.read().decode("utf-8")

    events = _parse_sse(body)
    meta_events = _events_of(events, "meta")
    assert meta_events, "stream did not emit a meta event"
    assert meta_events[0]["intent"] == "chat"

    assert stream_called["n"] == 1, "the normal LLM stream path was not used"
    delta_texts = [p.get("text", "") for p in _events_of(events, "delta")]
    assert "".join(delta_texts) == "Adın Alice."

    done_events = _events_of(events, "done")
    assert done_events, "stream did not emit a done event"
    assert done_events[-1]["intent"] == "chat"
    assert done_events[-1]["text"] == "Adın Alice."


def test_stream_survives_persist_failures(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Edge case: if the episodic persist (sqlite lock/disk) crashes, the stream does NOT DIE.

    Old behavior: a ``persist_user_turn``/``persist_assistant_turn`` exception was
    thrown out of the SSE generator, and the connection dropped without a done/error
    event ("the page died mid-response" in the UI). New behavior: the error is logged,
    and the deltas + done event flow normally.
    """

    async def _mock_stream(*_args, **_kwargs) -> AsyncIterator[dict[str, Any]]:
        yield {"delta": "Mer", "done": False}
        yield {"delta": "haba", "done": False}
        yield {
            "done": True,
            "text": "Merhaba",
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "tool_calls": []},
            "status": "finished",
            "tool_calls": [],
        }

    monkeypatch.setattr(
        "akana_server.api.routes.chat.stream_user_chat", _mock_stream
    )

    def _boom(*_args, **_kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(
        "akana_server.api.routes.chat.persist_user_turn", _boom
    )
    monkeypatch.setattr(
        "akana_server.api.routes.chat.persist_assistant_turn", _boom
    )

    with client.stream(
        "POST", "/api/v1/chat/stream", json={"text": "selam"}
    ) as response:
        assert response.status_code == 200
        body = response.read().decode("utf-8")

    events = _parse_sse(body)
    delta_texts = [p.get("text", "") for p in _events_of(events, "delta")]
    assert "".join(delta_texts) == "Merhaba"
    done_events = _events_of(events, "done")
    assert done_events, "the persist error swallowed the done event"
    assert done_events[-1]["text"] == "Merhaba"
    assert done_events[-1]["memory_writes"] == []  # persist did not happen, the stream continued


def test_stream_persists_with_normal_deltas(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Baseline: deltas arrive normally; done.text must not double the body."""

    async def _mock_stream(*_args, **_kwargs) -> AsyncIterator[dict[str, Any]]:
        yield {"delta": "Mer", "done": False}
        yield {"delta": "haba", "done": False}
        yield {"delta": " dünya.", "done": False}
        yield {
            "done": True,
            "text": "Merhaba dünya.",
            "usage": {"prompt_tokens": 1, "completion_tokens": 4, "tool_calls": []},
            "status": "finished",
            "tool_calls": [],
        }

    monkeypatch.setattr(
        "akana_server.api.routes.chat.stream_user_chat",
        _mock_stream,
    )

    created = client.post("/api/v1/conversations", json={"title": "Normal stream"})
    cid = created.json()["id"]

    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"text": "selam", "conversation_id": cid},
    ) as response:
        assert response.status_code == 200
        body = response.read().decode("utf-8")

    events = _parse_sse(body)
    delta_texts = [p.get("text", "") for p in _events_of(events, "delta")]
    assert "".join(delta_texts) == "Merhaba dünya."  # no fallback duplication

    messages = client.get(f"/api/v1/conversations/{cid}/messages").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "Merhaba dünya."


def test_stream_done_not_blocked_by_memory_capture(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: ``done`` (→ closing the "Typing" indicator in the UI) must NOT wait
    UNTIL memory capture (the 2nd post-turn LLM call) finishes. A slow/hanging capture
    must not lock the response; the capture runs in the background AFTER ``done``.

    In the old (buggy) behavior the capture ran inline BEFORE ``done`` → even though
    the text had finished streaming, "Typing" stayed stuck until that call returned
    (for seconds if slow, indefinitely if it hung).
    """
    import asyncio as _a
    import threading

    proceed = threading.Event()  # switch to release the background capture
    capture_done = threading.Event()  # whether the capture ACTUALLY completed

    async def _mock_stream(*_args, **_kwargs) -> AsyncIterator[dict[str, Any]]:
        yield {"delta": "Tam yanıt.", "done": False}
        yield {
            "done": True,
            "text": "Tam yanıt.",
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "tool_calls": []},
            "status": "finished",
            "tool_calls": [],
        }

    async def _blocking_capture(*_args, **_kwargs):
        # Block without clogging the loop: it must not finish until done is sent. proceed
        # is set by the test (AFTER the assertion); the timeout is a leak safeguard.
        await _a.get_running_loop().run_in_executor(
            None, lambda: proceed.wait(timeout=10)
        )
        capture_done.set()
        return []

    monkeypatch.setattr(
        "akana_server.api.routes.chat.stream_user_chat", _mock_stream
    )
    monkeypatch.setattr(
        "akana_server.api.routes.chat.propose_memory_captures",
        _blocking_capture,
    )

    created = client.post("/api/v1/conversations", json={"title": "BG capture"})
    cid = created.json()["id"]
    try:
        with client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"text": "merhaba", "conversation_id": cid},
        ) as response:
            assert response.status_code == 200
            body = response.read().decode("utf-8")

        events = _parse_sse(body)
        done_events = _events_of(events, "done")
        assert done_events, "no done event — stream did not complete"
        assert done_events[-1]["text"] == "Tam yanıt."
        # CRITICAL: when done arrives, the capture must NOT be finished yet (in the
        # background, not blocking). If it were inline (the regression), done would wait
        # for the capture → capture_done would be set BEFORE done, and this assert would fail.
        assert not capture_done.is_set(), (
            "done waited until memory capture finished — capture was not moved "
            "to the background (regression: 'Typing' hangs on the 2nd LLM call)"
        )
    finally:
        proceed.set()  # release the background task — leave no leak


def test_llm_capture_lands_in_v2_inbox(client: TestClient, monkeypatch) -> None:
    """A7: a candidate captured during the turn appears in the v2 inbox (GET /api/v1/memory/staging).

    Drift regression: capture used to go to the v1 ``staging.db`` but the inbox
    (v2 API) read ``memory.db`` → captured items NEVER showed up. This test proves the
    end-to-end closure: blocking /chat turn → (mocked) capture → candidate in the
    v2 inbox. Capture + API use the same ``get_memory_core(settings.data_dir)``
    instance → consistent.
    """
    from akana_server.memory_capture import MemoryCaptureCandidate

    async def _ok(settings, user_text, **kwargs):
        return "tamam.", {"prompt_tokens": 1, "completion_tokens": 1, "tool_calls": []}

    async def _propose(*_args, **_kwargs):
        return [MemoryCaptureCandidate(key="soyad", value="Yılmaz", reason="kullanıcı bildirdi")]

    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage", _ok
    )
    monkeypatch.setattr(
        "akana_server.api.routes.chat.propose_memory_captures", _propose
    )

    cid = client.post("/api/v1/conversations", json={"title": "Yakalama"}).json()["id"]
    r = client.post("/api/v1/chat", json={"text": "soyadım Yılmaz", "conversation_id": cid})
    assert r.status_code == 200
    writes = r.json().get("memory_writes") or []
    assert any(w.get("kind") == "staging" and w.get("key") == "soyad" for w in writes)

    # CRITICAL: the candidate appears in the v2 inbox (the old v1 staging.db drift is closed).
    inbox = client.get("/api/v1/memory/staging?status=pending").json()
    items = {i["key"]: i for i in inbox["items"]}
    assert "soyad" in items
    assert items["soyad"]["value"] == "Yılmaz"
    assert inbox["pending_count"] >= 1