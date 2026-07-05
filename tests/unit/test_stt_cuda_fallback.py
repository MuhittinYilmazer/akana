"""STT helpers: CUDA fallback + WAV decode/input validation (no model loaded)."""

from __future__ import annotations

import asyncio
import dataclasses
import io
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

from akana_server.config import load_settings
from akana_server.voice import stt


def test_is_cuda_runtime_error() -> None:
    assert stt._is_cuda_runtime_error(RuntimeError("Library libcublas.so.12 is not found"))
    assert not stt._is_cuda_runtime_error(ValueError("bad wav"))


def test_reset_whisper_model_clears_globals() -> None:
    stt._model = object()
    stt._model_device = "cuda"
    stt._reset_whisper_model()
    assert stt._model is None
    assert stt._model_device is None


# ── decode / input validation: clear error without ever reaching Whisper ─────


def _wav_bytes(*, frames: int, sampwidth: int = 2, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(sampwidth)
        wf.setframerate(rate)
        wf.writeframes(b"\x00" * frames * sampwidth)
    return buf.getvalue()


def test_decode_rejects_empty_and_tiny_payload() -> None:
    for payload in (b"", b"RIF", b"x" * 43):
        with pytest.raises(stt.SttError) as exc:
            stt.decode_wav_to_float_mono16k(payload, max_seconds=30)
        assert exc.value.status_code == 400


def test_decode_rejects_non_wav_garbage() -> None:
    with pytest.raises(stt.SttError) as exc:
        stt.decode_wav_to_float_mono16k(b"not-a-wav" * 16, max_seconds=30)
    assert exc.value.status_code == 400


def test_decode_rejects_unsupported_sample_width() -> None:
    with pytest.raises(stt.SttError) as exc:
        stt.decode_wav_to_float_mono16k(_wav_bytes(frames=1600, sampwidth=1), max_seconds=30)
    assert "unsupported WAV" in exc.value.message


def test_decode_resamples_to_16k() -> None:
    audio = stt.decode_wav_to_float_mono16k(_wav_bytes(frames=4800, rate=48000), max_seconds=30)
    assert audio.dtype.name == "float32"
    assert abs(len(audio) - 1600) <= 1


def test_transcribe_too_short_audio_raises_before_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    settings = dataclasses.replace(load_settings(), data_dir=tmp_path)
    with pytest.raises(stt.SttError) as exc:
        asyncio.run(stt.transcribe_wav_bytes(_wav_bytes(frames=80), settings))
    assert "too short" in exc.value.message


def test_whisper_missing_gives_install_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    settings = dataclasses.replace(load_settings(), data_dir=tmp_path)
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    stt._reset_whisper_model()
    try:
        with pytest.raises(stt.SttError) as exc:
            stt._get_whisper_model(settings)
        assert exc.value.status_code == 503
        assert "faster-whisper is not installed" in exc.value.message
    finally:
        stt._reset_whisper_model()


# ── initial_prompt: term glossary → mixed TR-EN accuracy (#21) ───────────────


class _FakeSeg:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeInfo:
    language = "tr"


class _FakeModel:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def transcribe(self, _audio, **kw):
        self.calls.append(kw)
        return [_FakeSeg("merhaba")], _FakeInfo()


def test_transcribe_passes_initial_prompt() -> None:
    m = _FakeModel()
    text, lang = stt._transcribe_with_model(
        m, np.zeros(160, dtype="float32"), "tr", "API, commit, pack"
    )
    assert text == "merhaba" and lang == "tr"
    assert m.calls[0]["initial_prompt"] == "API, commit, pack"
    assert m.calls[0]["vad_filter"] is True


def test_transcribe_empty_prompt_becomes_none() -> None:
    m = _FakeModel()
    stt._transcribe_with_model(m, np.zeros(160, dtype="float32"), "tr", "")
    assert m.calls[0]["initial_prompt"] is None  # "" → None (behaviour-neutral)


def test_transcribe_sync_cpu_fallback_ignores_stale_device(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#4 (TOCTOU): EVEN IF another thread has already switched ``_model_device``
    to 'cpu', a request that hits a CUDA error must fall through to the CPU
    fallback. The old code, gated on ``_model_device == 'cuda'``, would wrongly
    drop this request to a 503."""
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    settings = dataclasses.replace(load_settings(), data_dir=tmp_path)

    class _CudaFailModel:
        def transcribe(self, _audio, **_kw):
            raise RuntimeError("Library libcublas.so.12 is not found")

    cpu_model = _FakeModel()  # the second attempt uses this → ("merhaba", "tr")
    monkeypatch.setattr(stt, "_get_whisper_model", lambda _s: _CudaFailModel())
    monkeypatch.setattr(stt, "_fallback_to_cpu_model", lambda _s: cpu_model)
    # As if a concurrent thread already switched to CPU (defeats the old 'cuda' check):
    monkeypatch.setattr(stt, "_model_device", "cpu")

    text, lang = stt._transcribe_sync(np.zeros(160, dtype="float32"), settings, "tr")
    assert (text, lang) == ("merhaba", "tr")
    assert cpu_model.calls, "the CPU model should have been called on the second attempt"


def test_resolve_stt_prompt_from_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("WHISPER_PROMPT", raising=False)
    settings = dataclasses.replace(
        load_settings(), data_dir=tmp_path, whisper_prompt="özel terimlerim"
    )
    assert stt._resolve_stt_prompt(settings) == "özel terimlerim"


def test_resolve_stt_prompt_empty_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("WHISPER_PROMPT", raising=False)
    settings = dataclasses.replace(load_settings(), data_dir=tmp_path, whisper_prompt="")
    assert stt._resolve_stt_prompt(settings) is None
