"""The phantom turn-id, connector + schedule side: a receipt nobody checked.

Both writers on this surface used to treat the freshly minted ULID as proof of a
row. ``turn_writer`` now returns "" when nothing landed, and these tests pin what
the USER gets when ``memory.db`` refuses the write the way production refuses it —
``remember_turn`` raises, the writer retries, and the row is genuinely absent:

  * a Telegram turn is announced ``status="error"`` with NO assistant_turn_id, so
    the web pane does not reload a log that has nothing in it;
  * a scheduled run whose result was not stored is NOT recorded "ok", NOT bound to
    the empty thread it created, and NOT toasted as ready;
  * neither writer ever leaves an ORPHAN assistant row — an answer with no question
    becomes the next turn's LLM history and the model contradicts itself.

The failure is injected at the STORE, never by stubbing the writer's return value:
stubbing skips the retry/verify path that decides what the receipt says.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from akana_server.connectors.base import InboundMessage, OutboundMessage
from akana_server.connectors.registry import ConnectorRegistry
from akana_server.connectors.router import InboundRouter
from akana_server.connectors.service import _make_turn_guard
from akana_server.conversation_service import ConversationService
from akana_server.events import EventHub
from akana_server.orchestrator import memory_tools, turn_writer
from akana_server.schedule import engine
from akana_server.schedule.model import Delivery
from akana_server.schedule.store import TR_TZ, ScheduleStore, to_iso
from akana_server.skills.turn_injection import SkillTurnPlan

T0 = datetime(2026, 7, 11, 10, 0, tzinfo=TR_TZ)


@pytest.fixture(autouse=True)
def _no_llm_titles(monkeypatch) -> None:
    """The chat titler fires a REAL provider call per new conversation."""
    monkeypatch.setenv("AKANA_LLM_CHAT_TITLES", "0")


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #


def _break_writes(monkeypatch, tmp_path, *roles: str):
    """Make ``memory.db`` refuse the given roles exactly as production does.

    ``remember_turn`` raises → the writer burns its retries → the row is really
    not there afterwards, which is what every assertion below reads back. The
    backoff is zeroed so three attempts do not cost a third of a second."""
    from akana_server.memory_core import get_memory_core

    monkeypatch.setattr(turn_writer, "_PERSIST_BACKOFF_S", 0.0)
    mem = get_memory_core(Path(tmp_path))
    real = mem.remember_turn
    blocked = set(roles) or {"user", "assistant", "error"}

    def refuse(**kw):
        if kw.get("role") in blocked:
            raise sqlite3.OperationalError("database is locked")
        return real(**kw)

    monkeypatch.setattr(mem, "remember_turn", refuse)
    return mem


class _RecordingHub(EventHub):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[dict] = []

    async def broadcast_json(self, data):  # type: ignore[override]
        self.sent.append(data)


class _FakeConnector:
    connector_id = "fake"
    max_message_len = 0

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def start(self, inbound) -> None:  # pragma: no cover - unused
        pass

    async def stop(self) -> None:  # pragma: no cover - unused
        pass

    async def send(self, message: OutboundMessage) -> None:
        self.sent.append(message)

    def status(self) -> dict:
        return {"id": self.connector_id, "running": True}


class _FakeRegistry:
    """Enough of ConnectorRegistry for the schedule's connector delivery."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def get(self, channel):
        return self if channel == "telegram" else None

    async def send_to(self, channel, chat_id, text) -> None:
        self.sent.append(text)


async def _no_skills(settings, text: str) -> SkillTurnPlan:
    return SkillTurnPlan()


def _settings(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(
        data_dir=Path(tmp_path),
        telegram_enabled=True,
        telegram_bot_token="tok",
        telegram_allowed_chat_ids=("42",),
    )


def _app(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(
            event_hub=_RecordingHub(),
            settings=_settings(tmp_path),
            conversation_service=ConversationService(Path(tmp_path)),
            active_turns={},
        )
    )


def _router(tmp_path, complete, app) -> tuple[InboundRouter, _FakeConnector]:
    """PRODUCTION wiring: the real turn guard from ``connectors.service``."""
    connector = _FakeConnector()
    reg = ConnectorRegistry()
    reg.register(connector)
    return (
        InboundRouter(
            _settings(tmp_path),
            reg,
            complete=complete,
            conversations=ConversationService(Path(tmp_path)),
            skill_planner=_no_skills,
            turn_guard=_make_turn_guard(app),
            app=app,
        ),
        connector,
    )


def _frames(app, kind: str) -> list[dict]:
    return [e for e in app.state.event_hub.sent if e.get("type") == kind]


def _rows(tmp_path, conv_id) -> list[tuple[str, str]]:
    svc = ConversationService(Path(tmp_path))
    return [(m.role, m.content) for m in svc.list_messages(conv_id)]


def _only_conv(tmp_path) -> str:
    convs = ConversationService(Path(tmp_path)).list_conversations()
    assert len(convs) == 1, convs
    return convs[0].id


async def _reply(settings, text: str, **kw) -> str:
    return f"reply: {text}"


# --------------------------------------------------------------------------- #
# B1 — the connector guard, end to end
# --------------------------------------------------------------------------- #


def test_connector_turn_that_never_reached_the_store_is_announced_as_error(
    tmp_path, monkeypatch
) -> None:
    """The whole Telegram exchange is lost: the user is answered on their phone, but
    nothing is in memory.db. Announcing "ok" with a minted id made the web pane
    reload the log and find neither the question nor the reply."""
    app = _app(tmp_path)
    router, connector = _router(tmp_path, _reply, app)
    _break_writes(monkeypatch, tmp_path)

    async def main() -> str:
        out = await router.handle(
            InboundMessage(connector_id="fake", chat_id="42", text="merhaba")
        )
        await asyncio.sleep(0.1)  # let the guard's announcements land
        return out

    text = asyncio.run(main())

    # The user is still answered — a storage failure must not eat the reply.
    assert text == "reply: merhaba"
    assert [m.text for m in connector.sent] == ["reply: merhaba"]

    completed = _frames(app, "turn_completed")
    assert len(completed) == 1, app.state.event_hub.sent
    assert completed[0]["status"] == "error"
    assert "assistant_turn_id" not in completed[0]  # no id for a row that is not there
    assert _rows(tmp_path, _only_conv(tmp_path)) == []


def test_connector_reply_is_not_archived_without_its_question(
    tmp_path, monkeypatch
) -> None:
    """X1: the user row is lost, the assistant row would still have gone in. That
    orphan is fed to the NEXT channel turn as history, so the model answers a
    message it cannot see."""
    app = _app(tmp_path)
    router, _connector = _router(tmp_path, _reply, app)
    _break_writes(monkeypatch, tmp_path, "user")

    async def main() -> None:
        await router.handle(
            InboundMessage(connector_id="fake", chat_id="42", text="merhaba")
        )
        await asyncio.sleep(0.1)

    asyncio.run(main())

    conv = _only_conv(tmp_path)
    assert _rows(tmp_path, conv) == [], "an answer with no question must not be stored"
    completed = _frames(app, "turn_completed")
    assert len(completed) == 1
    assert completed[0]["status"] == "error"
    assert "assistant_turn_id" not in completed[0]


def test_connector_half_written_pair_is_not_ok(tmp_path, monkeypatch) -> None:
    """The question landed, the answer did not — the pair is atomic in meaning, so
    the turn is an error even though half of it is on disk."""
    app = _app(tmp_path)
    router, _connector = _router(tmp_path, _reply, app)
    _break_writes(monkeypatch, tmp_path, "assistant")

    async def main() -> None:
        await router.handle(
            InboundMessage(connector_id="fake", chat_id="42", text="merhaba")
        )
        await asyncio.sleep(0.1)

    asyncio.run(main())

    conv = _only_conv(tmp_path)
    assert _rows(tmp_path, conv) == [("user", "merhaba")]
    completed = _frames(app, "turn_completed")
    assert len(completed) == 1
    assert completed[0]["status"] == "error"
    assert "assistant_turn_id" not in completed[0]


def test_connector_turn_that_landed_still_reports_ok_with_its_id(
    tmp_path, monkeypatch
) -> None:
    """The receipt only reports losses: an untouched store still yields one "ok"
    completion carrying the id the frontend reloads."""
    app = _app(tmp_path)
    router, _connector = _router(tmp_path, _reply, app)

    async def main() -> None:
        await router.handle(
            InboundMessage(connector_id="fake", chat_id="42", text="merhaba")
        )
        await asyncio.sleep(0.1)

    asyncio.run(main())

    conv = _only_conv(tmp_path)
    rows = _rows(tmp_path, conv)
    assert rows == [("user", "merhaba"), ("assistant", "reply: merhaba")]
    completed = _frames(app, "turn_completed")
    assert len(completed) == 1
    assert completed[0]["status"] == "ok"
    msgs = ConversationService(Path(tmp_path)).list_messages(conv)
    assert completed[0]["assistant_turn_id"] == msgs[-1].id


# --------------------------------------------------------------------------- #
# B2 — the schedule engine: no "ran ok" for a result nobody has
# --------------------------------------------------------------------------- #


def _stub_llm(monkeypatch) -> None:
    monkeypatch.setattr(memory_tools, "mcp_servers_payload", lambda *a, **k: None)


async def _briefing(settings, prompt, **kw):
    return ("Good morning summary", {"tool_calls": []}, None)


def _schedule(tmp_path, *, mode: str = "thread") -> tuple[ScheduleStore, str]:
    store = ScheduleStore(tmp_path)
    item = store.create(
        title="Morning",
        prompt="brief me",
        kind="once",
        when=to_iso(T0),
        delivery=Delivery(mode=mode, channel="telegram", chat_id="42"),
        now=T0,
    )
    return store, item.id


def _fire(tmp_path, app, *, registry=None) -> int:
    return asyncio.run(
        engine.run_due_schedules(
            SimpleNamespace(data_dir=Path(tmp_path)),
            conversations=ConversationService(Path(tmp_path)),
            registry=registry,
            now=T0,
            complete=_briefing,
            app=app,
        )
    )


def test_scheduled_result_that_was_not_stored_is_not_recorded_ok(
    tmp_path, monkeypatch
) -> None:
    """The toast said the briefing was ready and mark_ran rolled the schedule
    forward — clicking through opened an empty conversation and the run was never
    retried. Nothing but a log line revealed the loss."""
    _stub_llm(monkeypatch)
    store, sid = _schedule(tmp_path)
    app = _app(tmp_path)
    _break_writes(monkeypatch, tmp_path)

    assert _fire(tmp_path, app) == 1

    got = store.get(sid)
    assert got.last_run["status"] == "skipped"
    assert "not stored" in (got.last_run.get("error") or "")
    # NOT rehomed: the schedule is not bound to a thread that holds nothing.
    assert got.delivery.conversation_id is None
    assert got.last_run.get("conversation_id") is None
    # No "your result is ready" for a result the user cannot open.
    assert _frames(app, "turn_completed") == []
    assert _rows(tmp_path, _only_conv(tmp_path)) == []


def test_scheduled_run_does_not_leave_an_answer_without_its_prompt(
    tmp_path, monkeypatch
) -> None:
    """X1: the prompt row is lost. Writing the result anyway leaves a thread whose
    only content is an answer — the next run reads it back as history."""
    _stub_llm(monkeypatch)
    store, sid = _schedule(tmp_path)
    app = _app(tmp_path)
    _break_writes(monkeypatch, tmp_path, "user")

    assert _fire(tmp_path, app) == 1

    assert _rows(tmp_path, _only_conv(tmp_path)) == []
    assert store.get(sid).last_run["status"] == "skipped"


def test_scheduled_half_written_pair_is_not_ok(tmp_path, monkeypatch) -> None:
    """The prompt landed, the result did not: the run delivered nothing, so it is
    not "ok" and the thread is not bound for the next fire."""
    _stub_llm(monkeypatch)
    store, sid = _schedule(tmp_path)
    app = _app(tmp_path)
    _break_writes(monkeypatch, tmp_path, "assistant")

    assert _fire(tmp_path, app) == 1

    conv = _only_conv(tmp_path)
    assert _rows(tmp_path, conv) == [("user", "brief me")]
    got = store.get(sid)
    assert got.last_run["status"] == "skipped"
    assert got.delivery.conversation_id is None
    assert _frames(app, "turn_completed") == []


def test_connector_delivery_still_counts_when_the_thread_write_is_lost(
    tmp_path, monkeypatch
) -> None:
    """"both" mode: the Telegram copy DID arrive, so the run is partial — not "ok"
    (the archive is empty) and not "skipped" (the user got their briefing)."""
    _stub_llm(monkeypatch)
    store, sid = _schedule(tmp_path, mode="both")
    app = _app(tmp_path)
    registry = _FakeRegistry()
    _break_writes(monkeypatch, tmp_path)

    assert _fire(tmp_path, app, registry=registry) == 1

    assert registry.sent == ["Good morning summary"]
    got = store.get(sid)
    assert got.last_run["status"] == "partial"
    assert "not stored" in (got.last_run.get("error") or "")


def test_scheduled_run_that_landed_is_ok_and_binds_its_thread(
    tmp_path, monkeypatch
) -> None:
    """Happy path through the REAL writer: both rows readable, "ok" recorded, the
    thread bound for the next fire, and the ready-toast broadcast."""
    _stub_llm(monkeypatch)
    store, sid = _schedule(tmp_path)
    app = _app(tmp_path)

    assert _fire(tmp_path, app) == 1

    conv = _only_conv(tmp_path)
    assert _rows(tmp_path, conv) == [
        ("user", "brief me"),
        ("assistant", "Good morning summary"),
    ]
    got = store.get(sid)
    assert got.last_run["status"] == "ok"
    assert got.delivery.conversation_id == conv
    completed = _frames(app, "turn_completed")
    assert len(completed) == 1 and completed[0]["status"] == "ok"
