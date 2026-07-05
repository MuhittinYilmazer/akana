"""Streaming TTS helpers — sentence split, speakability filter, markdown strip,
engine registry fallback (fake error injection; no real audio/network/model)."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import Any

import pytest

from akana_server.config import load_settings
from akana_server.voice.engines import VoiceSelection
from akana_server.voice.engines import base as engines_base
from akana_server.voice.streaming_tts import (
    is_speakable_text,
    resolve_voice_selection,
    split_first_sentence,
    stream_text_to_tts_chunks,
    strip_markdown_for_tts,
    synthesize_with_fallback,
)
from akana_server.voice.tts import TtsError, TtsTimeout
from akana_server.voice_preferences import save_voice_preferences, update_voice_preferences


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Tamam.", True),
        ("| Ad | Alice |", True),
        ("—", False),
        ("...", False),
        ("|-------|-------|", False),
        ("", False),
        ("  ", False),
        ("a", False),
        ("ab", True),
    ],
)
def test_is_speakable_text(text: str, expected: bool) -> None:
    assert is_speakable_text(text) is expected


def test_strip_markdown_table_rule_still_speakable_when_has_words() -> None:
    spoken = strip_markdown_for_tts("**CEN222** — en riskli.")
    assert is_speakable_text(spoken)


# ── split_first_sentence: Turkish abbreviations, numbers, short-sentence lock ─────


@pytest.mark.parametrize(
    "buf,sentence,remainder",
    [
        # A short first sentence ("Evet.") does not lock the stream — it merges into the next boundary.
        (
            "Evet. Şimdi uzun bir açıklama geliyor ve devam ediyor. Kalan",
            "Evet. Şimdi uzun bir açıklama geliyor ve devam ediyor.",
            "Kalan",
        ),
        # The "Dr." abbreviation is not a sentence end (the old code locked the stream entirely).
        ("Dr. Ayşe Hanım bugün geldi. Sonra gitti.", "Dr. Ayşe Hanım bugün geldi.", "Sonra gitti."),
        # "vb." in the middle — the sentence is not split in two.
        (
            "Elma, armut vb. meyveleri aldım. Sonra eve döndüm.",
            "Elma, armut vb. meyveleri aldım.",
            "Sonra eve döndüm.",
        ),
        # The decimal number "3.5" is not split.
        ("Oran 3.5 olarak ölçüldü. Sonra arttı.", "Oran 3.5 olarak ölçüldü.", "Sonra arttı."),
        # The ordinal "3. madde" (continues in lowercase) is not split.
        ("Detay için 3. maddeye bak. Sonra devam et.", "Detay için 3. maddeye bak.", "Sonra devam et."),
        # Normal behavior is preserved.
        ("Merhaba dünya. Nasılsın", "Merhaba dünya.", "Nasılsın"),
    ],
)
def test_split_first_sentence_turkish(buf: str, sentence: str, remainder: str) -> None:
    got_sentence, got_remainder = split_first_sentence(buf)
    assert got_sentence == sentence
    assert got_remainder.strip() == remainder.strip()


def test_split_waits_when_no_full_sentence_yet() -> None:
    assert split_first_sentence("Dr. Ayşe Hanım") == (None, "Dr. Ayşe Hanım")
    assert split_first_sentence("Evet. Şimdi") == (None, "Evet. Şimdi")


def test_split_hard_flush_long_text_without_terminator() -> None:
    buf = "x" * 130
    assert split_first_sentence(buf) == (buf, "")
    # Long terminator-less text starting with an abbreviation is also flushed.
    buf2 = "Dr. " + "a" * 130
    assert split_first_sentence(buf2) == (buf2, "")


def test_split_short_final_sentence_without_remainder_kept() -> None:
    assert split_first_sentence("Tamam. ") == ("Tamam.", "")


# ── strip_markdown_for_tts: leftover fence + emoji stripping ────────────────────


def test_strip_orphan_code_fence_not_spoken() -> None:
    # Sentence splitting can cut a code block in half; a fence line left on its own
    # ("```python") should not be spoken.
    assert strip_markdown_for_tts("```python") == ""
    assert not is_speakable_text(strip_markdown_for_tts("```python"))


def test_strip_full_code_fence_keeps_content() -> None:
    out = strip_markdown_for_tts("Şöyle yap:\n```python\nprint(1)\n```\nBitti.")
    assert "```" not in out
    assert "print(1)" in out and "Bitti." in out


def test_strip_removes_emoji_keeps_turkish() -> None:
    out = strip_markdown_for_tts("Harika oldu 🎉🚀✨ devam!")
    assert "🎉" not in out and "🚀" not in out
    assert "Harika oldu" in out and "devam!" in out
    assert strip_markdown_for_tts("Şu ığüşöç İIıi metni aynen kalmalı.") == (
        "Şu ığüşöç İIıi metni aynen kalmalı."
    )


def test_emoji_only_sentence_not_speakable() -> None:
    assert not is_speakable_text(strip_markdown_for_tts("🎉🎉 ✅"))


# ── Engine registry: fallback to Piper via fake error injection ───────────────────


class _FakeEngine:
    def __init__(
        self,
        name: str,
        *,
        ok: bool = True,
        available: bool = True,
        exc: type[TtsError] = TtsError,
    ) -> None:
        self.name = name
        self.ok = ok
        self._available = available
        self._exc = exc
        self.calls: list[str] = []

    def available(self) -> bool:  # type: ignore[override]
        return self._available

    def default_voice(self, lang: str) -> str:
        return f"{self.name}-{lang}"

    def synthesize(self, text: str, voice: str) -> tuple[bytes, str]:
        self.calls.append(text)
        if not self.ok:
            raise self._exc(f"{self.name} bilerek patladı", status_code=503)
        return f"{self.name}:{text}".encode(), "audio/wav"

    def list_voices(self) -> list[dict[str, Any]]:
        return []


@pytest.fixture
def fake_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TTS_ENGINE", "")
    s = load_settings()
    return dataclasses.replace(
        s,
        data_dir=tmp_path,
        piper_voice_tr=tmp_path / "tr-yok.onnx",
        piper_voice_en=tmp_path / "en-yok.onnx",
        tts_engine="",
    )


def _patch_engines(
    monkeypatch: pytest.MonkeyPatch, edge: _FakeEngine, piper: _FakeEngine
) -> None:
    monkeypatch.setitem(engines_base._FACTORIES, "edge", lambda _s: edge)
    monkeypatch.setitem(engines_base._FACTORIES, "piper", lambda _s: piper)


async def _collect_chunks(deltas: list[str], settings, selection) -> list[dict[str, object]]:
    async def _iter():
        for d in deltas:
            yield d

    out: list[dict[str, object]] = []
    async for chunk in stream_text_to_tts_chunks(_iter(), settings, selection=selection):
        out.append(chunk)
    return out


def test_stream_demotes_failing_edge_to_piper(
    monkeypatch: pytest.MonkeyPatch, fake_settings
) -> None:
    edge = _FakeEngine("edge", ok=False)
    piper = _FakeEngine("piper", ok=True)
    _patch_engines(monkeypatch, edge, piper)
    chunks = asyncio.run(
        _collect_chunks(
            [
                "Birinci cümle tamam. ",
                "İkinci cümle de tamam. ",
                "Üçüncü cümle bitti.",
            ],
            fake_settings,
            VoiceSelection(engine="edge", voice="tr-TR-EmelNeural"),
        )
    )
    # The stream does not break: all three sentences come from Piper.
    assert [c["seq"] for c in chunks] == [1, 2, 3]
    assert all(c["mime"] == "audio/wav" and c["audio_b64"] for c in chunks)
    # After a demote the primary is NEVER tried again: because of prefetch, at most
    # the sentence in flight at the moment of demote also sees the primary (≤2), but the third
    # sentence must DEFINITELY not try edge — otherwise every sentence times out = 10s pause.
    assert len(edge.calls) <= 2
    assert "Üçüncü cümle bitti." not in edge.calls
    assert len(piper.calls) == 3


def test_stream_timeout_skips_sentence_keeps_edge_no_piper(
    monkeypatch: pytest.MonkeyPatch, fake_settings
) -> None:
    """TtsTimeout (edge slow, not unreachable): the sentence is skipped, edge is KEPT
    (no demote), piper DOES NOT STEP IN. Behavior distinct from unreachability (→ piper)."""

    class _FlakyEdge(_FakeEngine):
        """Times out on the first sentence, succeeds on the rest."""

        def synthesize(self, text: str, voice: str) -> tuple[bytes, str]:
            self.calls.append(text)
            if len(self.calls) == 1:
                raise TtsTimeout("edge yavaş", status_code=503)
            return f"{self.name}:{text}".encode(), "audio/wav"

    edge = _FlakyEdge("edge")
    piper = _FakeEngine("piper", ok=True)
    _patch_engines(monkeypatch, edge, piper)
    chunks = asyncio.run(
        _collect_chunks(
            [
                "Birinci cümle tamam. ",
                "İkinci cümle de tamam. ",
                "Üçüncü cümle bitti.",
            ],
            fake_settings,
            VoiceSelection(engine="edge", voice="tr-TR-EmelNeural"),
        )
    )
    # First sentence skipped (timeout) → only two chunks emitted (seq is not skipped,
    # counts emitted chunks). The remaining two came from EDGE (base64 starts with "edge:").
    import base64

    assert [c["seq"] for c in chunks] == [1, 2]
    assert all(c["mime"] == "audio/wav" for c in chunks)
    assert all(
        base64.b64decode(c["audio_b64"]).startswith(b"edge:")  # type: ignore[arg-type]
        for c in chunks
    )
    # Edge was NOT demoted: tried on all three sentences; piper was never called.
    assert len(edge.calls) == 3
    assert piper.calls == []


def test_one_shot_timeout_does_not_fall_to_piper(
    monkeypatch: pytest.MonkeyPatch, fake_settings
) -> None:
    """One-shot: TtsTimeout does not fall back to robotic piper — the error propagates up."""
    edge = _FakeEngine("edge", ok=False, exc=TtsTimeout)
    piper = _FakeEngine("piper", ok=True)
    _patch_engines(monkeypatch, edge, piper)
    with pytest.raises(TtsTimeout):
        asyncio.run(
            synthesize_with_fallback(
                "Merhaba dünya.",
                fake_settings,
                selection=VoiceSelection(engine="edge", voice="v"),
            )
        )
    assert piper.calls == []


class _TimedEngine:
    """Fake engine with a synthesis duration; records start/end events."""

    def __init__(self, name: str, *, synth_delay: float, log: list[str]) -> None:
        self.name = name
        self._delay = synth_delay
        self._log = log
        self.calls: list[str] = []

    def available(self) -> bool:
        return True

    def default_voice(self, lang: str) -> str:
        return f"{self.name}-{lang}"

    def synthesize(self, text: str, voice: str) -> tuple[bytes, str]:
        # Blocking engine (in reality runs inside to_thread) — via time.sleep.
        import time as _t

        self.calls.append(text)
        self._log.append(f"synth_start:{text[:6]}")
        _t.sleep(self._delay)
        self._log.append(f"synth_end:{text[:6]}")
        return f"{self.name}:{text}".encode(), "audio/wav"

    def list_voices(self) -> list[dict[str, Any]]:
        return []


def test_stream_prefetches_next_sentence_during_consumption(
    monkeypatch: pytest.MonkeyPatch, fake_settings
) -> None:
    """Bug «stalls for 10s»: prefetch — while one chunk is being consumed (played),
    synthesis of the next sentence MUST START in the background. Event order: the second
    sentence's synth_start must be seen while the consumer is processing the first chunk,
    NOT after the first chunk reaches the consumer (overlap)."""
    timeline: list[str] = []
    edge = _TimedEngine("edge", synth_delay=0.05, log=timeline)
    piper = _FakeEngine("piper", ok=True)
    _patch_engines(monkeypatch, edge, piper)

    async def _run() -> None:
        async def _iter():
            yield "Birinci cümle tamam. "
            yield "İkinci cümle de tamam. "
            yield "Üçüncü cümle bitti."

        seen = 0
        async for chunk in stream_text_to_tts_chunks(
            _iter(), fake_settings, selection=VoiceSelection(engine="edge", voice="v")
        ):
            seen += 1
            timeline.append(f"consume:{seen}")
            # The consumer (browser playback) takes time — meanwhile synthesis of the
            # next sentence must already have started.
            await asyncio.sleep(0.03)

    asyncio.run(_run())

    # While the first chunk is being consumed ("consume:1") synthesis of the second sentence must have started.
    consume1 = timeline.index("consume:1")
    second_start = next(
        i for i, ev in enumerate(timeline) if ev.startswith("synth_start") and "İkinci"[:6] in ev
    )
    assert second_start < consume1, (
        f"no prefetch — the second synthesis started after the first chunk was consumed: {timeline}"
    )
    assert len(edge.calls) == 3


def test_stream_early_abort_cancels_prefetched_task(
    monkeypatch: pytest.MonkeyPatch, fake_settings
) -> None:
    """If the consumer closes early (abort) the in-flight prefetch task is cancelled,
    leaving no dangling task / unhandled exception."""
    edge = _FakeEngine("edge", ok=True)
    piper = _FakeEngine("piper", ok=True)
    _patch_engines(monkeypatch, edge, piper)

    async def _run() -> None:
        async def _iter():
            yield "Birinci cümle tamam. "
            yield "İkinci cümle de tamam. "
            yield "Üçüncü cümle bitti."

        gen = stream_text_to_tts_chunks(
            _iter(), fake_settings, selection=VoiceSelection(engine="edge", voice="v")
        )
        # Take only the first chunk, then close the generator (abort).
        first = await gen.__anext__()
        assert first["seq"] == 1
        await gen.aclose()

    asyncio.run(_run())  # If aclose returns cleanly, there is no dangling task.


def test_stream_keeps_edge_when_it_works(
    monkeypatch: pytest.MonkeyPatch, fake_settings
) -> None:
    edge = _FakeEngine("edge", ok=True)
    piper = _FakeEngine("piper", ok=True)
    _patch_engines(monkeypatch, edge, piper)
    chunks = asyncio.run(
        _collect_chunks(
            ["Merhaba dünya, bu bir test."],
            fake_settings,
            VoiceSelection(engine="edge", voice="v"),
        )
    )
    assert len(chunks) == 1
    assert len(edge.calls) == 1 and not piper.calls


def test_one_shot_fallback_and_double_failure(
    monkeypatch: pytest.MonkeyPatch, fake_settings
) -> None:
    edge = _FakeEngine("edge", ok=False)
    piper = _FakeEngine("piper", ok=True)
    _patch_engines(monkeypatch, edge, piper)
    audio, mime = asyncio.run(
        synthesize_with_fallback(
            "Merhaba dünya.", fake_settings, selection=VoiceSelection(engine="edge", voice="v")
        )
    )
    assert audio.startswith(b"piper:") and mime == "audio/wav"

    # If both fail, TtsError propagates up (not silently swallowed).
    piper_dead = _FakeEngine("piper", ok=False)
    _patch_engines(monkeypatch, edge, piper_dead)
    with pytest.raises(TtsError):
        asyncio.run(
            synthesize_with_fallback(
                "Merhaba dünya.",
                fake_settings,
                selection=VoiceSelection(engine="edge", voice="v"),
            )
        )


def test_resolve_returns_piper_when_nothing_available(
    monkeypatch: pytest.MonkeyPatch, fake_settings
) -> None:
    edge = _FakeEngine("edge", available=False)
    piper = _FakeEngine("piper", available=False)
    _patch_engines(monkeypatch, edge, piper)
    # Offline guarantee: if no engine is ready, Piper is returned; the error message
    # carries the setup hint from Piper.
    engine = engines_base.resolve("auto", fake_settings)
    assert engine.name == "piper"


def test_xtts_is_selectable_but_excluded_from_auto_chain(fake_settings) -> None:
    """XTTS opt-in contract: visible in the UI + explicitly selectable, BUT not in the auto chain.

    available() only checks the torch+TTS import; if it were in the auto chain, once those
    packages are installed it would shadow Piper (the offline guarantee / last resort). This test
    locks that regression.
    """
    assert "xtts" in engines_base.registered_engines()  # UI/diagnostics: visible
    assert "xtts" not in engines_base._AUTO_ORDER  # auto chain: edge→piper
    engine = engines_base.resolve("xtts", fake_settings)  # explicit selection resolves
    assert engine.name == "xtts"


def test_real_piper_missing_voice_raises_english_hint(fake_settings) -> None:
    from akana_server.voice.engines.piper import PiperEngine

    engine = PiperEngine(fake_settings)
    with pytest.raises(TtsError) as exc:
        engine.synthesize("Merhaba dünya.", str(fake_settings.piper_voice_tr))
    assert "Run" in exc.value.message  # English install hint


def test_engine_preference_change_is_instant(
    monkeypatch: pytest.MonkeyPatch, fake_settings
) -> None:
    """A voice_preferences.json change takes effect immediately on the next request (no cache)."""
    edge = _FakeEngine("edge", available=True)
    piper = _FakeEngine("piper", available=True)
    _patch_engines(monkeypatch, edge, piper)

    update_voice_preferences(fake_settings.data_dir, {"tts_engine": "edge"})
    assert resolve_voice_selection(fake_settings).engine == "edge"

    update_voice_preferences(fake_settings.data_dir, {"tts_engine": "piper"})
    assert resolve_voice_selection(fake_settings).engine == "piper"

    # A voice-name change is also instant: the language-based preference is read for edge.
    prefs = update_voice_preferences(
        fake_settings.data_dir, {"tts_engine": "edge", "tts_voice_tr": "tr-TR-AhmetNeural"}
    )
    save_voice_preferences(fake_settings.data_dir, prefs)
    assert resolve_voice_selection(fake_settings, lang="tr").voice == "tr-TR-AhmetNeural"


def test_resolve_voice_selection_auto_uses_lang_hint_not_turkish_default(
    monkeypatch: pytest.MonkeyPatch, fake_settings
) -> None:
    """VB-3: lang='auto' is an UNRESOLVED marker, not a concrete language — it must fall
    to the voice_path/primary_lang hint, NOT leak 'auto' into the voice (which made edge
    always pick the Turkish default for auto-language turns)."""
    edge = _FakeEngine("edge", available=True)
    piper = _FakeEngine("piper", available=True)
    _patch_engines(monkeypatch, edge, piper)
    # Distinct tr/en edge voices so the picked language is unambiguous.
    prefs = update_voice_preferences(
        fake_settings.data_dir,
        {"tts_engine": "edge", "tts_voice_en": "EN-VOICE", "tts_voice_tr": "TR-VOICE"},
    )
    save_voice_preferences(fake_settings.data_dir, prefs)

    # An English hint (voice_path starting "en") must win under 'auto' → English voice,
    # never the Turkish default and never a literal 'auto' voice id.
    en_path = fake_settings.data_dir / "en-US-model.onnx"
    sel = resolve_voice_selection(fake_settings, lang="auto", voice_path=en_path)
    assert "auto" not in sel.voice
    assert sel.voice == "EN-VOICE"

    # 'auto' with no hint resolves to primary_lang, identical to passing None.
    assert (
        resolve_voice_selection(fake_settings, lang="auto").voice
        == resolve_voice_selection(fake_settings, lang=None).voice
    )
