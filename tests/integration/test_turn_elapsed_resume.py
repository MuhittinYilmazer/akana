"""The elapsed-time strip must survive a reconnect (F5) — the turn's REAL age.

Live bug: refreshing the page while a turn was running restarted the "working… · 0:42"
strip at 0:00, so a turn that had been thinking for minutes read as brand new. The client
that reconnects has no memory of the turn, so the SERVER has to say when it started: the
resume endpoint (GET /chat/active/{id}) reports it in ``X-Akana-Turn-Started`` (epoch ms)
and the frontend seeds its clock from that (akana-turn-status.begin(convId, startedAtMs)).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import FastAPI, Request

from akana_server.api.app import create_app
from akana_server.api.routes import chat as chat_routes
from akana_server.api.routes.chat.chat_state import _ActiveTurn


def _request(app: FastAPI, path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "query_string": b"",
            "app": app,
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8766),
            "scheme": "http",
            "root_path": "",
        }
    )


def test_active_turn_records_when_it_started():
    """Every running turn carries its own start stamp (nothing else can reconstruct it —
    the buffer has no timestamps)."""
    before = time.time()
    turn = _ActiveTurn(conversation_id="c1")
    assert before <= turn.started_at <= time.time()


def test_resume_reports_the_turns_real_start_so_the_clock_survives_f5():
    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            # a turn that has been running for a while (what F5 lands in the middle of)
            turn = _ActiveTurn(conversation_id=meta.id)
            turn.started_at = time.time() - 137.0
            turn.chunks.append(b"data: {}\n\n")
            app.state.active_turns = {meta.id: turn}

            resp = await chat_routes.get_chat_active(
                meta.id, _request(app, f"/api/v1/chat/active/{meta.id}")
            )
            header = resp.headers.get("X-Akana-Turn-Started")
            assert header, "the resume response must say when the turn started"
            started_ms = int(header)
            age_s = (time.time() * 1000 - started_ms) / 1000
            # ~137s, NOT ~0 — the reconnecting client must not restart the clock
            assert 130 < age_s < 200, f"expected the real turn age, got {age_s:.0f}s"
            # release the follower so the lifespan can shut down cleanly
            body = getattr(resp, "body_iterator", None)
            if body is not None:
                await body.aclose()

    asyncio.run(main())


def test_no_active_turn_still_answers_204_without_the_header():
    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            app.state.active_turns = {}
            resp: Any = await chat_routes.get_chat_active(
                meta.id, _request(app, f"/api/v1/chat/active/{meta.id}")
            )
            assert resp.status_code == 204
            assert "X-Akana-Turn-Started" not in resp.headers

    asyncio.run(main())
