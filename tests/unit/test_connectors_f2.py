"""ConnectorEngine F2 — Telegram full conversation: persistence, /yeni, /durum,
4096 split, requires_approval rejection, egress (fake Telegram + fake LLM, NO network)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx

from akana_server import audit
from akana_server.connectors.base import InboundMessage, OutboundMessage, split_text
from akana_server.connectors.conversation import (
    ChannelBindingStore,
    channel_title,
    parse_command,
    trim_history,
)
from akana_server.connectors.egress_filter import REDACTION
from akana_server.connectors.registry import ConnectorRegistry
from akana_server.connectors.router import (
    NEW_CONVERSATION_REPLY,
    InboundRouter,
)
from akana_server.connectors.service import _make_turn_guard
from akana_server.connectors.telegram import MAX_MESSAGE_LEN, TelegramConnector
from akana_server.conversation_service import ConversationService
from akana_server.skills.turn_injection import SkillTurnPlan


def _settings(tmp_path: Path, **kw) -> SimpleNamespace:
    base = {
        "data_dir": tmp_path,
        "telegram_enabled": True,
        "telegram_bot_token": "tok-ENV",
        "telegram_allowed_chat_ids": ("42",),
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _msg(text: str, *, chat_id: str = "42", sender: str = "Alice") -> InboundMessage:
    return InboundMessage(
        connector_id="fake", chat_id=chat_id, text=text, sender_name=sender
    )


class FakeConnector:
    connector_id = "fake"
    max_message_len = 0  # 0 → no split; set per test

    def __init__(self, limit: int = 0) -> None:
        self.max_message_len = limit
        self.sent: list[OutboundMessage] = []

    async def start(self, inbound) -> None:  # pragma: no cover - unused
        pass

    async def stop(self) -> None:  # pragma: no cover
        pass

    async def send(self, message: OutboundMessage) -> None:
        self.sent.append(message)

    def status(self) -> dict:
        return {"id": self.connector_id, "running": True}


def _stack(tmp_path: Path, complete, *, limit: int = 0, skill_planner=None):
    """Router + fake channel + REAL persistence (v2 ``memory.db``, the same layer as the app).

    After A5, turn writing goes to v2 (turn_writer is the single writer). In production
    ``app.state.conversation_service`` = :class:`ConversationService`
    (see connectors/service.py); the tests use the same adapter so read+write are
    verified consistently in a single store (v2).
    """
    settings = _settings(tmp_path)
    reg = ConnectorRegistry()
    fake = FakeConnector(limit)
    reg.register(fake)
    conversations = ConversationService(tmp_path)
    router = InboundRouter(
        settings,
        reg,
        complete=complete,
        conversations=conversations,
        skill_planner=skill_planner or _no_skills,
    )
    return router, fake, conversations


async def _no_skills(settings, text: str) -> SkillTurnPlan:
    return SkillTurnPlan()


# -- pure helpers ---------------------------------------------------------------


def test_parse_command_variants() -> None:
    assert parse_command("/yeni") == "yeni"
    assert parse_command("  /Durum  ") == "durum"
    assert parse_command("/yeni@AkanaBot lütfen") == "yeni"
    assert parse_command("/start") is None
    assert parse_command("merhaba /yeni") is None
    assert parse_command("") is None


def test_channel_title() -> None:
    assert channel_title("telegram", "Alice", "42") == "Telegram: Alice"
    assert channel_title("telegram", "", "42") == "Telegram: 42"
    assert channel_title("matrix", "ali", "1") == "Matrix: ali"


def test_trim_history_budget_keeps_newest() -> None:
    msgs = [
        {"role": "user", "content": "a" * 100},
        {"role": "assistant", "content": "b" * 100},
        {"role": "user", "content": "c" * 50},
    ]
    out = trim_history(msgs, max_chars=160)
    assert [m["content"][0] for m in out] == ["b", "c"]  # the oldest was dropped
    # Even if a single message exceeds the budget, the newest is kept.
    assert trim_history(msgs, max_chars=10) == [msgs[-1]]
    assert trim_history(msgs, max_chars=0) == msgs  # no budget


def test_split_text_respects_limit_and_boundaries() -> None:
    text = "satır bir\n\nsatır iki " + "kelime " * 30
    chunks = split_text(text, 50)
    assert all(len(c) <= 50 for c in chunks)
    assert "".join(c + " " for c in chunks).split() == text.split()  # no content is lost
    assert split_text("kısa", 4096) == ["kısa"]
    # A whitespace-free huge block is hard-cut but the limit is not exceeded.
    assert all(len(c) <= 10 for c in split_text("x" * 35, 10))


def test_binding_store_roundtrip(tmp_path: Path) -> None:
    store = ChannelBindingStore(tmp_path)
    assert store.get("telegram", "42") is None
    store.bind("telegram", "42", "conv-1")
    assert store.get("telegram", "42") == "conv-1"
    store.bind("telegram", "42", "conv-2")  # /yeni: overwrite
    assert store.get("telegram", "42") == "conv-2"
    store.clear("telegram", "42")
    assert store.get("telegram", "42") is None
    # A corrupt file starts from scratch, no exception leaks.
    (tmp_path / "connector_bindings.json").write_text("{bozuk", encoding="utf-8")
    assert store.get("telegram", "42") is None


# -- multi-turn persistence --------------------------------------------------------


def test_multi_turn_history_reaches_llm_and_persists(tmp_path: Path) -> None:
    seen_histories: list[list[dict[str, str]]] = []

    async def fake_complete(settings, text: str, *, history=None, conversation_id=None):
        seen_histories.append(list(history or []))
        return f"yanıt: {text}"

    router, fake, conversations = _stack(tmp_path, fake_complete)

    async def run() -> None:
        await router.handle(_msg("ilk mesaj"))
        await router.handle(_msg("ikinci mesaj"))

    asyncio.run(run())

    # 1st turn has no history; the 2nd turn sees the first pair as history.
    assert seen_histories[0] == []
    assert [m["content"] for m in seen_histories[1]] == ["ilk mesaj", "yanıt: ilk mesaj"]
    assert [m["role"] for m in seen_histories[1]] == ["user", "assistant"]

    # The conversation appears in the web UI list with a «Telegram: …» title (fake channel → Fake).
    convs = conversations.list_conversations()
    assert len(convs) == 1
    meta = convs[0]
    assert meta.title == "Fake: Alice"
    assert meta.message_count == 4  # 2 user + 2 assistant
    assert conversations.get_json_metadata(meta.id)["channel"] == "fake"
    assert conversations.get_json_metadata(meta.id)["channel_chat_id"] == "42"

    # The turns are in the episodic archive, identical to what was sent to the channel.
    msgs = conversations.list_messages(meta.id)
    assert [m.content for m in msgs] == [
        "ilk mesaj",
        "yanıt: ilk mesaj",
        "ikinci mesaj",
        "yanıt: ikinci mesaj",
    ]
    assert fake.sent[-1].text == "yanıt: ikinci mesaj"


def test_separate_chats_get_separate_conversations(tmp_path: Path) -> None:
    async def ok(settings, text: str, **kw) -> str:
        return "tamam"

    router, _fake, conversations = _stack(tmp_path, ok)

    async def run() -> None:
        await router.handle(_msg("selam", chat_id="42", sender="Alice"))
        await router.handle(_msg("selam", chat_id="77", sender="Ayşe"))

    asyncio.run(run())
    titles = {c.title for c in conversations.list_conversations()}
    assert titles == {"Fake: Alice", "Fake: Ayşe"}


# -- /yeni and /durum ----------------------------------------------------------------


def test_yeni_starts_fresh_conversation_keeps_old(tmp_path: Path) -> None:
    async def ok(settings, text: str, *, history=None, **kw) -> str:
        return f"gecmis:{len(history or [])}"

    router, fake, conversations = _stack(tmp_path, ok)

    async def run() -> None:
        await router.handle(_msg("birinci"))
        await router.handle(_msg("/yeni"))
        await router.handle(_msg("ikinci"))

    asyncio.run(run())
    assert fake.sent[1].text == NEW_CONVERSATION_REPLY
    assert fake.sent[2].text == "gecmis:0"  # the new conversation starts with no history
    convs = conversations.list_conversations()
    assert len(convs) == 2  # the old conversation was not deleted, still in the list
    counts = sorted(c.message_count for c in convs)
    assert counts == [2, 2]


def test_durum_reports_conversation(tmp_path: Path) -> None:
    # FULL AUTONOMY: the "Policy mode" line was removed from /durum (policy was dropped).
    async def ok(settings, text: str, **kw) -> str:
        return "tamam"

    router, fake, _conversations = _stack(tmp_path, ok)

    async def run() -> None:
        await router.handle(_msg("merhaba"))
        await router.handle(_msg("/durum"))

    asyncio.run(run())
    status = fake.sent[-1].text
    assert status.startswith("Akana status:")
    assert "Politika modu:" not in status
    assert "«Fake: Alice» (2 messages)" in status  # EN status output asserted


def test_commands_are_not_persisted(tmp_path: Path) -> None:
    async def ok(settings, text: str, **kw) -> str:
        return "tamam"

    router, _fake, conversations = _stack(tmp_path, ok)

    async def run() -> None:
        await router.handle(_msg("merhaba"))
        await router.handle(_msg("/durum"))

    asyncio.run(run())
    meta = conversations.list_conversations()[0]
    assert meta.message_count == 2  # the /durum pair did not enter the archive


# -- boundary split --------------------------------------------------------------------


def test_long_reply_is_split_to_channel_limit(tmp_path: Path) -> None:
    long_reply = "cümle bu. " * 30  # 300 chars

    async def verbose(settings, text: str, **kw) -> str:
        return long_reply.strip()

    router, fake, _conversations = _stack(tmp_path, verbose, limit=100)
    asyncio.run(router.handle(_msg("anlat")))
    assert len(fake.sent) > 1
    assert all(len(m.text) <= 100 for m in fake.sent)
    assert " ".join(m.text for m in fake.sent).split() == long_reply.split()


def test_telegram_connector_declares_4096_limit() -> None:
    assert MAX_MESSAGE_LEN == 4096
    assert TelegramConnector.max_message_len == 4096


# -- skill injection: the [Yetenek] block is prepended to the LLM prompt -------------


def test_injected_skill_block_prepended_to_llm_prompt(tmp_path: Path) -> None:
    prompts: list[str] = []

    async def fake_complete(settings, text: str, **kw) -> str:
        prompts.append(text)
        return "tamam"

    async def inject_planner(settings, text: str) -> SkillTurnPlan:
        plan = SkillTurnPlan()
        plan.injected = [{"id": "ozet", "title": "Özet", "status": "injected"}]
        plan.prompt_block = "[Yetenek: ozet — Özet]\ngövde\n[/Yetenek]"
        return plan

    router, _fake, _conversations = _stack(
        tmp_path, fake_complete, skill_planner=inject_planner
    )
    asyncio.run(router.handle(_msg("özetle")))
    assert prompts[0].startswith("[Yetenek: ozet — Özet]")
    assert prompts[0].endswith("özetle")


# -- egress: on the conversation path too every reply passes the filter ------------------


def test_egress_filter_masks_reply_and_archive(tmp_path: Path) -> None:
    async def leaky(settings, text: str, **kw) -> str:
        return "Anahtarın: api_key = sk-live-GIZLI999"

    router, fake, conversations = _stack(tmp_path, leaky)
    sent = asyncio.run(router.handle(_msg("anahtarı söyle")))
    assert "sk-live-GIZLI999" not in sent
    assert REDACTION in fake.sent[0].text
    # The archived record is the same (masked) as what was sent to the channel — the leak does not persist.
    meta = conversations.list_conversations()[0]
    archived = conversations.list_messages(meta.id)[-1].content
    assert "sk-live-GIZLI999" not in archived
    assert REDACTION in archived
    kinds = [e.get("kind") for e in audit.read_tail(tmp_path)]
    assert "connector_egress_filtered" in kinds


# -- end-to-end: fake Telegram API + fake LLM ---------------------------------------------


def _update(update_id: int, chat_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": 7, "first_name": "Alice"},
            "text": text,
        },
    }


class FakeTelegramAPI:
    def __init__(self, updates: list[list[dict]]) -> None:
        self.update_batches = list(updates)
        self.sent: list[dict] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            batch = self.update_batches.pop(0) if self.update_batches else []
            return httpx.Response(200, json={"ok": True, "result": batch})
        if request.url.path.endswith("/sendMessage"):
            self.sent.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        return httpx.Response(404, json={"ok": False})  # pragma: no cover


def test_end_to_end_telegram_multi_turn(tmp_path: Path) -> None:
    api = FakeTelegramAPI(
        updates=[[_update(1, 42, "adım Alice")], [_update(2, 42, "adım ne?")]]
    )
    settings = _settings(tmp_path)
    conn = TelegramConnector(
        settings, transport=api.transport(), poll_timeout=0, error_backoff=0
    )
    reg = ConnectorRegistry()
    reg.register(conn)

    async def fake_complete(settings, text: str, *, history=None, **kw) -> str:
        if any("Alice" in m["content"] for m in (history or [])):
            return "Adın Alice."
        return "Memnun oldum."

    router = InboundRouter(
        settings,
        reg,
        complete=fake_complete,
        conversations=ConversationService(tmp_path),
        skill_planner=_no_skills,
    )

    async def run() -> None:
        await reg.start_all()
        router.start()
        for _ in range(2000):  # up to ~20 s — headroom for a contended CI runner
            if len(api.sent) >= 2:
                break
            await asyncio.sleep(0.01)
        await router.stop()
        await reg.stop_all()

    asyncio.run(run())
    assert [m["text"] for m in api.sent] == ["Memnun oldum.", "Adın Alice."]
    convs = ConversationService(tmp_path).list_conversations()
    assert any(c.title == "Telegram: Alice" for c in convs)


# -- backward-compat: if there is no conversation service, F1 stateless behavior ------------


def test_stateless_mode_without_services_still_works(tmp_path: Path) -> None:
    async def legacy(settings, text: str) -> str:  # old 2-arg signature
        return "eski yol"

    reg = ConnectorRegistry()
    fake = FakeConnector()
    reg.register(fake)
    router = InboundRouter(_settings(tmp_path), reg, complete=legacy)
    assert asyncio.run(router.handle(_msg("selam"))) == "eski yol"
    # In a setup without a conversation, /yeni explains politely, does not blow up.
    reply = asyncio.run(router.handle(_msg("/yeni")))
    assert "archive is off" in reply


# -- R4-E #1: a connector turn is mutually exclusive with the web/voice busy-guard ----------


def _guard_app() -> SimpleNamespace:
    """A minimal app.state carrier where the chat_state busy-registry lives."""
    return SimpleNamespace(state=SimpleNamespace())


def test_connector_guard_marks_conversation_busy_for_web() -> None:
    """While a connector turn is RUNNING the web/voice path must see the same conv as BUSY (a
    concurrent 2nd turn gets 409 → no daemon session_key collision / history race)."""
    from akana_server.api.routes.chat.chat_state import _is_turn_running

    app = _guard_app()
    guard = _make_turn_guard(app)

    async def run() -> None:
        assert _is_turn_running(app, "convX") is False
        async with guard("convX"):
            assert _is_turn_running(app, "convX") is True  # the web now sees BUSY
        assert _is_turn_running(app, "convX") is False  # free on exit

    asyncio.run(run())


def test_connector_guard_waits_until_web_turn_frees_conversation() -> None:
    """While a web/voice turn runs on the same conv, a connector turn does NOT raise 409 → it WAITS;
    it continues when the web turn finishes (there is no client to retry on the connector)."""
    from akana_server.api.routes.chat.chat_state import (
        _register_nonstreaming_turn,
        _release_nonstreaming_turn,
    )

    app = _guard_app()
    guard = _make_turn_guard(app)

    async def run() -> None:
        entered = asyncio.Event()
        web_release = asyncio.Event()

        async def web_turn() -> None:  # separate task: mark the conv busy, hold until released
            handle = _register_nonstreaming_turn(app, "convY")
            try:
                await web_release.wait()
            finally:
                _release_nonstreaming_turn(app, "convY", handle)

        web = asyncio.create_task(web_turn())
        await asyncio.sleep(0.05)  # let the web turn become busy

        async def connector_turn() -> None:
            async with guard("convY"):
                entered.set()

        conn = asyncio.create_task(connector_turn())
        await asyncio.sleep(0.3)
        assert not entered.is_set()  # while web is BUSY the connector WAITS (did not enter)
        web_release.set()  # the web turn finished → the conv is free
        await asyncio.wait_for(conn, timeout=2)
        assert entered.is_set()  # once free, the connector turn continued
        await web

    asyncio.run(run())


def test_connector_guard_propagates_non_busy_error_instead_of_stalling() -> None:
    """smell:3 regression — the wait loop must treat ONLY 409 TURN_BUSY as 'busy'. A real
    fault raised by the turn-gate registration (e.g. an AttributeError after a chat_state
    refactor, or a non-409 HTTPException) must PROPAGATE immediately, not be silently
    reinterpreted as busy and polled for 90 seconds before 'processing anyway'."""
    import akana_server.api.routes.chat.turn_gate as turn_gate_mod
    from fastapi import HTTPException

    app = _guard_app()

    # A non-409 HTTPException (a genuine failure) must escape the busy wait loop at once.
    def _boom_http(_app: object, _conv: str | None):
        raise HTTPException(status_code=500, detail={"error": {"code": "BOOM"}})

    orig = turn_gate_mod.register_turn
    turn_gate_mod.register_turn = _boom_http  # type: ignore[assignment]
    try:
        guard = _make_turn_guard(app)

        async def run_http() -> None:
            started = asyncio.get_event_loop().time()
            try:
                async with guard("convZ"):
                    pass
                raise AssertionError("guard swallowed a non-busy HTTPException as 'busy'")
            except HTTPException as exc:
                assert exc.status_code == 500  # the real error propagated unchanged
            # It must NOT have polled for anywhere near _GUARD_MAX_WAIT_S (90s).
            assert asyncio.get_event_loop().time() - started < 1.0

        asyncio.run(run_http())

        # A plain (non-HTTP) exception must ALSO propagate (it is not caught at all).
        def _boom_attr(_app: object, _conv: str | None):
            raise AttributeError("chat_state moved")

        turn_gate_mod.register_turn = _boom_attr  # type: ignore[assignment]
        guard2 = _make_turn_guard(app)

        async def run_attr() -> None:
            try:
                async with guard2("convZ2"):
                    pass
                raise AssertionError("guard swallowed an AttributeError as 'busy'")
            except AttributeError:
                pass

        asyncio.run(run_attr())
    finally:
        turn_gate_mod.register_turn = orig  # type: ignore[assignment]


# -- R4-E #3: worker cap → backpressure (no unbounded workers), no loss ------------------------


def test_worker_cap_backpressures_then_drains(tmp_path: Path) -> None:
    """Worker cap full + a NEW chat → intake waits until a worker frees up
    (unbounded workers → the intake OOM guard is not pierced); once free ALL messages are processed."""
    gate = asyncio.Event()
    seen: list[str] = []

    async def slow_complete(settings, text: str, *, history=None, **kw) -> str:
        seen.append(text)
        await gate.wait()  # keep the worker alive → fill the cap
        return "ok"

    reg = ConnectorRegistry()
    reg.register(FakeConnector())
    router = InboundRouter(
        _settings(tmp_path),
        reg,
        complete=slow_complete,
        conversations=ConversationService(tmp_path),
        skill_planner=_no_skills,
        max_workers=2,
    )

    async def run() -> None:
        router.start()
        for i in range(4):  # 4 DIFFERENT chats → each wants a new worker
            await reg.inbound.put(
                InboundMessage(connector_id="fake", chat_id=f"c{i}", text=f"m{i}", sender_name="x")
            )
        await asyncio.sleep(0.25)  # let 2 workers fill up, held in complete
        assert len(router._workers) <= 2, f"worker cap exceeded: {len(router._workers)}"
        assert len(seen) <= 2, f"must be capped, no more than 2 should reach complete: {seen}"
        gate.set()  # free → drain + the remaining 2 chats are processed
        for _ in range(400):
            if len(seen) >= 4:
                break
            await asyncio.sleep(0.01)
        await router.stop()

    asyncio.run(run())
    assert sorted(seen) == ["m0", "m1", "m2", "m3"]  # NO LOSS (all processed)


# -- CTX-2: PUT /connectors/telegram persists atomically -------------------------


def test_put_telegram_atomic_persist_and_failure(monkeypatch, tmp_path: Path) -> None:
    """The Telegram PUT writes runtime keys all-or-none (set_many), and a storage
    failure surfaces as PERSIST_FAILED with NOTHING partially applied — the exact
    hazard a per-key set() loop reopened (telegram_enabled durable, allowlist not)."""
    from fastapi.testclient import TestClient

    from akana_server.api.app import create_app
    from akana_server.runtime_settings import get_store, reset_runtime_stores

    reset_runtime_stores()
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    app = create_app()

    with TestClient(app) as client:
        settings = app.state.settings
        store = get_store(settings.data_dir)

        # Happy path: both keys land together.
        r = client.put(
            "/api/v1/connectors/telegram",
            json={"enabled": True, "allowed_chat_ids": ["42", "7"]},
        )
        assert r.status_code == 200
        loaded = store.load()
        assert loaded["telegram_enabled"] is True
        assert loaded["telegram_allowed_chat_ids"] == ["42", "7"]

        # Inject a storage failure: the write is refused whole (PERSIST_FAILED) and
        # the prior state is intact — no partial telegram_enabled flip.
        def _boom(_values):
            raise OSError("disk full")

        monkeypatch.setattr(store, "set_many", _boom)
        r = client.put(
            "/api/v1/connectors/telegram",
            json={"enabled": False, "allowed_chat_ids": ["99"]},
        )
        assert r.status_code == 500
        assert r.json()["detail"]["error"]["code"] == "PERSIST_FAILED"
        after = store.load()
        assert after["telegram_enabled"] is True
        assert after["telegram_allowed_chat_ids"] == ["42", "7"]

    reset_runtime_stores()
