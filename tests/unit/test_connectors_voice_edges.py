"""QUALITY TURN — connectors/actions/voice boundary-case + bug regression tests.

Region: akana_server/{connectors,actions,voice}/** + api/routes/{connectors,voice}.
NO real network/device (httpx.MockTransport, in-process). Two real bugs are put
under regression here:

1. Egress: the card pattern swallowed the inner 16 digits of a spaced IBAN and left a
   partial leak («TR33 [GİZLİ] 8413 26»). The pattern order was fixed to IBAN→card.
2. Telegram: when the ``chat``/``from`` field was not a dict, ``.get`` raised AttributeError
   and pushed _poll_loop into backoff, locking polling with a single broken update.
"""

from __future__ import annotations

import asyncio
import io
import json
import wave
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from akana_server import audit
from akana_server.api.app import create_app
from akana_server.connectors.base import split_text
from akana_server.connectors.egress_filter import (
    REDACTION,
    filter_outbound,
)
from akana_server.connectors.telegram import TelegramConnector
from akana_server.voice.stt import SttError, decode_wav_to_float_mono16k
from akana_server.voice.streaming_tts import (
    is_speakable_text,
    split_first_sentence,
    strip_markdown_for_tts,
)
from akana_server.voice_preferences import (
    DEFAULT_TTS_VOICE_TR,
    load_voice_preferences,
)


# ============================================================================
# EGRESS FILTER — OTP/IBAN/card variants + Turkish preserved
# ============================================================================


@pytest.mark.parametrize(
    ("text", "pattern_id"),
    [
        ("Onay kodu 482913 geldi", "otp.code"),
        ("Doğrulama kodu: 1234", "otp.code"),
        ("tek kullanımlık şifre 99887", "otp.code"),
        ("your one-time code 55012", "otp.code"),
        ("verification code is 654321", "otp.code"),
        ("auth-token = abc.def.ghi", "credential.assignment"),
        ("BEARER: sk-xyz", "credential.assignment"),
        ("şifre: gizli123", "credential.assignment"),
        ("parola=YüceParola1", "credential.assignment"),
        ("4111 1111 1111 1111", "payment.card"),
        ("4111-1111-1111-1111", "payment.card"),
        ("4111111111111111", "payment.card"),
        ("TR330006100519786457841326", "payment.iban"),
        # Security-review leak #2 — bare provider/AWS/webhook secrets in prose:
        ("key AKIA" + "A" * 16 + " here", "credential.aws_access_key_id"),
        ("AKIAIOSFODNN7EXAMPLE", "credential.aws_access_key_id"),
        (
            "https://hooks.slack.com/services/T01234567/B01234567/aBcDeF0123456789xYz",
            "credential.slack_webhook",
        ),
        ("digest " + "deadBEEF1234" * 4, "credential.high_entropy"),
        # Security-review leak #1 — credential label over a value on the next line:
        ("password:\nhunter2longsecret", "credential.assignment"),
        ("Your password is:\nhunter2longsecret", "credential.label_value"),
    ],
)
def test_egress_variant_matches(text: str, pattern_id: str) -> None:
    res = filter_outbound(text)
    assert pattern_id in res.matched
    assert REDACTION in res.text


def test_egress_spaced_iban_fully_redacted_no_partial_leak() -> None:
    """BUG REGRESSION #1: a spaced IBAN is fully redacted; the prefix/last group does not leak.

    If the card pattern (16 digits) matched BEFORE the IBAN it swallowed the inner digits
    and left «TR33 [GİZLİ] 8413 26» — the IBAN prefix + last group exposed.
    """
    text = "Hesabım TR33 0006 1005 1978 6457 8413 26 numaralı hesap"
    res = filter_outbound(text)
    assert "payment.iban" in res.matched
    assert "TR33" not in res.text
    assert "8413" not in res.text
    assert "6457" not in res.text
    assert res.text == f"Hesabım {REDACTION} numaralı hesap"


def test_egress_preserves_turkish_text() -> None:
    """Non-sensitive Turkish text (including Turkish letters) is unchanged."""
    text = "Şifreli değil! Gül bahçesinde çay içtik, İstanbul güzeldi."
    res = filter_outbound(text)
    assert res.text == text
    assert res.matched == ()


def test_egress_phone_number_not_a_false_card() -> None:
    """A Turkish phone number (10-11 digits) does not trigger the 16-digit card pattern."""
    res = filter_outbound("Beni 0555 111 22 33 numarasından ara")
    assert res.matched == ()


def test_egress_empty_and_none_safe() -> None:
    assert filter_outbound("").text == ""
    assert filter_outbound("").matched == ()
    assert filter_outbound(None).text == ""  # type: ignore[arg-type]


def test_egress_multiple_patterns_one_pass() -> None:
    # On separate LINES: each pattern triggers independently (the credential mask stops at
    # the line end, does not swallow the IBAN). Previously they were all on one line, but
    # that stopped only at the buggy ``\\S+`` token so the IBAN stayed exposed → in the safe
    # behavior (line-end mask) separate lines are required.
    res = filter_outbound(
        "Doğrulama kodu 123456\ntoken=abc\nIBAN TR330006100519786457841326"
    )
    assert {"otp.code", "credential.assignment", "payment.iban"} <= set(res.matched)


def test_egress_credential_line_with_iban_no_leak() -> None:
    """Security: even if the IBAN on the SAME line as a credential is not detected separately,
    it is swallowed by the line-end mask → does not leak (over-redaction is the safe side)."""
    res = filter_outbound("token=abc ve IBAN TR330006100519786457841326")
    assert "credential.assignment" in res.matched
    assert "TR330006100519786457841326" not in res.text
    assert "TR33" not in res.text


# ============================================================================
# split_text — channel message limit (Telegram 4096) exact boundary
# ============================================================================


def test_split_text_exact_limit_single_chunk() -> None:
    assert split_text("a" * 4096, 4096) == ["a" * 4096]


def test_split_text_one_over_limit_splits() -> None:
    chunks = split_text("a" * 4097, 4096)
    assert [len(c) for c in chunks] == [4096, 1]


def test_split_text_giant_word_hard_cut() -> None:
    chunks = split_text("x" * 25, 10)
    assert all(len(c) <= 10 for c in chunks)
    assert "".join(chunks) == "x" * 25


def test_split_text_prefers_paragraph_then_line_then_space() -> None:
    text = "Birinci paragraf.\n\nİkinci paragraf burada uzunca."
    chunks = split_text(text, 25)
    assert all(len(c) <= 25 for c in chunks)
    assert chunks[0] == "Birinci paragraf."


def test_split_text_zero_or_negative_limit_passthrough() -> None:
    assert split_text("merhaba", 0) == ["merhaba"]
    assert split_text("merhaba", -5) == ["merhaba"]


def test_split_text_empty_returns_single_empty() -> None:
    assert split_text("", 4096) == [""]


# ============================================================================
# TELEGRAM — broken update payload (chat/from non-dict), offset, duplicate
# ============================================================================


def _tg_settings(tmp_path: Path, **kw) -> SimpleNamespace:
    base = {
        "data_dir": tmp_path,
        "telegram_enabled": True,
        "telegram_bot_token": "tok",
        "telegram_allowed_chat_ids": ("42",),
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _handle(conn: TelegramConnector, update: dict) -> asyncio.Queue:
    async def run() -> asyncio.Queue:
        conn._queue = asyncio.Queue()
        conn._allowed = frozenset({"42"})
        await conn._handle_update(update)
        return conn._queue

    return asyncio.run(run())


def test_telegram_chat_non_dict_does_not_crash(tmp_path: Path) -> None:
    """BUG REGRESSION #3: if ``chat`` is a str it is silently skipped (the poll loop does not lock)."""
    conn = TelegramConnector(_tg_settings(tmp_path))
    q = _handle(conn, {"update_id": 1, "message": {"chat": "bad", "text": "hi"}})
    assert q.empty()  # no crash, and did not land in the queue


def test_telegram_from_non_dict_still_queues(tmp_path: Path) -> None:
    """BUG REGRESSION #3: if ``from`` is a str the sender is treated as empty, the message flows."""
    conn = TelegramConnector(_tg_settings(tmp_path))
    q = _handle(
        conn,
        {"update_id": 2, "message": {"chat": {"id": 42}, "text": "merhaba", "from": "x"}},
    )
    msg = q.get_nowait()
    assert msg.text == "merhaba"
    assert msg.sender_id == ""


def test_telegram_denied_chat_with_bad_from_audits_safely(tmp_path: Path) -> None:
    """Chat outside the allowlist + a broken ``from``: _audit_denied must not crash."""
    conn = TelegramConnector(_tg_settings(tmp_path))
    q = _handle(
        conn,
        {"update_id": 3, "message": {"chat": {"id": 666}, "text": "selam", "from": "bad"}},
    )
    assert q.empty()
    events = [e for e in audit.read_tail(tmp_path) if e.get("kind") == "connector_chat_denied"]
    assert events and events[0]["data"]["chat_id"] == "666"


def test_telegram_message_non_dict_ignored(tmp_path: Path) -> None:
    conn = TelegramConnector(_tg_settings(tmp_path))
    q = _handle(conn, {"update_id": 4, "message": "plain string"})
    assert q.empty()


def test_telegram_offset_advances_to_max_plus_one(tmp_path: Path) -> None:
    conn = TelegramConnector(_tg_settings(tmp_path))

    async def run() -> int:
        conn._queue = asyncio.Queue()
        conn._allowed = frozenset({"42"})
        await conn._handle_update({"update_id": 100, "message": {}})
        await conn._handle_update({"update_id": 50, "message": {}})  # old → offset does not drop
        return conn._offset

    assert asyncio.run(run()) == 101  # max(100,50)+1


def test_telegram_huge_update_id_no_overflow(tmp_path: Path) -> None:
    conn = TelegramConnector(_tg_settings(tmp_path))

    async def run() -> int:
        conn._queue = asyncio.Queue()
        conn._allowed = frozenset({"42"})
        await conn._handle_update({"update_id": 10**20, "message": {}})
        return conn._offset

    assert asyncio.run(run()) == 10**20 + 1


def test_telegram_invalid_update_id_keeps_offset(tmp_path: Path) -> None:
    conn = TelegramConnector(_tg_settings(tmp_path))

    async def run() -> int:
        conn._queue = asyncio.Queue()
        conn._allowed = frozenset({"42"})
        await conn._handle_update({"update_id": "not-a-number", "message": {}})
        return conn._offset

    assert asyncio.run(run()) == 0


def test_telegram_poll_broken_json_backs_off_then_recovers(tmp_path: Path) -> None:
    """If getUpdates returns broken JSON: the error is logged, the loop does not die, then recovers."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, content=b"not json at all")
        return httpx.Response(200, json={"ok": True, "result": []})

    conn = TelegramConnector(
        _tg_settings(tmp_path),
        transport=httpx.MockTransport(handler),
        poll_timeout=0,
        error_backoff=0,
    )

    async def run() -> str:
        await conn.start(asyncio.Queue())
        for _ in range(200):
            if calls["n"] >= 2:
                break
            await asyncio.sleep(0.005)
        err = conn._last_error
        await conn.stop()
        return err

    asyncio.run(run())
    assert calls["n"] >= 2  # the first broken response did not kill the loop


# ============================================================================
# VOICE — TTS sentence splitting (Turkish abbrev/number/emoji), STT decode limits
# ============================================================================


@pytest.mark.parametrize(
    ("buf", "expect_sentence"),
    [
        ("Dr. Ahmet geldi. Tamam.", "Dr. Ahmet geldi."),
        ("vb. örnekler aldım. Bitti.", "vb. örnekler aldım."),
        ("3. madde önemli burada. Son.", "3. madde önemli burada."),
        ("Oran 3.5 olarak hesaplandı. Son.", "Oran 3.5 olarak hesaplandı."),
        ("A. Yılmaz konuştu burada. Son.", "A. Yılmaz konuştu burada."),
    ],
)
def test_tts_split_respects_turkish_abbrev_and_numbers(
    buf: str, expect_sentence: str
) -> None:
    sentence, _rest = split_first_sentence(buf)
    assert sentence == expect_sentence


def test_tts_split_no_terminator_waits_under_hardflush() -> None:
    sentence, rest = split_first_sentence("y" * 100)
    assert sentence is None
    assert rest == "y" * 100


def test_tts_split_hard_flush_over_threshold() -> None:
    sentence, rest = split_first_sentence("z" * 125)
    assert sentence == "z" * 125
    assert rest == ""


def test_tts_strip_markdown_removes_emoji_keeps_turkish() -> None:
    out = strip_markdown_for_tts("Merhaba 😀 **dünya** 🎉 İstanbul 🇹🇷 güzel")
    assert "😀" not in out and "🎉" not in out and "🇹🇷" not in out
    assert "İstanbul" in out and "dünya" in out


def test_tts_strip_markdown_handles_code_fence() -> None:
    out = strip_markdown_for_tts("Kod:\n```python\nprint('x')\n```\nbitti")
    assert "```" not in out
    assert "bitti" in out


@pytest.mark.parametrize(
    ("text", "speakable"),
    [
        ("Merhaba dünya", True),
        ("—", False),
        ("...", False),
        ("| --- | --- |", False),
        ("Işık", True),
        ("", False),
    ],
)
def test_tts_is_speakable(text: str, speakable: bool) -> None:
    assert is_speakable_text(text) is speakable


def _wav_bytes(*, channels: int = 1, sampwidth: int = 2, framerate: int = 16000, frames: int = 1600) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(framerate)
        wf.writeframes(b"\x00\x01" * frames * channels)
    return buf.getvalue()


def test_stt_decode_empty_payload_rejected() -> None:
    with pytest.raises(SttError) as ei:
        decode_wav_to_float_mono16k(b"", max_seconds=30)
    assert ei.value.status_code == 400


def test_stt_decode_garbage_44_bytes_rejected() -> None:
    with pytest.raises(SttError):
        decode_wav_to_float_mono16k(b"X" * 44, max_seconds=30)


def test_stt_decode_8bit_unsupported_rejected() -> None:
    with pytest.raises(SttError):
        decode_wav_to_float_mono16k(_wav_bytes(sampwidth=1), max_seconds=30)


def test_stt_decode_mono_16k_ok() -> None:
    out = decode_wav_to_float_mono16k(_wav_bytes(), max_seconds=30)
    assert out.shape[0] == 1600


def test_stt_decode_stereo_downmix_ok() -> None:
    out = decode_wav_to_float_mono16k(_wav_bytes(channels=2), max_seconds=30)
    assert out.ndim == 1


def test_stt_decode_resamples_44100() -> None:
    out = decode_wav_to_float_mono16k(_wav_bytes(framerate=44100, frames=4410), max_seconds=30)
    assert out.shape[0] == 1600  # 0.1s @16k


def test_stt_decode_truncates_to_max_seconds() -> None:
    out = decode_wav_to_float_mono16k(
        _wav_bytes(framerate=16000, frames=16000), max_seconds=0.1
    )
    assert out.shape[0] == 1600  # 0.1s * 16k


# ============================================================================
# VOICE PREFERENCES — robustness to a corrupt file / invalid value
# ============================================================================


def test_voice_preferences_corrupt_file_falls_back(tmp_path: Path) -> None:
    (tmp_path / "voice_preferences.json").write_text("{bozuk json", encoding="utf-8")
    prefs = load_voice_preferences(tmp_path)
    assert prefs.tts_engine == "auto"
    assert prefs.tts_voice_tr == DEFAULT_TTS_VOICE_TR


def test_voice_preferences_non_object_root_falls_back(tmp_path: Path) -> None:
    (tmp_path / "voice_preferences.json").write_text("[1,2,3]", encoding="utf-8")
    prefs = load_voice_preferences(tmp_path)
    assert prefs.tts_engine == "auto"


def test_voice_preferences_invalid_engine_kept_default(tmp_path: Path) -> None:
    (tmp_path / "voice_preferences.json").write_text(
        json.dumps({"tts_engine": "magic-engine"}), encoding="utf-8"
    )
    prefs = load_voice_preferences(tmp_path)
    assert prefs.tts_engine == "auto"  # invalid engine → default preserved


def test_voice_preferences_missing_file_defaults(tmp_path: Path) -> None:
    prefs = load_voice_preferences(tmp_path)
    assert prefs.tts_engine == "auto"
    assert prefs.wake_autostart is False


# ============================================================================
# ROUTE — 4xx + bearer (connectors + voice)
# ============================================================================


def _app_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    monkeypatch.setenv("AKANA_TELEGRAM_ENABLED", "0")
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_route_connectors_requires_bearer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _app_env(monkeypatch, tmp_path, AKANA_TOKEN="gizli")
    # The token gate applies only to PROXIED requests; X-Forwarded-For makes the
    # request look proxied so the configured token is actually enforced.
    proxied = {"X-Forwarded-For": "1.2.3.4"}
    with TestClient(create_app()) as c:
        assert c.get("/api/v1/connectors", headers=proxied).status_code == 401
        ok = c.get(
            "/api/v1/connectors",
            headers={**proxied, "Authorization": "Bearer gizli"},
        )
        assert ok.status_code == 200


def test_route_voice_preferences_requires_bearer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _app_env(monkeypatch, tmp_path, AKANA_TOKEN="gizli")
    # The token gate applies only to PROXIED requests; X-Forwarded-For makes the
    # request look proxied so the configured token is actually enforced.
    proxied = {"X-Forwarded-For": "1.2.3.4"}
    with TestClient(create_app()) as c:
        assert c.get("/api/v1/voice/preferences", headers=proxied).status_code == 401
        ok = c.get(
            "/api/v1/voice/preferences",
            headers={**proxied, "Authorization": "Bearer gizli"},
        )
        assert ok.status_code == 200


def test_route_voice_tts_validates_empty_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Empty text becomes 422 via pydantic min_length=1 (no LLM/network call)."""
    _app_env(monkeypatch, tmp_path)
    with TestClient(create_app()) as c:
        r = c.post("/api/v1/voice/tts", json={"text": ""})
        assert r.status_code == 422
