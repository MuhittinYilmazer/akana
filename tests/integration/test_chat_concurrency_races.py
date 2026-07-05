"""Deep concurrency races — double-send window, multi-cancel, queue FIFO.

The existing detached-turn tests cover SEQUENTIAL scenarios; this file targets
TRULY concurrent (``asyncio.gather``) race windows: racing first-message to the
same conv, BULK cancellation of running turns, lossless FIFO-ordered drain of a
queue stacked on a single conv, and the concurrent behavior of the blocking
``/chat`` surface.

The primary machinery ``post_chat_stream`` (does not use ``Depends``) is called
directly; because the blocking ``/chat`` uses ``Depends(get_services)`` it is
driven through the REAL ASGI stack (``httpx.ASGITransport``).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from akana_server.api.app import create_app
from akana_server.api.chat_turn_queue import queue_depth
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


def _parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    name = "message"
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
        if data_lines:
            try:
                payload = json.loads("\n".join(data_lines))
            except json.JSONDecodeError:
                payload = {"raw": "\n".join(data_lines)}
            events.append((name, payload))
    return events


# -- Double-send race window ------------------------------------------------------


def test_concurrent_first_message_exactly_one_streams_no_500(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """8 concurrent FIRST messages to the same conv — EXACTLY 1 streams 200, remaining 7 queue/busy.

    SYMPTOM target: in the race window between the ``_is_turn_running`` check and
    the registry set, two turns collide on the same conv, or a RuntimeError leaks
    into a 500. ``post_chat_stream`` is driven directly with gather (single loop,
    real interleave).
    """
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["yanıt ", "akıyor"], delay=0.2),
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)

            async def _send(i: int):
                try:
                    return await chat_routes.post_chat_stream(
                        ChatRequest(text=f"ilk-{i}", conversation_id=meta.id),
                        req,
                        tts=None,
                    )
                except HTTPException as e:
                    return e

            results = await asyncio.gather(*(_send(i) for i in range(8)))
            streams = [r for r in results if isinstance(r, StreamingResponse)]
            queued = [r for r in results if isinstance(r, JSONResponse)]
            errors = [r for r in results if isinstance(r, HTTPException)]
            # None should leak a 500 RuntimeError.
            for e in errors:
                assert e.status_code in (404, 409), f"unexpected error: {e.status_code}"
            # EXACTLY 1 live stream — registry must hold a single turn.
            assert len(streams) == 1, (
                f"{len(streams)} concurrent streams on the same conv (collision!): "
                f"streams={len(streams)} queued={len(queued)} errors={len(errors)}"
            )
            active = chat_routes._active_turns(app)
            assert list(active.keys()) == [meta.id]
            # Drain the flowing stream.
            async for _ in streams[0].body_iterator:
                pass
            turn = active.get(meta.id)
            if turn is not None and turn.task is not None:
                await asyncio.wait_for(turn.task, timeout=10)

    asyncio.run(main())


# -- Bulk cancel across multiple conversations ------------------------------------


def test_cancel_all_active_conversations_at_once(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While 5 conversations are streaming, STOP them ALL at once — all cancelled, registry empty, no crash.

    SYMPTOM target: during concurrent cancellation, cancelling one conv affects
    another conv (wrong registry pop), or a crash.
    """
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["uzun ", "yanıt ", "akıyor"], delay=0.4),
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            metas = [svc.create() for _ in range(5)]
            req = _make_request(app)
            # Start them all.
            for m in metas:
                await chat_routes.post_chat_stream(
                    ChatRequest(text="ilk", conversation_id=m.id), req, tts=None
                )
            assert len(chat_routes._active_turns(app)) == 5
            # Cancel them all AT THE SAME TIME.
            cancels = await asyncio.gather(
                *(chat_routes.cancel_chat_active(m.id, req) for m in metas)
            )
            assert all(c["cancelled"] is True for c in cancels), (
                f"some cancellations failed: {cancels}"
            )
            await asyncio.sleep(0.2)
            # Registry completely empty — no zombies.
            assert chat_routes._active_turns(app) == {}, (
                f"zombie after bulk cancel: {list(chat_routes._active_turns(app))}"
            )
            # Each conversation is open to a new message.
            for m in metas:
                assert not chat_routes._is_turn_running(app, m.id)  # not busy

    asyncio.run(main())


# -- Queue FIFO + lossless drain (single-conv stacking) ---------------------------


def test_queued_messages_drain_fifo_without_loss(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Single conv with 1 active + 4 queued → all must persist FIFO-ordered, lossless.

    SYMPTOM target (owner's "dropped turns"): message loss / order corruption /
    stall during drain from the queue.
    """
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["ok"], delay=0.05),
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            # First message streams.
            await chat_routes.post_chat_stream(
                ChatRequest(text="m0", conversation_id=meta.id), req, tts=None
            )
            # 4 messages to the queue (order must be preserved).
            for i in range(1, 5):
                resp = await chat_routes.post_chat_stream(
                    ChatRequest(text=f"m{i}", conversation_id=meta.id), req, tts=None
                )
                assert isinstance(resp, JSONResponse) and resp.status_code == 202
            assert queue_depth(app, meta.id) == 4

            # Wait until the whole queue drains (sequential detached turns).
            for _ in range(60):
                await asyncio.sleep(0.1)
                if queue_depth(app, meta.id) == 0 and not chat_routes._is_turn_running(app, meta.id):
                    break
            assert queue_depth(app, meta.id) == 0, "queue did not drain (stuck?)"

            # ALL 5 user messages must be persisted (no loss), in order.
            msgs = svc.list_messages(meta.id, limit=50)
            user_texts = [m.content for m in msgs if m.role == "user"]
            assert user_texts == ["m0", "m1", "m2", "m3", "m4"], (
                f"FIFO broken / message loss: {user_texts}"
            )

    asyncio.run(main())


# -- Blocking /chat concurrent (ASGI) ---------------------------------------------


def test_blocking_chat_concurrent_distinct_conversations_no_crosstalk(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocking POST /chat — 5 distinct conversations concurrent, no cross-talk.

    SYMPTOM target: on the blocking surface, concurrent turns mix up responses.
    ``complete_chat_with_usage`` is replaced with a fake that echoes conv_id.
    """

    async def _complete(settings: Any, user_for_llm: str, *_a: Any, **kw: Any) -> tuple[str, dict[str, Any]]:
        conv_id = str(kw.get("conversation_id") or "?")
        await asyncio.sleep(0.05)  # force turns to actually interleave
        return f"BLK[{conv_id}]", {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "tool_calls": [],
        }

    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage", _complete
    )
    app = create_app()

    async def main() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=30.0
            ) as client:
                conv_ids = [f"blk-{i}" for i in range(5)]

                async def _post(cid: str):
                    r = await client.post(
                        "/api/v1/chat", json={"text": "x", "conversation_id": cid}
                    )
                    return cid, r

                results = await asyncio.gather(*(_post(c) for c in conv_ids))
            for cid, r in results:
                assert r.status_code == 200, f"{cid}: {r.status_code} {r.text[:200]}"
                data = r.json()
                assert data["conversation_id"] == cid
                assert data["text"] == f"BLK[{cid}]", (
                    f"CROSS-TALK (blocking): {cid} got {data['text']!r}"
                )

    asyncio.run(main())


def test_concurrent_same_conversation_blocking_only_one_runs(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Convergence A #2: concurrent blocking turns on the same conv — only ONE runs,
    the others get 409 TURN_BUSY. Formerly, because blocking/voice turns were not
    registered in the busy-registry, they all passed + collided on the same conv."""

    async def _complete(settings: Any, user_for_llm: str, *_a: Any, **kw: Any):
        await asyncio.sleep(0.1)  # let the others arrive while the turn is registered
        return "ok", {"prompt_tokens": 1, "completion_tokens": 1, "tool_calls": []}

    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage", _complete
    )
    app = create_app()

    async def main() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=30.0
            ) as client:
                cid = "busyconv-1"

                async def _post():
                    return await client.post(
                        "/api/v1/chat", json={"text": "x", "conversation_id": cid}
                    )

                results = await asyncio.gather(*(_post() for _ in range(5)))
            codes = sorted(r.status_code for r in results)
            assert codes.count(200) == 1, f"exactly 1 turn should run, got: {codes}"
            assert codes.count(409) == 4, f"the others should be 409 TURN_BUSY: {codes}"

    asyncio.run(main())


def test_blocking_busy_released_after_error_and_success(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NO sticky-busy failure mode: when a turn ends with an error OR success, the conv is freed.

    Registry cleanup is tied to the request task's done-callback → the registration
    is dropped even on exception/cancel. This test catches the 'conv stuck at 409'
    regression."""
    from akana_server.orchestrator.llm_dispatch import LLMCallError

    state = {"boom": True}

    async def _complete(settings: Any, user_for_llm: str, *_a: Any, **kw: Any):
        if state["boom"]:
            raise LLMCallError("patla", status_code=502)
        return "ok", {"prompt_tokens": 1, "completion_tokens": 1, "tool_calls": []}

    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage", _complete
    )
    app = create_app()

    async def main() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=30.0
            ) as client:
                cid = "rel-1"
                r1 = await client.post(
                    "/api/v1/chat", json={"text": "x", "conversation_id": cid}
                )
                assert r1.status_code == 502  # LLM error
                state["boom"] = False  # after the error the conv must be FREE
                r2 = await client.post(
                    "/api/v1/chat", json={"text": "x", "conversation_id": cid}
                )
                assert r2.status_code == 200, f"busy stuck after error: {r2.status_code}"
                r3 = await client.post(
                    "/api/v1/chat", json={"text": "x", "conversation_id": cid}
                )
                assert r3.status_code == 200, f"busy stuck after success: {r3.status_code}"

    asyncio.run(main())


def test_cancel_active_turn_cancels_blocking_turn(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Convergence A #3: _cancel_active_turn_impl also cancels a running
    blocking/voice turn (formerly it only cancelled the streaming turn → voice
    'stop'/conversation delete could not stop an in-flight voice turn)."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocked(settings: Any, user_text: str, *_a: Any, **_kw: Any):
        started.set()
        await release.wait()  # block in the LLM until cancel arrives
        return "ok", {"prompt_tokens": 1, "completion_tokens": 1, "tool_calls": []}

    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage", _blocked
    )
    app = create_app()

    async def main() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=30.0
            ) as client:
                cid = "cancelblk-1"
                req_task = asyncio.create_task(
                    client.post("/api/v1/chat", json={"text": "x", "conversation_id": cid})
                )
                await asyncio.wait_for(started.wait(), timeout=5.0)  # turn registered + in the LLM
                assert chat_routes._is_turn_running(app, cid)

                assert await chat_routes._cancel_active_turn_impl(app, cid) is True
                assert not chat_routes._is_turn_running(app, cid)  # registration dropped

                release.set()
                req_task.cancel()
                try:
                    await req_task
                except BaseException:
                    pass

    asyncio.run(main())


def test_stop_on_blocking_turn_preserves_queue(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """b8: STOP on a blocking/voice (non-streaming) turn must NOT auto-run the queued message
    (K4 'STOP preserves the queue'). The non-streaming guard used to drain on EVERY exit,
    including cancel — so a STOP silently ran the next queued message."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocked(settings: Any, user_text: str, *_a: Any, **_kw: Any):
        started.set()
        await release.wait()
        return "ok", {"prompt_tokens": 1, "completion_tokens": 1, "tool_calls": []}

    monkeypatch.setattr("akana_server.api.routes.chat.complete_chat_with_usage", _blocked)
    monkeypatch.setattr(
        chat_routes, "stream_user_chat", _slow_stream_factory(["x"], delay=0.01)
    )
    app = create_app()

    async def main() -> None:
        async with app.router.lifespan_context(app):
            meta = app.state.conversation_service.create()
            cid = meta.id
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=30.0
            ) as client:
                req_task = asyncio.create_task(
                    client.post("/api/v1/chat", json={"text": "x", "conversation_id": cid})
                )
                await asyncio.wait_for(started.wait(), timeout=5.0)
                assert chat_routes._is_turn_running(app, cid)
                # Queue a message behind the running blocking turn (202).
                req = _make_request(app)
                resp = await chat_routes.post_chat_stream(
                    ChatRequest(text="queued", conversation_id=cid), req, tts=None
                )
                assert isinstance(resp, JSONResponse) and resp.status_code == 202
                assert queue_depth(app, cid) == 1
                # STOP the blocking turn.
                assert await chat_routes._cancel_active_turn_impl(app, cid) is True
                await asyncio.sleep(0.3)  # give any (wrongly) spawned drain a chance to run
                assert queue_depth(app, cid) == 1, "STOP wrongly drained the queue (b8)"
                release.set()
                req_task.cancel()
                try:
                    await req_task
                except BaseException:
                    pass

    asyncio.run(main())


def test_connector_turn_completion_drains_web_queue(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """b1: a web message queued (202) while a connector (Telegram) turn holds the conversation
    must be drained when the connector turn COMPLETES — it used to be stranded forever (the
    connector guard never drained, unlike the web guards)."""
    from akana_server.connectors.service import _make_turn_guard

    monkeypatch.setattr(
        chat_routes, "stream_user_chat", _slow_stream_factory(["ok"], delay=0.01)
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            cid = svc.create().id
            guard = _make_turn_guard(app)
            req = _make_request(app)
            async with guard(cid):  # a connector turn holds the conversation
                resp = await chat_routes.post_chat_stream(
                    ChatRequest(text="web-queued", conversation_id=cid), req, tts=None
                )
                assert isinstance(resp, JSONResponse) and resp.status_code == 202
                assert queue_depth(app, cid) == 1
            # Connector turn done → its guard finally must drain the web queue.
            for _ in range(60):
                await asyncio.sleep(0.1)
                if queue_depth(app, cid) == 0 and not chat_routes._is_turn_running(app, cid):
                    break
            assert queue_depth(app, cid) == 0, "connector completion did not drain the web queue (b1)"
            msgs = svc.list_messages(cid, limit=50)
            assert any(m.role == "user" and m.content == "web-queued" for m in msgs)

    asyncio.run(main())


def test_instant_delete_does_not_wait_for_nonstreaming_cancel(env) -> None:
    """b23: the DELETE path (_cancel_nonstreaming_turn await_cancel=False) must NOT block waiting
    for a mid-LLM blocking/voice turn's slow cancel finally — it returns promptly (the tombstone
    already blocks late writes)."""
    from akana_server.api.routes.chat.chat_state import (
        _cancel_nonstreaming_turn,
        _nonstreaming_busy,
    )

    app = create_app()

    async def main() -> None:
        async with app.router.lifespan_context(app):
            cid = "instdel-1"

            async def _turn() -> None:
                try:
                    await asyncio.sleep(30)  # "in the LLM"
                except asyncio.CancelledError:
                    await asyncio.sleep(2.0)  # a SLOW cancel finally (partial persist / abort)
                    raise

            task = asyncio.create_task(_turn())
            _nonstreaming_busy(app)[cid] = task
            await asyncio.sleep(0.05)
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            assert await _cancel_nonstreaming_turn(app, cid, await_cancel=False) is True
            elapsed = loop.time() - t0
            assert elapsed < 1.0, f"instant delete blocked {elapsed:.2f}s on the cancel finally (b23)"
            await asyncio.wait({task}, timeout=3.0)  # the cancel completes in the background

    asyncio.run(main())


def test_queued_voice_turn_carries_tts(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """b6: a queued (202) voice turn keeps its ?tts language on the body, so when it is later
    drained the hands-free reply is still spoken (the query param used to be dropped on enqueue)."""
    from akana_server.api.chat_turn_queue import pop_next

    monkeypatch.setattr(
        chat_routes, "stream_user_chat", _slow_stream_factory(["ok"], delay=0.05)
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            cid = app.state.conversation_service.create().id
            req = _make_request(app)
            # First message occupies the conversation.
            await chat_routes.post_chat_stream(
                ChatRequest(text="m0", conversation_id=cid), req, tts=None
            )
            # A voice message arrives while busy → queued WITH its tts language.
            resp = await chat_routes.post_chat_stream(
                ChatRequest(text="voice-q", conversation_id=cid, voice=True), req, tts="tr"
            )
            assert isinstance(resp, JSONResponse) and resp.status_code == 202
            item = pop_next(app, cid)
            assert item is not None
            assert item.payload.get("tts") == "tr", "queued voice turn must carry its tts"

    asyncio.run(main())


def test_one_conversation_crash_does_not_corrupt_others(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One of 5 concurrent turns CRASHES mid-stream — the others survive, the registry is cleaned up.

    SYMPTOM target: one turn crashing corrupts shared state (registry/queue) and
    affects other conversations, or the crashing turn leaves a zombie in the
    registry. Conversation "crash-2" raises a RuntimeError mid-stream; the other 4
    finish normally.
    """

    async def _maybe_crash(settings: Any, user_for_llm: str, *_a: Any, **kw: Any):
        conv_id = str(kw.get("conversation_id") or "?")
        yield {"delta": f"CONV[{conv_id}]", "done": False}
        await asyncio.sleep(0.03)
        if conv_id == "crash-2":
            raise RuntimeError("bridge patladı (simüle)")
        yield {
            "done": True,
            "text": f"CONV[{conv_id}]",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "tool_calls": []},
            "status": "finished",
            "tool_calls": [],
        }

    monkeypatch.setattr(chat_routes, "stream_user_chat", _maybe_crash)
    app = create_app()

    async def _drain(resp: StreamingResponse) -> str:
        body = b""
        async for c in resp.body_iterator:
            body += c
        return body.decode("utf-8")

    async def main() -> None:
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            conv_ids = [f"crash-{i}" for i in range(5)]
            metas = {cid: svc.ensure(cid) for cid in conv_ids}  # noqa: F841
            req = _make_request(app)
            resps = await asyncio.gather(
                *(
                    chat_routes.post_chat_stream(
                        ChatRequest(text="x", conversation_id=cid), req, tts=None
                    )
                    for cid in conv_ids
                )
            )
            bodies = await asyncio.gather(*(_drain(r) for r in resps))
            await asyncio.sleep(0.2)
            # ALL turns, including the crashing one, must drop from the registry (no zombies).
            assert chat_routes._active_turns(app) == {}, (
                f"zombie registry after crash: {list(chat_routes._active_turns(app))}"
            )
            for cid, body in zip(conv_ids, bodies):
                events = _parse_sse(body)
                names = [n for n, _ in events]
                if cid == "crash-2":
                    # The crashing turn must close cleanly with error/done (must not hang).
                    assert "error" in names or "done" in names, (
                        f"{cid}: crashing turn did not close cleanly: {names}"
                    )
                else:
                    done = [p for n, p in events if n == "done"]
                    assert done, f"{cid}: affected by neighbor's crash, no done: {names}"
                    assert f"CONV[{cid}]" in done[-1].get("text", "")
            # Healthy conversations are open to a new message.
            for cid in conv_ids:
                if cid != "crash-2":
                    assert not chat_routes._is_turn_running(app, cid)

    asyncio.run(main())


def test_queue_full_returns_429_not_crash(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the queue ceiling (QUEUE_MAX_DEPTH=10) is exceeded, 429 — not silent loss / crash.

    SYMPTOM target: messages stacked on a single conv grow unbounded (memory), or
    the 11th message results in a crash/silent loss.
    """
    from akana_server.api.chat_turn_queue import QUEUE_MAX_DEPTH

    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["uzun"], delay=2.0),  # the first turn streams for a long time
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            # First message streams (occupies the registry).
            await chat_routes.post_chat_stream(
                ChatRequest(text="m0", conversation_id=meta.id), req, tts=None
            )
            # QUEUE_MAX_DEPTH messages fit into the queue.
            for i in range(QUEUE_MAX_DEPTH):
                resp = await chat_routes.post_chat_stream(
                    ChatRequest(text=f"q{i}", conversation_id=meta.id), req, tts=None
                )
                assert isinstance(resp, JSONResponse) and resp.status_code == 202
            assert queue_depth(app, meta.id) == QUEUE_MAX_DEPTH
            # A message over the ceiling must get 429 QUEUE_FULL (not a crash).
            with pytest.raises(HTTPException) as exc:
                await chat_routes.post_chat_stream(
                    ChatRequest(text="taşan", conversation_id=meta.id), req, tts=None
                )
            assert exc.value.status_code == 429
            assert exc.value.detail["error"]["code"] == "QUEUE_FULL"
            # The queue stayed at the ceiling (the overflow message was not added → no unbounded growth).
            assert queue_depth(app, meta.id) == QUEUE_MAX_DEPTH
            # Cleanup: cancel the streaming turn (don't wait out the 2s delay).
            await chat_routes.cancel_chat_active(meta.id, req)

    asyncio.run(main())


def test_concurrent_turns_get_distinct_trace_ids(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each concurrent turn must get a DISTINCT trace_id (contextvar isolation).

    SYMPTOM target (debuggability): if detached tasks inherit each other's
    trace_id due to context copying, logs/anchors get mixed up → "which turn is
    which" becomes indistinguishable. Each conv's meta event must carry a unique
    trace_id.
    """
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["a", "b"], delay=0.05),
    )
    app = create_app()

    async def _drain(resp: StreamingResponse) -> list[tuple[str, dict[str, Any]]]:
        body = b""
        async for c in resp.body_iterator:
            body += c
        return _parse_sse(body.decode("utf-8"))

    async def main() -> None:
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            metas = [svc.create() for _ in range(5)]
            req = _make_request(app)
            resps = await asyncio.gather(
                *(
                    chat_routes.post_chat_stream(
                        ChatRequest(text="x", conversation_id=m.id), req, tts=None
                    )
                    for m in metas
                )
            )
            all_events = await asyncio.gather(*(_drain(r) for r in resps))
            trace_ids = []
            for m, events in zip(metas, all_events):
                metas_ev = [p for n, p in events if n == "meta"]
                assert metas_ev, f"{m.id}: no meta event"
                tid = metas_ev[0].get("trace_id")
                assert tid and tid != "-", f"{m.id}: invalid trace_id {tid!r}"
                trace_ids.append(tid)
            # All unique — no turn inherited another's id.
            assert len(set(trace_ids)) == len(trace_ids), (
                f"trace_id collision (contextvar leak): {trace_ids}"
            )
            await asyncio.sleep(0.1)

    asyncio.run(main())


def test_concurrent_turn_completed_events_have_correct_conv_ids(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As concurrent turns finish, turn_completed WS broadcasts must carry the correct conv_id.

    SYMPTOM target: under a shared ``broadcast_json``, as turns finish interleaved,
    one turn's turn_completed is broadcast with another conv_id (WS cross-talk).
    """
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["x"], delay=0.05),
    )
    app = create_app()
    events: list[dict[str, Any]] = []
    lock = asyncio.Lock()

    async def main() -> None:
        async with app.router.lifespan_context(app):
            hub = app.state.event_hub

            async def capture(data: dict[str, Any]) -> None:
                async with lock:
                    events.append(data)

            hub.broadcast_json = capture  # type: ignore[method-assign]
            svc = app.state.conversation_service
            metas = [svc.create() for _ in range(5)]
            req = _make_request(app)
            resps = await asyncio.gather(
                *(
                    chat_routes.post_chat_stream(
                        ChatRequest(text="x", conversation_id=m.id), req, tts=None
                    )
                    for m in metas
                )
            )

            async def _drain(resp: StreamingResponse) -> None:
                async for _ in resp.body_iterator:
                    pass

            await asyncio.gather(*(_drain(r) for r in resps))
            # Let all turns finish.
            for m in metas:
                turn = chat_routes._active_turns(app).get(m.id)
                if turn is not None and turn.task is not None:
                    await asyncio.wait_for(turn.task, timeout=10)
            await asyncio.sleep(0.2)

        completed = [e for e in events if e.get("type") == "turn_completed"]
        completed_convs = sorted(e["conversation_id"] for e in completed)
        expected = sorted(m.id for m in metas)
        # EXACTLY one turn_completed per conversation — no missing/extra/wrong conv_id.
        assert completed_convs == expected, (
            f"turn_completed conv_id set is wrong (WS cross-talk?): "
            f"got={completed_convs} expected={expected}"
        )
        # All in ok status.
        assert all(e["status"] == "ok" for e in completed)

    asyncio.run(main())


def test_blocking_chat_busy_when_stream_active_same_conv(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While a stream turn is running on the same conv, blocking /chat must get 409 TURN_BUSY.

    SYMPTOM target: if the blocking surface is blind to the active stream turn, two
    turns collide. (The detached test did this by calling the route directly; here
    it goes through the REAL ASGI — starts the stream from the primitive and tries
    the blocking one with httpx.)
    """
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["uzun ", "yanıt"], delay=0.4),
    )
    app = create_app()

    async def main() -> None:
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            # Start the stream turn directly from the primitive (in a streaming state).
            req = _make_request(app)
            await chat_routes.post_chat_stream(
                ChatRequest(text="akan", conversation_id=meta.id), req, tts=None
            )
            assert meta.id in chat_routes._active_turns(app)

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=30.0
            ) as client:
                r = await client.post(
                    "/api/v1/chat", json={"text": "blok", "conversation_id": meta.id}
                )
            assert r.status_code == 409, f"blocking did not get TURN_BUSY: {r.status_code}"
            assert r.json()["detail"]["error"]["code"] == "TURN_BUSY"

            turn = chat_routes._active_turns(app).get(meta.id)
            if turn is not None and turn.task is not None:
                await asyncio.wait_for(turn.task, timeout=10)

    asyncio.run(main())


# -- User repro: A long turn + abandon follower + B concurrent + return to A ------


def test_multiconv_abandon_follower_then_resume_no_crash(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User repro: start a turn on A → ABANDON the follower (new-conversation abort) →
    turn on B (A still running = 2 concurrent detached turns) → RETURN to A (resume).

    SYMPTOM target: "akana started doing its work and the server went away" — a
    crash/hang or loss of the user/assistant turn during two concurrent detached
    turns + abandoned follower + resume. Context-assembly + persist run
    CONCURRENTLY.
    """
    monkeypatch.setattr(
        chat_routes,
        "stream_user_chat",
        _slow_stream_factory(["özet ", "akıyor ", "bitti"], delay=0.12),
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            a = svc.create()
            b = svc.create()
            req = _make_request(app)
            # 1) Start a turn on A (detached).
            respA = await chat_routes.post_chat_stream(
                ChatRequest(text="bugünü ve dünü özetle", conversation_id=a.id), req, tts=None
            )
            assert isinstance(respA, StreamingResponse)
            # 2) Read 1 chunk from A's follower, then ABANDON (new-conversation abort).
            itA = respA.body_iterator
            await itA.__anext__()
            await itA.aclose()
            # 3) Start a turn on B (A still running → 2 concurrent detached turns).
            respB = await chat_routes.post_chat_stream(
                ChatRequest(text="dünü özetle", conversation_id=b.id), req, tts=None
            )
            assert isinstance(respB, StreamingResponse)
            # 4) RETURN to A: resume (get_chat_active) + consume B.
            respA2 = await chat_routes.get_chat_active(a.id, req)
            if isinstance(respA2, StreamingResponse):
                async for _ in respA2.body_iterator:
                    pass
            async for _ in respB.body_iterator:
                pass
            # Let the detached turns finish.
            for _ in range(80):
                if not chat_routes._active_turns(app):
                    break
                await asyncio.sleep(0.1)
            for conv in (a.id, b.id):
                t = chat_routes._active_turns(app).get(conv)
                if t is not None and getattr(t, "task", None) is not None:
                    await asyncio.wait_for(t.task, timeout=10)
            # No crash/hang + both convs persisted the user turn.
            ua = [m.content for m in svc.list_messages(a.id, limit=50) if m.role == "user"]
            ub = [m.content for m in svc.list_messages(b.id, limit=50) if m.role == "user"]
            assert ua, "A user turn must persist (visible on mid-turn return)"
            assert ub, "B user turn must persist"
            assert not chat_routes._active_turns(app), "registry must stay empty (no zombies)"

    asyncio.run(asyncio.wait_for(main(), timeout=45))
