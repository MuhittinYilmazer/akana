"""XTTS-v2 engine adapter — pure logic (model is MOCKed, no torch/download needed).

Real synthesis (model loading + GPU) is heavy + a ~2GB download, so it is not
run in CI; here voice-id parsing, kwargs generation (language/speaker/
speaker_wav) and WAV encoding are verified by mocking ``_load_model``.
"""

from __future__ import annotations

import io
import wave
from pathlib import Path

import pytest

from akana_server.config import load_settings
from akana_server.voice.engines import xtts as xtts_mod
from akana_server.voice.engines.xtts import XttsEngine
from akana_server.voice.tts import TtsError


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CURSOR_API_KEY", "")
    return load_settings()


class _FakeSynth:
    output_sample_rate = 24000


class _FakeModel:
    """Captures the ``model.tts(**kwargs)`` call; returns a fixed waveform."""

    speakers = ["Spk One", "Spk Two"]

    def __init__(self) -> None:
        self.synthesizer = _FakeSynth()
        self.calls: list[dict] = []

    def tts(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        return [0.0, 0.5, -0.5, 1.0, -1.0]  # [-1,1] float wave


@pytest.fixture
def fake_model(monkeypatch: pytest.MonkeyPatch) -> _FakeModel:
    m = _FakeModel()
    monkeypatch.setattr(xtts_mod, "_load_model", lambda: m)
    monkeypatch.setattr(xtts_mod, "_DEFAULT_SPEAKER", "Spk One")
    return m


def test_default_voice_lang(settings) -> None:
    e = XttsEngine(settings)
    assert e.default_voice("tr") == "tr"
    assert e.default_voice("en-US") == "en"
    assert e.default_voice("") == "tr"


def test_parse_voice_variants(settings) -> None:
    e = XttsEngine(settings)
    assert e._parse_voice("tr") == ("tr", "")
    assert e._parse_voice("EN") == ("en", "")
    assert e._parse_voice("tr|Claribel") == ("tr", "Claribel")
    assert e._parse_voice("tr|/x/ref.wav") == ("tr", "/x/ref.wav")
    # unknown language → falls back to tr
    assert e._parse_voice("xx") == ("tr", "")


def test_list_voices_shape(settings) -> None:
    voices = XttsEngine(settings).list_voices()
    assert {v["lang"] for v in voices} == {"tr", "en"}
    assert all(v["engine"] == "xtts" for v in voices)


def test_synthesize_default_speaker_and_wav(settings, fake_model: _FakeModel) -> None:
    audio, mime = XttsEngine(settings).synthesize("merhaba", "tr")
    assert mime == "audio/wav"
    # valid WAV? (24kHz, mono, 16-bit, 5 samples)
    with wave.open(io.BytesIO(audio), "rb") as w:
        assert w.getframerate() == 24000
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getnframes() == 5
    # NO ref.wav → internal default speaker + language=tr
    call = fake_model.calls[-1]
    assert call["language"] == "tr"
    assert call["speaker"] == "Spk One"
    assert "speaker_wav" not in call


def test_synthesize_named_speaker(settings, fake_model: _FakeModel) -> None:
    XttsEngine(settings).synthesize("selam", "en|Spk Two")
    call = fake_model.calls[-1]
    assert call["language"] == "en"
    assert call["speaker"] == "Spk Two"


def test_synthesize_clone_from_wav(
    settings, fake_model: _FakeModel, tmp_path: Path
) -> None:
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFF....WAVEfmt ")  # mere existence is enough; content is not read in the mock
    XttsEngine(settings).synthesize("klon", f"tr|{ref}")
    call = fake_model.calls[-1]
    assert call["speaker_wav"] == str(ref)
    assert "speaker" not in call  # clone → the built-in speaker is not passed


def test_synthesize_empty_text_400(settings, fake_model: _FakeModel) -> None:
    with pytest.raises(TtsError) as ei:
        XttsEngine(settings).synthesize("   ", "tr")
    assert ei.value.status_code == 400


def test_available_returns_bool(settings) -> None:
    # returns a bool whether or not torch is installed (the probe never blows up)
    assert isinstance(XttsEngine(settings).available(), bool)


def test_prewarm_loads_and_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(xtts_mod, "_load_model", lambda: calls.append(1))
    assert xtts_mod.prewarm() is True
    assert calls == [1]

    def _boom() -> None:
        raise RuntimeError("yükleme patladı")

    monkeypatch.setattr(xtts_mod, "_load_model", _boom)
    assert xtts_mod.prewarm() is False  # the error is SWALLOWED (does not break startup)


def test_prewarm_hook_fires_only_for_xtts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_maybe_prewarm_xtts`` triggers prewarming only when tts_engine=xtts."""
    import threading

    from akana_server.api.app import _maybe_prewarm_xtts

    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CURSOR_API_KEY", "")
    s = load_settings()
    fired = threading.Event()
    monkeypatch.setattr(xtts_mod, "prewarm", fired.set)
    monkeypatch.setattr(XttsEngine, "available", lambda self: True)

    # edge → prewarming is NOT triggered (no wasted VRAM/time)
    monkeypatch.setenv("AKANA_TTS_ENGINE", "edge")
    _maybe_prewarm_xtts(s)
    assert not fired.wait(0.4)

    # xtts → triggered (a background thread calls prewarm)
    monkeypatch.setenv("AKANA_TTS_ENGINE", "xtts")
    _maybe_prewarm_xtts(s)
    assert fired.wait(2.0)
