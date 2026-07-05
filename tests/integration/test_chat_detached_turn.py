"""UNBREAKABLE RESPONSE — a chat turn runs as a server-side task independent of the client.

Live bug: while the SSE generator was attached to the client, closing the tab/switching
conversation cancelled the LLM turn. New behavior:

* the turn runs as a `_run_turn_detached` task; the HTTP response is a follower watching
  the buffer — when the follower closes (client disconnected) the turn CONTINUES and persists,
* when the turn ends a `turn_completed {conversation_id}` broadcast is sent to the WS,
* while a turn is in progress on the same conversation a second **stream** message is queued with **202**,
* lifespan shutdown cleanly cancels active turns (partial persist is preserved).

NOTE: the starlette TestClient buffers the response body COMPLETELY — a real client
disconnect cannot be simulated over HTTP. That is why the tests call the route functions
directly inside a real lifespan and close the follower by hand.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from akana_server.api.chat_turn_queue import queue_depth
from akana_server.api.app import create_app
from akana_server import chat_context
from akana_server.api.routes import chat as chat_routes
from akana_server.api.services import get_services
from akana_server.api.routes.chat import ChatRequest


def _make_request(app: FastAPI) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/chat/stream",
            "headers": [],
            "query_string": b"",
            "app": app,
            "client": None,
        }
    )


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8766")
    monkeypatch.setenv("CURSOR_API_KEY", "x")
    monkeypatch.setenv("AKANA_MEMORY_LLM_CAPTURE", "0")
    return tmp_path


def _slow_stream_factory(deltas: list[str], *, delay: float = 0.05, full: str | None = None):
    text = full if full is not None else "".join(deltas)

    async def _stream(*_args: Any, **_kwargs: Any):
        for d in deltas:
            yield {"delta": d, "done": False}
            await asyncio.sleep(delay)
        yield {
            "done": True,
            "text": text,
            "usage": {"prompt_tokens": 1, "completion_tokens": 3, "tool_calls": []},
            "status": "finished",
            "tool_calls": [],
        }

    return _stream


def test_consumer_disconnect_does_not_cancel_turn(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client disconnects after the first event — the turn finishes, the response is
    persisted, a turn_completed broadcast is emitted (refreshes the conversation when the UI returns)."""
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["Mer", "haba ", "dünya."]),
    )
    events: list[dict[str, Any]] = []

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            hub = app.state.event_hub

            async def capture(data: dict[str, Any]) -> None:
                events.append(data)

            hub.broadcast_json = capture  # type: ignore[method-assign]
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            resp = await chat_routes.post_chat_stream(
                ChatRequest(text="selam", conversation_id=meta.id), req, tts=None
            )
            it = resp.body_iterator
            first = await it.__anext__()  # client connected, received meta
            assert b"event: meta" in first
            turn = chat_routes._active_turns(app).get(meta.id)
            assert turn is not None and turn.task is not None
            await it.aclose()  # client disconnected (tab closed / conversation switched)

            # The turn completes without a client — NO cancellation.
            await asyncio.wait_for(turn.task, timeout=10)
            msgs = svc.list_messages(meta.id, limit=10)
            pairs = [(m.role, m.content) for m in msgs]
            assert ("user", "selam") in pairs
            assert ("assistant", "Merhaba dünya.") in pairs
            # registry cleared — conversation open to a new message
            assert meta.id not in chat_routes._active_turns(app)
            assert not chat_routes._is_turn_running(app, meta.id)  # not busy

        completed = [e for e in events if e.get("type") == "turn_completed"]
        assert completed, "turn_completed broadcast was not emitted"
        assert completed[-1]["conversation_id"] == meta.id
        assert completed[-1]["status"] == "ok"

    asyncio.run(main())


def test_detached_turn_persists_tool_calls_without_client(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REGRESSION (concurrent 2-conversation bug): tool calls persist ON THE SERVER.

    Bug: tool cards were only written to localStorage when the client received the
    SSE ``done`` event. With two conversations running at once, the user looks at
    one while the OTHER finishes IN THE BACKGROUND (detached task) — that turn's
    ``done`` never reaches the client → the tool cards would be lost permanently.
    Now the turn is written to ``turns.tool_calls`` together with the assistant
    turn even if no client ever consumes it, and /messages (list_messages) returns it.
    """
    calls = [
        {"id": "c1", "name": "read_file", "phase": "end", "status": "ok",
         "args": {"path": "a.md"}, "result": "içerik"},
        {"id": "c2", "name": "list_dir", "phase": "end", "status": "ok",
         "args": {"path": "."}, "result": "a\nb"},
    ]

    async def _stream_with_tools(*_args: Any, **_kwargs: Any):
        yield {"delta": "Okuyorum…", "done": False}
        for c in calls:
            yield {"tool_call": c, "done": False}
        yield {
            "done": True,
            "text": "Okudum.",
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "tool_calls": calls},
            "status": "finished",
            "tool_calls": calls,
        }

    monkeypatch.setattr(chat_routes, "stream_user_chat", _stream_with_tools)

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            resp = await chat_routes.post_chat_stream(
                ChatRequest(text="dosyayı oku", conversation_id=meta.id), req, tts=None
            )
            # Do NOT mimic the client: close the follower without even reading meta
            # (the user is looking at another conversation → this turn finishes entirely in the background).
            await resp.body_iterator.aclose()
            turn = chat_routes._active_turns(app).get(meta.id)
            assert turn is not None and turn.task is not None
            await asyncio.wait_for(turn.task, timeout=10)

            # /messages (list_messages) must return the tool calls in full —
            # the client NEVER connected to ``done``.
            msgs = svc.list_messages(meta.id, limit=10)
            asst = [m for m in msgs if m.role == "assistant"]
            assert asst, "assistant turn was not persisted"
            assert [c["name"] for c in asst[-1].tool_calls] == ["read_file", "list_dir"]
            assert asst[-1].tool_calls[0]["result"] == "içerik"

    asyncio.run(main())


def test_second_message_while_turn_active_enqueues_stream(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While a turn is in progress, a second stream message is queued with 202, not 409."""
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["Ya", "vaş ", "yanıt"], delay=0.2),
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            await chat_routes.post_chat_stream(
                ChatRequest(text="ilk mesaj", conversation_id=meta.id), req, tts=None
            )

            resp2 = await chat_routes.post_chat_stream(
                ChatRequest(text="ikinci mesaj", conversation_id=meta.id),
                req,
                tts=None,
            )
            assert isinstance(resp2, JSONResponse)
            assert resp2.status_code == 202
            body = json.loads(resp2.body)
            assert body["queued"] is True
            assert body["depth"] == 1
            assert queue_depth(app, meta.id) == 1

            with pytest.raises(HTTPException) as exc:
                await chat_routes.post_chat(
                    ChatRequest(text="blocking", conversation_id=meta.id), req
                )
            assert exc.value.status_code == 409

            turn = chat_routes._active_turns(app)[meta.id]
            await asyncio.wait_for(turn.task, timeout=10)
            await asyncio.sleep(0.05)
            assert queue_depth(app, meta.id) == 0

    asyncio.run(main())


def test_cancel_active_turn_clears_busy(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """STOP: the cancel endpoint cancels the detached turn → registry is cleared, TURN_BUSY lifts."""
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["uzun ", "yanıt ", "akıyor"], delay=0.5),
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            await chat_routes.post_chat_stream(
                ChatRequest(text="ilk mesaj", conversation_id=meta.id), req, tts=None
            )
            turn = chat_routes._active_turns(app)[meta.id]
            assert not turn.done

            # Cancel while the turn is in progress → it is cancelled and dropped from the registry.
            res = await chat_routes.cancel_chat_active(meta.id, req)
            assert res["cancelled"] is True
            assert turn.done
            assert meta.id not in chat_routes._active_turns(app)

            # A new message no longer hits TURN_BUSY.
            assert not chat_routes._is_turn_running(app, meta.id)

            # Cancel when there is no active turn → no_active_turn.
            res2 = await chat_routes.cancel_chat_active(meta.id, req)
            assert res2["cancelled"] is False

    asyncio.run(main())


def test_cancel_active_turn_preserves_agent_id(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """STOP/cancel: agent_id must be preserved — the next turn continues via Agent.resume."""
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["uzun ", "yanıt"], delay=0.5),
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            chat_context.persist_agent_id(req, meta.id, "agent-keep-xyz")
            assert chat_context.get_agent_id(req, meta.id) == "agent-keep-xyz"

            await chat_routes.post_chat_stream(
                ChatRequest(text="ilk mesaj", conversation_id=meta.id), req, tts=None
            )
            res = await chat_routes.cancel_chat_active(meta.id, req)
            assert res["cancelled"] is True
            assert chat_context.get_agent_id(req, meta.id) == "agent-keep-xyz"

    asyncio.run(main())


def test_cancel_active_turn_aborts_bridge_run(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """STOP/cancel: abort_run on the bridge — not close_session (hard reset)."""
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["uzun ", "yanıt"], delay=0.5),
    )
    aborted: list[str] = []

    async def _track_abort(_settings, conv_id: str | None) -> None:
        if conv_id:
            aborted.append(conv_id)

    monkeypatch.setattr(chat_routes, "_abort_bridge_run_for_conversation", _track_abort)

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            await chat_routes.post_chat_stream(
                ChatRequest(text="ilk mesaj", conversation_id=meta.id), req, tts=None
            )
            res = await chat_routes.cancel_chat_active(meta.id, req)
            assert res["cancelled"] is True
            assert aborted == [meta.id]

    asyncio.run(main())


def test_recover_bridge_preserves_agent_id(env) -> None:
    """Orphaned active-run: recover abort_run — agent_id is not deleted."""

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            chat_context.persist_agent_id(req, meta.id, "agent-recover-1")
            res = await chat_routes.recover_chat_bridge(meta.id, req, services=get_services(req))
            assert res["recovered"] is True
            assert res["mode"] == "abort_run"
            assert chat_context.get_agent_id(req, meta.id) == "agent-recover-1"

    asyncio.run(main())


def test_soft_recover_clears_registered_active_turn(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BK2: soft recover (?hard=0) must also cancel a REGISTERED stuck turn.

    Previously soft recover only aborted the bridge run and did not touch
    ``_active_turns`` → if the turn was stuck in the registry it stayed "busy", the
    next message would forever become TURN_BUSY/queued, and "recover" appeared to do
    nothing. Now recover also cancels the registered turn → the registry is cleared
    and the conversation opens to a new message."""
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["a", "b", "c", "d", "e"], delay=0.5),
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            resp = await chat_routes.post_chat_stream(
                ChatRequest(text="selam", conversation_id=meta.id), req, tts=None
            )
            it = resp.body_iterator
            await it.__anext__()  # meta arrived → turn REGISTERED + running
            turn = chat_routes._active_turns(app).get(meta.id)
            assert turn is not None and not turn.done, "precondition: turn registered + running"

            res = await chat_routes.recover_chat_bridge(
                meta.id, req, services=get_services(req)
            )
            assert res["recovered"] is True
            assert res["mode"] == "abort_run"

            # BK2 LOCK: soft recover must clear the registry (must not leave it busy).
            assert meta.id not in chat_routes._active_turns(app), (
                "soft recover must clear the registered turn (otherwise the next message stays stuck on TURN_BUSY)"
            )
            assert not chat_routes._is_turn_running(app, meta.id)  # should NOT be busy
            await it.aclose()

    asyncio.run(main())


def test_recover_bridge_hard_resets_agent_id(env) -> None:
    """?hard=1: unrecoverable stuck — agent_id is cleared."""

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            chat_context.persist_agent_id(req, meta.id, "agent-dead-1")
            req = Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": f"/api/v1/chat/active/{meta.id}/recover",
                    "headers": [],
                    "query_string": b"hard=1",
                    "app": app,
                    "client": None,
                }
            )
            res = await chat_routes.recover_chat_bridge(meta.id, req, services=get_services(req))
            assert res["recovered"] is True
            assert res["mode"] == "hard_reset"
            assert chat_context.get_agent_id(req, meta.id) is None

    asyncio.run(main())


async def _collect(it, *, max_events: int = 1000) -> list[bytes]:
    out: list[bytes] = []
    async for chunk in it:
        out.append(chunk)
        if len(out) >= max_events:
            break
    return out


def test_resume_replays_buffer_then_streams_live(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resume endpoint: REPLAY the buffer accumulated so far + remaining chunks LIVE.

    When the user navigates to another page and returns, they see the in-progress
    turn (meta + already-produced deltas) from the start and it continues live."""
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["bir ", "iki ", "üç ", "dört"], delay=0.1),
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            resp = await chat_routes.post_chat_stream(
                ChatRequest(text="say", conversation_id=meta.id), req, tts=None
            )
            primary = resp.body_iterator
            first = await primary.__anext__()
            assert b"event: meta" in first
            await asyncio.sleep(0.25)  # let a few deltas land in the buffer

            # Resume: a new follower — must REPLAY the buffer from the start (including meta).
            active = await chat_routes.get_chat_active(meta.id, req)
            assert isinstance(active, StreamingResponse)
            chunks = await _collect(active.body_iterator)
            joined = b"".join(chunks)
            assert b"event: meta" in joined  # replay from the start
            # continued live until done → all deltas + done present.
            assert b"event: done" in joined
            text = b"".join(
                c for c in chunks if b"event: delta" in c
            ).decode("utf-8")
            assert "bir" in text and "dört" in text

            # The primary follower also sees the full turn (it did not disconnect).
            rest = await _collect(primary)
            assert b"event: done" in b"".join(rest)
            # The turn completed → registry cleared (no memory leak).
            assert meta.id not in chat_routes._active_turns(app)

    asyncio.run(main())


def test_resume_returns_204_when_no_active_turn(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When there is no active turn, resume returns 204 (the frontend falls back to the normal messages fetch)."""

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            resp = await chat_routes.get_chat_active(meta.id, req)
            assert resp.status_code == 204
            # An unknown conversation is also 204.
            resp2 = await chat_routes.get_chat_active("yok-böyle", req)
            assert resp2.status_code == 204

    asyncio.run(main())


def test_multiple_followers_each_get_full_stream(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multiple followers: three followers watch the same turn concurrently, none loses a chunk."""
    deltas = ["a", "b", "c", "d", "e", "f"]
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(deltas, delay=0.05),
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            resp = await chat_routes.post_chat_stream(
                ChatRequest(text="çoklu", conversation_id=meta.id), req, tts=None
            )
            f1 = resp.body_iterator
            # After the first event arrives, attach two more resume-followers.
            await f1.__anext__()
            r2 = await chat_routes.get_chat_active(meta.id, req)
            r3 = await chat_routes.get_chat_active(meta.id, req)
            assert isinstance(r2, StreamingResponse) and isinstance(r3, StreamingResponse)

            c1, c2, c3 = await asyncio.gather(
                _collect(f1),  # f1: first event already read, collect the rest
                _collect(r2.body_iterator),
                _collect(r3.body_iterator),
            )
            for label, chunks in (("f1", c1), ("r2", c2), ("r3", c3)):
                joined = b"".join(chunks)
                assert b"event: done" in joined, f"{label} did not see done"
            # The resume followers (r2/r3) saw all deltas (replay+live).
            for chunks in (c2, c3):
                text = b"".join(c for c in chunks if b"event: delta" in c).decode()
                for d in deltas:
                    assert d in text

    asyncio.run(main())


def test_late_follower_reads_full_buffer_after_done(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A follower caught AFTER the turn FINISHES but before it is deleted from the registry reads the full buffer.

    (Late-follower-after-done scenario — `_follow_turn` fully drains the done buffer
    and finishes.)"""
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["x", "y", "z"], delay=0.01),
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            await chat_routes.post_chat_stream(
                ChatRequest(text="bit", conversation_id=meta.id), req, tts=None
            )
            turn = chat_routes._active_turns(app)[meta.id]
            # Wait until done but hold the reference before the registry is cleared.
            await asyncio.wait_for(turn.task, 10)
            assert turn.done is True
            # Late follower: reads the buffer in full and finishes immediately (does not hang).
            chunks = await asyncio.wait_for(_collect(_follow_turn_iter(turn)), 5)
            joined = b"".join(chunks)
            assert b"event: meta" in joined and b"event: done" in joined

    asyncio.run(main())


def _follow_turn_iter(turn):
    return chat_routes._follow_turn(turn)


def test_turn_completed_payload_has_conversation_and_status(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """turn_completed payload: conversation_id + status (+ assistant_turn_id if present)."""
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["tamam"], delay=0.01),
    )
    events: list[dict[str, Any]] = []

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            hub = app.state.event_hub

            async def capture(data: dict[str, Any]) -> None:
                events.append(data)

            hub.broadcast_json = capture  # type: ignore[method-assign]
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            await chat_routes.post_chat_stream(
                ChatRequest(text="bitir", conversation_id=meta.id), req, tts=None
            )
            turn = chat_routes._active_turns(app)[meta.id]
            await asyncio.wait_for(turn.task, 10)

        completed = [e for e in events if e.get("type") == "turn_completed"]
        assert completed, "turn_completed was not emitted"
        payload = completed[-1]
        assert payload["conversation_id"] == meta.id
        assert payload["status"] == "ok"
        assert payload.get("assistant_turn_id")  # extracted from meta
        # The turn_active signal was also emitted at the start of the turn.
        active = [e for e in events if e.get("type") == "turn_active"]
        assert active and active[0]["conversation_id"] == meta.id

    asyncio.run(main())


def test_lifespan_shutdown_cancels_active_turns_and_persists_partial(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Server shutdown: the active turn is cleanly cancelled, the partial response is not lost."""
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["Kısmi ", "yanıt "] + ["..."] * 200, delay=0.05),
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            await chat_routes.post_chat_stream(
                ChatRequest(text="uzun iş", conversation_id=meta.id), req, tts=None
            )
            turn = chat_routes._active_turns(app)[meta.id]
            # Wait until the first two deltas ("Kısmi ", "yanıt ") are actually buffered
            # instead of assuming a fixed 0.2s sleep accumulates them: the Windows CI
            # runner's coarser timer resolution can yield zero deltas inside that window,
            # so the partial-persist then sees an empty buffer and the test flakes.
            # Counting buffered ``event: delta`` chunks is deterministic on every OS;
            # the turn is still mid-flight (200 trailing "..." deltas remain unsent).
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 5.0
            while loop.time() < deadline:
                buffered = b"".join(list(turn.chunks)).decode("utf-8", "replace")
                if buffered.count("event: delta") >= 2:
                    break
                await asyncio.sleep(0.01)
            assert not turn.task.done()
        # lifespan finally → shutdown_active_turns: task cancelled + registry empty
        assert turn.task.done()
        assert chat_routes._active_turns(app) == {}
        msgs = svc.list_messages(meta.id, limit=10)
        assistant = [m for m in msgs if m.role == "assistant"]
        assert assistant, "partial response was not persisted on shutdown"
        assert assistant[-1].content.startswith("Kısmi yanıt")

    asyncio.run(main())


def test_post_voice_turn_busy_when_chat_stream_active(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The voice path returns 409 TURN_BUSY when a chat turn is in progress on the same conversation."""
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["uzun ", "yanıt"], delay=0.5),
    )

    async def _fake_transcribe(*_args: Any, **_kwargs: Any) -> tuple[str, str]:
        return "merhaba", "tr"

    monkeypatch.setattr(
        "akana_server.api.routes.voice.transcribe_wav_bytes",
        _fake_transcribe,
    )

    class _FakeUpload:
        filename = "konusma.wav"

        async def read(self) -> bytes:
            return b"RIFFfake"

    async def main() -> None:
        from akana_server.api.routes import voice as voice_routes

        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            await chat_routes.post_chat_stream(
                ChatRequest(text="ilk mesaj", conversation_id=meta.id), req, tts=None
            )
            assert meta.id in chat_routes._active_turns(app)

            with pytest.raises(HTTPException) as exc:
                await voice_routes.post_voice(
                    req,
                    services=get_services(req),
                    audio=_FakeUpload(),
                    lang=None,
                    tts=None,
                    tts_lang=None,
                    conversation_id=meta.id,
                )
            assert exc.value.status_code == 409
            assert exc.value.detail["error"]["code"] == "TURN_BUSY"

    asyncio.run(main())


def test_cleanup_cancels_active_turn_and_clears_queue(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D1/D2: the delete path cancels the running turn, clears the queue, does not start a drain."""
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["uzun ", "yanıt"], delay=0.5),
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            await chat_routes.post_chat_stream(
                ChatRequest(text="ilk mesaj", conversation_id=meta.id), req, tts=None
            )
            resp2 = await chat_routes.post_chat_stream(
                ChatRequest(text="kuyrukta", conversation_id=meta.id),
                req,
                tts=None,
            )
            assert isinstance(resp2, JSONResponse) and resp2.status_code == 202
            assert queue_depth(app, meta.id) == 1

            await chat_routes.cleanup_conversation_chat_state(app, meta.id)
            assert meta.id not in chat_routes._active_turns(app)
            assert queue_depth(app, meta.id) == 0

            svc.soft_delete(meta.id)
            svc._episodic.delete_conversation(meta.id)
            await chat_routes._maybe_drain_queue(app, meta.id)
            await asyncio.sleep(0.05)
            assert meta.id not in chat_routes._active_turns(app)
            assert svc.get(meta.id) is None

    asyncio.run(main())


def test_drain_skips_tombstoned_conversation(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H2/D3: the queue is not drained for a deleted conversation; soft-delete is not revived."""
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["yanıt"], delay=0.01),
    )

    async def main() -> None:
        from akana_server.api.chat_turn_queue import enqueue_message

        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            enqueue_message(
                app,
                meta.id,
                ChatRequest(text="bekleyen", conversation_id=meta.id).model_dump(
                    mode="json"
                ),
            )
            assert queue_depth(app, meta.id) == 1

            chat_routes._chat_cleanup_tombstones(app).add(meta.id)
            svc.soft_delete(meta.id)

            await chat_routes._maybe_drain_queue(app, meta.id)
            await asyncio.sleep(0.05)
            assert meta.id not in chat_routes._active_turns(app)
            assert queue_depth(app, meta.id) == 0
            assert svc.get(meta.id) is None

    asyncio.run(main())


def test_enqueue_to_deleted_conversation_returns_404(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enqueue after tombstone+soft-delete while a turn is in progress returns 404 (no ensure/revive)."""
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["uzun"], delay=0.5),
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            await chat_routes.post_chat_stream(
                ChatRequest(text="ilk", conversation_id=meta.id), req, tts=None
            )
            assert meta.id in chat_routes._active_turns(app)
            chat_routes._chat_cleanup_tombstones(app).add(meta.id)
            svc.soft_delete(meta.id)

            with pytest.raises(HTTPException) as exc:
                await chat_routes.post_chat_stream(
                    ChatRequest(text="geç", conversation_id=meta.id),
                    req,
                    tts=None,
                )
            assert exc.value.status_code == 404

    asyncio.run(main())


def test_stream_to_deleted_conversation_returns_404_not_500(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multi-tab: another tab deleted the conversation (tombstone+soft-delete). When this tab
    sends a stream message to the same id with NO TURN active, it must get a clean 404 (parity
    with the 404 on the busy path). Old code turned `_start_detached_chat_turn`'s "not usable"
    RuntimeError into a 500 on path-3 → the client could not handle a clean 404 and fall back to a new session."""
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["x"], delay=0.01),
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            # The server trace of a delete: tombstone + soft-delete (NO turn).
            chat_routes._chat_cleanup_tombstones(app).add(meta.id)
            svc.soft_delete(meta.id)
            assert meta.id not in chat_routes._active_turns(app)
            req = _make_request(app)

            with pytest.raises(HTTPException) as exc:
                await chat_routes.post_chat_stream(
                    ChatRequest(text="merhaba", conversation_id=meta.id),
                    req,
                    tts=None,
                )
            assert exc.value.status_code == 404
            assert exc.value.detail["error"]["code"] == "NOT_FOUND"

    asyncio.run(main())


def test_queue_updated_ws_broadcast_on_enqueue(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G1/G3: queue_updated WS broadcast after enqueue (multi-tab sync)."""
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["a"], delay=0.5),
    )
    events: list[dict[str, Any]] = []

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            hub = app.state.event_hub

            async def capture(data: dict[str, Any]) -> None:
                events.append(data)

            hub.broadcast_json = capture  # type: ignore[method-assign]
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            await chat_routes.post_chat_stream(
                ChatRequest(text="ilk", conversation_id=meta.id), req, tts=None
            )
            await chat_routes.post_chat_stream(
                ChatRequest(text="ikinci", conversation_id=meta.id),
                req,
                tts=None,
            )
            await chat_routes.post_chat_stream(
                ChatRequest(text="üçüncü", conversation_id=meta.id),
                req,
                tts=None,
            )

        qu = [e for e in events if e.get("type") == "queue_updated" and e.get("conversation_id") == meta.id]
        assert len(qu) >= 2
        assert qu[-1]["depth"] == 2

    asyncio.run(main())


def test_double_enqueue_shares_single_server_queue(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G1: consecutive enqueues to the same conv accumulate in a single FIFO queue."""
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["a"], delay=0.5),
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            await chat_routes.post_chat_stream(
                ChatRequest(text="ilk", conversation_id=meta.id), req, tts=None
            )
            for i in range(3):
                resp = await chat_routes.post_chat_stream(
                    ChatRequest(text=f"kuyruk-{i}", conversation_id=meta.id),
                    req,
                    tts=None,
                )
                assert isinstance(resp, JSONResponse) and resp.status_code == 202
            assert queue_depth(app, meta.id) == 3

    asyncio.run(main())


def test_cancel_active_turn_preserves_queue(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """STOP: the queue is not drained after cancellation (K4)."""
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["uzun"], delay=0.5),
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            await chat_routes.post_chat_stream(
                ChatRequest(text="ilk", conversation_id=meta.id), req, tts=None
            )
            resp2 = await chat_routes.post_chat_stream(
                ChatRequest(text="bekleyen", conversation_id=meta.id),
                req,
                tts=None,
            )
            assert isinstance(resp2, JSONResponse) and resp2.status_code == 202
            assert queue_depth(app, meta.id) == 1

            res = await chat_routes.cancel_chat_active(meta.id, req)
            assert res["cancelled"] is True
            await asyncio.sleep(0.15)
            assert queue_depth(app, meta.id) == 1
            assert meta.id not in chat_routes._active_turns(app)

    asyncio.run(main())


def test_normal_message_during_active_turn_still_enqueues(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression shield: a message arriving while a turn is in progress must be queued with 202.
    (The pre-LLM command short-circuit was removed — every message is an LLM turn; while busy it
    goes into the queue and is drained once the running turn finishes.)"""
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["uzun ", "yanıt"], delay=0.4),
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            await chat_routes.post_chat_stream(
                ChatRequest(text="ilk mesaj", conversation_id=meta.id), req, tts=None
            )
            resp = await chat_routes.post_chat_stream(
                ChatRequest(text="bu normal bir mesaj", conversation_id=meta.id),
                req,
                tts=None,
            )
            assert isinstance(resp, JSONResponse) and resp.status_code == 202
            assert queue_depth(app, meta.id) == 1

            turn = chat_routes._active_turns(app)[meta.id]
            await asyncio.wait_for(turn.task, timeout=10)

    asyncio.run(main())


def test_drain_reenqueues_item_when_turn_races_in(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drain race: if another incoming message grabs the turn in the window between the
    `_is_turn_running` check and the registry set, the item `_maybe_drain_queue` popped
    MUST NOT BE LOST — it must be put back into the queue (drained again once that turn
    finishes). The old behavior swallowed the item with `except Exception: log`."""
    from akana_server.api.chat_turn_queue import enqueue_message

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            enqueue_message(
                app,
                meta.id,
                ChatRequest(text="kaybolmasin", conversation_id=meta.id).model_dump(
                    mode="json"
                ),
            )
            assert queue_depth(app, meta.id) == 1

            # Replace `_start_detached_chat_turn` with a fake that puts a turn in the
            # registry and throws "turn already running" — deterministically mimics
            # another message grabbing the turn AFTER the drain pop (the race window).
            real_active_turn = chat_routes._ActiveTurn

            async def _fake_start(request: Request, body: Any, **_kw: Any) -> Any:
                reg = chat_routes._active_turns(request.app)
                reg[meta.id] = real_active_turn(conversation_id=meta.id)
                raise RuntimeError(f"turn already running for {meta.id}")

            monkeypatch.setattr(chat_routes, "_start_detached_chat_turn", _fake_start)

            await chat_routes._maybe_drain_queue(app, meta.id)
            assert queue_depth(app, meta.id) == 1, (
                "the item popped during the drain race must be re-enqueued (no message loss)"
            )
            # Drop the fake turn from the registry (so lifespan shutdown doesn't deal with task=None).
            chat_routes._active_turns(app).pop(meta.id, None)

    asyncio.run(main())


def test_drain_delivers_gate_response_instead_of_dropping(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A GATE-RESPONSE message drained from the queue (plan proposal / skill approval)
    MUST NOT be silently DROPPED; it must be persisted and broadcast as a one-shot
    detached turn, and the REST of the queue must keep being drained.

    Root bug: the busy path was queuing a message that was NOT a command but produced
    gates.response (plan approval); on drain, `_start_detached_chat_turn` rejected it
    with "queued command response not supported" and SILENTLY dropped the turn, and on
    top of that the rest of the queue STALLED without being drained."""
    from akana_server.api.chat_turn_queue import enqueue_message

    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["normal yanıt"], delay=0.01),
    )

    real_gates = chat_routes._run_turn_gates

    async def _fake_gates(request: Request, body: ChatRequest):
        if body.text == "plan gerektiren":
            resp = chat_routes.ChatResponse(
                turn_id="test-plan-turn",
                text="İşte planım: 1) adım. Onaylıyor musun?",
                lang=body.lang,
                conversation_id=(body.conversation_id or ""),
                intent="system_action",
                action="plan_proposed",
            )
            return chat_routes._GateResult(
                intent="chat", approval_required=False, body=body, response=resp
            )
        return await real_gates(request, body)

    monkeypatch.setattr(chat_routes, "_run_turn_gates", _fake_gates)
    events: list[dict[str, Any]] = []

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            hub = app.state.event_hub

            async def capture(data: dict[str, Any]) -> None:
                events.append(data)

            hub.broadcast_json = capture  # type: ignore[method-assign]
            svc = app.state.conversation_service
            meta = svc.create()
            # Queue two items: gate-response (plan) + normal. Drain must process both.
            enqueue_message(
                app,
                meta.id,
                ChatRequest(text="plan gerektiren", conversation_id=meta.id).model_dump(
                    mode="json"
                ),
            )
            enqueue_message(
                app,
                meta.id,
                ChatRequest(text="normal", conversation_id=meta.id).model_dump(
                    mode="json"
                ),
            )
            assert queue_depth(app, meta.id) == 2

            await chat_routes._maybe_drain_queue(app, meta.id)
            # Each turn (command turn then normal turn), on finishing, drains the next one.
            for _ in range(4):
                turn = chat_routes._active_turns(app).get(meta.id)
                if turn is not None and turn.task is not None:
                    await asyncio.wait_for(turn.task, timeout=10)
                await asyncio.sleep(0.05)
                if queue_depth(app, meta.id) == 0 and meta.id not in chat_routes._active_turns(app):
                    break

            # The queue drained completely (no single broken/response item stalled it).
            assert queue_depth(app, meta.id) == 0
            assert meta.id not in chat_routes._active_turns(app)

            pairs = [(m.role, m.content) for m in svc.list_messages(meta.id, limit=20)]
            # The plan proposal was DELIVERED + PERSISTED (visible on re-fetch).
            assert ("user", "plan gerektiren") in pairs
            assert any(
                role == "assistant" and "Onaylıyor musun" in content
                for role, content in pairs
            ), "the plan proposal response was not persisted (dropped during drain)"
            # The next normal message was also processed (the queue did not stall).
            assert ("user", "normal") in pairs

        completed = [e for e in events if e.get("type") == "turn_completed"]
        assert completed, "command turn turn_completed broadcast was not emitted"

    asyncio.run(main())


def test_turn_crash_drains_queued_message(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E5: if a turn crashes, the queue is drained with the next message."""
    call_n = {"n": 0}

    async def _flaky_stream(*_args: Any, **_kwargs: Any):
        call_n["n"] += 1
        if call_n["n"] == 1:
            yield {"delta": "kısmi", "done": False}
            raise RuntimeError("bridge crash")
        yield {"delta": "tamam", "done": False}
        yield {
            "done": True,
            "text": "tamam",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "tool_calls": []},
            "status": "finished",
            "tool_calls": [],
        }

    monkeypatch.setattr(chat_routes, "stream_user_chat", _flaky_stream)

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            await chat_routes.post_chat_stream(
                ChatRequest(text="ilk", conversation_id=meta.id), req, tts=None
            )
            await chat_routes.post_chat_stream(
                ChatRequest(text="sırada", conversation_id=meta.id),
                req,
                tts=None,
            )
            assert queue_depth(app, meta.id) == 1
            turn = chat_routes._active_turns(app)[meta.id]
            await asyncio.wait_for(turn.task, timeout=10)
            await asyncio.sleep(0.2)
            assert queue_depth(app, meta.id) == 0
            msgs = svc.list_messages(meta.id, limit=20)
            user_texts = [m.content for m in msgs if m.role == "user"]
            assert "sırada" in user_texts

    asyncio.run(main())


def test_shutdown_background_tasks_cancels_and_clears() -> None:
    """Regression (lifecycle): lifespan shutdown NEVER cancelled fire-and-forget background
    tasks (the memory-capture 2nd LLM call + queue drain) → when shutdown_bridge_pool pulled
    the daemon out from under them, a dead-daemon error / half-write /
    "Task was destroyed but pending". shutdown_background_tasks cancels + clears them all."""
    from types import SimpleNamespace

    from akana_server.api.routes.chat import shutdown_background_tasks

    async def main() -> None:
        started = asyncio.Event()

        async def _long() -> None:
            started.set()
            await asyncio.sleep(3600)  # shutdown must cancel this

        task = asyncio.create_task(_long())
        await started.wait()
        app = SimpleNamespace(state=SimpleNamespace(chat_background_tasks={task}))
        await shutdown_background_tasks(app)
        assert task.cancelled()
        assert app.state.chat_background_tasks == set()

    asyncio.run(main())


def test_delete_path_skips_turn_cancel_await_reset_keeps_it(monkeypatch) -> None:
    """Regression (5-6s delete): DELETE (tombstone=True) with an active turn MUST NOT WAIT
    for the turn's finally (mid-LLM cancel, up to _CANCEL_AWAIT_TIMEOUT=15s) — the
    conversation is already being deleted + tombstoned (late writes blocked). cleanup must
    pass await_cancel=False. RESET (tombstone=False) awaits (so the partial write finishes
    BEFORE the reset → ordering guarantee)."""
    from types import SimpleNamespace

    import akana_server.api.routes.chat.chat_detached as cd

    seen: list[bool] = []

    async def _fake_cancel(app, conv_id, *, await_cancel=True):
        seen.append(await_cancel)
        return True

    monkeypatch.setattr(cd, "_cancel_active_turn_impl", _fake_cancel)

    async def main() -> None:
        app = SimpleNamespace(state=SimpleNamespace())
        await cd.cleanup_conversation_chat_state(app, "conv-del", tombstone=True)
        await cd.cleanup_conversation_chat_state(app, "conv-reset", tombstone=False)

    asyncio.run(main())
    assert seen == [False, True]  # delete: no wait; reset: wait
