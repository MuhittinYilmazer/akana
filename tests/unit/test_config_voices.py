"""Voice path resolution (broken symlinks, data_dir preference)."""

from __future__ import annotations

from pathlib import Path

import pytest

from akana_server import config as cfg


def test_voice_path_ignores_missing_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    voices = tmp_path / "voices"
    voices.mkdir()
    good = voices / "tr_TR-dfki-medium.onnx"
    good.write_bytes(b"x" * 64)

    monkeypatch.setenv("PIPER_VOICE_TR", "/nonexistent/other.onnx")
    got = cfg._voice_path("PIPER_VOICE_TR", voices, "tr_TR-dfki-medium.onnx")
    assert got == good.resolve()


def test_resolve_voices_dir_prefers_data_with_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_voices = tmp_path / "data" / "voices"
    data_voices.mkdir(parents=True)
    (data_voices / "tr_TR-dfki-medium.onnx").write_bytes(b"x" * 64)

    monkeypatch.delenv("AKANA_VOICES_DIR", raising=False)

    got = cfg._resolve_voices_dir(tmp_path / "data")
    assert got == data_voices.resolve()


def test_resolve_voices_dir_ignores_old_akana_repo_piper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0.4: old akana ``models/piper`` fallback removed — repo models are not used."""
    repo_piper = tmp_path / "models" / "piper"
    repo_piper.mkdir(parents=True)
    (repo_piper / "tr_TR-dfki-medium.onnx").write_bytes(b"x" * 64)

    monkeypatch.setattr(cfg, "_repo_root", lambda: tmp_path)
    monkeypatch.delenv("AKANA_VOICES_DIR", raising=False)

    data_dir = tmp_path / "data"
    got = cfg._resolve_voices_dir(data_dir)
    assert got == (data_dir / "voices").resolve()
    assert got != repo_piper.resolve()
