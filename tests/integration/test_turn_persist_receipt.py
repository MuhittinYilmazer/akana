"""What the user is told when ``memory.db`` refuses the write (web chat surfaces).

The turn writer swallows db errors by design — the answer the user is already reading
must not be broken by a storage problem — and it used to return the freshly minted ULID
either way, so a lost turn was announced as a completed one: ``done`` carried a turn id
with no row behind it, the audit said "ok", and the ``finally``'s re-persist (the ONE
retry that exists for this failure) was skipped because the caller had already set its
"persisted" flag.

The failure is simulated the way production produces it: ``Memory.remember_turn`` raises,
so the writer exhausts its retries, asks the store, finds nothing and reports "". Patching
the persist helpers to return "" instead would skip the very code under test.

Covered: A1 (the flag + the rescue), A2 (memory_writes is not a receipt), A3 (the blocking
POST /chat probe) and X1 (a pair is atomic — no answer without its question).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app
from akana_server.conversation_service import ConversationService


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("CURSOR_API_KEY", "x")
    # Both post-turn LLM calls are off: the capture and the chat titler each spawn a REAL
    # provider process (the titler runs the full CLI), which these tests neither need nor
    # can wait for — and a titler breaker trip silently drops memory capture elsewhere.
    monkeypatch.setenv("AKANA_MEMORY_LLM_CAPTURE", "0")
    monkeypatch.setenv("AKANA_LLM_CHAT_TITLES", "0")
    with TestClient(create_app()) as c:
        yield c


def _break_writes(monkeypatch: pytest.MonkeyPatch, *roles: str) -> None:
    """Make the STORE refuse the given roles, exactly like a wedged sqlite would.

    ``turn_writer`` catches this, retries ``_PERSIST_ATTEMPTS`` times, re-reads the row,
    finds it absent and returns "" — the real production sequence. The other roles keep
    working, so a test can lose one half of a pair and watch what the other half does.
    """
    from akana.memory import Memory

    original = Memory.remember_turn

    def _guarded(self: Any, **kwargs: Any) -> Any:
        if kwargs.get("role") in roles:
            raise RuntimeError("database is locked")
        return original(self, **kwargs)

    monkeypatch.setattr(Memory, "remember_turn", _guarded)


def _count_assistant_writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every ``persist_assistant_turn`` attempt (the package-level patch surface)."""
    import akana_server.api.routes.chat as chatpkg

    original = chatpkg.persist_assistant_turn
    attempts: list[str] = []

    def _counting(**kwargs: Any) -> str:
        attempts.append(str(kwargs.get("assistant_turn_id") or ""))
        return original(**kwargs)

    monkeypatch.setattr(chatpkg, "persist_assistant_turn", _counting)
    return attempts


async def _mock_complete(*_a: Any, **_k: Any) -> tuple[str, dict[str, Any]]:
    return "Bloklayan yanıt.", {"prompt_tokens": 1, "completion_tokens": 1, "tool_calls": []}


def _mock_stream_factory(text: str):
    async def _mock_stream(*_args: Any, **_kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        yield {"delta": text, "done": False}
        yield {
            "done": True,
            "text": text,
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "tool_calls": []},
            "status": "finished",
            "tool_calls": [],
        }

    return _mock_stream


def _parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
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
                events.append((event_name, json.loads("\n".join(data_lines))))
            except json.JSONDecodeError:
                events.append((event_name, {"raw": "\n".join(data_lines)}))
    return events


def _events_of(events: list[tuple[str, dict[str, Any]]], name: str) -> list[dict[str, Any]]:
    return [p for n, p in events if n == name]


def _record_broadcasts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every WS broadcast the app makes, in order (the turn-lifecycle frames included)."""
    from akana_server.events import EventHub

    original = EventHub.broadcast_json
    sent: list[dict[str, Any]] = []

    async def _recording(self: Any, data: Any) -> Any:
        if isinstance(data, dict):
            sent.append(data)
        return await original(self, data)

    monkeypatch.setattr(EventHub, "broadcast_json", _recording)
    return sent


def _settled_frames(
    client: TestClient, sent: list[dict[str, Any]], kind: str
) -> list[dict[str, Any]]:
    """Frames of ``kind`` once the turn's background announce tasks have all run.

    The completion is announced from a spawned task, so a plain read right after the
    response can race it; each cheap request yields the loop. Two quiet rounds after the
    first hit is what makes "EXACTLY one completion" a real assertion and not a lucky read.
    """
    quiet = 0
    for _ in range(40):
        hits = [f for f in sent if f.get("type") == kind]
        quiet = quiet + 1 if hits else 0
        if quiet >= 3:
            return hits
        client.get("/api/v1/conversations")
    raise AssertionError(f"no {kind!r} frame was broadcast")


def _drain_ready(ws: Any) -> None:
    assert ws.receive_json().get("type") == "ready"


def _await_event(ws: Any, want_type: str, *, max_frames: int = 12) -> dict[str, Any]:
    for _ in range(max_frames):
        evt = ws.receive_json()
        if evt.get("type") == want_type:
            return evt
    raise AssertionError(f"did not receive a {want_type!r} event")


def _stream_turn(client: TestClient, cid: str, text: str = "merhaba") -> str:
    with client.stream(
        "POST", "/api/v1/chat/stream", json={"text": text, "conversation_id": cid}
    ) as response:
        assert response.status_code == 200
        return response.read().decode("utf-8")


def test_stream_lost_answer_is_not_reported_as_a_completed_turn(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A1/A2: the assistant row never lands → no phantom receipt, and the retry RUNS.

    The user still gets their answer (a storage fault must not break the response), but
    nothing may claim the turn is in the log: ``memory_writes`` stays empty, the turn's
    announced completion is an error, and an error marker takes the announced turn id so
    the reload the client does next finds an explanation instead of a void.
    """
    monkeypatch.setattr(
        "akana_server.api.routes.chat.stream_user_chat", _mock_stream_factory("Tam yanıt.")
    )
    attempts = _count_assistant_writes(monkeypatch)
    _break_writes(monkeypatch, "assistant")

    cid = client.post("/api/v1/conversations", json={"title": "lost answer"}).json()["id"]
    with client.websocket_connect("/ws/events") as ws:
        _drain_ready(ws)
        body = _stream_turn(client, cid)
        completed = _await_event(ws, "turn_completed")

    events = _parse_sse(body)
    done = _events_of(events, "done")
    assert len(done) == 1
    assert done[0]["text"] == "Tam yanıt."  # the answer still reaches the user
    assert done[0]["memory_writes"] == [], "announced a write that never landed"

    # The ONE retry the code has for this failure was reachable (2 calls, not 1) and is
    # one-shot (not 3 — the finally must not re-run what the normal branch already spent).
    assert len(attempts) == 2, f"rescue re-persist did not run exactly once: {attempts}"

    errors = _events_of(events, "error")
    assert [e["code"] for e in errors] == ["TURN_NOT_PERSISTED"]
    assert completed["status"] == "error", "a lost turn was announced as ok"

    svc = ConversationService(tmp_path)
    messages = svc.list_messages(cid)
    assert [m.role for m in messages] == ["user", "error"], (
        "the answer must not be reported as stored, and the failure must be visible"
    )
    # The id the client was handed resolves to a row (the marker), so the post-turn log
    # reload renders an error card instead of dropping the exchange silently.
    assert messages[1].id == done[0]["turn_id"]
    # The lost answer is not smuggled into the next turn's context either.
    assert [m["role"] for m in svc.recent_llm_messages(cid, max_turns=10)] == ["user"]


def test_stream_persisted_answer_is_written_once(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The other half of the receipt: a write that LANDS must not be retried.

    Gating the flag on the result has to leave the happy path untouched — a second write
    of the same turn would re-run the conversation-meta bump path for nothing.
    """
    monkeypatch.setattr(
        "akana_server.api.routes.chat.stream_user_chat", _mock_stream_factory("Kaydedildi.")
    )
    attempts = _count_assistant_writes(monkeypatch)

    cid = client.post("/api/v1/conversations", json={"title": "ok turn"}).json()["id"]
    body = _stream_turn(client, cid)

    events = _parse_sse(body)
    assert _events_of(events, "error") == []
    done = _events_of(events, "done")
    assert [w["kind"] for w in done[0]["memory_writes"]] == ["episodic", "episodic"]
    assert len(attempts) == 1, f"a successful write was retried: {attempts}"

    svc = ConversationService(tmp_path)
    assert [m.role for m in svc.list_messages(cid)] == ["user", "assistant"]
    meta = svc.get(cid)
    assert meta is not None and meta.message_count == 2, "the meta counter double-bumped"


def test_stream_answer_is_not_written_without_its_question(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """X1: the user row is lost → the answer is NOT stored on its own.

    An orphan assistant row is the worst outcome of the family: ``recent_llm_messages``
    replays it as history, so the next turn sees an answer to a question that is not
    there and the assistant contradicts itself.
    """
    monkeypatch.setattr(
        "akana_server.api.routes.chat.stream_user_chat", _mock_stream_factory("Öksüz yanıt.")
    )
    _break_writes(monkeypatch, "user")

    cid = client.post("/api/v1/conversations", json={"title": "orphan"}).json()["id"]
    body = _stream_turn(client, cid)

    svc = ConversationService(tmp_path)
    roles = [m.role for m in svc.list_messages(cid)]
    assert "assistant" not in roles, "an answer was stored with no question above it"
    assert svc.recent_llm_messages(cid, max_turns=10) == []
    done = _events_of(_parse_sse(body), "done")
    assert done[0]["memory_writes"] == []


def test_blocking_chat_does_not_return_ok_for_an_unstored_exchange(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A3: POST /chat (also the voice-routed surface) skipped the durability probe.

    It answered 200 with a turn id and ``memory_writes`` for an exchange that is not in
    ``memory.db``; the user's next reload showed neither the question nor the reply.
    """

    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage", _mock_complete
    )
    sent = _record_broadcasts(monkeypatch)
    _break_writes(monkeypatch, "assistant")

    cid = client.post("/api/v1/conversations", json={"title": "blocking"}).json()["id"]
    resp = client.post("/api/v1/chat", json={"text": "merhaba", "conversation_id": cid})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["text"] == "Bloklayan yanıt."  # the reply is still delivered
    assert payload["memory_writes"] == [], "announced a write that never landed"

    svc = ConversationService(tmp_path)
    messages = svc.list_messages(cid)
    assert [m.role for m in messages] == ["user", "error"]
    assert messages[1].id == payload["turn_id"]
    assert [m["role"] for m in svc.recent_llm_messages(cid, max_turns=10)] == ["user"]

    completed = _settled_frames(client, sent, "turn_completed")
    assert [f["status"] for f in completed] == ["error"], completed
    assert completed[0]["source"] == "user"


def test_blocking_chat_still_announces_ok_for_a_stored_exchange(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Control for the outcome seam: a healthy turn keeps its single "ok" completion."""
    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage", _mock_complete
    )
    sent = _record_broadcasts(monkeypatch)

    cid = client.post("/api/v1/conversations", json={"title": "blocking ok"}).json()["id"]
    resp = client.post("/api/v1/chat", json={"text": "merhaba", "conversation_id": cid})
    assert resp.status_code == 200
    assert [w["kind"] for w in resp.json()["memory_writes"]] == ["episodic", "episodic"]

    completed = _settled_frames(client, sent, "turn_completed")
    assert [f["status"] for f in completed] == ["ok"], completed
    assert [m.role for m in ConversationService(tmp_path).list_messages(cid)] == [
        "user",
        "assistant",
    ]
