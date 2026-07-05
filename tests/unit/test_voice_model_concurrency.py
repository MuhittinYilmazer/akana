"""Verifies that shared voice-model inference serializes under concurrency.

Two independent bugs sat together: wake (openWakeWord) and Piper TTS share the
SINGLE process-wide cached model object; their ``reset()/predict_clip()`` and
``synthesize()`` calls are stateful and NOT thread-safe. When ``anyio`` worker
threads (wake) and the streaming prefetch fan-out (Piper, PREFETCH_DEPTH) enter
the same object at the same time, the internal state interleaved and corrupted the
score/audio. The fix put an inference lock on both paths — these tests prove the
lock really serializes (without the lock ``max_inside`` would exceed 1).

The model objects are monkeypatched → runs even WITHOUT openwakeword/piper installed.
"""

from __future__ import annotations

import dataclasses
import io
import threading
import time
import wave
from pathlib import Path

import pytest

from akana_server.config import load_settings


def _wav_bytes(*, frames: int, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00" * frames * 2)
    return buf.getvalue()


def _run_concurrently(target, n: int) -> list[BaseException]:
    """Start ``target`` on ``n`` threads at once with a barrier; collect the errors."""
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            target()
        except BaseException as e:  # noqa: BLE001 - catch all for diagnostics
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_wake_scoring_serializes_shared_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#1: on the shared oWW model, reset()+predict_clip() must serialize under
    concurrent calls (at most 1 thread inside at a time)."""
    from akana_server.voice import wake

    state = {"inside": 0, "max_inside": 0}
    guard = threading.Lock()

    class _SharedStatefulModel:
        def reset(self) -> None:
            with guard:
                state["inside"] += 1
                state["max_inside"] = max(state["max_inside"], state["inside"])

        def predict_clip(self, _pcm, **_kw):
            time.sleep(0.02)  # widen the collision window
            with guard:
                state["inside"] -= 1
            return [{"hey_akana": 0.95}]

    shared = _SharedStatefulModel()
    monkeypatch.setattr(wake, "_get_oww_model", lambda *a, **k: shared)
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    settings = dataclasses.replace(
        load_settings(), data_dir=tmp_path, wake_model="hey_akana"
    )
    wav = _wav_bytes(frames=1600)  # >= WAKE_CHUNK_SAMPLES (1280)

    errors = _run_concurrently(
        lambda: wake.score_wake_wav_bytes_sync(wav, settings), n=4
    )
    assert not errors, f"wake scoring raised under concurrency: {errors!r}"
    assert state["max_inside"] == 1, (
        f"reset()+predict_clip() did not serialize (max concurrent={state['max_inside']})"
    )


def test_piper_synthesize_serializes_shared_voice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#2: the shared PiperVoice.synthesize() must serialize under concurrent calls
    (espeak global state — at most 1 thread inside at a time)."""
    from akana_server.voice import tts

    state = {"inside": 0, "max_inside": 0}
    guard = threading.Lock()

    class _FakeChunk:
        sample_channels = 1
        sample_width = 2
        sample_rate = 22050
        audio_int16_bytes = b"\x00\x00" * 256

    class _SharedVoice:
        def synthesize(self, _text):
            with guard:
                state["inside"] += 1
                state["max_inside"] = max(state["max_inside"], state["inside"])
            time.sleep(0.02)
            with guard:
                state["inside"] -= 1
            return [_FakeChunk()]

    shared = _SharedVoice()
    monkeypatch.setattr(tts, "_get_piper_voice", lambda _p: shared)
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    settings = dataclasses.replace(load_settings(), data_dir=tmp_path)
    vpath = Path("x.onnx")

    errors = _run_concurrently(
        lambda: tts._synthesize_sync("merhaba", settings, vpath), n=4
    )
    assert not errors, f"piper synth raised under concurrency: {errors!r}"
    assert state["max_inside"] == 1, (
        f"voice.synthesize() did not serialize (max concurrent={state['max_inside']})"
    )
