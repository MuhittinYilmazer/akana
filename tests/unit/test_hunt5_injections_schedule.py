"""Hunt 5 / F2 — durability of background results (injection inbox + schedule engine).

The theme: a parked background result must survive a transient failure, a crash, a
deleted target conversation and a STOP; and "the conversation is busy" must mean
EVERY kind of running turn, not only the streaming ones.

Locked here:
  * ``conversation_busy`` sees blocking/voice/connector turns (``_nonstreaming_busy``),
    so an injection parks instead of interleaving itself into a mid-turn log;
  * the post-turn drain is gated on a RUNNING turn only — a queued user message must
    not keep the result out of the history that very turn is about to read;
  * the drain is deliver-THEN-remove, and a transient read/persist failure keeps the
    item parked instead of conflating it with "the conversation was deleted";
  * a background FAILURE report broadcasts ``status="error"`` so the notifier's own
    failure gate can fire;
  * a same-chat delivery whose origin chat is gone falls back to a fresh thread whose
    id is written back, and every announced turn gets exactly ONE completion.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from akana_server.chat_injections import (
    _load,
    conversation_busy,
    deliver_or_queue,
    drain_pending,
)
from akana_server.conversation_service import ConversationService
from akana_server.events import EventHub
from akana_server.orchestrator import llm_dispatch, memory_tools
from akana_server.schedule import engine
from akana_server.schedule.model import Delivery
from akana_server.schedule.store import TR_TZ, ScheduleStore, to_iso

T0 = datetime(2026, 7, 20, 9, 0, tzinfo=TR_TZ)


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


class _LiveTask:
    """Stand-in for the request task recorded in ``_nonstreaming_busy``."""

    def done(self) -> bool:
        return False


class _FakeConversations:
    def __init__(self) -> None:
        self.created: list[tuple[str, str | None]] = []
        self._store: dict[str, SimpleNamespace] = {}

    def create(self, *, title=None):
        cid = f"new-conv-{len(self.created) + 1}"
        self.created.append((cid, title))
        obj = SimpleNamespace(id=cid)
        self._store[cid] = obj
        return obj

    def get(self, cid):
        return self._store.get(cid)


def _stub_llm(monkeypatch, fn):
    monkeypatch.setattr(llm_dispatch, "complete_chat_aggregated", fn)
    monkeypatch.setattr(memory_tools, "mcp_servers_payload", lambda *a, **k: None)


# --------------------------------------------------------------------------- #
# silent-drops-1 / event-contracts-5 — busy must mean EVERY running turn
# --------------------------------------------------------------------------- #


def test_nonstreaming_turn_makes_the_conversation_busy(tmp_path):
    """A voice/blocking/connector turn registers ONLY in ``_nonstreaming_busy``; the
    injection must park behind it exactly like behind a streaming turn."""
    app = _app()
    settings = _settings(tmp_path)
    conv = _make_conv(tmp_path)
    app.state.nonstreaming_busy = {conv: _LiveTask()}

    assert conversation_busy(app, conv) is True
    out = asyncio.run(deliver_or_queue(app, settings, conv, "reminder body", title="R"))
    assert out == "queued"
    assert _turns(tmp_path, conv) == []  # nothing written mid-turn
    assert not app.state.event_hub.sent  # and no mid-turn log-reload broadcast


def test_finished_nonstreaming_turn_does_not_block_delivery(tmp_path):
    app = _app()
    settings = _settings(tmp_path)
    conv = _make_conv(tmp_path)

    class _Done(_LiveTask):
        def done(self) -> bool:
            return True

    app.state.nonstreaming_busy = {conv: _Done()}
    assert conversation_busy(app, conv) is False
    assert asyncio.run(deliver_or_queue(app, settings, conv, "body")) == "delivered"


# --------------------------------------------------------------------------- #
# server-turn-lifecycle-3 — the drain must beat the next QUEUED user turn
# --------------------------------------------------------------------------- #


def test_drain_lands_before_a_queued_user_message(tmp_path):
    """The post-turn hook drains injections BEFORE starting the queued follow-up, so
    that turn's history already contains the result the user is asking about. A queued
    message must therefore not count as 'busy' for the drain."""
    from collections import deque

    app = _app()
    settings = _settings(tmp_path)
    conv = _make_conv(tmp_path)
    app.state.active_turns[conv] = object()
    assert asyncio.run(deliver_or_queue(app, settings, conv, "job result")) == "queued"

    # The turn ended, but the user's follow-up is waiting in the queue.
    del app.state.active_turns[conv]
    app.state.chat_turn_queues = {conv: deque([object()])}
    assert conversation_busy(app, conv) is True  # still busy for the PARKING decision

    assert asyncio.run(drain_pending(app, settings, conv)) == 1
    assert any("job result" in t.content for t in _turns(tmp_path, conv))


# --------------------------------------------------------------------------- #
# silent-drops-4 / background-lifecycle-7 — deliver-then-remove
# --------------------------------------------------------------------------- #


def test_transient_read_failure_keeps_the_item_parked(tmp_path, monkeypatch):
    app = _app()
    settings = _settings(tmp_path)
    conv = _make_conv(tmp_path)
    app.state.active_turns[conv] = object()
    asyncio.run(deliver_or_queue(app, settings, conv, "promised result"))
    del app.state.active_turns[conv]

    boom = {"n": 0}
    real_get = ConversationService.get

    def flaky_get(self, cid):
        boom["n"] += 1
        raise RuntimeError("database is locked")

    monkeypatch.setattr(ConversationService, "get", flaky_get)
    assert asyncio.run(drain_pending(app, settings, conv)) == 0
    assert boom["n"] >= 1
    # STILL PARKED: a transient read error is not "the conversation was deleted".
    assert [i["text"] for i in _load(tmp_path)["pending"][conv]] == ["promised result"]

    monkeypatch.setattr(ConversationService, "get", real_get)
    assert asyncio.run(drain_pending(app, settings, conv)) == 1
    assert any("promised result" in t.content for t in _turns(tmp_path, conv))


def test_persist_failure_keeps_the_item_parked(tmp_path, monkeypatch):
    from akana_server.orchestrator import turn_writer

    app = _app()
    settings = _settings(tmp_path)
    conv = _make_conv(tmp_path)
    app.state.active_turns[conv] = object()
    asyncio.run(deliver_or_queue(app, settings, conv, "durable result"))
    del app.state.active_turns[conv]

    def boom(**kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(turn_writer, "persist_assistant_turn", boom)
    assert asyncio.run(drain_pending(app, settings, conv)) == 0
    assert [i["text"] for i in _load(tmp_path)["pending"][conv]] == ["durable result"]


def test_deleted_conversation_drops_the_item_for_good(tmp_path):
    """The other half of the same predicate: a genuinely deleted conversation must
    not park the item forever (an unbounded retry loop)."""
    app = _app()
    settings = _settings(tmp_path)
    conv = _make_conv(tmp_path)
    app.state.active_turns[conv] = object()
    asyncio.run(deliver_or_queue(app, settings, conv, "orphan"))
    del app.state.active_turns[conv]
    ConversationService(Path(tmp_path)).soft_delete(conv)

    assert asyncio.run(drain_pending(app, settings, conv)) == 0
    assert _load(tmp_path)["pending"].get(conv) in (None, [])


def test_transient_failure_on_the_direct_path_parks_instead_of_dropping(
    tmp_path, monkeypatch
):
    app = _app()
    settings = _settings(tmp_path)
    conv = _make_conv(tmp_path)

    def flaky_get(self, cid):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(ConversationService, "get", flaky_get)
    assert asyncio.run(deliver_or_queue(app, settings, conv, "keep me")) == "queued"
    assert [i["text"] for i in _load(tmp_path)["pending"][conv]] == ["keep me"]


# --------------------------------------------------------------------------- #
# dead-machinery-4 / event-contracts-4 — a failed job must not broadcast "ok"
# --------------------------------------------------------------------------- #


def test_failure_injection_broadcasts_status_error(tmp_path):
    app = _app()
    settings = _settings(tmp_path)
    conv = _make_conv(tmp_path)
    out = asyncio.run(
        deliver_or_queue(app, settings, conv, "⚠️ could not complete", status="error")
    )
    assert out == "delivered"
    [evt] = [e for e in app.state.event_hub.sent if e["type"] == "turn_completed"]
    assert evt["status"] == "error"
    assert evt["source"] == "background"


def test_parked_failure_keeps_its_status_through_the_drain(tmp_path):
    app = _app()
    settings = _settings(tmp_path)
    conv = _make_conv(tmp_path)
    app.state.active_turns[conv] = object()
    asyncio.run(deliver_or_queue(app, settings, conv, "⚠️ failed", status="error"))
    del app.state.active_turns[conv]
    assert asyncio.run(drain_pending(app, settings, conv)) == 1
    [evt] = [e for e in app.state.event_hub.sent if e["type"] == "turn_completed"]
    assert evt["status"] == "error"


def test_engine_failure_report_is_announced_as_an_error(tmp_path, monkeypatch):
    """The engine's '⚠️ … could not complete' body reached the notifier as status
    'ok', so a failed job popped 'Background work finished'.

    ONE completion, not merely 'every completion says error': the failure report path
    and the engine's own settle both want to answer this turn_active, and a double emit
    clears the indicator for whatever turn is running by then."""

    async def boom(settings, prompt, **kw):
        raise RuntimeError("provider exploded")

    _stub_llm(monkeypatch, boom)
    settings = _settings(tmp_path)
    conv = _make_conv(tmp_path)
    app = _app()
    store = ScheduleStore(tmp_path)
    store.create(
        title="Rapor",
        prompt="do it",
        kind="once",
        when=to_iso(T0),
        delivery=Delivery(mode="thread", conversation_id=conv, same_chat=True),
        now=T0,
    )
    asyncio.run(engine.run_due_schedules(settings, conversations=None, now=T0, app=app))
    active = [e for e in app.state.event_hub.sent if e["type"] == "turn_active"]
    completed = [e for e in app.state.event_hub.sent if e["type"] == "turn_completed"]
    assert len(active) == 1, app.state.event_hub.sent
    assert len(completed) == 1, app.state.event_hub.sent
    assert completed[0]["status"] == "error", completed
    assert completed[0]["conversation_id"] == conv


# --------------------------------------------------------------------------- #
# background-lifecycle-3 — a deleted origin chat must not eat the result
# --------------------------------------------------------------------------- #


def test_same_chat_delivery_to_a_deleted_chat_falls_back_to_a_thread(
    tmp_path, monkeypatch
):
    async def ok(settings, prompt, **kw):
        return ("the briefing", {}, None)

    _stub_llm(monkeypatch, ok)
    appended: list[tuple] = []
    monkeypatch.setattr(
        engine,
        "_append_turn_pair",
        lambda dd, cid, prompt, result: appended.append((cid, prompt, result)),
    )
    settings = _settings(tmp_path)
    app = _app()
    convs = _FakeConversations()
    store = ScheduleStore(tmp_path)
    item = store.create(
        title="Briefing",
        prompt="brief me",
        kind="daily",
        when="08:00",
        delivery=Delivery(mode="thread", conversation_id="gone-conv", same_chat=True),
        now=T0,
    )
    asyncio.run(
        engine.run_schedule_now(settings, item.id, conversations=convs, now=T0, app=app)
    )
    # The result landed somewhere the user can actually see it…
    assert appended and appended[0][2] == "the briefing"
    new_cid = appended[0][0]
    # …and the schedule now points at that thread (no re-drop tomorrow).
    assert store.get(item.id).delivery.conversation_id == new_cid
    # The announced turn on the ORIGINAL conversation is released, honestly.
    released = [
        e
        for e in app.state.event_hub.sent
        if e["type"] == "turn_completed" and e["conversation_id"] == "gone-conv"
    ]
    assert len(released) == 1 and released[0]["status"] == "error", app.state.event_hub.sent


# --------------------------------------------------------------------------- #
# background-lifecycle-5 — the recreated thread id must be written back
# --------------------------------------------------------------------------- #


def test_mark_ran_overwrites_a_stale_conversation_id(tmp_path):
    store = ScheduleStore(tmp_path)
    item = store.create(
        title="t",
        prompt="p",
        kind="daily",
        when="08:00",
        delivery=Delivery(mode="thread", conversation_id="old-conv"),
        now=T0,
    )
    store.mark_ran(item.id, status="ok", conversation_id="fresh-conv", now=T0)
    assert store.get(item.id).delivery.conversation_id == "fresh-conv"


def test_recurring_thread_schedule_stops_spawning_new_threads(tmp_path, monkeypatch):
    """After the delivery thread is deleted ONCE, every later fire kept creating a
    brand-new one-message conversation because the fresh id was never persisted."""

    async def ok(settings, prompt, **kw):
        return ("daily text", {}, None)

    _stub_llm(monkeypatch, ok)
    monkeypatch.setattr(engine, "_append_turn_pair", lambda *a: None)
    settings = _settings(tmp_path)
    convs = _FakeConversations()
    store = ScheduleStore(tmp_path)
    item = store.create(
        title="Daily",
        prompt="p",
        kind="daily",
        when="08:00",
        delivery=Delivery(mode="thread", conversation_id="deleted-conv"),
        now=T0,
    )
    asyncio.run(engine.run_schedule_now(settings, item.id, conversations=convs, now=T0))
    first = store.get(item.id).delivery.conversation_id
    assert first in convs._store
    asyncio.run(
        engine.run_schedule_now(settings, item.id, conversations=convs, now=T0)
    )
    assert len(convs.created) == 1, convs.created  # reused, not respawned
    assert store.get(item.id).delivery.conversation_id == first


# --------------------------------------------------------------------------- #
# event-contracts-2 — background work has a server-side footprint
# --------------------------------------------------------------------------- #


def test_engine_run_is_visible_in_the_background_activity_registry(
    tmp_path, monkeypatch
):
    """The one-shot turn_active event was the ONLY footprint of a running engine
    turn, so an F5 / reconnect / second tab saw 'nothing is running'."""
    from akana_server import background_activity as bga

    settings = _settings(tmp_path)
    conv = _make_conv(tmp_path)
    app = _app()
    seen: list[float | None] = []

    async def ok(settings_, prompt, **kw):
        seen.append(bga.background_started_at(app, conv))
        return ("done", {}, None)

    _stub_llm(monkeypatch, ok)
    store = ScheduleStore(tmp_path)
    store.create(
        title="Job",
        prompt="p",
        kind="once",
        when=to_iso(T0),
        delivery=Delivery(mode="thread", conversation_id=conv, same_chat=True),
        now=T0,
    )
    asyncio.run(engine.run_due_schedules(settings, conversations=None, now=T0, app=app))
    assert seen and seen[0] is not None  # visible WHILE the job runs
    assert bga.background_started_at(app, conv) is None  # released when it ends


def test_cancelled_engine_run_still_releases_the_working_strip(tmp_path, monkeypatch):
    """Rule 1 of the turn-lifecycle contract: a cancelled turn emits turn_completed
    with status 'cancelled' — 'cancelled emits nothing' latches consumers forever."""
    from akana_server import background_activity as bga

    async def cancelled(settings, prompt, **kw):
        raise asyncio.CancelledError()

    _stub_llm(monkeypatch, cancelled)
    settings = _settings(tmp_path)
    conv = _make_conv(tmp_path)
    app = _app()
    store = ScheduleStore(tmp_path)
    item = store.create(
        title="Job",
        prompt="p",
        kind="once",
        when=to_iso(T0),
        delivery=Delivery(mode="thread", conversation_id=conv, same_chat=True),
        now=T0,
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            engine.run_schedule_now(settings, item.id, conversations=None, app=app)
        )
    completed = [e for e in app.state.event_hub.sent if e["type"] == "turn_completed"]
    assert [e["status"] for e in completed] == ["cancelled"], app.state.event_hub.sent
    assert bga.background_started_at(app, conv) is None
