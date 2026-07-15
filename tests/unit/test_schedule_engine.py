"""ScheduleEngine — the due-sweep: LLM turn, delivery, failure isolation.

Hermetic: the LLM is a monkeypatched ``llm_dispatch.complete_chat_aggregated``,
the thread-append is patched (no real ``memory.db``), the MCP payload is stubbed
out, and ``now`` is injected. Covers thread + connector delivery, the egress
filter on connector delivery, one-shot self-disable, recurring roll-forward, and
LLM-error isolation (a failed turn advances the schedule but never crashes the
sweep).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from akana_server.orchestrator import llm_dispatch, memory_tools
from akana_server.schedule import engine
from akana_server.schedule.model import Delivery
from akana_server.schedule.store import TR_TZ, ScheduleStore, to_iso

T0 = datetime(2026, 7, 11, 10, 0, tzinfo=TR_TZ)


def _settings(tmp_path):
    return SimpleNamespace(data_dir=tmp_path)


class _FakeConversations:
    """Records create()/get() so thread delivery can be asserted without memory.db."""

    def __init__(self) -> None:
        self.created: list[tuple[str, str | None]] = []
        self._store: dict[str, SimpleNamespace] = {}

    def create(self, *, title=None):
        cid = f"conv-{len(self.created) + 1}"
        self.created.append((cid, title))
        obj = SimpleNamespace(id=cid)
        self._store[cid] = obj
        return obj

    def get(self, cid):
        return self._store.get(cid)


class _FakeConnector:
    connector_id = "telegram"
    max_message_len = 4096

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def start(self, inbound):  # pragma: no cover - unused in these tests
        pass

    async def stop(self):  # pragma: no cover
        pass

    async def send(self, message):
        self.sent.append(message.text)

    def status(self):  # pragma: no cover
        return {"id": "telegram", "running": True}


def _stub_llm(monkeypatch, fn):
    monkeypatch.setattr(llm_dispatch, "complete_chat_aggregated", fn)
    # Keep the run fully offline — do not build a real MCP payload.
    monkeypatch.setattr(memory_tools, "mcp_servers_payload", lambda *a, **k: None)


# --- thread delivery --------------------------------------------------------


def test_thread_delivery_creates_conversation_and_disables_once(tmp_path, monkeypatch):
    async def fake(settings, prompt, **kw):
        return ("Good morning summary", {"tool_calls": []}, None)

    _stub_llm(monkeypatch, fake)
    appended: list[tuple] = []
    monkeypatch.setattr(
        engine, "_append_turn_pair",
        lambda dd, cid, prompt, result: appended.append((cid, prompt, result)),
    )
    store = ScheduleStore(tmp_path)
    item = store.create(
        title="Morning", prompt="brief me", kind="once",
        when=to_iso(T0), delivery=Delivery(mode="thread"), now=T0,
    )
    convs = _FakeConversations()

    fired = asyncio.run(
        engine.run_due_schedules(_settings(tmp_path), conversations=convs, now=T0)
    )
    assert fired == 1
    assert convs.created[0][1] == "Morning"  # thread titled from the schedule
    assert appended == [("conv-1", "brief me", "Good morning summary")]
    got = store.get(item.id)
    assert got.enabled is False  # once → disabled after firing
    assert got.last_run["status"] == "ok"
    assert got.delivery.conversation_id == "conv-1"  # written back for reuse


def test_empty_result_is_skipped_not_delivered(tmp_path, monkeypatch):
    async def blank(settings, prompt, **kw):
        return ("   ", {}, None)

    _stub_llm(monkeypatch, blank)
    appended: list = []
    monkeypatch.setattr(engine, "_append_turn_pair", lambda *a: appended.append(a))
    store = ScheduleStore(tmp_path)
    item = store.create(title="t", prompt="p", kind="once", when=to_iso(T0), now=T0)
    asyncio.run(
        engine.run_due_schedules(_settings(tmp_path), conversations=_FakeConversations(), now=T0)
    )
    assert appended == []  # nothing delivered
    assert store.get(item.id).last_run["status"] == "skipped"


# --- connector delivery + egress filter -------------------------------------


def test_connector_delivery_applies_egress_filter(tmp_path, monkeypatch):
    async def leaky(settings, prompt, **kw):
        return ("Here is the password: hunter2secretvalue", {}, None)

    _stub_llm(monkeypatch, leaky)
    from akana_server.connectors.registry import ConnectorRegistry

    registry = ConnectorRegistry()  # bare (no settings → audit skipped)
    conn = _FakeConnector()
    registry.register(conn)
    store = ScheduleStore(tmp_path)
    item = store.create(
        title="ping", prompt="p", kind="once", when=to_iso(T0),
        delivery=Delivery(mode="connector", channel="telegram", chat_id="123"),
        now=T0,
    )

    asyncio.run(engine.run_due_schedules(_settings(tmp_path), registry=registry, now=T0))

    assert conn.sent, "connector should have received the message"
    joined = "".join(conn.sent)
    assert "hunter2secretvalue" not in joined  # secret masked on the way out
    assert "[REDACTED]" in joined
    assert store.get(item.id).last_run["status"] == "ok"


def test_connector_disabled_is_skipped_not_crashed(tmp_path, monkeypatch):
    async def fake(settings, prompt, **kw):
        return ("hi", {}, None)

    _stub_llm(monkeypatch, fake)
    from akana_server.connectors.registry import ConnectorRegistry

    registry = ConnectorRegistry()  # telegram NOT registered → not enabled
    store = ScheduleStore(tmp_path)
    item = store.create(
        title="ping", prompt="p", kind="once", when=to_iso(T0),
        delivery=Delivery(mode="connector", channel="telegram", chat_id="123"),
        now=T0,
    )
    asyncio.run(engine.run_due_schedules(_settings(tmp_path), registry=registry, now=T0))
    got = store.get(item.id)
    assert got.last_run["status"] == "skipped"
    assert "not enabled" in got.last_run["error"]


# --- failure isolation ------------------------------------------------------


def test_llm_error_does_not_crash_sweep_once_disabled(tmp_path, monkeypatch):
    async def boom(settings, prompt, **kw):
        raise llm_dispatch.LLMCallError("No LLM provider configured", status_code=503)

    _stub_llm(monkeypatch, boom)
    store = ScheduleStore(tmp_path)
    item = store.create(title="t", prompt="p", kind="once", when=to_iso(T0), now=T0)

    fired = asyncio.run(
        engine.run_due_schedules(_settings(tmp_path), conversations=_FakeConversations(), now=T0)
    )
    assert fired == 1  # sweep completed despite the error
    got = store.get(item.id)
    assert got.last_run["status"] == "error"
    assert "provider" in got.last_run["error"].lower()
    assert got.enabled is False  # once → disabled even on a failed attempt


def test_llm_error_rolls_recurring_forward(tmp_path, monkeypatch):
    async def boom(settings, prompt, **kw):
        raise RuntimeError("transient")

    _stub_llm(monkeypatch, boom)
    store = ScheduleStore(tmp_path)
    item = store.create(title="t", prompt="p", kind="interval", when="3600", now=T0)
    now2 = T0 + timedelta(seconds=3601)  # due

    asyncio.run(engine.run_due_schedules(_settings(tmp_path), now=now2))
    got = store.get(item.id)
    assert got.enabled is True  # recurring survives a failed occurrence
    assert got.last_run["status"] == "error"
    assert got.next_run_at == to_iso(now2 + timedelta(seconds=3600))  # rolled forward


# --- run-now (manual fire) --------------------------------------------------


def test_run_schedule_now_fires_even_when_not_due(tmp_path, monkeypatch):
    async def fake(settings, prompt, **kw):
        return ("done", {}, None)

    _stub_llm(monkeypatch, fake)
    monkeypatch.setattr(engine, "_append_turn_pair", lambda *a: None)
    store = ScheduleStore(tmp_path)
    item = store.create(title="t", prompt="p", kind="interval", when="3600", now=T0)
    assert store.due(T0) == []  # not due yet

    outcome = asyncio.run(
        engine.run_schedule_now(
            _settings(tmp_path), item.id, conversations=_FakeConversations(), now=T0
        )
    )
    assert outcome["status"] == "ok"


def test_run_schedule_now_unknown_id(tmp_path):
    assert asyncio.run(engine.run_schedule_now(_settings(tmp_path), "nope")) is None


def test_run_now_recurring_keeps_next_run_at(tmp_path, monkeypatch):
    """BUG 8: a manual run of a RECURRING schedule fires OUT OF BAND — it must NOT
    roll next_run_at forward and swallow the day's real slot."""
    async def fake(settings, prompt, **kw):
        return ("done", {"tool_calls": []}, None)

    _stub_llm(monkeypatch, fake)
    monkeypatch.setattr(engine, "_append_turn_pair", lambda *a: None)
    store = ScheduleStore(tmp_path)
    item = store.create(title="t", prompt="p", kind="daily", when="18:00", now=T0)
    slot_before = store.get(item.id).next_run_at  # today 18:00

    outcome = asyncio.run(
        engine.run_schedule_now(
            _settings(tmp_path), item.id, conversations=_FakeConversations(), now=T0
        )
    )
    assert outcome["status"] == "ok"
    got = store.get(item.id)
    assert got.enabled is True
    assert got.next_run_at == slot_before  # unchanged — real slot preserved
    assert got.last_run["status"] == "ok"  # but the run WAS recorded


def test_run_now_once_still_self_disables(tmp_path, monkeypatch):
    """BUG 8 corollary: a `once` fired via run-now still self-disables (it ran)."""
    async def fake(settings, prompt, **kw):
        return ("done", {"tool_calls": []}, None)

    _stub_llm(monkeypatch, fake)
    monkeypatch.setattr(engine, "_append_turn_pair", lambda *a: None)
    store = ScheduleStore(tmp_path)
    # A future once (not due yet) — run-now fires it regardless.
    item = store.create(
        title="t", prompt="p", kind="once",
        when=to_iso(T0 + timedelta(days=1)), now=T0,
    )
    asyncio.run(
        engine.run_schedule_now(
            _settings(tmp_path), item.id, conversations=_FakeConversations(), now=T0
        )
    )
    assert store.get(item.id).enabled is False


# --- BUG 9: verbatim message mode (no LLM turn) -----------------------------


def test_message_mode_delivers_verbatim_without_calling_llm(tmp_path, monkeypatch):
    """A schedule with a `message` delivers it AS-IS and runs NO LLM turn — the
    fake complete_chat_aggregated must never be invoked."""
    calls: list = []

    async def fake(settings, prompt, **kw):  # must NOT be called
        calls.append(prompt)
        return ("SHOULD NOT APPEAR", {"tool_calls": []}, None)

    _stub_llm(monkeypatch, fake)
    appended: list[tuple] = []
    monkeypatch.setattr(
        engine, "_append_turn_pair",
        lambda dd, cid, prompt, result: appended.append((cid, prompt, result)),
    )
    store = ScheduleStore(tmp_path)
    item = store.create(
        title="Su", message="Suyu iç", kind="once", when=to_iso(T0),
        delivery=Delivery(mode="thread"), now=T0,
    )
    convs = _FakeConversations()
    fired = asyncio.run(
        engine.run_due_schedules(_settings(tmp_path), conversations=convs, now=T0)
    )
    assert fired == 1
    assert calls == []  # the LLM was never called
    # The verbatim text was delivered as the assistant body.
    assert appended and appended[0][2] == "Suyu iç"
    assert store.get(item.id).last_run["status"] == "ok"


def test_sweep_prunes_old_spent_once(tmp_path, monkeypatch):
    """BUG 4b: run_due_schedules prunes disabled `once` rows older than the window."""
    async def fake(settings, prompt, **kw):  # pragma: no cover - nothing is due
        return ("x", {}, None)

    _stub_llm(monkeypatch, fake)
    store = ScheduleStore(tmp_path)
    old = store.create(title="old", prompt="p", kind="once", when=to_iso(T0), now=T0)
    store.mark_ran(old.id, status="ok", now=T0)  # disabled, ran at T0
    # Sweep 8 days later → the aged-out spent one-shot is pruned.
    asyncio.run(
        engine.run_due_schedules(_settings(tmp_path), now=T0 + timedelta(days=8))
    )
    assert store.get(old.id) is None


def test_interval_rolls_forward_from_completion_not_sweep_start(tmp_path, monkeypatch):
    """Regression: an interval turn that runs LONGER than its own interval must NOT
    leave next_run_at in the past (which would make it due on the very next poll —
    continuous back-to-back firing). In production the engine passes now=None so
    mark_ran re-samples the real completion time. We simulate by firing with
    now=None and a fast fake, then asserting next_run_at is strictly in the future."""
    from akana_server.schedule.store import TR_TZ, parse_iso
    from akana_server.timeutil import iso_now  # noqa: F401 - documents the tz source

    async def fake(settings, prompt, **kw):
        return ("ok", {"tool_calls": []}, None)

    _stub_llm(monkeypatch, fake)
    monkeypatch.setattr(engine, "_append_turn_pair", lambda *a: None)
    store = ScheduleStore(tmp_path)
    # The minimum interval (60s); seed next_run_at in the past so it is due now.
    past = datetime(2020, 1, 1, 0, 0, tzinfo=TR_TZ)
    item = store.create(title="t", prompt="p", kind="interval", when="60", now=past)

    # Production path: now=None → mark_ran anchors on the real completion instant.
    asyncio.run(engine.run_due_schedules(_settings(tmp_path), now=None))

    got = store.get(item.id)
    assert got.enabled is True
    nxt = parse_iso(got.next_run_at)
    now = datetime.now(tz=TR_TZ)
    # next_run must be in the FUTURE (completion + 2s), never the seeded past.
    assert nxt > now


def test_both_mode_partial_when_connector_fails(tmp_path, monkeypatch):
    """mode='both': a thread success must NOT mask a connector failure — the run is
    'partial', not the misleading 'ok' (regression: green while Telegram got nothing)."""
    async def fake(settings, prompt, **kw):
        return ("hello", {"tool_calls": []}, None)

    _stub_llm(monkeypatch, fake)
    monkeypatch.setattr(engine, "_append_turn_pair", lambda *a: None)
    convs = _FakeConversations()
    store = ScheduleStore(tmp_path)
    # channel 'telegram' but no registry/connector enabled → connector delivery fails.
    item = store.create(
        title="briefing", prompt="p", kind="once", when=to_iso(T0),
        delivery=Delivery(mode="both", channel="telegram", chat_id="123"),
        now=T0,
    )
    asyncio.run(
        engine.run_due_schedules(
            _settings(tmp_path), conversations=convs, registry=None, now=T0
        )
    )
    last = store.get(item.id).last_run
    assert last["status"] == "partial"  # thread ok, connector not enabled
    assert "connector" in (last.get("error") or "").lower()
