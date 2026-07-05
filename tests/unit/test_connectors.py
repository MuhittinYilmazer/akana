"""ConnectorEngine F0-F1 (Telegram MVP) — egress filter, registry, router,
fake Telegram API (httpx.MockTransport; NO real network) and the REST surface."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from akana_server import audit
from akana_server.api.app import create_app
from akana_server.config import parse_telegram_allowed_chat_ids
from akana_server.connectors.base import (
    ConnectorSendError,
    InboundMessage,
    OutboundMessage,
)
from akana_server.connectors.egress_filter import REDACTION, filter_outbound
from akana_server.connectors.registry import ConnectorRegistry, build_registry
from akana_server.connectors.router import (
    EMPTY_REPLY,
    LLM_ERROR_REPLY,
    InboundRouter,
)
from akana_server.connectors.telegram import (
    TelegramConnector,
    discover_chats,
    resolve_bot_token,
)
from akana_server.secret_store import set_secrets


def _settings(tmp_path: Path, **kw) -> SimpleNamespace:
    base = {
        "data_dir": tmp_path,
        "telegram_enabled": True,
        "telegram_bot_token": "tok-ENV",
        "telegram_allowed_chat_ids": ("42",),
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _audit_kinds(tmp_path: Path) -> list[str]:
    return [e.get("kind") for e in audit.read_tail(tmp_path)]


# -- egress filter -----------------------------------------------------------


def test_egress_clean_text_untouched() -> None:
    res = filter_outbound("Yarın saat 14:00'te diş randevun var.")
    assert res.text == "Yarın saat 14:00'te diş randevun var."
    assert res.matched == ()
    assert res.redacted is False


@pytest.mark.parametrize(
    ("text", "pattern_id"),
    [
        ("Doğrulama kodu: 482913 olarak geldi", "otp.code"),
        ("api_key = sk-live-AAAA1111", "credential.assignment"),
        ("password: hunter2!", "credential.assignment"),
        ("-----BEGIN RSA PRIVATE KEY-----\nMII...\n-----END RSA PRIVATE KEY-----", "credential.private_key"),
        ("kart: 4111 1111 1111 1111 son", "payment.card"),
        ("IBAN TR330006100519786457841326 hesabına", "payment.iban"),
    ],
)
def test_egress_sensitive_patterns_redacted(text: str, pattern_id: str) -> None:
    res = filter_outbound(text)
    assert pattern_id in res.matched
    assert REDACTION in res.text
    # The redacted raw content does not remain in the text.
    assert "482913" not in res.text or pattern_id != "otp.code"


def test_egress_redacts_credential_value_to_end_of_line() -> None:
    """When ``key/token = ...`` is seen the value is redacted to the END OF LINE.

    Previous behavior (BUG): a trailing ``\\S+`` hid only the FIRST token →
    the rest of a multi-word secret/passphrase leaked. The fix redacts the ENTIRE
    rest of the line (fail-closed); content BEFORE the credential is preserved, and
    redaction does NOT cross a newline (later lines are untouched)."""
    # Single line: everything after the credential (token + the prose that follows) is
    # fully hidden — the regex cannot distinguish the passphrase continuation from
    # innocent prose, so the safe side is full redaction.
    res = filter_outbound("Merhaba! token=abc123 işte böyle.")
    assert res.text.startswith("Merhaba!")
    assert "abc123" not in res.text
    assert res.text == f"Merhaba! {REDACTION}"
    # Multi-word passphrase: ALL words are redacted (old bug: only the first).
    multi = filter_outbound("parola: correct horse battery staple")
    assert multi.text == REDACTION
    assert "horse" not in multi.text and "staple" not in multi.text
    # Redaction does NOT cross a newline: the next line (not a secret) is preserved.
    multiline = filter_outbound("token=abc123 sızıntı\nSonraki satır kalır.")
    assert multiline.text == f"{REDACTION}\nSonraki satır kalır."


def test_egress_redacts_truncated_private_key_without_end_marker() -> None:
    """Regression (secret leak): for a private key with NO END marker (truncated/streaming)
    the END part was OPTIONAL in the old pattern → only the BEGIN header was redacted and
    ALL lines of the key material (base64 body) leaked. Now it is redacted from BEGIN to the
    end (fail-closed); content BEFORE BEGIN is preserved."""
    secret = "MIIEowIBAAKCAQEA1234verysecretkeymaterial5678"
    text = f"İşte anahtar:\n-----BEGIN RSA PRIVATE KEY-----\n{secret}\nABCD9999"
    res = filter_outbound(text)
    assert "credential.private_key" in res.matched
    assert secret not in res.text  # the key body does not leak
    assert "ABCD9999" not in res.text  # everything after BEGIN is redacted
    assert res.text.startswith("İşte anahtar:")  # content before BEGIN is preserved


def test_egress_redacts_credential_value_on_next_line() -> None:
    """Security-review leak #1: a credential LABEL over its VALUE.

    ``credential.assignment`` required the value on the SAME line as ``:``/``=`` →
    a "label over value" layout leaked. Two forms are covered now:
    (a) ``password:\\n<secret>`` — direct ``[:=]`` then newline then value;
    (b) ``Your password is:\\n<secret>`` — connective words ("is") between the
    label and ``:`` (caught by ``credential.label_value``). Only the value line is
    crossed; lines after the value are preserved."""
    # (a) direct label:value newline value
    direct = filter_outbound("password:\nhunter2longsecret")
    assert "hunter2longsecret" not in direct.text
    assert "credential.assignment" in direct.matched
    # (b) words between the label and the colon, value on the next line
    natural = filter_outbound("Your password is:\nhunter2longsecret")
    assert "hunter2longsecret" not in natural.text
    assert "credential.label_value" in natural.matched
    # Lines AFTER the value line are not swallowed (over-redaction stays bounded).
    bounded = filter_outbound("API token is:\nABCdef1234567890\nNormal sonraki satır.")
    assert "ABCdef1234567890" not in bounded.text
    assert bounded.text.endswith("Normal sonraki satır.")
    # A credential label followed by ordinary prose (no high-entropy token) is NOT
    # redacted — a pure-lowercase short word on the next line is left alone.
    innocent = filter_outbound("the password\nyesterday everyone went home")
    assert innocent.matched == ()


def test_egress_redacts_aws_access_key_id() -> None:
    """Security-review leak #2: a bare AWS access key id (``AKIA…``) passed through.

    The ``AKIA`` + 16 upper/digit shape is masked unconditionally (no assignment
    or header context required), since an LLM may echo it in plain prose."""
    akia = "AKIA" + "A" * 16  # synthetic key id (no real credential)
    res = filter_outbound(f"Buyrun anahtar: {akia} bu kadar")
    assert akia not in res.text
    assert "credential.aws_access_key_id" in res.matched
    assert res.text.startswith("Buyrun anahtar:")  # surrounding prose preserved
    # The canonical AWS example key id is also caught.
    assert "credential.aws_access_key_id" in filter_outbound("AKIAIOSFODNN7EXAMPLE").matched


def test_egress_redacts_slack_webhook_url() -> None:
    """Security-review leak #2: a Slack incoming-webhook URL passed through.

    Possession of the full ``hooks.slack.com/services/…`` URL lets anyone post to
    the workspace, so the URL (including the secret path segment) is masked."""
    url = "https://hooks.slack.com/services/T00000000/B00000000/abcDEF1234567890ghIJKL"
    res = filter_outbound(f"Webhook: {url} kullan")
    assert url not in res.text
    assert "hooks.slack.com/services" not in res.text  # secret path gone too
    assert "credential.slack_webhook" in res.matched


def test_egress_redacts_bare_high_entropy_secret() -> None:
    """Security-review leak #2: a bare ≥32-char hex / ≥40-char base64 secret leaked.

    A random-looking long blob (hashes, API secrets, AWS secret keys) is masked by
    the gated generic rule, while non-random long strings are NOT (false-positive
    guard via the entropy validator)."""
    sha = "a1b2c3d4e5f6a7b8" * 4  # 64 hex chars, mixed letters+digits
    res = filter_outbound(f"hash {sha} son")
    assert sha not in res.text
    assert "credential.high_entropy" in res.matched
    # AWS secret access key (40-char base64 with / and +) is caught by the generic rule.
    secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    assert "wJalrXUtnFEMI" not in filter_outbound(f"key={secret}").text
    # FALSE-POSITIVE GUARD: a long single-character run is left alone (not random).
    assert filter_outbound("x" * 50 + " kalsin").matched == ()


# -- registry -----------------------------------------------------------------


class FakeConnector:
    connector_id = "fake"

    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.sent: list[OutboundMessage] = []
        self.queue: asyncio.Queue[InboundMessage] | None = None

    async def start(self, inbound: asyncio.Queue[InboundMessage]) -> None:
        self.started = True
        self.queue = inbound

    async def stop(self) -> None:
        self.stopped = True

    async def send(self, message: OutboundMessage) -> None:
        self.sent.append(message)

    def status(self) -> dict:
        return {"id": self.connector_id, "running": self.started}


def test_registry_register_and_duplicate() -> None:
    reg = ConnectorRegistry()
    fake = FakeConnector()
    reg.register(fake)
    assert reg.connector_ids == ("fake",)
    assert reg.get("fake") is fake
    with pytest.raises(ValueError):
        reg.register(FakeConnector())


def test_registry_start_stop_send_status() -> None:
    reg = ConnectorRegistry()
    fake = FakeConnector()
    reg.register(fake)

    async def run() -> None:
        await reg.start_all()
        assert fake.started and fake.queue is reg.inbound
        await reg.send(OutboundMessage("fake", "1", "selam"))
        with pytest.raises(KeyError):
            await reg.send(OutboundMessage("yok", "1", "x"))
        await reg.stop_all()

    asyncio.run(run())
    assert fake.sent[0].text == "selam"
    assert fake.stopped
    assert reg.status() == [{"id": "fake", "running": True}]


def test_registry_send_redacts_at_the_seam(tmp_path: Path) -> None:
    """Security-review leak #3: ``filter_outbound`` only ran in ``InboundRouter.handle`` →
    any OTHER sender that reached a connector (reminders/proactive/future) bypassed it.

    The egress filter now lives in ``registry.send`` (the single send seam), so EVERY
    outbound message is scrubbed unconditionally — even one handed straight to
    ``send`` without going through the router. An audit line is written too."""
    reg = ConnectorRegistry(_settings(tmp_path))
    fake = FakeConnector()
    reg.register(fake)
    akia = "AKIA" + "A" * 16

    asyncio.run(
        reg.send(OutboundMessage("fake", "42", f"Reminder: your key is {akia} ok"))
    )
    assert akia not in fake.sent[0].text  # the secret never reaches the channel
    assert REDACTION in fake.sent[0].text
    assert "connector_egress_filtered" in _audit_kinds(tmp_path)


def test_registry_send_to_redacts_reminder_path(tmp_path: Path) -> None:
    """``send_to`` (ScheduleEngine reminders / internal consumers) is filtered too.

    It now routes its chunks through ``registry.send``, so a credential in a
    reminder body is redacted exactly like a router reply — closing the proactive
    bypass. Clean text is delivered verbatim (no over-redaction)."""
    reg = ConnectorRegistry(_settings(tmp_path))
    fake = FakeConnector()
    reg.register(fake)

    async def run() -> None:
        await reg.send_to("fake", "42", "password: hunter2longsecret leaked")
        await reg.send_to("fake", "42", "Toplantın yarın saat 15:00'te.")  # clean

    asyncio.run(run())
    assert "hunter2longsecret" not in fake.sent[0].text
    assert REDACTION in fake.sent[0].text
    assert fake.sent[1].text == "Toplantın yarın saat 15:00'te."  # untouched


def test_registry_send_no_double_redaction_for_clean_text(tmp_path: Path) -> None:
    """Already-clean (or router-pre-filtered) text passes the seam verbatim with NO audit.

    The router filters first for archive consistency; re-filtering its clean output
    at the seam is idempotent → no duplicate ``connector_egress_filtered`` line and
    no mangled text."""
    reg = ConnectorRegistry(_settings(tmp_path))
    fake = FakeConnector()
    reg.register(fake)

    asyncio.run(reg.send(OutboundMessage("fake", "42", "Tamam, not ettim.")))
    assert fake.sent[0].text == "Tamam, not ettim."
    assert "connector_egress_filtered" not in _audit_kinds(tmp_path)


def test_build_registry_respects_enabled_flag(tmp_path: Path) -> None:
    assert build_registry(_settings(tmp_path, telegram_enabled=False)).connector_ids == ()
    reg = build_registry(_settings(tmp_path))
    assert reg.connector_ids == ("telegram",)
    assert isinstance(reg.get("telegram"), TelegramConnector)


# -- inbound router (LLM → egress → send) -----------------------------


def _msg(text: str) -> InboundMessage:
    return InboundMessage(connector_id="fake", chat_id="42", text=text)


def _router(tmp_path: Path, complete) -> tuple[InboundRouter, FakeConnector]:
    reg = ConnectorRegistry()
    fake = FakeConnector()
    reg.register(fake)
    router = InboundRouter(_settings(tmp_path), reg, complete=complete)
    return router, fake


def test_router_allow_path_sends_llm_reply(tmp_path: Path) -> None:
    calls: list[str] = []

    async def fake_complete(settings, text: str) -> str:
        calls.append(text)
        return "Tabii, not ettim."

    router, fake = _router(tmp_path, fake_complete)
    sent = asyncio.run(router.handle(_msg("süt almayı hatırlat")))
    assert sent == "Tabii, not ettim."
    assert calls == ["süt almayı hatırlat"]
    assert fake.sent[0].chat_id == "42"
    assert fake.sent[0].text == "Tabii, not ettim."


def test_router_llm_error_returns_turkish_message(tmp_path: Path) -> None:
    async def boom(settings, text: str) -> str:
        raise RuntimeError("bridge down: secret-trace-detayı")

    router, fake = _router(tmp_path, boom)
    sent = asyncio.run(router.handle(_msg("selam")))
    assert sent == LLM_ERROR_REPLY
    assert "secret-trace" not in fake.sent[0].text  # exception detail does not leak


def test_router_empty_llm_reply_falls_back(tmp_path: Path) -> None:
    async def empty(settings, text: str) -> str:
        return "   "

    router, _fake = _router(tmp_path, empty)
    assert asyncio.run(router.handle(_msg("selam"))) == EMPTY_REPLY


def test_router_egress_filter_applies_and_audits(tmp_path: Path) -> None:
    async def leaky(settings, text: str) -> str:
        return "İşte anahtarın: api_key = sk-live-XYZ9999"

    router, fake = _router(tmp_path, leaky)
    sent = asyncio.run(router.handle(_msg("anahtarı söyle")))
    assert "sk-live-XYZ9999" not in sent
    assert REDACTION in fake.sent[0].text
    assert "connector_egress_filtered" in _audit_kinds(tmp_path)


def test_router_send_failure_does_not_raise(tmp_path: Path) -> None:
    async def ok(settings, text: str) -> str:
        return "cevap"

    class BrokenConnector(FakeConnector):
        async def send(self, message: OutboundMessage) -> None:
            raise ConnectorSendError("telegram down")

    reg = ConnectorRegistry()
    reg.register(BrokenConnector())
    router = InboundRouter(_settings(tmp_path), reg, complete=ok)
    msg = InboundMessage(connector_id="fake", chat_id="42", text="selam")
    assert asyncio.run(router.handle(msg)) == "cevap"  # the exception did not leak


def test_router_run_loop_consumes_queue(tmp_path: Path) -> None:
    async def ok(settings, text: str) -> str:
        return f"yanıt: {text}"

    router, fake = _router(tmp_path, ok)

    async def run() -> None:
        router.start()
        assert router.running
        await router._registry.inbound.put(_msg("merhaba"))
        for _ in range(100):
            if fake.sent:
                break
            await asyncio.sleep(0.01)
        await router.stop()

    asyncio.run(run())
    assert fake.sent and fake.sent[0].text == "yanıt: merhaba"
    assert not router.running


def test_router_distinct_chats_processed_in_parallel(tmp_path: Path) -> None:
    """Regression (concurrency): DIFFERENT conversations are processed in parallel — while
    one is waiting in the LLM the other must be able to start. The old serial design would
    DEADLOCK this test: the 2nd conversation never starts until the 1st's reply returns; the 1st waits on the 2nd."""
    both_in = asyncio.Event()
    entered: list[str] = []

    async def gated(settings, text: str, **kw) -> str:
        entered.append(text)
        if len(entered) >= 2:
            both_in.set()  # both conversations entered complete = proof of parallelism
        await both_in.wait()  # wait until the two meet (deadlock if serial)
        return f"yanıt: {text}"

    router, fake = _router(tmp_path, gated)

    async def run() -> None:
        router.start()
        await router._registry.inbound.put(
            InboundMessage(connector_id="fake", chat_id="1", text="bir")
        )
        await router._registry.inbound.put(
            InboundMessage(connector_id="fake", chat_id="2", text="iki")
        )
        # Both must enter complete and open the event; if processed serially it times out.
        await asyncio.wait_for(both_in.wait(), timeout=2.0)
        for _ in range(200):
            if len(fake.sent) >= 2:
                break
            await asyncio.sleep(0.01)
        await router.stop()

    asyncio.run(run())
    assert {m.text for m in fake.sent} == {"yanıt: bir", "yanıt: iki"}
    assert not router.running


def test_router_same_chat_stays_sequential(tmp_path: Path) -> None:
    """Messages of the same conversation are FIFO + DO NOT OVERLAP: the next turn does not
    start until one finishes completely (history consistency). If they interleave, `overlap` catches it."""
    active = 0
    overlap = False
    order: list[str] = []

    async def serialized(settings, text: str, **kw) -> str:
        nonlocal active, overlap
        active += 1
        if active > 1:
            overlap = True
        order.append(text)
        await asyncio.sleep(0.02)  # while a turn "runs" the second message must not overtake
        active -= 1
        return f"yanıt: {text}"

    router, fake = _router(tmp_path, serialized)

    async def run() -> None:
        router.start()
        for t in ("bir", "iki", "üç"):
            await router._registry.inbound.put(
                InboundMessage(connector_id="fake", chat_id="9", text=t)
            )
        for _ in range(300):
            if len(fake.sent) >= 3:
                break
            await asyncio.sleep(0.01)
        await router.stop()

    asyncio.run(run())
    assert overlap is False  # NO concurrent turn in the same conversation
    assert order == ["bir", "iki", "üç"]  # arrival order is preserved
    assert [m.text for m in fake.sent] == ["yanıt: bir", "yanıt: iki", "yanıt: üç"]


def test_external_stop_cancels_turn_but_worker_survives(tmp_path: Path) -> None:
    """D2: an external STOP/reset cancels the registered turn handle. That handle must be a
    per-TURN child task — NOT the long-lived conversation worker. If the worker were cancelled
    the chat would ZOMBIE (every future message silently dropped). Here STOP cancels turn 1
    and a follow-up message on the SAME chat must still be processed."""
    from contextlib import asynccontextmanager

    registered: dict[str, asyncio.Task] = {}
    entered = asyncio.Event()
    release = asyncio.Event()

    @asynccontextmanager
    async def guard(conversation_id):
        cid = (conversation_id or "").strip()
        if not cid:
            yield None
            return

        def register_turn(task: asyncio.Task) -> None:
            registered[cid] = task  # the per-turn child task = the cancel handle

        try:
            yield register_turn
        finally:
            registered.pop(cid, None)

    async def gated(settings, text: str, **kw) -> str:
        if text == "ilk":
            entered.set()
            await release.wait()  # block turn 1 until cancelled (release is never set)
        return f"yanıt: {text}"

    reg = ConnectorRegistry()
    fake = FakeConnector()
    reg.register(fake)
    router = InboundRouter(_settings(tmp_path), reg, complete=gated, turn_guard=guard)
    # No persistence in this harness → _conversation_for would return None (guard yields no
    # register-turn callback). Pin a conversation id so the per-turn child-task path is taken.
    # _conversation_for is async (awaited in the worker) → the stub MUST be async too, otherwise
    # `await "conv-77"` raises TypeError and the STOP-cancellation coverage silently dies.
    async def _pinned_conv(_msg: object) -> str:
        return "conv-77"

    router._conversation_for = _pinned_conv  # type: ignore[assignment]

    async def run() -> None:
        router.start()
        await router._registry.inbound.put(
            InboundMessage(connector_id="fake", chat_id="77", text="ilk")
        )
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        assert registered, "the per-turn child task must be registered as the cancel handle"
        _cid, turn_task = next(iter(registered.items()))
        turn_task.cancel()  # simulate an external STOP cancelling the registered handle
        # Follow-up on the SAME chat → same worker. If the worker had been cancelled (the bug),
        # this would never be processed.
        await router._registry.inbound.put(
            InboundMessage(connector_id="fake", chat_id="77", text="ikinci")
        )
        for _ in range(300):
            if any(m.text == "yanıt: ikinci" for m in fake.sent):
                break
            await asyncio.sleep(0.01)
        await router.stop()

    asyncio.run(run())
    assert any(m.text == "yanıt: ikinci" for m in fake.sent), (
        "worker zombied after STOP — the follow-up message on the same chat was dropped"
    )


# -- TelegramConnector (fake API; no real network) ------------------------------


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
    """httpx.MockTransport handler — getUpdates queue + sendMessage recording."""

    def __init__(self, updates: list[list[dict]] | None = None) -> None:
        self.update_batches = list(updates or [])
        self.sent: list[dict] = []
        self.seen_paths: list[str] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.seen_paths.append(request.url.path)
        if request.url.path.endswith("/getUpdates"):
            batch = self.update_batches.pop(0) if self.update_batches else []
            return httpx.Response(200, json={"ok": True, "result": batch})
        if request.url.path.endswith("/sendMessage"):
            self.sent.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        return httpx.Response(404, json={"ok": False})  # pragma: no cover


def _drain(queue: asyncio.Queue, *, tries: int = 200):
    async def inner():
        for _ in range(tries):
            if not queue.empty():
                return queue.get_nowait()
            await asyncio.sleep(0.005)
        return None

    return inner()


def test_telegram_poll_allowed_chat_reaches_queue(tmp_path: Path) -> None:
    api = FakeTelegramAPI(updates=[[_update(1, 42, "merhaba akana")]])
    conn = TelegramConnector(
        _settings(tmp_path), transport=api.transport(), poll_timeout=0, error_backoff=0
    )

    async def run() -> InboundMessage | None:
        queue: asyncio.Queue[InboundMessage] = asyncio.Queue()
        await conn.start(queue)
        msg = await _drain(queue)
        await conn.stop()
        return msg

    msg = asyncio.run(run())
    assert msg is not None
    assert msg.connector_id == "telegram"
    assert msg.chat_id == "42"
    assert msg.text == "merhaba akana"
    assert msg.sender_name == "Alice"


def test_telegram_disallowed_chat_silently_ignored_with_audit(tmp_path: Path) -> None:
    api = FakeTelegramAPI(updates=[[_update(1, 666, "ben yabancıyım")]])
    conn = TelegramConnector(
        _settings(tmp_path), transport=api.transport(), poll_timeout=0, error_backoff=0
    )

    async def run() -> bool:
        queue: asyncio.Queue[InboundMessage] = asyncio.Queue()
        await conn.start(queue)
        await asyncio.sleep(0.05)
        await conn.stop()
        return queue.empty()

    assert asyncio.run(run()) is True  # did not land in the queue
    assert api.sent == []  # silent: no reply was sent either
    events = [e for e in audit.read_tail(tmp_path) if e.get("kind") == "connector_chat_denied"]
    assert events and events[0]["data"]["chat_id"] == "666"


def test_telegram_send_posts_to_api(tmp_path: Path) -> None:
    api = FakeTelegramAPI()
    conn = TelegramConnector(_settings(tmp_path), transport=api.transport())

    asyncio.run(conn.send(OutboundMessage("telegram", "42", "selam!")))
    assert api.sent == [{"chat_id": "42", "text": "selam!"}]
    assert any("/bottok-ENV/sendMessage" in p for p in api.seen_paths)


def test_telegram_send_failure_raises_connector_error(tmp_path: Path) -> None:
    def deny(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"ok": False, "description": "forbidden"})

    conn = TelegramConnector(_settings(tmp_path), transport=httpx.MockTransport(deny))
    with pytest.raises(ConnectorSendError):
        asyncio.run(conn.send(OutboundMessage("telegram", "42", "x")))


def test_telegram_without_token_does_not_start(tmp_path: Path) -> None:
    conn = TelegramConnector(_settings(tmp_path, telegram_bot_token=None))

    async def run() -> dict:
        await conn.start(asyncio.Queue())
        status = conn.status()
        await conn.stop()
        return status

    status = asyncio.run(run())
    assert status["running"] is False
    assert status["token_set"] is False
    assert "token" in (status["last_error"] or "")


def test_telegram_empty_allowlist_does_not_start(tmp_path: Path) -> None:
    conn = TelegramConnector(_settings(tmp_path, telegram_allowed_chat_ids=()))

    async def run() -> dict:
        await conn.start(asyncio.Queue())
        status = conn.status()
        await conn.stop()
        return status

    status = asyncio.run(run())
    assert status["running"] is False
    assert "allowlist" in (status["last_error"] or "")


def test_telegram_token_secret_store_wins_over_env(tmp_path: Path) -> None:
    settings = _settings(tmp_path, telegram_bot_token="tok-ENV")
    assert resolve_bot_token(settings) == "tok-ENV"
    set_secrets(tmp_path, {"telegram_bot_token": "tok-STORE-1234"})
    assert resolve_bot_token(settings) == "tok-STORE-1234"


def test_telegram_status_never_leaks_token(tmp_path: Path) -> None:
    conn = TelegramConnector(_settings(tmp_path))
    assert "tok-ENV" not in json.dumps(conn.status())


class _BlockingTelegramAPI:
    """Simulates the getUpdates long-poll: it hangs until the request is cancelled.

    Evidence: getUpdates polling CONTINUED after shutdown. This transport creates a
    hanging long-poll; ``stop()`` must cancel the task + the httpx request — otherwise
    the test below gets stuck on the ``wait_for`` timeout.
    """

    def __init__(self) -> None:
        self.poll_started = asyncio.Event()
        self.poll_count = 0

    def transport(self) -> httpx.MockTransport:
        async def _handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/getUpdates"):
                self.poll_count += 1
                self.poll_started.set()
                # Hang like a real long-poll — resolved only by cancellation.
                await asyncio.sleep(3600)
            return httpx.Response(200, json={"ok": True, "result": []})

        return httpx.MockTransport(_handle)


def test_telegram_stop_cancels_inflight_long_poll(tmp_path: Path) -> None:
    """stop() with a hanging long-poll: the task is cleanly cancelled (no leak)."""
    api = _BlockingTelegramAPI()
    conn = TelegramConnector(
        _settings(tmp_path), transport=api.transport(), poll_timeout=25, error_backoff=0
    )

    async def run() -> None:
        await conn.start(asyncio.Queue())
        # Make sure the long-poll has started (a hanging getUpdates).
        await asyncio.wait_for(api.poll_started.wait(), timeout=2.0)
        task = conn._task
        assert task is not None and not task.done()
        # stop() must cancel the in-flight request + task within a 10s budget.
        await asyncio.wait_for(conn.stop(), timeout=5.0)
        assert task.done()  # the poll stopped — does not loop after shutdown
        assert conn._task is None

    asyncio.run(run())


def test_telegram_stop_idempotent_when_not_started(tmp_path: Path) -> None:
    """stop() on a connector that was never started is a safe no-op."""
    conn = TelegramConnector(_settings(tmp_path))
    asyncio.run(conn.stop())  # must not raise


def test_parse_allowed_chat_ids() -> None:
    assert parse_telegram_allowed_chat_ids("") == ()
    assert parse_telegram_allowed_chat_ids(" 42, -100123 ,7 ") == ("42", "-100123", "7")


# -- REST: GET /api/v1/connectors -----------------------------------------------


def _app_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    monkeypatch.setenv("AKANA_TELEGRAM_ENABLED", "0")
    monkeypatch.setenv("AKANA_TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("AKANA_TELEGRAM_ALLOWED_CHAT_IDS", "")
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_connectors_route_default_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _app_env(monkeypatch, tmp_path)
    with TestClient(create_app()) as c:
        r = c.get("/api/v1/connectors")
    assert r.status_code == 200
    body = r.json()
    # Telegram is always present (synthesized from settings) so the dashboard panel
    # has a stable shape even when the bridge is disabled — not running, no token.
    assert body["count"] == 1
    tg = body["connectors"][0]
    assert tg["id"] == "telegram"
    assert tg["enabled"] is False
    assert tg["running"] is False
    assert tg["token_set"] is False
    assert tg["token_hint"] is None
    assert tg["allowed_chat_ids"] == []


def test_connectors_route_enabled_without_token_reports_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Enabled but no token → polling does not start, no real network is hit; status is visible.
    _app_env(
        monkeypatch,
        tmp_path,
        AKANA_TELEGRAM_ENABLED="1",
        AKANA_TELEGRAM_ALLOWED_CHAT_IDS="42",
    )
    with TestClient(create_app()) as c:
        r = c.get("/api/v1/connectors")
    body = r.json()
    assert body["count"] == 1
    tg = body["connectors"][0]
    assert tg["id"] == "telegram"
    assert tg["enabled"] is True
    assert tg["running"] is False
    assert tg["token_set"] is False


def test_connectors_route_requires_bearer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _app_env(monkeypatch, tmp_path, AKANA_TOKEN="sekret")
    with TestClient(create_app()) as c:
        assert c.get("/api/v1/connectors", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 401
        ok = c.get(
            "/api/v1/connectors",
            headers={"Authorization": "Bearer sekret", "X-Forwarded-For": "1.2.3.4"},
        )
        assert ok.status_code == 200


# -- REST: GET/PUT /api/v1/connectors/telegram (live management) ----------------


def test_telegram_detail_snapshot_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _app_env(monkeypatch, tmp_path)
    with TestClient(create_app()) as c:
        r = c.get("/api/v1/connectors/telegram")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "telegram"
    assert body["enabled"] is False
    assert body["token_set"] is False
    assert body["token_hint"] is None
    assert body["allowed_chat_ids"] == []


def test_put_telegram_enables_and_reloads_live(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Disabled at boot; a PUT flips it on WITHOUT a restart. No token is set, so the
    # poll task never starts (no real network) — but the registry reflects the change.
    _app_env(monkeypatch, tmp_path)
    with TestClient(create_app()) as c:
        r = c.put(
            "/api/v1/connectors/telegram",
            json={"enabled": True, "allowed_chat_ids": ["42", " 43 "]},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["reloaded"] is True
        assert body["enabled"] is True
        assert body["running"] is False  # no token → polling not started
        assert body["allowed_chat_ids"] == ["42", "43"]
        # The live snapshot (no restart) reflects it.
        g = c.get("/api/v1/connectors/telegram").json()
        assert g["enabled"] is True
        assert g["allowed_chat_ids"] == ["42", "43"]


def test_put_telegram_saves_and_clears_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _app_env(monkeypatch, tmp_path)
    with TestClient(create_app()) as c:
        r = c.put(
            "/api/v1/connectors/telegram",
            json={"bot_token": "123456:ABCDEF_real_enough_token"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["token_set"] is True
        assert body["token_hint"] and body["token_hint"].startswith("…")
        # Empty string clears it.
        r2 = c.put("/api/v1/connectors/telegram", json={"bot_token": ""})
        assert r2.status_code == 200
        assert r2.json()["token_set"] is False


def test_put_telegram_rejects_placeholder_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _app_env(monkeypatch, tmp_path)
    with TestClient(create_app()) as c:
        r = c.put("/api/v1/connectors/telegram", json={"bot_token": "your-token-here"})
    assert r.status_code == 422
    err = r.json()["detail"]["error"]
    assert err["code"] == "VALIDATION"
    assert "bot_token" in err["fields"]


def test_put_telegram_empty_body_is_422(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _app_env(monkeypatch, tmp_path)
    with TestClient(create_app()) as c:
        r = c.put("/api/v1/connectors/telegram", json={})
    assert r.status_code == 422


def test_telegram_test_endpoint_no_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _app_env(monkeypatch, tmp_path)
    with TestClient(create_app()) as c:
        r = c.post("/api/v1/connectors/telegram/test")
    assert r.status_code == 400
    assert r.json()["detail"]["error"]["code"] == "NO_TOKEN"


def test_telegram_test_endpoint_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _app_env(monkeypatch, tmp_path, AKANA_TELEGRAM_BOT_TOKEN="123456:real_enough_token")

    async def _fake_verify(token: str, **_kw):
        assert token  # the resolved token is forwarded
        return {"ok": True, "id": 7, "username": "akana_bot", "first_name": "Akana"}

    monkeypatch.setattr(
        "akana_server.api.routes.connectors.verify_bot_token", _fake_verify
    )
    with TestClient(create_app()) as c:
        r = c.post("/api/v1/connectors/telegram/test")
    assert r.status_code == 200
    assert r.json()["bot"]["username"] == "akana_bot"


# -- discover: helper (one-shot getUpdates) + buffer + REST --------------------


def _updates_ok(updates: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": updates})


def _msg_update(update_id: int, chat: dict, text: str = "hi") -> dict:
    return {"update_id": update_id, "message": {"chat": chat, "text": text}}


def test_discover_chats_parses_distinct_chats() -> None:
    # Two messages from one chat collapse to a single descriptor; a group is distinct.
    updates = [
        _msg_update(1, {"id": 42, "type": "private", "first_name": "Mo", "username": "mo_x"}),
        _msg_update(2, {"id": 42, "type": "private", "first_name": "Mo", "username": "mo_x"}, "again"),
        _msg_update(3, {"id": -1001, "type": "supergroup", "title": "Team"}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert "getUpdates" in str(request.url)
        # No offset is sent → non-consuming; the real poll loop still gets these.
        assert "offset" not in request.url.params
        return _updates_ok(updates)

    chats = asyncio.run(discover_chats("tok", transport=httpx.MockTransport(handler)))
    by_id = {c["id"]: c for c in chats}
    assert set(by_id) == {"42", "-1001"}
    assert by_id["42"] == {"id": "42", "type": "private", "title": "Mo", "username": "mo_x"}
    assert by_id["-1001"]["title"] == "Team"


def test_discover_chats_skips_non_message_and_malformed() -> None:
    updates = [
        {"update_id": 1, "edited_message": {"chat": {"id": 7}}},  # not a 'message'
        {"update_id": 2, "message": {"chat": "broken", "text": "x"}},  # chat not a dict
        _msg_update(3, {"id": 9, "type": "private", "first_name": "A"}),
    ]
    chats = asyncio.run(
        discover_chats("tok", transport=httpx.MockTransport(lambda r: _updates_ok(updates)))
    )
    assert [c["id"] for c in chats] == ["9"]


def test_discover_chats_no_token_raises() -> None:
    with pytest.raises(ConnectorSendError):
        asyncio.run(discover_chats(""))


def test_discover_chats_api_not_ok_raises() -> None:
    def deny(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "Unauthorized"})

    with pytest.raises(ConnectorSendError) as ei:
        asyncio.run(discover_chats("tok", transport=httpx.MockTransport(deny)))
    assert "getUpdates failed" in str(ei.value)


def test_discover_chats_error_sanitizes_token() -> None:
    # A transport error may carry the request URL (with the token) — it must be masked.
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "fail at https://api.telegram.org/botSECRET123/getUpdates"
        )

    with pytest.raises(ConnectorSendError) as ei:
        asyncio.run(discover_chats("SECRET123", transport=httpx.MockTransport(boom)))
    assert "SECRET123" not in str(ei.value)
    assert "/bot***" in str(ei.value)


def test_connector_seen_chats_buffer_is_lru_mru_first(tmp_path: Path) -> None:
    # The poll loop feeds _record_seen_chat for EVERY chat — INCLUDING not-yet-allowed
    # ones (an empty allowlist denies both here): that is exactly what discovery needs
    # to surface. The buffer is most-recent-first; re-seeing a chat moves it to front.
    conn = TelegramConnector(_settings(tmp_path, telegram_allowed_chat_ids=()))
    asyncio.run(conn._handle_update(_msg_update(1, {"id": 42, "type": "private", "first_name": "Mo"})))
    asyncio.run(conn._handle_update(_msg_update(2, {"id": 99, "type": "group", "title": "Grp"})))
    assert [c["id"] for c in conn.recent_chats()] == ["99", "42"]
    # Re-seeing 42 refreshes it to the front.
    asyncio.run(conn._handle_update(_msg_update(3, {"id": 42, "type": "private", "first_name": "Mo"})))
    assert [c["id"] for c in conn.recent_chats()] == ["42", "99"]


def test_discover_endpoint_no_token_is_400(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _app_env(monkeypatch, tmp_path)
    with TestClient(create_app()) as c:
        r = c.post("/api/v1/connectors/telegram/discover")
    assert r.status_code == 400
    assert r.json()["detail"]["error"]["code"] == "NO_TOKEN"


def test_discover_endpoint_one_shot_annotates_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Disabled bridge (not polling) + a token set → endpoint takes the one-shot path
    # and annotates each chat against the CURRENT allowlist (42 is allowed, 99 not).
    _app_env(
        monkeypatch,
        tmp_path,
        AKANA_TELEGRAM_BOT_TOKEN="123456:real_enough_token",
        AKANA_TELEGRAM_ALLOWED_CHAT_IDS="42",
    )

    async def _fake_discover(token: str, **_kw):
        assert token
        return [
            {"id": "42", "type": "private", "title": "Mo", "username": "mo_x"},
            {"id": "99", "type": "group", "title": "Grp", "username": ""},
        ]

    monkeypatch.setattr(
        "akana_server.api.routes.connectors.discover_chats", _fake_discover
    )
    with TestClient(create_app()) as c:
        r = c.post("/api/v1/connectors/telegram/discover")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["source"] == "poll"
    assert body["count"] == 2
    allowed = {c["id"]: c["allowed"] for c in body["chats"]}
    assert allowed == {"42": True, "99": False}


def test_discover_endpoint_uses_buffer_when_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # When the poll loop is live, discovery must read the in-memory buffer (a second
    # getUpdates would 409). Inject a fake running connector to assert the branch.
    _app_env(
        monkeypatch,
        tmp_path,
        AKANA_TELEGRAM_BOT_TOKEN="123456:real_enough_token",
        AKANA_TELEGRAM_ALLOWED_CHAT_IDS="42",
    )

    def _boom(*_a, **_k):  # the one-shot path must NOT be taken
        raise AssertionError("discover_chats must not be called when running")

    monkeypatch.setattr("akana_server.api.routes.connectors.discover_chats", _boom)
    fake_conn = SimpleNamespace(
        status=lambda: {"running": True},
        recent_chats=lambda: [
            {"id": "42", "type": "private", "title": "Mo", "username": "mo_x"},
            {"id": "99", "type": "group", "title": "Grp", "username": ""},
        ],
    )
    app = create_app()
    with TestClient(app) as c:
        # Override just .get on the live registry (keep stop_all for clean shutdown).
        app.state.connector_registry.get = (
            lambda cid: fake_conn if cid == "telegram" else None
        )
        r = c.post("/api/v1/connectors/telegram/discover")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "buffer"
    assert {c["id"]: c["allowed"] for c in body["chats"]} == {"42": True, "99": False}
