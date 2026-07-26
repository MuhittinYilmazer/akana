"""hunt5 misc — connector (Telegram) turns must announce themselves to the web UI.

A channel turn is persisted into the SAME conversation store the web UI renders, but
outside the chat SSE flow. Without a ``turn_completed`` push on ``/ws/events`` a bound
conversation left open in the browser stays stale until F5 — the exact symptom
``akana_server/conversation_events`` was built to prevent (see its module docstring;
the schedule engine and chat_injections both honour it).

Fake channel + fake LLM + real persistence, NO network.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

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


def _stack(tmp_path: Path, complete, *, app):
    """The PRODUCTION wiring, including the real ``_make_turn_guard``.

    Not the no-op guard: only the gate CLAIMS and announces the turn, and the router
    owes its ``turn_completed`` exactly when the gate announced one. A no-op guard
    therefore tests a path that cannot exist in production and hides everything the
    claim's lifetime governs."""
    settings = _settings(tmp_path)
    reg = ConnectorRegistry()
    fake = _FakeConnector()
    reg.register(fake)
    conversations = ConversationService(tmp_path)
    router = InboundRouter(
        settings,
        reg,
        complete=complete,
        conversations=conversations,
        skill_planner=_no_skills,
        turn_guard=_make_turn_guard(app),
        app=app,
    )
    return router, fake, conversations


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        data_dir=tmp_path,
        telegram_enabled=True,
        telegram_bot_token="tok",
        telegram_allowed_chat_ids=("42",),
    )


def _app(hub, tmp_path: Path) -> SimpleNamespace:
    """An ``app.state`` carrier with everything the turn-gate / drain seams read."""
    return SimpleNamespace(
        state=SimpleNamespace(
            event_hub=hub,
            settings=_settings(tmp_path),
            conversation_service=ConversationService(tmp_path),
            active_turns={},
        )
    )


def test_connector_turn_broadcasts_turn_completed(tmp_path: Path) -> None:
    async def ok(settings, text: str, **kw) -> str:
        return f"reply: {text}"

    hub = _RecordingHub()
    router, _fake, conversations = _stack(tmp_path, ok, app=_app(hub, tmp_path))

    async def run() -> None:
        await router.handle(_msg("merhaba"))
        await asyncio.sleep(0.1)  # let the fire-and-forget announcements land

    asyncio.run(run())

    conv = conversations.list_conversations()[0]
    active = [f for f in hub.sent if f.get("type") == "turn_active"]
    assert len(active) == 1, f"exactly one turn_active per channel turn, got {hub.sent}"
    frames = [f for f in hub.sent if f.get("type") == "turn_completed"]
    assert len(frames) == 1, f"exactly one turn_completed per channel turn, got {hub.sent}"
    frame = frames[0]
    assert frame["conversation_id"] == conv.id
    assert frame["status"] == "ok"
    # ``source`` is the producer marker consumers gate NOISE on, and the contract
    # defines exactly two values: "user" and "background". A channel message IS the
    # user's own send, so it refreshes the UI without popping an OS notification for
    # a message they just typed on their phone. ("connector" was off-contract; an
    # unknown value only happened to degrade to the right semantics.)
    assert frame["source"] == "user"
    # The completed turn's id lets the frontend reload exactly that turn.
    msgs = conversations.list_messages(conv.id)
    assert frame["assistant_turn_id"] == msgs[-1].id


def test_connector_broadcast_is_per_turn_and_survives_no_hub(tmp_path: Path) -> None:
    async def ok(settings, text: str, **kw) -> str:
        return "tamam"

    hub = _RecordingHub()
    router, _fake, _conversations = _stack(tmp_path, ok, app=_app(hub, tmp_path))

    async def run() -> None:
        await router.handle(_msg("bir"))
        await router.handle(_msg("iki"))
        await asyncio.sleep(0.1)

    asyncio.run(run())
    # One pair PER TURN, in order — never a completion that outruns its own start.
    # (The post-turn drain's ``queue_updated`` frames are not part of this contract.)
    lifecycle = [
        f["type"] for f in hub.sent if f["type"] in ("turn_active", "turn_completed")
    ]
    assert lifecycle == [
        "turn_active",
        "turn_completed",
        "turn_active",
        "turn_completed",
    ], hub.sent

    # No hub (headless / tests) → silent no-op, the reply flow is untouched.
    other = tmp_path / "b"
    other.mkdir()
    router2, fake2, _c2 = _stack(other, ok, app=_app(None, other))
    asyncio.run(router2.handle(_msg("uc")))
    assert fake2.sent[-1].text == "tamam"


def test_connector_command_replies_do_not_broadcast(tmp_path: Path) -> None:
    """``/durum`` writes no turn → no completion event (a broadcast without a
    persisted turn would make the UI reload for nothing)."""

    async def ok(settings, text: str, **kw) -> str:  # pragma: no cover - not reached
        return "x"

    hub = _RecordingHub()
    router, _fake, _conversations = _stack(tmp_path, ok, app=_app(hub, tmp_path))
    asyncio.run(router.handle(_msg("/durum")))
    assert hub.sent == []
