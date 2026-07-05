"""Unit tests for LLM memory capture JSON parsing."""

from __future__ import annotations

import pytest

from akana_server.memory_capture import (
    _capture_prompt,
    capture_enabled,
    parse_capture_response,
)
from akana_server.orchestrator.router import classify_intent


def test_capture_enabled_respects_memory_setting(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Memory Studio 'automatic capture' toggle (memory_settings.auto_capture, next to
    'save without approval') gates background capture; env AKANA_MEMORY_LLM_CAPTURE overrides it
    on load; with no data_dir it falls back to the env-only default."""
    from akana.memory.settings import MemorySettings, save_memory_settings

    monkeypatch.delenv("AKANA_MEMORY_LLM_CAPTURE", raising=False)
    # Persisted toggle OFF → disabled.
    save_memory_settings(tmp_path, MemorySettings(auto_capture=False))
    assert capture_enabled(tmp_path) is False
    # Persisted toggle ON → enabled.
    save_memory_settings(tmp_path, MemorySettings(auto_capture=True))
    assert capture_enabled(tmp_path) is True
    # Env overrides the persisted setting on load.
    monkeypatch.setenv("AKANA_MEMORY_LLM_CAPTURE", "0")
    assert capture_enabled(tmp_path) is False
    # No data_dir → env-only default.
    monkeypatch.setenv("AKANA_MEMORY_LLM_CAPTURE", "1")
    assert capture_enabled() is True


def test_parse_capture_json() -> None:
    raw = '{"capture": true, "facts": [{"key": "soyad", "value": "Yılmaz", "reason": "bildirdi"}]}'
    facts = parse_capture_response(raw)
    assert len(facts) == 1
    assert facts[0].key == "soyad"
    assert facts[0].value == "Yılmaz"


def test_parse_capture_false() -> None:
    assert parse_capture_response('{"capture": false, "facts": []}') == []


def test_parse_capture_markdown_fence() -> None:
    raw = '```json\n{"capture": true, "facts": [{"key": "ad", "value": "Ali"}]}\n```'
    facts = parse_capture_response(raw)
    assert len(facts) == 1
    assert facts[0].key == "ad"


def test_hafizaya_kaydet_is_normal_chat() -> None:
    assert classify_intent("soyadım Demir, hafızaya kaydet") == "chat"


def test_capture_prompt_is_bilingual() -> None:
    """The capture prompt the model reads follows the language (default EN)."""
    en = _capture_prompt(
        user_text="my name is Alice",
        assistant_text=None,
        existing_keys=["email"],
        recent_dialogue="(no prior turn)",
    )
    assert en.startswith("[Memory capture decision]")
    assert "Existing memory keys: email" in en
    assert "This turn — User" in en

    tr = _capture_prompt(
        user_text="adım Alice",
        assistant_text=None,
        existing_keys=["email"],
        recent_dialogue="(önceki tur yok)",
        language="tr",
    )
    assert tr.startswith("[Hafıza kayıt kararı]")
    assert "Mevcut hafıza anahtarları: email" in tr
    assert "Bu tur — Kullanıcı" in tr


def test_capture_prompt_lists_pending_items() -> None:
    """Pending inbox rows are shown to the capture model so it won't re-propose a fact
    already awaiting approval (the 'same info lands in the inbox again' bug)."""
    en = _capture_prompt(
        user_text="my name is Alice",
        assistant_text=None,
        existing_keys=[],
        recent_dialogue="(no prior turn)",
        pending_items=[("ad", "Alice")],
    )
    assert "Awaiting in the inbox" in en
    assert "- ad: Alice" in en


def test_capture_prompt_omits_pending_section_when_empty() -> None:
    """No pending rows → the section is not rendered (the prompt stays lean)."""
    lean = _capture_prompt(
        user_text="hi",
        assistant_text=None,
        existing_keys=[],
        recent_dialogue="(no prior turn)",
    )
    assert "Awaiting in the inbox" not in lean


def test_memory_settings_session_summary_default_and_roundtrip(tmp_path) -> None:
    """session_summary defaults ON and round-trips through memory_settings.yaml."""
    from akana.memory.settings import (
        MemorySettings,
        load_memory_settings,
        save_memory_settings,
    )

    assert MemorySettings().session_summary is True
    save_memory_settings(tmp_path, MemorySettings(session_summary=False))
    assert load_memory_settings(tmp_path).session_summary is False
