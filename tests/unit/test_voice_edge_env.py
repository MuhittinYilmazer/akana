"""Edge TTS engine — import-time env parsing must be crash-proof.

A malformed ``AKANA_TTS_EDGE_TIMEOUT_S`` must NOT raise during import of the voice
package (that would take the whole server down at startup and break the
graceful-degradation promise); it falls back to the default instead."""

from __future__ import annotations

from akana_server.voice.engines.edge import _DEFAULT_SYNTH_TIMEOUT_S, _env_timeout


def test_env_timeout_unset_returns_default(monkeypatch) -> None:
    monkeypatch.delenv("AKANA_TTS_EDGE_TIMEOUT_S", raising=False)
    assert _env_timeout() == _DEFAULT_SYNTH_TIMEOUT_S


def test_env_timeout_valid_override(monkeypatch) -> None:
    monkeypatch.setenv("AKANA_TTS_EDGE_TIMEOUT_S", "3.5")
    assert _env_timeout() == 3.5


def test_env_timeout_malformed_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("AKANA_TTS_EDGE_TIMEOUT_S", "ten")
    assert _env_timeout() == _DEFAULT_SYNTH_TIMEOUT_S


def test_env_timeout_nonpositive_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("AKANA_TTS_EDGE_TIMEOUT_S", "-5")
    assert _env_timeout() == _DEFAULT_SYNTH_TIMEOUT_S
    monkeypatch.setenv("AKANA_TTS_EDGE_TIMEOUT_S", "0")
    assert _env_timeout() == _DEFAULT_SYNTH_TIMEOUT_S


def test_env_timeout_empty_string_returns_default(monkeypatch) -> None:
    monkeypatch.setenv("AKANA_TTS_EDGE_TIMEOUT_S", "")
    assert _env_timeout() == _DEFAULT_SYNTH_TIMEOUT_S
