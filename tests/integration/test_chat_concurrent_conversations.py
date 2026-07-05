"""Concurrency hunt — messaging MULTIPLE conversations AT ONCE (owner's issue #1).

The owner says "there are lots of bugs": writing to 5 chats simultaneously causes
crashes / cross-talk between conversations / TURN_BUSY deadlocks / dropped-corrupted turns.

These tests fire concurrent POST ``/chat/stream`` requests to separate
``conversation_id``s over the REAL ASGI stack (``httpx.ASGITransport`` — unlike
TestClient it does not fully buffer the body, allowing a real concurrent request
flow; see test_chat_loop_stability.py). The LLM stream is faked via a
``routes.chat.stream_user_chat`` monkeypatch, as in the detached-turn tests.

Every test sets ``AKANA_MEMORY_LLM_CAPTURE=0`` (so background real ``claude -p``
processes don't leak) and an isolated ``AKANA_DATA_DIR``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from akana_server.api.app import create_app
from akana_server.api.routes import chat as chat_routes


@pytest.fixture
def app_env(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8766")
    monkeypatch.setenv("CURSOR_API_KEY", "x")
    monkeypatch.setenv("AKANA_MEMORY_LLM_CAPTURE", "0")
    return tmp_path


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
                payload = json.loads("\n".join(data_lines))
            except json.JSONDecodeError:
                payload = {"raw": "\n".join(data_lines)}
            events.append((event_name, payload))
    return events


def _conv_echo_stream():
    """LLM fake: each turn echoes the first user message of ITS OWN conversation.

    cross-talk detector: the response text carries the conversation_id embedded in
    the prompt. ``_stream_chat_response`` calls the LLM as ``stream_user_chat(settings,
    user_for_llm, ..., conversation_id=conv_id, ...)`` → we read conv_id from the argument.
    """

    async def _stream(settings: Any, user_for_llm: str, *_a: Any, **kw: Any) -> AsyncIterator[dict[str, Any]]:
        conv_id = str(kw.get("conversation_id") or "?")
        # Split into several deltas + await between each delta so the turns actually
        # run interleaved — not sequentially.
        marker = f"CONV[{conv_id}]"
        for piece in (marker, "::", "yanıt"):
            yield {"delta": piece, "done": False}
            await asyncio.sleep(0.02)
        yield {
            "done": True,
            "text": f"{marker}::yanıt",
            "usage": {"prompt_tokens": 1, "completion_tokens": 3, "tool_calls": []},
            "status": "finished",
            "tool_calls": [],
        }

    return _stream


async def _stream_collect(client: httpx.AsyncClient, conv_id: str, text: str) -> tuple[int, str]:
    """Read a single /chat/stream request to completion (status, body)."""
    async with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"text": text, "conversation_id": conv_id},
    ) as resp:
        body = ""
        if resp.status_code == 200:
            async for chunk in resp.aiter_text():
                body += chunk
        else:
            body = (await resp.aread()).decode("utf-8", "replace")
        return resp.status_code, body


def test_five_distinct_conversations_no_crosstalk(
    app_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stream to 5 DISTINCT conversations AT ONCE — no response should leak into another.

    SYMPTOM target: cross-talk (A's delta in B's stream) / crash / dropped turn.
    Each conversation's done.text must carry ITS OWN conv_id marker; it must not
    contain another conv_id marker.
    """
    monkeypatch.setattr(chat_routes, "stream_user_chat", _conv_echo_stream())
    app = create_app()

    async def main() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=30.0
            ) as client:
                conv_ids = [f"conv-{i}" for i in range(5)]
                results = await asyncio.gather(
                    *(_stream_collect(client, cid, f"merhaba {cid}") for cid in conv_ids)
                )

        for cid, (status, body) in zip(conv_ids, results):
            assert status == 200, f"{cid}: unexpected status {status}: {body[:200]}"
            events = _parse_sse(body)
            done = [p for n, p in events if n == "done"]
            assert done, f"{cid}: no done event — turn dropped? body={body[:300]}"
            done_text = done[-1].get("text", "")
            # Must carry ITS OWN marker.
            assert f"CONV[{cid}]" in done_text, (
                f"{cid}: did not get its own response (cross-talk?): {done_text!r}"
            )
            # Must carry NO other conv marker.
            for other in conv_ids:
                if other == cid:
                    continue
                assert f"CONV[{other}]" not in done_text, (
                    f"CROSS-TALK: {cid} response contains {other} marker: {done_text!r}"
                )
            # meta.conversation_id must also be correct.
            metas = [p for n, p in events if n == "meta"]
            assert metas and metas[0]["conversation_id"] == cid

    asyncio.run(main())


def test_five_distinct_conversations_all_persist_correctly(
    app_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AFTER 5 concurrent conversations, each one's episodic record must contain ITS OWN pair.

    SYMPTOM target: mixup in the persist layer — A's response is written to B, or a
    turn is lost during concurrent sqlite writes.
    """
    monkeypatch.setattr(chat_routes, "stream_user_chat", _conv_echo_stream())
    app = create_app()

    async def main() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=30.0
            ) as client:
                conv_ids = [f"persist-{i}" for i in range(5)]
                await asyncio.gather(
                    *(_stream_collect(client, cid, f"soru {cid}") for cid in conv_ids)
                )
            svc = app.state.conversation_service
            for cid in conv_ids:
                msgs = svc.list_messages(cid, limit=10)
                pairs = [(m.role, m.content) for m in msgs]
                assert ("user", f"soru {cid}") in pairs, (
                    f"{cid}: user message was not persisted: {pairs}"
                )
                asst = [c for r, c in pairs if r == "assistant"]
                assert asst, f"{cid}: assistant turn was not persisted: {pairs}"
                # Assistant content must carry ITS OWN marker (no persist cross-talk).
                assert f"CONV[{cid}]" in asst[-1], (
                    f"{cid}: wrong assistant content was persisted: {asst[-1]!r}"
                )

    asyncio.run(main())


def test_concurrent_same_conversation_first_streams_rest_enqueue(
    app_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """5 concurrent FIRST messages to the SAME conversation — one 200 stream, the rest 202 queued.

    SYMPTOM target: do concurrent first messages to the same conv_id leak a 500
    (RuntimeError "turn already running") / cause a TURN_BUSY deadlock? The race
    guard (post_chat_stream RuntimeError→202) should catch this.
    """
    monkeypatch.setattr(chat_routes, "stream_user_chat", _conv_echo_stream())
    app = create_app()

    async def main() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=30.0
            ) as client:
                cid = "race-same"
                results = await asyncio.gather(
                    *(_stream_collect(client, cid, f"mesaj-{i}") for i in range(5))
                )
            statuses = sorted(s for s, _ in results)
            # None should be 500 (the race guard converts RuntimeError to 202).
            assert all(s in (200, 202) for s in statuses), (
                f"concurrent first messages leaked a 500: {statuses}"
            )
            # At least one must get a live stream (200); at least one must fall into the queue (202).
            assert 200 in statuses, f"no live stream at all: {statuses}"
            assert 202 in statuses, f"no queue at all (all 200?): {statuses}"

    asyncio.run(main())


def test_concurrent_memory_capture_no_crosstalk(
    app_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """5 concurrent turns WITH memory capture ON — each capture must receive ITS OWN
    conv's (user_text, assistant_text, conversation_id) triple.

    SYMPTOM target: ``_capture_memory_background`` is create_task'd via
    ``_spawn_background`` (context copy). Even though conversation_id is an explicit
    argument, if the 5 concurrent captures pick up each other's text/id, memory gets
    written to the WRONG conversation. (AKANA_MEMORY_LLM_CAPTURE=1 +
    propose_memory_captures mock — no real LLM call.)
    """
    monkeypatch.setenv("AKANA_MEMORY_LLM_CAPTURE", "1")
    monkeypatch.setattr(chat_routes, "stream_user_chat", _conv_echo_stream())

    seen: list[dict[str, Any]] = []
    seen_lock = asyncio.Lock()

    async def _fake_propose(settings: Any, eng: Any, *, user_text: str,
                            assistant_text: str, conversation_id: str | None = None,
                            model: Any = None):
        await asyncio.sleep(0.02)  # widen the race window
        async with seen_lock:
            seen.append(
                {
                    "conversation_id": conversation_id,
                    "user_text": user_text,
                    "assistant_text": assistant_text,
                }
            )
        return []  # no candidate → does not go to staging (no DB side effect)

    monkeypatch.setattr(chat_routes, "propose_memory_captures", _fake_propose)
    app = create_app()

    async def main() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=30.0
            ) as client:
                conv_ids = [f"cap-{i}" for i in range(5)]
                await asyncio.gather(
                    *(_stream_collect(client, cid, f"soru {cid}") for cid in conv_ids)
                )
                # Wait for all 5 background captures to record into ``seen``. The
                # captures are fire-and-forget tasks tracked in
                # ``app.state.chat_background_tasks``. Awaiting that set once races:
                # a capture task may not be registered in it yet at snapshot time, so
                # it gets missed and ``seen`` stays short. Poll on ``seen`` (the ground
                # truth) instead, draining any registered tasks each round, with a
                # generous timeout for a contended CI runner.
                # Breaks early once all 5 have recorded (instant on a fast machine); the
                # large cap is headroom for a heavily-contended CI runner where a
                # fire-and-forget capture task can be starved of loop time for seconds.
                for _ in range(3000):  # up to ~30 s
                    _bg = getattr(app.state, "chat_background_tasks", None)
                    if isinstance(_bg, set):
                        _pending = [t for t in list(_bg) if not t.done()]
                        if _pending:
                            await asyncio.gather(*_pending, return_exceptions=True)
                    async with seen_lock:
                        if len(seen) >= len(conv_ids):
                            break
                    await asyncio.sleep(0.01)

            # Exactly one capture per conv, with the correct triple.
            by_conv = {s["conversation_id"]: s for s in seen}
            for cid in conv_ids:
                assert cid in by_conv, f"{cid}: capture never ran: {[s['conversation_id'] for s in seen]}"
                s = by_conv[cid]
                assert s["user_text"] == f"soru {cid}", (
                    f"CROSS-TALK (capture user_text): {cid} got {s['user_text']!r}"
                )
                assert f"CONV[{cid}]" in s["assistant_text"], (
                    f"CROSS-TALK (capture assistant_text): {cid} got {s['assistant_text']!r}"
                )

    asyncio.run(main())


def test_concurrent_conversations_then_busy_clears(
    app_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AFTER concurrent turns the registry must fully empty — TURN_BUSY must not get stuck.

    SYMPTOM target: even after the turns finish, a zombie lingers in the
    ``_active_turns`` registry, and the next message gets a permanent TURN_BUSY.
    """
    monkeypatch.setattr(chat_routes, "stream_user_chat", _conv_echo_stream())
    app = create_app()

    async def main() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=30.0
            ) as client:
                conv_ids = [f"clear-{i}" for i in range(5)]
                await asyncio.gather(
                    *(_stream_collect(client, cid, "x") for cid in conv_ids)
                )
                # Let the background drain/cleanup complete.
                await asyncio.sleep(0.1)
                # Registry empty — no zombie turns.
                assert chat_routes._active_turns(app) == {}, (
                    f"zombie registry after turns finished: "
                    f"{list(chat_routes._active_turns(app))}"
                )
                # Each conversation is open to a new message (not getting TURN_BUSY).
                results2 = await asyncio.gather(
                    *(_stream_collect(client, cid, "y") for cid in conv_ids)
                )
                for cid, (status, body) in zip(conv_ids, results2):
                    assert status == 200, (
                        f"{cid}: second turn got TURN_BUSY/error: {status} {body[:200]}"
                    )

    asyncio.run(main())
