"""hunt5 — the SERVER side of the turn-lifecycle event contract.

    turn_active     {type, conversation_id, source}
    turn_completed  {type, conversation_id, status, source, assistant_turn_id?}
    source: "user" (the sender is watching it) | "background" (arrived on its own)
    status: "ok" | "error" | "cancelled" — the REAL outcome

Rule 1 is the one this file mostly guards: EVERY turn that announces itself must emit
exactly ONE completion on EVERY exit path — success, error and CANCELLED. "Cancelled
emits nothing" left every consumer latched on "running" forever (a phantom ticking
"working…" strip on every revisit, a permanent sidebar "Responding" badge).

The tests call the route functions directly inside a real lifespan (the TestClient
buffers a streaming body completely, so a client disconnect/STOP cannot be simulated
over HTTP) — the same shape as tests/integration/test_chat_detached_turn.py.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request

from akana_server.api.app import create_app
from akana_server.api.chat_turn_queue import enqueue_message, queue_depth
from akana_server.api.routes import chat as chat_routes
from akana_server.api.routes.chat import ChatRequest
from akana_server.api.routes.chat import chat_detached as cd
from akana_server.api.routes.chat.turn_gate import register_turn, release_turn


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
    monkeypatch.setenv("AKANA_LLM_CHAT_TITLES", "0")
    return tmp_path


def _slow_stream_factory(deltas: list[str], *, delay: float = 0.05):
    text = "".join(deltas)

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


def _capture(app: FastAPI, events: list[dict[str, Any]]) -> None:
    async def capture(data: dict[str, Any]) -> None:
        events.append(data)

    app.state.event_hub.broadcast_json = capture  # type: ignore[method-assign]


def _lifecycle(events: list[dict[str, Any]], kind: str, conv_id: str) -> list[dict]:
    return [
        e for e in events if e.get("type") == kind and e.get("conversation_id") == conv_id
    ]


# --------------------------------------------------------------------------- #
# Rule 1 — a cancelled (STOP) turn still announces its outcome
# --------------------------------------------------------------------------- #


def test_cancelled_turn_broadcasts_turn_completed(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """STOP is an OUTCOME, not a non-event: turn_active must be answered by exactly one
    turn_completed(status="cancelled"), or the working strip / sidebar badge latch."""
    monkeypatch.setattr(
        chat_routes, "stream_user_chat", _slow_stream_factory(["uzun"], delay=0.5)
    )
    events: list[dict[str, Any]] = []

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            _capture(app, events)
            meta = app.state.conversation_service.create()
            req = _make_request(app)
            await chat_routes.post_chat_stream(
                ChatRequest(text="selam", conversation_id=meta.id), req, tts=None
            )
            res = await chat_routes.cancel_chat_active(meta.id, req)
            assert res["cancelled"] is True
            await asyncio.sleep(0.2)

            active = _lifecycle(events, "turn_active", meta.id)
            completed = _lifecycle(events, "turn_completed", meta.id)
            assert len(active) == 1, events
            assert len(completed) == 1, f"cancelled turn announced nothing: {events}"
            assert completed[0]["status"] == "cancelled"
            # source stamps WHO produced it; the user's own send must never drive the
            # background-work indicator or a desktop notification.
            assert completed[0]["source"] == "user"
            assert active[0]["source"] == "user"

    asyncio.run(main())


def test_cancelled_turn_drains_parked_background_results(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """STOP preserves the user QUEUE (K4) but must still deliver a parked background
    result — it is a finished job's answer, not a message waiting to run."""
    from akana_server import chat_injections

    monkeypatch.setattr(
        chat_routes, "stream_user_chat", _slow_stream_factory(["uzun"], delay=0.5)
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            req = _make_request(app)
            await chat_routes.post_chat_stream(
                ChatRequest(text="selam", conversation_id=meta.id), req, tts=None
            )
            # The background job finished WHILE the user's turn was streaming → parked.
            data_dir = app.state.settings.data_dir
            assert (
                chat_injections._enqueue(
                    data_dir, meta.id, "job finished: 42", "schedule", "Job"
                )
                == "queued"
            )
            await chat_routes.cancel_chat_active(meta.id, req)

            for _ in range(60):
                await asyncio.sleep(0.05)
                texts = [m.content for m in svc.list_messages(meta.id, limit=20)]
                if "job finished: 42" in texts:
                    return
            raise AssertionError(
                f"parked background result never delivered after STOP: {texts}"
            )

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# Rule 2 — status is the REAL outcome
# --------------------------------------------------------------------------- #


def test_error_sse_tail_completes_with_status_error(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An LLM failure is caught INSIDE the producer and emitted as an `error` SSE tail,
    so the generator returns normally. Reporting that as "ok" made a background chat
    toast "response ready" for a turn that actually failed."""
    from akana_server.orchestrator.llm_dispatch import LLMCallError

    async def _boom(*_args: Any, **_kwargs: Any):
        raise LLMCallError("provider down", status_code=503)
        yield  # pragma: no cover - generator marker

    monkeypatch.setattr(chat_routes, "stream_user_chat", _boom)
    events: list[dict[str, Any]] = []

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            _capture(app, events)
            meta = app.state.conversation_service.create()
            req = _make_request(app)
            resp = await chat_routes.post_chat_stream(
                ChatRequest(text="selam", conversation_id=meta.id), req, tts=None
            )
            body = b"".join([c async for c in resp.body_iterator])
            assert b"event: error" in body
            turn = chat_routes._active_turns(app).get(meta.id)
            if turn is not None and turn.task is not None:
                await asyncio.wait({turn.task}, timeout=5)
            await asyncio.sleep(0.1)

            completed = _lifecycle(events, "turn_completed", meta.id)
            assert len(completed) == 1, events
            assert completed[0]["status"] == "error", completed[0]

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# Blocking / voice / connector turns — the gate is the emission point
# --------------------------------------------------------------------------- #


def _gate_app(events: list[dict[str, Any]]):
    from akana_server.events import EventHub

    class _Hub(EventHub):
        async def broadcast_json(self, data):  # type: ignore[override]
            events.append(data)

    return SimpleNamespace(state=SimpleNamespace(event_hub=_Hub()))


def test_turn_gate_register_and_release_announce_the_pair() -> None:
    """Registering a non-streaming turn ANNOUNCES it and releasing announces the
    outcome — the blocking POST /chat, voice and connector surfaces emitted nothing."""
    events: list[dict[str, Any]] = []
    app = _gate_app(events)

    async def main() -> None:
        handle = register_turn(app, "convX")
        assert handle is not None
        release_turn(app, "convX", handle, status="ok")
        await asyncio.sleep(0.05)

    asyncio.run(main())
    types = [e["type"] for e in events]
    assert types == ["turn_active", "turn_completed"], events
    assert events[0]["source"] == "user"
    assert events[1]["status"] == "ok" and events[1]["source"] == "user"


def test_turn_gate_release_reports_the_real_outcome() -> None:
    """A failed/cancelled non-streaming turn must NOT be announced as "ok": consumers
    gate their toast + notification on the status field."""
    for status in ("error", "cancelled"):
        events: list[dict[str, Any]] = []
        app = _gate_app(events)

        async def main() -> None:
            handle = register_turn(app, "convX")
            release_turn(app, "convX", handle, status=status)
            await asyncio.sleep(0.05)

        asyncio.run(main())
        completed = [e for e in events if e["type"] == "turn_completed"]
        assert [e["status"] for e in completed] == [status], events


def test_blocking_turn_guard_emits_one_pair_including_on_cancel(env) -> None:
    """The blocking/voice decorator is wired to the announcing seam, and a STOP
    (CancelledError) still reports "cancelled" instead of vanishing."""
    from akana_server.api.routes.chat._base import guard_nonstreaming_turn

    events: list[dict[str, Any]] = []
    app = _gate_app(events)

    @guard_nonstreaming_turn(lambda kw: kw.get("conv_id"))
    async def _handler(*, request: Any, conv_id: str) -> str:
        raise asyncio.CancelledError()

    async def main() -> None:
        req = SimpleNamespace(app=app)
        with pytest.raises(asyncio.CancelledError):
            await _handler(request=req, conv_id="convV")
        await asyncio.sleep(0.05)

    asyncio.run(main())
    assert [e["type"] for e in events] == ["turn_active", "turn_completed"], events
    assert events[1]["status"] == "cancelled", events


def test_blocking_turn_finally_drains_parked_injections(env) -> None:
    """The blocking/voice finally drained only the message QUEUE, so a background result
    parked during a voice turn waited for some LATER streaming turn to complete."""
    from akana_server import chat_injections
    from akana_server.api.routes.chat._base import guard_nonstreaming_turn

    @guard_nonstreaming_turn(lambda kw: kw.get("conv_id"))
    async def _handler(*, request: Any, conv_id: str) -> str:
        return "spoken reply"

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            chat_injections._enqueue(
                app.state.settings.data_dir, meta.id, "job finished: 7", "schedule", "Job"
            )
            await _handler(request=_make_request(app), conv_id=meta.id)
            for _ in range(60):
                await asyncio.sleep(0.05)
                texts = [m.content for m in svc.list_messages(meta.id, limit=20)]
                if "job finished: 7" in texts:
                    return
            raise AssertionError(f"parked result not drained after the turn: {texts}")

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# The resume probe must not report "idle" while the server is busy
# --------------------------------------------------------------------------- #


def test_get_chat_active_reports_a_running_nonstreaming_turn(env) -> None:
    """After F5 during a voice/blocking turn the client asked /chat/active and got 204
    ("idle"), then had its next message silently queued behind the invisible turn."""

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            meta = app.state.conversation_service.create()
            req = _make_request(app)
            running = asyncio.Event()
            finish = asyncio.Event()

            async def blocking_turn() -> None:
                handle = register_turn(app, meta.id)
                running.set()
                try:
                    await finish.wait()
                finally:
                    release_turn(app, meta.id, handle)

            task = asyncio.create_task(blocking_turn())
            await running.wait()
            resp = await chat_routes.get_chat_active(meta.id, req)
            assert resp.status_code == 202, resp.status_code
            finish.set()
            await task
            await asyncio.sleep(0.05)
            resp2 = await chat_routes.get_chat_active(meta.id, req)
            assert resp2.status_code == 204

    asyncio.run(main())


def test_get_chat_active_reports_a_running_background_job(env) -> None:
    """A schedule/background_run job has no follower buffer, but it IS running: the
    reload probe must say so (with started_at) instead of showing an idle chat."""
    from akana_server.background_activity import (
        clear_background_active,
        mark_background_active,
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            meta = app.state.conversation_service.create()
            req = _make_request(app)
            mark_background_active(app, meta.id)
            resp = await chat_routes.get_chat_active(meta.id, req)
            assert resp.status_code == 202, resp.status_code
            import json

            payload = json.loads(bytes(resp.body).decode("utf-8"))
            assert payload["running"] is True and payload["kind"] == "background"
            assert payload["started_at"] > 0
            # The independent flag the client keys its marker rebuild off (the ``kind``
            # priority alone hides a background job behind a nonstreaming turn).
            assert payload["background"] is True
            assert payload["background_started_at"] == payload["started_at"]
            clear_background_active(app, meta.id)
            resp2 = await chat_routes.get_chat_active(meta.id, req)
            assert resp2.status_code == 204

    asyncio.run(main())


def test_get_chat_active_shows_background_work_behind_a_nonstreaming_turn(env) -> None:
    """The 202 answer probes in a FIXED priority and reports exactly one ``kind``. With a
    Telegram/blocking/voice turn live at the same time the background job was invisible:
    after an F5 (which wipes the client's marker map — the whole reason the registry
    exists) the client read kind="nonstreaming", took neither branch, and never rebuilt
    the working strip for a job that ran for minutes more."""
    import json

    from akana_server.background_activity import (
        clear_background_active,
        mark_background_active,
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            meta = app.state.conversation_service.create()
            req = _make_request(app)
            running = asyncio.Event()
            finish = asyncio.Event()

            async def blocking_turn() -> None:
                handle = register_turn(app, meta.id)
                running.set()
                try:
                    await finish.wait()
                finally:
                    release_turn(app, meta.id, handle)

            mark_background_active(app, meta.id)
            task = asyncio.create_task(blocking_turn())
            await running.wait()
            resp = await chat_routes.get_chat_active(meta.id, req)
            assert resp.status_code == 202, resp.status_code
            payload = json.loads(bytes(resp.body).decode("utf-8"))
            # ``kind`` stays the followability hint — the client depends on it…
            assert payload["kind"] == "nonstreaming", payload
            # …but the background job must be reported independently of it.
            assert payload["background"] is True, payload
            assert payload["background_started_at"] > 0, payload
            finish.set()
            await task
            clear_background_active(app, meta.id)
            await asyncio.sleep(0.05)
            assert (await chat_routes.get_chat_active(meta.id, req)).status_code == 204

    asyncio.run(main())


def test_overlapping_background_jobs_are_one_continuous_working_period(env) -> None:
    """``mark`` is a ``setdefault`` (deliberately: two overlapping jobs in one
    conversation are ONE working period to the user) but ``clear`` was an unconditional
    ``pop`` — so the first job to finish erased the shared record while the second ran on
    for minutes, and every reconcile after that read "idle" and retired its indicator."""
    from akana_server.background_activity import (
        background_started_at,
        clear_background_active,
        mark_background_active,
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            meta = app.state.conversation_service.create()
            req = _make_request(app)
            mark_background_active(app, meta.id)
            first = background_started_at(app, meta.id)
            await asyncio.sleep(0.02)
            mark_background_active(app, meta.id)  # a second job joins
            clear_background_active(app, meta.id)  # …and the FIRST one finishes
            assert background_started_at(app, meta.id) == first, (
                "the surviving job's elapsed clock must stay continuous"
            )
            assert (await chat_routes.get_chat_active(meta.id, req)).status_code == 202
            clear_background_active(app, meta.id)  # the second one finishes
            assert background_started_at(app, meta.id) is None
            assert (await chat_routes.get_chat_active(meta.id, req)).status_code == 204
            # An unpaired clear must not go negative and wedge the registry busy.
            clear_background_active(app, meta.id)
            mark_background_active(app, meta.id)
            assert background_started_at(app, meta.id) is not None
            clear_background_active(app, meta.id)
            assert background_started_at(app, meta.id) is None

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# Queue integrity around reset / shutdown
# --------------------------------------------------------------------------- #


def test_reset_during_the_drain_gate_does_not_resurrect_the_queue(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drain suspended on its gates must not requeue its popped item when the slot was
    taken by a RESET (which cleared the queue on purpose) rather than by a STOP."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _slow_gates(request: Any, body: Any):
        entered.set()
        await release.wait()
        return SimpleNamespace(
            response=None,
            body=body,
            intent="chat",
            approval_required=False,
            skill_plan=None,
            image_block="",
        )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            meta = app.state.conversation_service.create()
            enqueue_message(app, meta.id, {"text": "eski mesaj"})
            monkeypatch.setattr(chat_routes, "_run_turn_gates", _slow_gates)
            drain = asyncio.create_task(cd._maybe_drain_queue(app, meta.id))
            await asyncio.wait_for(entered.wait(), timeout=5)

            # The user clears the chat while the drain sits in its gates.
            await chat_routes.cleanup_conversation_chat_state(
                app, meta.id, tombstone=False
            )
            assert queue_depth(app, meta.id) == 0
            release.set()
            await asyncio.wait_for(drain, timeout=5)
            assert queue_depth(app, meta.id) == 0, "the cleared message came back"
            # …and it must not have been started either.
            assert chat_routes._active_turns(app).get(meta.id) is None

    asyncio.run(main())


def test_shutdown_surfaces_queued_messages_it_drops(env) -> None:
    """The client was told 202 "queued"; the in-memory queue does not survive a restart,
    so the message must at least leave an error marker instead of vanishing."""

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            enqueue_message(app, meta.id, {"text": "bekleyen mesaj"})
            await cd.shutdown_active_turns(app)
            roles = [(m.role, m.content) for m in svc.list_messages(meta.id, limit=20)]
            assert ("user", "bekleyen mesaj") in roles, roles
            assert any(r == "error" for r, _ in roles), roles
            assert queue_depth(app, meta.id) == 0

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# Live voice (Gemini Live / OpenAI Realtime)
# --------------------------------------------------------------------------- #


def test_live_voice_turn_announces_turn_completed() -> None:
    """The bridges announced spoken turns only via `chat_done`, an event with ZERO
    frontend consumers — so a whole live-voice exchange stayed invisible in the open
    chat pane until F5. The chat log refreshes on turn_completed."""
    from akana_server.voice.realtime_base import RealtimeBridge

    events: list[dict[str, Any]] = []
    app = _gate_app(events)
    bridge = RealtimeBridge(
        websocket=None, settings=SimpleNamespace(), app=app, conv_id="convL"
    )

    asyncio.run(bridge._broadcast_done("merhaba", 120, assistant_turn_id="t7"))

    completed = [e for e in events if e["type"] == "turn_completed"]
    assert len(completed) == 1, events
    assert completed[0]["conversation_id"] == "convL"
    assert completed[0]["status"] == "ok"
    # The user is speaking this turn themselves → no background indicator, no notification.
    assert completed[0]["source"] == "user"
    assert completed[0]["assistant_turn_id"] == "t7"
    # chat_done is kept for the voice/aurora surfaces that already read it.
    assert any(e["type"] == "chat_done" for e in events), events
