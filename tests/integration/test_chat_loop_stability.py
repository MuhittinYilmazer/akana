"""P0 stability regression — the chat hot path MUST NOT BLOCK the event loop.

Live bug: while the response streams, synchronous sqlite/file side effects
(policy.db ``busy_timeout=10000``, episodic persist, audit.jsonl) run on the
event loop; a single lock contention froze the whole server (every endpoint + WS).
Fix: the side effects were moved to a worker thread via ``_off_loop``/``asyncio.to_thread``.

The tests here replace the persist path with a fake that sleeps 1 s and measure
the delay of a heartbeat coroutine on the SAME loop while the chat request runs:
if persist ran on the loop, the heartbeat would stall for ≥1 s.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from akana_server.api.app import create_app, setup_file_logging

#: Sleep duration of the fake persist. If persist wrongly ran ON the loop, the
#: heartbeat freezes for ~this long — so we want it comfortably LARGER than any
#: plausible scheduler jitter, to keep the signal (a real block) well clear of the
#: noise (load-induced gaps). 2.0s gives a 2:1 signal-to-noise ratio against the
#: gap threshold below.
_SLOW_S = 2.0
#: Largest heartbeat gap tolerated. A real synchronous block shows up as ~_SLOW_S
#: (2.0s), so 1.0s still catches it with room to spare, while tolerating up to a
#: full second of scheduler jitter on a loaded/slow CI runner. The earlier 0.5s
#: value flaked under full-suite load on contended Windows CI even though the code
#: correctly offloads persist (proven: the test passes deterministically in
#: isolation — a genuine on-loop block would fail there too, regardless of this
#: threshold).
_MAX_GAP_S = 1.0


@pytest.fixture
def app_env(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8766")
    monkeypatch.setenv("CURSOR_API_KEY", "x")
    return tmp_path


def _slow_persist_user_turn(**kwargs: Any) -> str:
    time.sleep(_SLOW_S)  # mimics a synchronous side effect (slow disk / db lock)
    return kwargs.get("turn_id") or "fake-user-turn"


def _slow_persist_assistant_turn(**kwargs: Any) -> str:
    time.sleep(_SLOW_S)
    return kwargs.get("assistant_turn_id") or "fake-asst-turn"


async def _heartbeat(stop: asyncio.Event, gaps: list[float]) -> None:
    """A 20ms heartbeat on the same loop — if the loop blocks, the gap grows."""
    last = time.perf_counter()
    while not stop.is_set():
        await asyncio.sleep(0.02)
        now = time.perf_counter()
        gaps.append(now - last)
        last = now


async def _run_with_heartbeat(app, do_request) -> tuple[Any, float, float]:
    """Returns (response, request duration, max heartbeat gap)."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30.0
        ) as client:
            stop = asyncio.Event()
            gaps: list[float] = []
            hb = asyncio.create_task(_heartbeat(stop, gaps))
            t0 = time.perf_counter()
            resp = await do_request(client)
            elapsed = time.perf_counter() - t0
            stop.set()
            await hb
    return resp, elapsed, max(gaps) if gaps else 0.0


def test_stream_chat_slow_persist_does_not_block_loop(
    app_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SSE path: the loop must stay responsive while 2×1s fake persists run."""

    async def _mock_stream(*_a: Any, **_k: Any) -> AsyncIterator[dict[str, Any]]:
        yield {"delta": "Merhaba ", "done": False}
        yield {"delta": "dünya", "done": False}
        yield {
            "done": True,
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "tool_calls": []},
            "status": "finished",
            "tool_calls": [],
        }

    monkeypatch.setattr(
        "akana_server.api.routes.chat.stream_user_chat", _mock_stream
    )
    monkeypatch.setattr(
        "akana_server.api.routes.chat.persist_user_turn",
        _slow_persist_user_turn,
    )
    monkeypatch.setattr(
        "akana_server.api.routes.chat.persist_assistant_turn",
        _slow_persist_assistant_turn,
    )

    app = create_app()

    async def _do(client: httpx.AsyncClient):
        return await client.post(
            "/api/v1/chat/stream", json={"text": "merhaba dünya testi"}
        )

    resp, elapsed, max_gap = asyncio.run(_run_with_heartbeat(app, _do))

    assert resp.status_code == 200
    body = resp.text
    assert "Merhaba " in body and "done" in body
    # The fake slow persists actually ran (user + assistant turn).
    assert elapsed >= _SLOW_S, f"fake persist did not run (elapsed={elapsed:.2f}s)"
    # Proof: the loop did not block — if persist ran on the loop, max_gap would be ≥ 1s.
    assert max_gap < _MAX_GAP_S, (
        f"event loop froze for {max_gap:.2f}s — a synchronous side effect is running on the loop"
    )


def test_blocking_chat_slow_persist_does_not_block_loop(
    app_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocking POST /chat path: the same proof."""

    async def _mock_complete(*_a: Any, **_k: Any) -> tuple[str, dict[str, Any]]:
        return "tamamdır", {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "tool_calls": [],
        }

    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage",
        _mock_complete,
    )
    monkeypatch.setattr(
        "akana_server.api.routes.chat.persist_user_turn",
        _slow_persist_user_turn,
    )
    monkeypatch.setattr(
        "akana_server.api.routes.chat.persist_assistant_turn",
        _slow_persist_assistant_turn,
    )

    app = create_app()

    async def _do(client: httpx.AsyncClient):
        return await client.post("/api/v1/chat", json={"text": "merhaba dünya testi"})

    resp, elapsed, max_gap = asyncio.run(_run_with_heartbeat(app, _do))

    assert resp.status_code == 200
    assert resp.json()["text"] == "tamamdır"
    assert elapsed >= _SLOW_S, f"fake persist did not run (elapsed={elapsed:.2f}s)"
    assert max_gap < _MAX_GAP_S, (
        f"event loop froze for {max_gap:.2f}s — a synchronous side effect is running on the loop"
    )


def test_server_file_logging_smoke(tmp_path) -> None:
    """setup_file_logging: file + start marker + log line + idempotency."""
    log_path = setup_file_logging(tmp_path)
    assert log_path == tmp_path / "logs" / "server.log"
    assert log_path.exists()
    assert "akana server log started" in log_path.read_text(encoding="utf-8")

    logging.getLogger("akana_server.smoke").warning("kanarya-0xC0FFEE")
    for h in logging.getLogger().handlers:
        h.flush()
    assert "kanarya-0xC0FFEE" in log_path.read_text(encoding="utf-8")

    # A second call does not attach a second handler to the same file (idempotent).
    assert setup_file_logging(tmp_path) == log_path
    ours = [
        h
        for h in logging.getLogger().handlers
        if getattr(h, "_akana_server_log", False)
    ]
    assert len(ours) == 1
