"""voice_turn_suffix — the [mode: voice] directive appended to text-chat voice turns.

Regression for the "English mode still replies in Turkish" voice bug: the text-chat
voice path used to inject a HARDCODED Turkish block, ignoring the language picker and
the user-editable voice directive. It now flows through the persona registry's
editable, bilingual voice directive (override or language default) + a streaming-only
opening-words line. These tests pin: language follows the picker, the user override
wins, and the opening-words line is SSE-only.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from akana_server.api.routes.chat._base import voice_turn_suffix
from akana_server.persona.registry import (
    get_persona_registry,
    reset_persona_registries,
)
from akana_server.runtime_settings.store import get_store, reset_runtime_stores


@pytest.fixture
def settings(tmp_path, monkeypatch: pytest.MonkeyPatch):
    # A clean env so language resolves from the store/default, not a stray env var.
    monkeypatch.delenv("AKANA_LANGUAGE", raising=False)
    reset_persona_registries()
    reset_runtime_stores()
    yield SimpleNamespace(data_dir=tmp_path)
    reset_persona_registries()
    reset_runtime_stores()


def test_default_is_english_and_tagged(settings) -> None:
    out = voice_turn_suffix(settings, streaming=True)
    assert out.startswith("[mode: voice]")
    assert "Voice mode is active" in out  # EN directive default
    assert "OPENING REQUIRED" in out  # EN streaming opening
    assert "Sesli mod aktif" not in out


def test_blocking_path_has_no_opening_words(settings) -> None:
    out = voice_turn_suffix(settings, streaming=False)
    assert "Voice mode is active" in out
    assert "OPENING REQUIRED" not in out  # opening-words is SSE-only


def test_language_tr_switches_directive_and_opening(settings) -> None:
    get_store(settings.data_dir).set("language", "tr")
    streaming = voice_turn_suffix(settings, streaming=True)
    assert "Sesli mod aktif" in streaming  # TR directive default
    assert "AÇILIŞ ZORUNLU" in streaming  # TR streaming opening
    assert "Voice mode is active" not in streaming
    blocking = voice_turn_suffix(settings, streaming=False)
    assert "Sesli mod aktif" in blocking
    assert "AÇILIŞ ZORUNLU" not in blocking


def test_user_override_wins_over_language_default(settings) -> None:
    get_persona_registry(settings.data_dir).set_voice_directive("SPEAK LIKE A PIRATE.")
    out = voice_turn_suffix(settings, streaming=True)
    assert "SPEAK LIKE A PIRATE." in out
    assert "Voice mode is active" not in out  # the default is replaced by the override
    # The streaming opening-words mechanic is independent of the directive body.
    assert "OPENING REQUIRED" in out


def test_never_raises_on_broken_settings() -> None:
    # No data_dir attribute → resolution fails defensively, bare tag still returned.
    out = voice_turn_suffix(SimpleNamespace(), streaming=True)
    assert out.startswith("[mode: voice]")
