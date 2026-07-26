"""hunt5 review — the SERVER regressions the parallel fix wave introduced.

Eight agents fixed the "live state" bugs at the same time and two of them fixed the
same symptom incompatibly. What they broke is the turn-lifecycle contract itself::

    turn_active     {type, conversation_id, source}
    turn_completed  {type, conversation_id, status, source, assistant_turn_id?}
    source: "user" (the sender is watching it) | "background" (arrived on its own)
    status: "ok" | "error" | "cancelled" — the REAL outcome

RULE: a turn that announces itself emits EXACTLY ONE completion on EVERY exit path.
Zero latches every consumer on "running" forever; two clears a turn that is still
live. This file locks the paths where the wave produced zero or two:

  * a connector (Telegram) turn — announced by the turn gate, completed by the
    router: one pair, the real status, and the persisted assistant turn id;
  * a background result parked BEHIND a connector turn — the connector guard has to
    drain the injection inbox, not only the user-message queue;
  * ``drain_pending`` under two overlapping drains — deliver-then-remove must not
    duplicate a delivery nor discard the next, still-undelivered item;
  * an "inbox full" park, which is NOT "the origin chat is gone" and must not
    permanently redirect a same-chat schedule into a recovery thread;
  * ``release_turn`` when a STREAMING turn took the conversation over — the old
    turn still owes its completion, and its record must not leak;
  * STOP of a blocking/voice turn, which drains parked injections exactly like the
    streaming STOP path;
  * a parked item dropped as undeliverable — the engine already handed it the debt.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from akana_server.chat_injections import (
    MAX_PENDING_PER_CONV,
    _enqueue,
    _load,
    _turn_running,
    deliver_or_queue,
    drain_pending,
)
from akana_server.connectors.base import InboundMessage, OutboundMessage
from akana_server.connectors.registry import ConnectorRegistry
from akana_server.connectors.router import InboundRouter
from akana_server.connectors.service import _make_turn_guard
from akana_server.conversation_service import ConversationService
from akana_server.events import EventHub
from akana_server.skills.turn_injection import SkillTurnPlan


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


async def _no_skills(settings, text: str) -> SkillTurnPlan:
    return SkillTurnPlan()


def _msg(text: str, *, chat_id: str = "42") -> InboundMessage:
    return InboundMessage(
        connector_id="fake", chat_id=chat_id, text=text, sender_name="Alice"
    )


def _settings(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(
        data_dir=Path(tmp_path),
        telegram_enabled=True,
        telegram_bot_token="tok",
        telegram_allowed_chat_ids=("42",),
    )


def _app(tmp_path, hub: _RecordingHub | None = None) -> SimpleNamespace:
    """An app.state carrier with everything the chat/connector seams read."""
    return SimpleNamespace(
        state=SimpleNamespace(
            event_hub=hub or _RecordingHub(),
            settings=_settings(tmp_path),
            conversation_service=ConversationService(Path(tmp_path)),
            active_turns={},
        )
    )


def _gated_router(tmp_path, complete, app) -> InboundRouter:
    """The PRODUCTION wiring: the real turn guard from ``connectors.service``."""
    reg = ConnectorRegistry()
    reg.register(_FakeConnector())
    return InboundRouter(
        _settings(tmp_path),
        reg,
        complete=complete,
        conversations=ConversationService(Path(tmp_path)),
        skill_planner=_no_skills,
        turn_guard=_make_turn_guard(app),
        app=app,
    )


def _make_conv(tmp_path, title="Chat") -> str:
    return ConversationService(Path(tmp_path)).create(title=title).id


def _texts(tmp_path, conv_id) -> list[str]:
    return [m.content for m in ConversationService(Path(tmp_path)).list_messages(conv_id)]


def _frames(app, kind: str) -> list[dict]:
    return [e for e in app.state.event_hub.sent if e.get("type") == kind]


# --------------------------------------------------------------------------- #
# 1 — a connector turn: ONE turn_active, ONE turn_completed
# --------------------------------------------------------------------------- #


def test_connector_turn_emits_exactly_one_completion_end_to_end(tmp_path) -> None:
    """Two agents taught the connector to announce itself — the turn gate's release AND
    the router's own broadcast — so every Telegram turn cleared its indicator twice, the
    first time BEFORE the reply was even persisted."""

    async def ok(settings, text: str, **kw) -> str:
        return f"reply: {text}"

    app = _app(tmp_path)
    router = _gated_router(tmp_path, ok, app)

    async def main() -> None:
        await router.handle(_msg("merhaba"))
        await asyncio.sleep(0.1)  # let the fire-and-forget announcements land

    asyncio.run(main())

    active = _frames(app, "turn_active")
    completed = _frames(app, "turn_completed")
    assert len(active) == 1, app.state.event_hub.sent
    assert len(completed) == 1, f"one turn, one completion: {app.state.event_hub.sent}"
    conv = ConversationService(Path(tmp_path)).list_conversations()[0]
    assert completed[0]["conversation_id"] == conv.id
    assert completed[0]["status"] == "ok"
    # The user sent this from their own phone — it must not arm the desktop notifier.
    assert completed[0]["source"] == "user"
    # The one completion is the one that can point at the persisted turn.
    msgs = ConversationService(Path(tmp_path)).list_messages(conv.id)
    assert completed[0]["assistant_turn_id"] == msgs[-1].id


# --------------------------------------------------------------------------- #
# 3 — a failed connector turn is not "ok"
# --------------------------------------------------------------------------- #


def test_failed_connector_turn_is_announced_as_an_error(tmp_path) -> None:
    """The guard released with the default status, so a turn that answered "sorry, I
    can't generate a reply" reached the notifier as finished work."""

    async def boom(settings, text: str, **kw) -> str:
        raise RuntimeError("provider exploded")

    app = _app(tmp_path)
    router = _gated_router(tmp_path, boom, app)

    async def main() -> None:
        await router.handle(_msg("merhaba"))
        await asyncio.sleep(0.1)

    asyncio.run(main())

    completed = _frames(app, "turn_completed")
    assert len(completed) == 1, app.state.event_hub.sent
    assert completed[0]["status"] == "error", completed[0]


# --------------------------------------------------------------------------- #
# 2 — a result parked behind a connector turn must not be stranded
# --------------------------------------------------------------------------- #


def test_connector_turn_drains_parked_injections_when_it_ends(tmp_path) -> None:
    """A Telegram-bound chat has no later web/voice turn: the connector guard drained
    only the user-message queue, so a background result parked during its turn (and the
    turn_completed the engine deferred to it) waited for a restart."""

    bound: dict[str, str] = {}
    app = _app(tmp_path)

    async def ok(settings, text: str, **kw) -> str:
        if text == "ikinci":
            # The background job finishes WHILE this connector turn is running, so it
            # parks — only THIS turn's completion can hand it over. Driven through the
            # PRODUCER entry point (deliver_or_queue → the busy predicate), not a direct
            # ``_enqueue``: parking by hand cannot see whether the claim is actually live.
            assert (
                await deliver_or_queue(
                    app,
                    _settings(tmp_path),
                    bound["conv"],
                    "job finished: 42",
                    kind="schedule",
                    title="Job",
                )
                == "queued"
            )
        return f"cevap: {text}"

    router = _gated_router(tmp_path, ok, app)

    async def main() -> str:
        # Bind the chat first so the parked item targets the SAME conversation.
        await router.handle(_msg("selam"))
        conv = ConversationService(Path(tmp_path)).list_conversations()[0].id
        bound["conv"] = conv
        await router.handle(_msg("ikinci"))
        for _ in range(60):
            await asyncio.sleep(0.05)
            # The drain is DELIVER-then-remove (two separate thread hops), so waiting on
            # the text alone races the inbox pop — wait for both halves of the handoff.
            if "job finished: 42" in _texts(tmp_path, conv) and not _load(tmp_path)[
                "pending"
            ].get(conv):
                return conv
        raise AssertionError(
            f"parked result stranded after the connector turn: {_texts(tmp_path, conv)} "
            f"pending={_load(tmp_path)['pending'].get(conv)}"
        )

    conv = asyncio.run(main())
    assert _load(tmp_path)["pending"].get(conv) in (None, [])
    # …and it landed AFTER the turn that unblocked it, not wedged between the user's
    # message and its answer: the drain runs on release, so the turn pair has to be
    # archived INSIDE the claim, not after it.
    assert _texts(tmp_path, conv) == [
        "selam",
        "cevap: selam",
        "ikinci",
        "cevap: ikinci",
        "job finished: 42",
    ]


# --------------------------------------------------------------------------- #
# 4 — overlapping drains must not duplicate a delivery nor discard the next item
# --------------------------------------------------------------------------- #


def test_overlapping_drains_deliver_each_item_exactly_once(tmp_path, monkeypatch) -> None:
    """peek → deliver → remove with no serialisation: both drains peek item X, both
    persist it, and the loser's id-less removal drops the HEAD — deleting the still
    undelivered item Y. The old pop-under-lock could do neither."""
    from akana_server.orchestrator import turn_writer

    app = _app(tmp_path)
    settings = SimpleNamespace(data_dir=Path(tmp_path))
    conv = _make_conv(tmp_path)
    assert _enqueue(tmp_path, conv, "first result", "task", "A") == "queued"
    assert _enqueue(tmp_path, conv, "second result", "task", "B") == "queued"

    real_persist = turn_writer.persist_assistant_turn

    def slow_persist(**kw):
        time.sleep(0.15)  # widen the peek→remove window (runs in a worker thread)
        return real_persist(**kw)

    monkeypatch.setattr(turn_writer, "persist_assistant_turn", slow_persist)

    async def main() -> tuple[int, int]:
        return await asyncio.gather(  # type: ignore[return-value]
            drain_pending(app, settings, conv), drain_pending(app, settings, conv)
        )

    a, b = asyncio.run(main())
    texts = _texts(tmp_path, conv)
    assert texts.count("first result") == 1, texts
    assert texts.count("second result") == 1, texts
    assert a + b == 2, (a, b)
    assert _load(tmp_path)["pending"].get(conv) in (None, [])


# --------------------------------------------------------------------------- #
# 5 — "inbox full" is not "the origin chat is gone"
# --------------------------------------------------------------------------- #


def test_full_inbox_does_not_rebind_the_schedule_to_a_recovery_thread(
    tmp_path, monkeypatch
) -> None:
    """A transient overflow made the engine treat the origin chat as DELETED: it moved
    the result to a fresh thread and mark_ran wrote that id back with same_chat still
    true — so every later fire landed in the recovery thread, forever."""
    from akana_server.orchestrator import llm_dispatch, memory_tools
    from akana_server.schedule import engine
    from akana_server.schedule.model import Delivery
    from akana_server.schedule.store import TR_TZ, ScheduleStore

    from datetime import datetime

    t0 = datetime(2026, 7, 20, 9, 0, tzinfo=TR_TZ)

    async def ok(settings, prompt, **kw):
        return ("the briefing", {}, None)

    monkeypatch.setattr(llm_dispatch, "complete_chat_aggregated", ok)
    monkeypatch.setattr(memory_tools, "mcp_servers_payload", lambda *a, **k: None)

    created: list[str] = []

    class _Convs:
        def create(self, *, title=None):
            cid = f"recovery-{len(created) + 1}"
            created.append(cid)
            return SimpleNamespace(id=cid)

        def get(self, cid):
            return SimpleNamespace(id=cid) if cid in created else None

    monkeypatch.setattr(engine, "_append_turn_pair", lambda *a: None)

    app = _app(tmp_path)
    settings = SimpleNamespace(data_dir=Path(tmp_path))
    conv = _make_conv(tmp_path)
    # The conversation is BUSY (so the result parks) and its inbox is already full.
    app.state.active_turns[conv] = object()
    for i in range(MAX_PENDING_PER_CONV):
        _enqueue(tmp_path, conv, f"old {i}", "task", "old")

    store = ScheduleStore(tmp_path)
    item = store.create(
        title="Briefing",
        prompt="brief me",
        kind="daily",
        when="08:00",
        delivery=Delivery(mode="thread", conversation_id=conv, same_chat=True),
        now=t0,
    )
    asyncio.run(
        engine.run_schedule_now(settings, item.id, conversations=_Convs(), now=t0, app=app)
    )

    assert created == [], "a full inbox is not a deleted chat — no recovery thread"
    assert store.get(item.id).delivery.conversation_id == conv
    assert store.get(item.id).delivery.same_chat is True
    completed = [
        e for e in _frames(app, "turn_completed") if e["conversation_id"] == conv
    ]
    assert len(completed) == 1 and completed[0]["status"] == "error", app.state.event_hub.sent


# --------------------------------------------------------------------------- #
# 6 — release_turn must not drop its completion on a streaming takeover
# --------------------------------------------------------------------------- #


def test_release_announces_when_a_streaming_turn_took_the_conversation_over() -> None:
    """STOP frees the non-streaming registry before the handler's slow finally; a resend
    then starts a STREAMING turn, which never writes an ``_announced`` record. The old
    "someone else is running → they re-announced" shortcut therefore swallowed this
    turn's completion and left its started_at behind for /chat/active to serve."""
    from akana_server.api.routes.chat.chat_state import _ActiveTurn, _active_turns
    from akana_server.api.routes.chat.turn_gate import (
        _announced,
        busy_registry,
        register_turn,
        release_turn,
    )

    events: list[dict] = []

    class _Hub(EventHub):
        async def broadcast_json(self, data):  # type: ignore[override]
            events.append(data)

    app = SimpleNamespace(state=SimpleNamespace(event_hub=_Hub()))

    async def main() -> None:
        handle = register_turn(app, "convX")
        # STOP: the cancel path pops the busy record before our finally runs…
        busy_registry(app).pop("convX", None)
        # …and the user's resend starts a STREAMING turn on the same conversation.
        _active_turns(app)["convX"] = _ActiveTurn(conversation_id="convX")
        release_turn(app, "convX", handle, status="cancelled")
        await asyncio.sleep(0.05)

    asyncio.run(main())
    completed = [e for e in events if e["type"] == "turn_completed"]
    assert len(completed) == 1, events
    assert completed[0]["status"] == "cancelled", completed[0]
    assert "convX" not in _announced(app), "stale started_at left for /chat/active"


def test_release_stays_silent_when_a_later_nonstreaming_turn_overwrote_the_record() -> None:
    """The other half of the same predicate: a genuine non-streaming takeover DID
    re-announce under its own record, so the old turn must not clear the live one."""
    from akana_server.api.routes.chat.turn_gate import (
        busy_registry,
        register_turn,
        release_turn,
    )

    events: list[dict] = []

    class _Hub(EventHub):
        async def broadcast_json(self, data):  # type: ignore[override]
            events.append(data)

    app = SimpleNamespace(state=SimpleNamespace(event_hub=_Hub()))

    async def main() -> None:
        # Separate TASKS: the handle is the running task, so two turns from one task
        # would share a handle and prove nothing.
        old_may_finish = asyncio.Event()
        new_may_finish = asyncio.Event()

        async def old_turn() -> None:
            handle = register_turn(app, "convX")
            try:
                await old_may_finish.wait()
            finally:
                release_turn(app, "convX", handle, status="cancelled")

        async def new_turn() -> None:
            handle = register_turn(app, "convX")
            try:
                await new_may_finish.wait()
            finally:
                release_turn(app, "convX", handle, status="ok")

        old = asyncio.create_task(old_turn())
        await asyncio.sleep(0.02)
        busy_registry(app).pop("convX", None)  # STOP freed the slot
        new = asyncio.create_task(new_turn())  # the takeover announces its own pair
        await asyncio.sleep(0.02)
        old_may_finish.set()
        await old
        await asyncio.sleep(0.05)
        assert [e["type"] for e in events] == ["turn_active", "turn_active"], events
        new_may_finish.set()
        await new
        await asyncio.sleep(0.05)

    asyncio.run(main())
    completed = [e for e in events if e["type"] == "turn_completed"]
    assert [e["status"] for e in completed] == ["ok"], events


# --------------------------------------------------------------------------- #
# 7 — STOP of a blocking/voice turn drains parked injections
# --------------------------------------------------------------------------- #


def test_stop_of_a_blocking_turn_drains_parked_injections(tmp_path) -> None:
    """The streaming STOP path deliberately delivers parked results (they are finished
    work, not a message waiting to run); the blocking/voice guard skipped every drain on
    cancel, so the promised result waited for some later turn."""
    from akana_server.api.routes.chat._base import guard_nonstreaming_turn

    app = _app(tmp_path)
    conv = _make_conv(tmp_path)
    assert _enqueue(tmp_path, conv, "job finished: 7", "schedule", "Job") == "queued"

    @guard_nonstreaming_turn(lambda kw: kw.get("conv_id"))
    async def _handler(*, request, conv_id: str) -> str:
        raise asyncio.CancelledError()

    async def main() -> None:
        with pytest.raises(asyncio.CancelledError):
            await _handler(request=SimpleNamespace(app=app), conv_id=conv)
        for _ in range(60):
            await asyncio.sleep(0.05)
            if "job finished: 7" in _texts(tmp_path, conv):
                return
        raise AssertionError(
            f"parked result stranded by STOP: {_texts(tmp_path, conv)}"
        )

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 10 — a parked item dropped as undeliverable still owes a completion
# --------------------------------------------------------------------------- #


def test_dropping_an_undeliverable_parked_item_releases_the_turn(tmp_path) -> None:
    """The engine hands the delivery path its ``turn_completed`` debt the moment the
    item is parked ("queued"). If the drain later finds the conversation gone it removes
    the item silently — nobody ever answered that turn_active."""
    app = _app(tmp_path)
    settings = SimpleNamespace(data_dir=Path(tmp_path))
    conv = _make_conv(tmp_path)
    assert _enqueue(tmp_path, conv, "orphan result", "schedule", "Job") == "queued"
    ConversationService(Path(tmp_path)).soft_delete(conv)

    assert asyncio.run(drain_pending(app, settings, conv)) == 0
    assert _load(tmp_path)["pending"].get(conv) in (None, [])
    completed = _frames(app, "turn_completed")
    assert len(completed) == 1, app.state.event_hub.sent
    assert completed[0]["conversation_id"] == conv
    assert completed[0]["status"] == "error", completed[0]


# =========================================================================== #
# ROUND 2 — what the adversarial review found still broken in the fixes above
# =========================================================================== #


# --------------------------------------------------------------------------- #
# S1 — the degraded "processing anyway" path never claimed, so it owes NOTHING
# --------------------------------------------------------------------------- #


def test_degraded_connector_guard_announces_and_completes_nothing(
    tmp_path, monkeypatch
) -> None:
    """After ``_GUARD_MAX_WAIT_S`` the guard gives up on the claim and processes the
    message anyway. ``register_turn`` therefore never ran its announce block — ZERO
    turn_active — but the router set ``ann.conv`` unconditionally and broadcast an
    UNPAIRED turn_completed into a conversation whose OTHER turn is still live."""
    from akana_server.api.routes.chat.chat_state import _ActiveTurn, _active_turns
    from akana_server.connectors import service as conn_service

    async def ok(settings, text: str, **kw) -> str:
        return f"cevap: {text}"

    app = _app(tmp_path)
    router = _gated_router(tmp_path, ok, app)

    async def main() -> str:
        await router.handle(_msg("selam"))  # binds the chat to a conversation
        conv = ConversationService(Path(tmp_path)).list_conversations()[0].id
        # A web turn is STREAMING in the same conversation and never ends…
        live = _ActiveTurn(conversation_id=conv)
        _active_turns(app)[conv] = live
        monkeypatch.setattr(conn_service, "_GUARD_MAX_WAIT_S", 0.0)
        app.state.event_hub.sent.clear()
        await router.handle(_msg("ikinci"))
        await asyncio.sleep(0.1)
        assert not live.done, "the live streaming turn must still be running"
        return conv

    conv = asyncio.run(main())
    assert _frames(app, "turn_active") == []
    assert _frames(app, "turn_completed") == [], (
        "a turn that announced NOTHING must complete nothing — this completion clears "
        f"the live streaming turn's indicator: {app.state.event_hub.sent}"
    )
    assert conv


# --------------------------------------------------------------------------- #
# S2 — the claim must outlive the child LLM task, all the way through the persist
# --------------------------------------------------------------------------- #


def test_connector_claim_stays_live_until_the_turn_pair_is_persisted(
    tmp_path, monkeypatch
) -> None:
    """``_gate_swap_handle`` re-points the claim at the per-TURN child task, and every
    busy predicate is ``not busy.done()`` — so the moment ``await turn_task`` returns the
    conversation reports IDLE while the egress filter and BOTH persists still run inside
    the claim. A background result completing in that window took the fast ``_inject_now``
    path and wedged itself between the user's message and its answer.

    Drives the PRODUCTION predicate (``chat_injections._turn_running``) and the real
    ``deliver_or_queue`` entry point — calling ``_enqueue`` directly cannot see this."""
    from akana_server.orchestrator import turn_writer

    settings = _settings(tmp_path)
    app = _app(tmp_path)
    state: dict[str, str] = {}
    busy_seen: list[bool] = []
    reached = threading.Event()
    proceed = threading.Event()
    real_persist_user = turn_writer.persist_user_turn

    def spy_persist_user(**kw):
        # Inside the claim, after the child task finished: this is the window.
        busy_seen.append(_turn_running(app, state["conv"]))
        reached.set()
        proceed.wait(10)
        return real_persist_user(**kw)

    async def ok(settings_, text: str, **kw) -> str:
        return f"cevap: {text}"

    router = _gated_router(tmp_path, ok, app)

    async def main() -> str:
        await router.handle(_msg("selam"))
        conv = ConversationService(Path(tmp_path)).list_conversations()[0].id
        state["conv"] = conv
        monkeypatch.setattr(turn_writer, "persist_user_turn", spy_persist_user)
        turn = asyncio.create_task(router.handle(_msg("ikinci")))
        await asyncio.to_thread(reached.wait, 10)
        # A background job finishes RIGHT HERE — the real producer entry point.
        outcome = await deliver_or_queue(
            app, settings, conv, "job finished: 42", kind="schedule", title="Job"
        )
        proceed.set()
        await turn
        for _ in range(60):
            await asyncio.sleep(0.05)
            if "job finished: 42" in _texts(tmp_path, conv):
                break
        assert outcome == "queued", f"the conversation was still busy: {outcome}"
        return conv

    conv = asyncio.run(main())
    assert busy_seen == [True], "the claim went dead the instant the child task finished"
    assert _texts(tmp_path, conv) == [
        "selam",
        "cevap: selam",
        "ikinci",
        "cevap: ikinci",
        "job finished: 42",
    ]


def test_stop_of_a_connector_turn_kills_only_the_turn(tmp_path) -> None:
    """The claim is now held by a sentinel that outlives the child LLM task, so what an
    external STOP cancels is that sentinel — it must still reach only the per-TURN child
    (cancelling the long-lived conversation WORKER zombies the chat), free the
    conversation, retire the announce record, and answer with "cancelled"."""
    from akana_server.api.routes.chat.chat_state import _cancel_nonstreaming_turn
    from akana_server.api.routes.chat.turn_gate import _announced, is_turn_busy

    entered = asyncio.Event()
    never = asyncio.Event()

    async def blocking(settings, text: str, **kw) -> str:
        if text == "bekle":
            entered.set()
            await never.wait()
        return f"cevap: {text}"

    app = _app(tmp_path)
    router = _gated_router(tmp_path, blocking, app)

    async def main() -> str:
        await router.handle(_msg("selam"))
        conv = ConversationService(Path(tmp_path)).list_conversations()[0].id
        app.state.event_hub.sent.clear()
        turn = asyncio.create_task(router.handle(_msg("bekle")))
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert is_turn_busy(app, conv) is True
        assert await _cancel_nonstreaming_turn(app, conv) is True
        await asyncio.wait_for(turn, timeout=2)  # the caller survived the STOP
        await asyncio.sleep(0.1)
        assert is_turn_busy(app, conv) is False
        assert conv not in _announced(app), "stale started_at left for /chat/active"
        # The conversation is genuinely free again: the next message claims it.
        app.state.event_hub.sent.clear()
        await router.handle(_msg("ucuncu"))
        await asyncio.sleep(0.1)
        return conv

    conv = asyncio.run(main())
    assert len(_frames(app, "turn_active")) == 1, app.state.event_hub.sent
    completed = _frames(app, "turn_completed")
    assert len(completed) == 1 and completed[0]["status"] == "ok", app.state.event_hub.sent
    assert _texts(tmp_path, conv) == ["selam", "cevap: selam", "ucuncu", "cevap: ucuncu"]


# --------------------------------------------------------------------------- #
# S3 — a persist that silently wrote NOTHING must not consume the parked item
# --------------------------------------------------------------------------- #


def test_drain_keeps_the_item_when_the_persist_wrote_nothing(
    tmp_path, monkeypatch
) -> None:
    """``turn_writer`` catches its own db failure, log.errors and returns — while
    ``persist_assistant_turn`` hands back the ULID it MINTED anyway. So a truthy turn id
    is not proof of a row: the drain announced "your response is ready" with the id of a
    message that does not exist and then deleted the durable inbox copy forever."""
    from akana_server.orchestrator import turn_writer

    monkeypatch.setattr(turn_writer, "_persist_turn", lambda **kw: None)

    app = _app(tmp_path)
    settings = SimpleNamespace(data_dir=Path(tmp_path))
    conv = _make_conv(tmp_path)
    assert _enqueue(tmp_path, conv, "job finished: 42", "schedule", "Job") == "queued"

    assert asyncio.run(drain_pending(app, settings, conv)) == 0
    assert _texts(tmp_path, conv) == []
    pending = _load(tmp_path)["pending"].get(conv) or []
    assert [i["text"] for i in pending] == ["job finished: 42"], (
        "the item left the durable inbox on a write that landed nothing"
    )
    assert _frames(app, "turn_completed") == [], app.state.event_hub.sent


def test_inject_now_reports_a_silent_write_failure_as_retry(tmp_path, monkeypatch) -> None:
    """The same hole on the NON-busy path, where it additionally made the schedule engine
    record ``mark_ran(status="ok")`` for a result nobody got: ``deliver_or_queue`` must
    fall through to the durable inbox instead of returning "delivered"."""
    from akana_server.orchestrator import turn_writer

    monkeypatch.setattr(turn_writer, "_persist_turn", lambda **kw: None)

    app = _app(tmp_path)
    settings = SimpleNamespace(data_dir=Path(tmp_path))
    conv = _make_conv(tmp_path)

    outcome = asyncio.run(deliver_or_queue(app, settings, conv, "the briefing", kind="schedule"))
    assert outcome == "queued", outcome
    assert _texts(tmp_path, conv) == []
    assert _frames(app, "turn_completed") == [], app.state.event_hub.sent


# --------------------------------------------------------------------------- #
# S5 — a realtime spoken turn is a turn: it must register and pair its announce
# --------------------------------------------------------------------------- #


class _FakeWS:
    async def send_json(self, payload):  # pragma: no cover - unused here
        pass

    async def send_bytes(self, data):  # pragma: no cover - unused here
        pass

    async def close(self, code=1000, reason=""):
        pass


def _bridge(tmp_path, app, conv_id: str):
    from akana_server.voice.realtime_base import RealtimeBridge

    class _Bridge(RealtimeBridge):
        _broadcast_source = "voice_test"

        def _available(self) -> bool:
            return True

        def _begin_turn_mode(self) -> str:
            return "voice_test"

        async def _open_session(self) -> None:  # pragma: no cover - overridden per test
            pass

        async def _from_browser(self, session):  # pragma: no cover - unused
            pass

        async def _from_provider(self, session):  # pragma: no cover - unused
            pass

    settings = SimpleNamespace(data_dir=Path(tmp_path), primary_lang="en")
    return _Bridge(_FakeWS(), settings, app=app, conv_id=conv_id)


def test_realtime_spoken_turn_registers_and_pairs_its_announcement(tmp_path) -> None:
    """The bridge writes the user+assistant PAIR only at the END of the utterance, and
    registered in NEITHER turn registry — so a background job completing mid-utterance
    saw a free conversation and injected its result ABOVE the question it answers. The
    bridge also broadcast a turn_completed the gate had no turn_active for."""
    app = _app(tmp_path)
    settings = _settings(tmp_path)
    conv = _make_conv(tmp_path)
    bridge = _bridge(tmp_path, app, conv)

    async def main() -> None:
        bridge._in_buf += "what is the weather"  # first input transcript
        assert _turn_running(app, conv) is True, "the spoken turn claims nothing"
        assert (
            await deliver_or_queue(app, settings, conv, "job finished: 42", kind="task")
            == "queued"
        )
        bridge._out_buf += "it is sunny"
        await bridge._persist_turn()
        await asyncio.sleep(0.05)
        assert _turn_running(app, conv) is False, "the claim outlived the spoken turn"

    asyncio.run(main())
    active = _frames(app, "turn_active")
    completed = _frames(app, "turn_completed")
    assert len(active) == 1, app.state.event_hub.sent
    assert active[0]["source"] == "user", active[0]
    assert len(completed) == 1, app.state.event_hub.sent
    assert completed[0]["status"] == "ok" and completed[0]["source"] == "user"
    assert _texts(tmp_path, conv) == ["what is the weather", "it is sunny"]


def test_realtime_session_end_pairs_an_unfinished_spoken_turn(tmp_path) -> None:
    """The user speaks, the provider never answers and the socket closes: the announced
    spoken turn still owes exactly one completion, and the claim must not survive the
    session (a permanently busy conversation)."""
    app = _app(tmp_path)
    conv = _make_conv(tmp_path)
    bridge = _bridge(tmp_path, app, conv)

    async def _open_session() -> None:
        bridge._in_buf += "are you there"

    bridge._open_session = _open_session  # type: ignore[method-assign]

    async def main() -> None:
        await bridge.run()
        await asyncio.sleep(0.05)

    asyncio.run(main())
    assert _turn_running(app, conv) is False
    assert len(_frames(app, "turn_active")) == 1, app.state.event_hub.sent
    completed = _frames(app, "turn_completed")
    assert len(completed) == 1, app.state.event_hub.sent
    assert completed[0]["status"] == "cancelled", completed[0]


def test_a_spoken_turn_cannot_wedge_the_connector_gate(tmp_path, monkeypatch) -> None:
    """The two claims this round lengthens/adds must not be able to hold each other: a
    spoken turn makes the conversation busy, so the connector guard WAITS — bounded by
    its own ``_GUARD_MAX_WAIT_S`` ceiling, after which it processes the message with no
    claim (and therefore announces and completes nothing)."""
    from akana_server.connectors import service as conn_service

    async def ok(settings, text: str, **kw) -> str:
        return f"cevap: {text}"

    app = _app(tmp_path)
    router = _gated_router(tmp_path, ok, app)

    async def main() -> str:
        await router.handle(_msg("selam"))
        conv = ConversationService(Path(tmp_path)).list_conversations()[0].id
        bridge = _bridge(tmp_path, app, conv)
        bridge._in_buf += "what is the weather"  # the spoken turn owns the conversation
        assert _turn_running(app, conv) is True
        await asyncio.sleep(0.05)  # let the spoken turn's own turn_active land
        monkeypatch.setattr(conn_service, "_GUARD_MAX_WAIT_S", 0.3)
        app.state.event_hub.sent.clear()
        await asyncio.wait_for(router.handle(_msg("ikinci")), timeout=5)
        assert _frames(app, "turn_active") == [], app.state.event_hub.sent
        assert _frames(app, "turn_completed") == [], app.state.event_hub.sent
        # …and the spoken turn is untouched: it still owns the claim and still owes
        # exactly one completion, which it pairs when the utterance is written.
        assert _turn_running(app, conv) is True
        bridge._out_buf += "it is sunny"
        await bridge._persist_turn()
        await asyncio.sleep(0.05)
        assert _turn_running(app, conv) is False
        return conv

    asyncio.run(main())
    assert len(_frames(app, "turn_completed")) == 1, app.state.event_hub.sent


def test_realtime_never_fails_on_a_busy_conversation(tmp_path) -> None:
    """A live voice session must never raise 409: the claim is best-effort. When the
    conversation is already busy the bridge simply does not own the claim — and then
    must not announce, nor answer somebody else's turn_active on release."""
    from akana_server.api.routes.chat.chat_state import _ActiveTurn, _active_turns

    app = _app(tmp_path)
    conv = _make_conv(tmp_path)
    _active_turns(app)[conv] = _ActiveTurn(conversation_id=conv)
    bridge = _bridge(tmp_path, app, conv)

    async def main() -> None:
        bridge._in_buf += "hello"
        bridge._out_buf += "hi"
        await bridge._persist_turn()
        await asyncio.sleep(0.05)

    asyncio.run(main())
    assert _frames(app, "turn_active") == [], app.state.event_hub.sent
    # The bridge's OWN completion (it persisted a real pair) is still owed exactly once.
    assert len(_frames(app, "turn_completed")) == 1, app.state.event_hub.sent


# --------------------------------------------------------------------------- #
# S6 — a verbatim same-chat fire must announce the turn its delivery completes
# --------------------------------------------------------------------------- #


def test_verbatim_same_chat_fire_announces_its_background_turn(tmp_path, monkeypatch) -> None:
    """The announce block sat inside the ``else:`` of ``if verbatim:`` while the DELIVERY
    section is shared — so a plain reminder emitted a turn_completed{source:"background"}
    with no turn_active. On the client that completion decrements the per-conversation
    background counter a genuine concurrent background_run job owns, killing that job's
    working strip while it keeps running."""
    from akana_server.orchestrator import memory_tools
    from akana_server.schedule import engine
    from akana_server.schedule.model import Delivery
    from akana_server.schedule.store import TR_TZ, ScheduleStore, to_iso

    from datetime import datetime

    t0 = datetime(2026, 7, 20, 9, 0, tzinfo=TR_TZ)
    monkeypatch.setattr(memory_tools, "mcp_servers_payload", lambda *a, **k: None)

    app = _app(tmp_path)
    settings = SimpleNamespace(data_dir=Path(tmp_path))
    conv = _make_conv(tmp_path)
    store = ScheduleStore(tmp_path)
    item = store.create(
        title="Pill",
        prompt="",
        message="take your pill",  # verbatim → no LLM turn
        kind="once",
        when=to_iso(t0),
        delivery=Delivery(mode="thread", conversation_id=conv, same_chat=True),
        now=t0,
    )
    asyncio.run(engine.run_schedule_now(settings, item.id, conversations=None, now=t0, app=app))

    active = _frames(app, "turn_active")
    completed = _frames(app, "turn_completed")
    assert len(active) == 1, app.state.event_hub.sent
    assert active[0]["source"] == "background", active[0]
    assert len(completed) == 1, app.state.event_hub.sent
    assert completed[0]["source"] == "background", completed[0]
    assert "take your pill" in "\n".join(_texts(tmp_path, conv))
