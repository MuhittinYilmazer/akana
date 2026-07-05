"""The wake sustain gate: a trigger requires N CONSECUTIVE frames at/above the
threshold, not a single peak frame over the ~3 s poll window.

Rationale: openWakeWord scores are a probability capped at 1.0, so the threshold
alone cannot be pushed "higher" to reject more — a ~37-frame window gives one
spurious frame ~37 chances to cross. ``wake_min_frames`` (openWakeWord's own
debounce guidance) collapses those false wakes while a real "hey akana" — which
holds many hot frames in a row — still fires. ``max_score`` (the live meter) is
deliberately NOT gated by the run length.

The model is monkeypatched → runs WITHOUT openwakeword installed.
"""

from __future__ import annotations

import dataclasses
import io
import wave
from pathlib import Path

import pytest

from akana_server.config import load_settings


def _wav_bytes(*, frames: int = 1600, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00" * frames * 2)
    return buf.getvalue()


class _ScriptedModel:
    """Returns a fixed per-frame score series for the ``hey_akana`` key."""

    def __init__(self, series: list[float]) -> None:
        self._series = series

    def reset(self) -> None:  # noqa: D401 - stateful shim, nothing to reset
        pass

    def predict_clip(self, _pcm, **_kw):
        return [{"hey_akana": s} for s in self._series]


def _score(monkeypatch, tmp_path, series: list[float], *, min_frames: int, threshold: float = 0.5):
    from akana_server.voice import wake

    monkeypatch.setattr(wake, "_get_oww_model", lambda *a, **k: _ScriptedModel(series))
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    settings = dataclasses.replace(
        load_settings(),
        data_dir=tmp_path,
        wake_model="hey_akana",
        wake_threshold=threshold,
        wake_min_frames=min_frames,
    )
    return wake.score_wake_wav_bytes_sync(_wav_bytes(), settings)


def test_single_hot_frame_does_not_trigger(monkeypatch, tmp_path: Path) -> None:
    # One lone 0.99 frame amid cold ones: peak crosses 0.5, but the run is length 1.
    res = _score(monkeypatch, tmp_path, [0.1, 0.99, 0.1, 0.2], min_frames=3)
    assert res.max_score == pytest.approx(0.99)  # meter still sees the peak
    assert res.run_frames == 1
    assert res.triggered is False


def test_consecutive_hot_frames_trigger(monkeypatch, tmp_path: Path) -> None:
    # Three frames in a row at/above 0.5 → a real "hey akana".
    res = _score(monkeypatch, tmp_path, [0.1, 0.8, 0.7, 0.9, 0.1], min_frames=3)
    assert res.run_frames == 3
    assert res.triggered is True


def test_run_just_short_does_not_trigger(monkeypatch, tmp_path: Path) -> None:
    # Two consecutive hot frames when three are required → still rejected.
    res = _score(monkeypatch, tmp_path, [0.9, 0.9, 0.1, 0.9], min_frames=3)
    assert res.run_frames == 2
    assert res.triggered is False


def test_min_frames_one_is_legacy_single_peak(monkeypatch, tmp_path: Path) -> None:
    # min_frames=1 restores the old single-frame behavior.
    res = _score(monkeypatch, tmp_path, [0.1, 0.99, 0.1], min_frames=1)
    assert res.triggered is True
    assert res.min_frames == 1
