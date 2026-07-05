"""Per-conversation LLM settings — merge/persist helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from akana_server import chat_context
from akana_server.config import load_settings
from akana_server.llm_settings import (
    LlmSettings,
    conversation_llm_patch_from_meta,
    merge_conversation_llm,
    save_llm_settings,
)


def test_conversation_llm_legacy_provider_from_agent_meta() -> None:
    meta = {"cursor_agent_provider": "cursor"}
    assert conversation_llm_patch_from_meta(meta) == {"provider": "cursor"}


def test_merge_conversation_llm_overrides_global_provider() -> None:
    base = LlmSettings(provider="claude", cursor_model="", claude_model="opus")
    merged = merge_conversation_llm(base, {"llm_provider": "cursor", "llm_cursor_model": "composer-2"})
    assert merged.provider == "cursor"
    assert merged.cursor_model == "composer-2"
    assert merged.claude_model == "opus"


def test_merge_conversation_llm_invalid_override_keeps_global_provider() -> None:
    # An out-of-enum per-conversation override must NOT wipe a valid global
    # provider (CTX-1): the base value is preserved, not reset to "" (which broke
    # chat with "no provider configured").
    base = LlmSettings(provider="claude")
    merged = merge_conversation_llm(base, {"llm_provider": "gpt4-typo"})
    assert merged.provider == "claude"


def test_effective_provider_uses_explicit_conversation_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """An explicit llm_provider record overrides the global during a turn run."""
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    save_llm_settings(tmp_path, LlmSettings(provider="claude"))

    from akana_server.conversation_service import ConversationService

    settings = load_settings()
    svc = ConversationService.for_data_dir(tmp_path)
    svc.ensure("conv-1")
    svc.merge_json_metadata("conv-1", {"llm_provider": "cursor"})
    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=settings,
            conversation_service=svc,
            llm_settings=LlmSettings(provider="claude"),
        )
    )
    request = SimpleNamespace(app=app)
    assert chat_context.effective_provider(request, "conv-1") == "cursor"


def test_restore_llm_uses_legacy_agent_provider_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    save_llm_settings(tmp_path, LlmSettings(provider="claude"))
    from akana_server.conversation_service import ConversationService

    settings = load_settings()
    svc = ConversationService.for_data_dir(tmp_path)
    svc.ensure("conv-1")
    svc.merge_json_metadata(
        "conv-1", {"cursor_agent_provider": "cursor", "cursor_agent_id": "x"}
    )
    app = SimpleNamespace(
        state=SimpleNamespace(settings=settings, conversation_service=svc)
    )
    request = SimpleNamespace(app=app)
    assert chat_context.restore_llm_settings(request, "conv-1").provider == "cursor"
    assert chat_context.effective_llm_settings(request, "conv-1").provider == "claude"
