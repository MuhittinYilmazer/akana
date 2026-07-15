"""Live-UI turn-event broadcasts from background producers.

The schedule engine creates conversations + appends turns outside the chat SSE
flow; without a WS broadcast the web UI never learns a new thread appeared (the
reported bug: reminder chat doesn't show until refresh). These tests prove the
producers emit ``turn_active`` /
``turn_completed`` on the EventHub, and that the helpers are safe with no hub.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from akana_server.conversation_events import (
    broadcast_turn_active,
    broadcast_turn_completed,
    event_hub,
)
from akana_server.events import EventHub


class _RecordingHub(EventHub):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[dict] = []

    async def broadcast_json(self, data):  # type: ignore[override]
        self.sent.append(data)


def _app_with_hub(hub):
    return SimpleNamespace(state=SimpleNamespace(event_hub=hub))


# -- helper safety ---------------------------------------------------------------


def test_event_hub_none_when_absent():
    assert event_hub(SimpleNamespace(state=SimpleNamespace())) is None
    assert event_hub(None) is None


def test_broadcast_is_noop_without_hub():
    # No hub / no conversation id → silent no-op, never raises.
    asyncio.run(broadcast_turn_active(_app_with_hub(None), "c1"))
    asyncio.run(broadcast_turn_completed(_app_with_hub(_RecordingHub()), ""))


def test_broadcast_active_and_completed_shapes():
    hub = _RecordingHub()
    app = _app_with_hub(hub)
    asyncio.run(broadcast_turn_active(app, "c1"))
    asyncio.run(broadcast_turn_completed(app, "c1", status="ok", assistant_turn_id="t9"))
    assert hub.sent[0] == {"type": "turn_active", "conversation_id": "c1"}
    assert hub.sent[1] == {
        "type": "turn_completed",
        "conversation_id": "c1",
        "status": "ok",
        "assistant_turn_id": "t9",
    }


# -- schedule engine emits --------------------------------------------------------


def test_schedule_engine_broadcasts_completed(tmp_path, monkeypatch):
    from akana_server.schedule import engine
    from akana_server.schedule.model import Delivery
    from akana_server.schedule.store import ScheduleStore, to_iso

    async def fake(settings, prompt, **kw):
        return ("hi", {"tool_calls": []}, None)

    monkeypatch.setattr(engine.llm_dispatch, "complete_chat_aggregated", fake)
    monkeypatch.setattr(engine, "_mcp_servers", lambda *a, **k: None)
    monkeypatch.setattr(engine, "_append_turn_pair", lambda *a: None)

    class _Convs:
        def create(self, *, title=None):
            return SimpleNamespace(id="sched-conv-1")

        def get(self, cid):
            return None

    hub = _RecordingHub()
    store = ScheduleStore(tmp_path)
    from datetime import datetime

    from akana_server.schedule.store import TR_TZ

    t0 = datetime(2026, 7, 12, 10, 0, tzinfo=TR_TZ)
    store.create(
        title="Reminder", prompt="p", kind="once", when=to_iso(t0),
        delivery=Delivery(mode="thread"), now=t0,
    )
    asyncio.run(
        engine.run_due_schedules(
            SimpleNamespace(data_dir=tmp_path),
            conversations=_Convs(),
            now=t0,
            app=_app_with_hub(hub),
        )
    )
    completed = [e for e in hub.sent if e["type"] == "turn_completed"]
    assert completed and completed[0]["conversation_id"] == "sched-conv-1"
    assert completed[0]["status"] == "ok"
