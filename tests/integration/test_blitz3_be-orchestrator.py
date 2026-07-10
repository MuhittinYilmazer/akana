"""Blitz-3 regression tests — area be-orchestrator.

be-orchestrator-1: the sidebar preview + last_message_at must be derived from the newest
    USER/ASSISTANT turn, never from a failed ``role="error"`` turn (the write path
    deliberately does not bump ``updated_at`` for an error turn, so an unfiltered newest
    turn pins the failure text/timestamp on a stale-sorting row).

be-orchestrator-2: on the blocking /chat route the ``dropped_turns`` counter is recomputed
    AFTER this turn's fresh agent id is persisted (which flips bootstrap_needed to False),
    so a bootstrap turn that truncated history to chat_max_turns reported 0 dropped. The
    response/audit must reconcile against the pre-turn assembled count.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request

from akana_server.api.app import create_app
from akana_server.api.routes import chat as chat_routes
from akana_server.api.routes.chat import ChatRequest
from akana_server.api.services import get_services
from akana_server.conversation_service import ConversationService


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8766")
    monkeypatch.setenv("CURSOR_API_KEY", "x")
    monkeypatch.setenv("AKANA_MEMORY_LLM_CAPTURE", "0")
    monkeypatch.setenv("AKANA_LLM_CHAT_TITLES", "0")
    return tmp_path


def _make_request(app: FastAPI) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/chat",
            "headers": [],
            "query_string": b"",
            "app": app,
            "client": None,
        }
    )


# -- be-orchestrator-1 --------------------------------------------------------


def test_sidebar_preview_ignores_error_turns(tmp_path: Path) -> None:
    """A ``role="error"`` turn is the newest row but must NOT drive the preview.

    Provider outage → persist_error_turn writes a role="error" turn with the friendly
    error text as the newest row (the user turn was persisted first). The write path
    keeps the row's stale sort position (updated_at not bumped); if the read side derived
    the preview from the unfiltered newest turn, the sidebar would show the failure text
    and the failure timestamp as the conversation's latest message.
    """
    svc = ConversationService(tmp_path)
    meta = svc.create()
    cid = meta.id
    ep = svc._episodic
    ep.append_turn(
        turn_id="t0", conversation_id=cid, role="user", text="hello",
        ts="2026-06-02T10:00:00.000Z",
    )
    ep.append_turn(
        turn_id="t1", conversation_id=cid, role="assistant", text="hi there",
        ts="2026-06-02T10:00:01.000Z",
    )
    ep.append_turn(
        turn_id="t2", conversation_id=cid, role="error",
        text="Cursor authentication failed — check your API key in Settings.",
        ts="2026-06-02T10:00:02.000Z",
    )

    rows = svc.list_conversations()
    row = next(r for r in rows if r.id == cid)
    assert row.preview == "hi there", (
        "sidebar preview leaked the failed (role=error) turn as the latest message"
    )
    assert row.last_message_at == "2026-06-02T10:00:01.000Z", (
        "last_message_at was pinned to the failure timestamp"
    )

    # The single-conversation view (get) derives the same fields → same contract.
    single = svc.get(cid)
    assert single is not None
    assert single.preview == "hi there"
    assert single.last_message_at == "2026-06-02T10:00:01.000Z"


def test_sidebar_preview_none_when_only_error_turns(tmp_path: Path) -> None:
    """If EVERY turn is an error (first turn failed), there is no user/assistant preview →
    fall through to the stored json_metadata preview / meta timestamp, not the error text."""
    svc = ConversationService(tmp_path)
    meta = svc.create()
    cid = meta.id
    svc._episodic.append_turn(
        turn_id="e0", conversation_id=cid, role="error",
        text="model is currently unavailable",
        ts="2026-06-02T10:00:00.000Z",
    )
    row = next(r for r in svc.list_conversations() if r.id == cid)
    assert row.preview != "model is currently unavailable"


# -- be-orchestrator-2 --------------------------------------------------------


def _seed_over_window(svc: ConversationService, cid: str, *, pairs: int) -> None:
    """Store ``pairs`` user/assistant exchanges + bump the meta counter so the
    conversation exceeds chat_max_turns (a bootstrap will truncate the history)."""
    ep = svc._episodic
    for i in range(pairs):
        ep.append_turn(
            turn_id=f"u{i:03d}", conversation_id=cid, role="user", text=f"q{i}",
            ts=f"2026-06-02T09:{i:02d}:00.000Z",
        )
        svc._meta_store.on_user_message(cid, f"q{i}")
        ep.append_turn(
            turn_id=f"a{i:03d}", conversation_id=cid, role="assistant", text=f"a{i}",
            ts=f"2026-06-02T09:{i:02d}:30.000Z",
        )
        svc._meta_store.on_assistant_message(cid)


def test_blocking_bootstrap_reports_dropped_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bootstrap turn on the blocking route must report the turns it dropped.

    The conversation exceeds chat_max_turns and has NO stored agent id → the history is
    bootstrapped + truncated (assembled.dropped_turns > 0). The turn returns a fresh
    agent id that persist_agent_id stores; the post-persist recount then sees a resumable
    session and returns 0. The response's dropped_turns must not be silently suppressed.
    """
    monkeypatch.setenv("LLM_PROVIDER", "claude")

    async def _complete_with_agent(*_a: Any, **_k: Any):
        return "cevap", {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "tool_calls": [],
            "agent_id": "sess-block-drop-1",
        }

    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage", _complete_with_agent
    )

    async def main() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            cid = meta.id
            # 20 stored messages > chat_max_turns (12) → bootstrap drops the older 8.
            _seed_over_window(svc, cid, pairs=10)
            assert svc.get(cid).message_count >= 20  # precondition
            # No agent id stored → the leak guard forces a history bootstrap this turn.
            from akana_server import chat_context

            req = _make_request(app)
            assert chat_context.get_agent_id(req, cid) is None

            resp = await chat_routes.post_chat(
                ChatRequest(text="şimdiki soru", conversation_id=cid),
                req,
                services=get_services(req),
            )
            assert resp.text == "cevap"
            # The turn actually bootstrapped + truncated → the response must say so.
            assert resp.dropped_turns > 0, (
                "bootstrap turn reported dropped_turns=0 — the recount ran after the fresh "
                "agent id was persisted and did not reconcile with the assembled count"
            )
            # Sanity: the fresh id WAS persisted (this is what suppressed the naive recount).
            assert chat_context.get_agent_id(req, cid) == "sess-block-drop-1"

    asyncio.run(main())


def _parse_done(chunks: list[bytes]) -> dict[str, Any]:
    """Extract the ``done`` SSE event payload from a list of raw SSE chunks."""
    import json

    joined = b"".join(chunks).decode("utf-8", "replace")
    blocks = joined.split("\n\n")
    for block in blocks:
        if "event: done" in block:
            for line in block.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[len("data:"):].strip())
    raise AssertionError("no done event found in stream")


def test_streaming_bootstrap_retry_reports_dropped_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Streaming resume-lost retry: the done event must report the turns the bootstrap
    dropped.

    First attempt resumes (stored agent id) → dropped_before=0. The agent resume is lost
    (need_history_bootstrap) → the retry clears the id, reloads + truncates history, then
    mints a FRESH agent id. Post-persist the recount sees a resumable session again and
    returns 0, so max(dropped_before=0, dropped_after=0)=0 unless the reloaded retry count
    is folded into dropped_before.
    """
    monkeypatch.setenv("LLM_PROVIDER", "cursor")
    call_n = {"n": 0}

    async def _resume_then_bootstrap(*_args: Any, **_kwargs: Any):
        call_n["n"] += 1
        if call_n["n"] == 1:
            yield {"need_history_bootstrap": True, "done": False}
            return
        # Retry: mint a fresh session id (persisted → suppresses the naive post-persist
        # recount) and stream a normal reply.
        yield {"agent_id": "sess-retry-fresh", "done": False}
        yield {"delta": "yeni ", "done": False}
        yield {
            "done": True,
            "text": "yeni yanıt",
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "tool_calls": []},
            "status": "finished",
            "tool_calls": [],
        }

    monkeypatch.setattr(chat_routes, "stream_user_chat", _resume_then_bootstrap)

    async def main() -> None:
        from akana_server import chat_context

        app = create_app()
        async with app.router.lifespan_context(app):
            svc = app.state.conversation_service
            meta = svc.create()
            cid = meta.id
            _seed_over_window(svc, cid, pairs=10)  # 20 messages > chat_max_turns (12)
            req = _make_request(app)
            # Store a resumable agent id so the FIRST attempt takes the resume path
            # (dropped_before=0), then the resume is lost and the retry bootstraps.
            chat_context.persist_agent_id(req, cid, "sess-old-resume")
            assert chat_context.get_agent_id(req, cid) == "sess-old-resume"

            resp = await chat_routes.post_chat_stream(
                ChatRequest(text="şimdiki soru", conversation_id=cid), req, tts=None
            )
            chunks: list[bytes] = []
            async for chunk in resp.body_iterator:
                chunks.append(chunk)
            turn = chat_routes._active_turns(app).get(cid)
            if turn is not None and turn.task is not None:
                await asyncio.wait_for(turn.task, timeout=10)

            assert call_n["n"] == 2, "expected a bootstrap retry after need_history_bootstrap"
            done = _parse_done(chunks)
            assert done["dropped_turns"] > 0, (
                "streaming bootstrap-retry reported dropped_turns=0 — the reloaded retry "
                "count was discarded and the post-persist recount saw a fresh session"
            )

    asyncio.run(main())


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
