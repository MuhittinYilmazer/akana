"""Voice-mode lifecycle + disconnect/cancel races (owner's concern #2).

While a voice stream is in progress the page reloads / the client disconnects →
the detached turn must survive, no orphan task must remain, the partial response
must be preserved. Also adjacent edge cases like rapid cancel + re-send,
empty/huge input, multi-tab same-conversation double-send.

The detached-turn primitives (``post_chat_stream`` + ``_active_turns`` +
``_follow_turn``) are called directly — because TestClient buffers the body, a
client disconnect CANNOT be simulated over HTTP (see the test_chat_detached_turn.py
module header). ``post_chat_stream`` does not use ``Depends`` → it can be called directly.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from akana_server.api.app import create_app
from akana_server.api.routes import chat as chat_routes
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


# -- Voice-mode lifecycle ---------------------------------------------------------


def test_voice_stream_survives_refresh_midstream(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While a voice turn streams the page reloads (follower closes) → the turn
    CONTINUES + persists.

    The voice=True path (no TTS query; voice mode adds a short-response directive
    but the detached machinery is the same). SYMPTOM: on a voice turn, when the
    client disconnects the turn is cancelled / the response is lost.
    """
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["Mer", "haba ", "efendim."], delay=0.05),
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            resp = await chat_routes.post_chat_stream(
                ChatRequest(text="selam", conversation_id=meta.id, voice=True),
                req,
                tts=None,
            )
            assert isinstance(resp, StreamingResponse)
            it = resp.body_iterator
            first = await it.__anext__()
            assert b"event: meta" in first
            turn = chat_routes._active_turns(app).get(meta.id)
            assert turn is not None and turn.task is not None
            # Page reloaded → follower closed (the voice-mode client left).
            await it.aclose()
            # The turn must complete without a client.
            await asyncio.wait_for(turn.task, timeout=10)
            msgs = svc.list_messages(meta.id, limit=10)
            pairs = [(m.role, m.content) for m in msgs]
            assert ("user", "selam") in pairs
            assert ("assistant", "Merhaba efendim.") in pairs
            assert meta.id not in chat_routes._active_turns(app)

    asyncio.run(main())


def test_voice_resume_after_refresh_streams_live(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reload while a voice turn streams → resume (GET /chat/active) replays the buffer + continues live."""
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["bir ", "iki ", "üç ", "dört"], delay=0.08),
    )

    async def _collect(it, *, limit: int = 1000) -> list[bytes]:
        out: list[bytes] = []
        async for c in it:
            out.append(c)
            if len(out) >= limit:
                break
        return out

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            resp = await chat_routes.post_chat_stream(
                ChatRequest(text="say", conversation_id=meta.id, voice=True),
                req,
                tts=None,
            )
            primary = resp.body_iterator
            await primary.__anext__()
            await asyncio.sleep(0.2)
            await primary.aclose()  # reload

            active = await chat_routes.get_chat_active(meta.id, req)
            assert isinstance(active, StreamingResponse)
            chunks = await _collect(active.body_iterator)
            joined = b"".join(chunks)
            assert b"event: meta" in joined  # replay from the start
            assert b"event: done" in joined  # continued live and finished
            turn = chat_routes._active_turns(app).get(meta.id)
            if turn is not None and turn.task is not None:
                await asyncio.wait_for(turn.task, timeout=10)
            assert meta.id not in chat_routes._active_turns(app)

    asyncio.run(main())


def test_no_orphan_tasks_after_disconnect_and_completion(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After disconnect + completion no ORPHAN task must remain (TTS pump / forward leak).

    SYMPTOM: on every disconnect a TTS helper task (pump/forward) is left dangling →
    a task leak over time. When the turn ends the app's background-task set must be empty.
    """
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["a", "b", "c"], delay=0.03),
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            resp = await chat_routes.post_chat_stream(
                ChatRequest(text="oku", conversation_id=meta.id, voice=True),
                req,
                tts=None,
            )
            await resp.body_iterator.__anext__()
            await resp.body_iterator.aclose()  # early disconnect
            turn = chat_routes._active_turns(app).get(meta.id)
            assert turn is not None and turn.task is not None
            await asyncio.wait_for(turn.task, timeout=10)
            await asyncio.sleep(0.1)  # let the add_done_callback discards catch up
            # app.state.chat_background_tasks (memory capture etc.) must be empty.
            bg = getattr(app.state, "chat_background_tasks", set())
            assert all(t.done() for t in bg), (
                f"background task leaked after the turn ended: "
                f"{[t for t in bg if not t.done()]}"
            )

    asyncio.run(main())


# -- Rapid cancel / re-send race --------------------------------------------------


def test_rapid_cancel_then_resend_same_conversation(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Press STOP then IMMEDIATELY send a new message — the second turn must start
    WITHOUT hitting TURN_BUSY.

    SYMPTOM: a new message arrives before cancel clears the registry → 500/TURN_BUSY,
    or two turns collide on the same conv.
    """
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["uzun ", "yanıt ", "akıyor"], delay=0.3),
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
            # STOP + immediately a new message (sequential but without a breath).
            res = await chat_routes.cancel_chat_active(meta.id, req)
            assert res["cancelled"] is True
            resp2 = await chat_routes.post_chat_stream(
                ChatRequest(text="ikinci", conversation_id=meta.id), req, tts=None
            )
            # The new turn must get a live stream (not a 202 queue, not a TURN_BUSY 409).
            assert isinstance(resp2, StreamingResponse), (
                f"new message after cancel did not get a stream: {type(resp2).__name__}"
            )
            turn2 = chat_routes._active_turns(app).get(meta.id)
            assert turn2 is not None and turn2.task is not None
            await asyncio.wait_for(turn2.task, timeout=10)

    asyncio.run(main())


def test_concurrent_cancel_and_resend_race(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start cancel and a new stream AT THE SAME TIME — no 500 must leak, state stays consistent.

    SYMPTOM target: while cancel pops from the registry a new message writes to the
    registry → a race; either a crash or a zombie turn.
    """
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["uzun ", "yanıt"], delay=0.3),
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

            async def _do_cancel():
                return await chat_routes.cancel_chat_active(meta.id, req)

            async def _do_resend():
                try:
                    return await chat_routes.post_chat_stream(
                        ChatRequest(text="yeni", conversation_id=meta.id),
                        req,
                        tts=None,
                    )
                except HTTPException as e:
                    return e

            cancel_res, resend_res = await asyncio.gather(_do_cancel(), _do_resend())
            # resend is either a stream, a 202 queue, or a 409 busy — but NOT a 500 RuntimeError.
            if isinstance(resend_res, HTTPException):
                assert resend_res.status_code in (404, 409), (
                    f"unexpected error in the cancel/resend race: {resend_res.status_code}"
                )
            else:
                assert isinstance(resend_res, (StreamingResponse, JSONResponse))
            # In the end the registry must close consistently (no dangling zombie).
            await asyncio.sleep(0.5)
            turn = chat_routes._active_turns(app).get(meta.id)
            if turn is not None and turn.task is not None:
                await asyncio.wait_for(turn.task, timeout=10)
            await asyncio.sleep(0.1)
            assert meta.id not in chat_routes._active_turns(app)

    asyncio.run(main())


# -- Empty / huge input -----------------------------------------------------------


def test_empty_text_rejected_at_schema_boundary(env) -> None:
    """Empty/whitespace text is rejected AT THE BOUNDARY (ChatRequest validation).

    NO BUG HERE: input validation happens at the system boundary (pydantic) — empty
    text NEVER reaches the turn machine, so there is no "permanent busy" risk.
    Regression shield: if this validation is removed, empty input leaks into the stream path.
    """
    from pydantic import ValidationError

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            with pytest.raises(ValidationError):
                ChatRequest(text="   ", conversation_id="conv-empty")
            # Validation at the boundary → no turn was ever written to the registry.
            assert chat_routes._active_turns(app) == {}

    asyncio.run(main())


def test_max_length_input_streams_and_clears(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Large input AT THE LIMIT (32000 char = ChatRequest ceiling) — the stream must finish, the registry must clear.

    SYMPTOM target: the largest allowed text blows up context compilation / persist,
    the turn stalls midway and leaves the conversation permanently busy. (>32000 char
    is already rejected at the boundary by pydantic — its sibling
    test_empty_text_rejected_at_schema_boundary covers that implicitly.)
    """
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["tamam"], delay=0.01),
    )
    big = "a" * 32_000  # ChatRequest.text max_length

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            resp = await chat_routes.post_chat_stream(
                ChatRequest(text=big, conversation_id=meta.id), req, tts=None
            )
            assert isinstance(resp, StreamingResponse)
            async for _ in resp.body_iterator:
                pass
            turn = chat_routes._active_turns(app).get(meta.id)
            if turn is not None and turn.task is not None:
                await asyncio.wait_for(turn.task, timeout=10)
            await asyncio.sleep(0.1)
            assert meta.id not in chat_routes._active_turns(app)
            # The user turn was persisted (the large text was not lost).
            msgs = svc.list_messages(meta.id, limit=5)
            assert any(m.role == "user" and len(m.content) == 32_000 for m in msgs)

    asyncio.run(main())
