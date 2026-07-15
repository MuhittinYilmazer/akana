"""Same-chat background delivery — the busy-safe injection inbox.

Covers the user-visible contract:
* a task/schedule result created FROM a conversation lands back IN that
  conversation as an assistant message (no separate thread),
* if the user's own turn is streaming there, the message parks durably and
  drains right after the turn ends — BEFORE the next queued user message,
* agent-resume providers get a context note on the next turn (popped once).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from akana_server.chat_injections import (
    _load,
    conversation_busy,
    deliver_or_queue,
    drain_all_pending,
    drain_pending,
    pop_context_notes,
)
from akana_server.conversation_service import ConversationService
from akana_server.events import EventHub


class _RecordingHub(EventHub):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[dict] = []

    async def broadcast_json(self, data):  # type: ignore[override]
        self.sent.append(data)


def _app(hub=None):
    return SimpleNamespace(
        state=SimpleNamespace(event_hub=hub or _RecordingHub(), active_turns={})
    )


def _settings(tmp_path):
    return SimpleNamespace(data_dir=Path(tmp_path))


def _make_conv(tmp_path, title="Chat"):
    return ConversationService(Path(tmp_path)).create(title=title).id


def _turns(tmp_path, conv_id):
    return ConversationService(Path(tmp_path)).list_messages(conv_id)


# -- free path: immediate delivery -------------------------------------------------


def test_free_conversation_gets_immediate_injection(tmp_path):
    app = _app()
    settings = _settings(tmp_path)
    conv = _make_conv(tmp_path)
    out = asyncio.run(
        deliver_or_queue(app, settings, conv, "✅ Görev bitti: sonuç", kind="task", title="T")
    )
    assert out == "delivered"
    texts = [t.content for t in _turns(tmp_path, conv)]
    assert any("Görev bitti" in x for x in texts)
    # LIVE UI event fired with the conversation id
    hub = app.state.event_hub
    assert any(
        e["type"] == "turn_completed" and e["conversation_id"] == conv for e in hub.sent
    )
    # context note recorded for the next turn (agent-resume bridge)
    notes = pop_context_notes(settings, conv)
    assert notes and "Görev bitti" in notes[0]
    assert pop_context_notes(settings, conv) == []  # popped exactly once


def test_unknown_conversation_drops(tmp_path):
    out = asyncio.run(
        deliver_or_queue(_app(), _settings(tmp_path), "no-such-conv", "text")
    )
    assert out == "dropped"


# -- busy path: park + drain --------------------------------------------------------


def test_busy_conversation_parks_then_drains_in_order(tmp_path):
    app = _app()
    settings = _settings(tmp_path)
    conv = _make_conv(tmp_path)
    app.state.active_turns[conv] = object()  # a live turn is streaming
    assert conversation_busy(app, conv) is True

    r1 = asyncio.run(deliver_or_queue(app, settings, conv, "first result", title="A"))
    r2 = asyncio.run(deliver_or_queue(app, settings, conv, "second result", title="B"))
    assert (r1, r2) == ("queued", "queued")
    assert _turns(tmp_path, conv) == []  # nothing written mid-turn
    # durable: the inbox file holds both, surviving a restart
    stored = _load(tmp_path)["pending"][conv]
    assert [i["text"] for i in stored] == ["first result", "second result"]

    # the turn ends → drain delivers both, in arrival order
    del app.state.active_turns[conv]
    delivered = asyncio.run(drain_pending(app, settings, conv))
    assert delivered == 2
    texts = [t.content for t in _turns(tmp_path, conv)]
    assert texts.index("first result") < texts.index("second result")
    assert _load(tmp_path)["pending"].get(conv) is None  # inbox emptied


def test_drain_stops_when_conversation_becomes_busy_again(tmp_path):
    app = _app()
    settings = _settings(tmp_path)
    conv = _make_conv(tmp_path)
    app.state.active_turns[conv] = object()
    asyncio.run(deliver_or_queue(app, settings, conv, "r1"))
    asyncio.run(deliver_or_queue(app, settings, conv, "r2"))
    del app.state.active_turns[conv]

    # After the first delivery, a NEW user turn starts (busy again) → the rest waits.
    orig_busy = conversation_busy
    calls = {"n": 0}

    def busy_after_one(app_, conv_):
        calls["n"] += 1
        return calls["n"] > 1  # free for the first check, busy afterwards

    import akana_server.chat_injections as ci

    ci_busy = ci.conversation_busy
    try:
        ci.conversation_busy = busy_after_one
        delivered = asyncio.run(drain_pending(app, settings, conv))
    finally:
        ci.conversation_busy = ci_busy
    assert delivered == 1
    assert len(_load(tmp_path)["pending"][conv]) == 1  # r2 still parked
    _ = orig_busy  # readability


def test_startup_sweep_delivers_leftovers(tmp_path):
    app = _app()
    settings = _settings(tmp_path)
    conv = _make_conv(tmp_path)
    app.state.active_turns[conv] = object()
    asyncio.run(deliver_or_queue(app, settings, conv, "leftover"))
    # simulate restart: fresh app (no active turns)
    app2 = _app()
    delivered = asyncio.run(drain_all_pending(app2, settings))
    assert delivered == 1
    assert any("leftover" in t.content for t in _turns(tmp_path, conv))


# -- post-turn hook ordering ---------------------------------------------------------


def test_post_turn_hook_drains_injections_before_queue(monkeypatch, tmp_path):
    """The detached-turn finally must deliver parked injections BEFORE draining
    the next queued user message (so that turn sees the result in history)."""
    from akana_server.api.routes.chat import chat_detached

    order: list[str] = []

    async def fake_drain_pending(app, settings, conv):
        order.append("injections")
        return 0

    async def fake_drain_queue(app, conv):
        order.append("queue")

    import akana_server.chat_injections as ci

    monkeypatch.setattr(ci, "drain_pending", fake_drain_pending)
    monkeypatch.setattr(chat_detached, "_maybe_drain_queue", fake_drain_queue)
    app = SimpleNamespace(state=SimpleNamespace(settings=_settings(tmp_path)))
    asyncio.run(chat_detached._drain_injections_then_queue(app, "c1"))
    assert order == ["injections", "queue"]


# -- same-chat schedule path ---------------------------------------------------------


def test_same_chat_schedule_injects_assistant_only(tmp_path, monkeypatch):
    from datetime import datetime

    from akana_server.schedule import engine
    from akana_server.schedule.model import Delivery
    from akana_server.schedule.store import TR_TZ, ScheduleStore, to_iso

    async def fake(settings, prompt, **kw):
        return ("Hatırlatma içeriği", {"tool_calls": []}, None)

    monkeypatch.setattr(engine.llm_dispatch, "complete_chat_aggregated", fake)
    monkeypatch.setattr(engine, "_mcp_servers", lambda *a, **k: None)

    settings = _settings(tmp_path)
    conv = _make_conv(tmp_path, title="Ana sohbet")
    app = _app()
    store = ScheduleStore(tmp_path)
    t0 = datetime(2026, 7, 12, 10, 0, tzinfo=TR_TZ)
    store.create(
        title="test",
        prompt="hatırlat",
        kind="once",
        when=to_iso(t0),
        delivery=Delivery(mode="thread", conversation_id=conv, same_chat=True),
        now=t0,
    )
    fired = asyncio.run(
        engine.run_due_schedules(settings, conversations=None, now=t0, app=app)
    )
    assert fired == 1
    texts = [t.content for t in _turns(tmp_path, conv)]
    # Assistant-only injection with the reminder header — NO user-turn pair.
    assert any("⏰" in x and "Hatırlatma içeriği" in x for x in texts)
    roles = [t.role for t in _turns(tmp_path, conv)]
    assert "user" not in roles


# -- producer note consumption (agent-resume bridge) ---------------------------------


def test_producer_notes_block_renders_and_pops_once(tmp_path):
    from akana_server.api.routes.chat.chat_producer import _injection_notes_block

    app = _app()
    settings = _settings(tmp_path)
    conv = _make_conv(tmp_path)
    asyncio.run(
        deliver_or_queue(app, settings, conv, "Görev sonucu burada", kind="task", title="T")
    )
    block = _injection_notes_block(settings, conv)
    assert "Background updates" in block or "Arka plan" in block
    assert "Görev sonucu burada" in block
    # popped exactly once — the next turn gets a clean prompt
    assert _injection_notes_block(settings, conv) == ""
