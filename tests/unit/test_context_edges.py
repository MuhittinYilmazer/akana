"""Additional ContextAssembler boundary-value tests — empty component, huge history,
exact budget boundary, history-failure propagation.

Complements the existing ``test_context_assembler.py`` matrix.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from akana_server.config import load_settings
from akana_server.context import ContextAssembler, ContextRequest
from akana_server.context import assembler as assembler_mod
from akana_server.conversation_service import ConversationService
from akana_server.persona.builtin import CHAT_SYSTEM_PREFIX
from akana_server.persona.registry import (
    get_persona_registry,
    reset_persona_registries,
)

CONV = "conv-ctx-edges"


@pytest.fixture
def req(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AKANA_CONTEXT_MAX_CHARS", raising=False)
    reset_persona_registries()
    settings = load_settings()
    svc = ConversationService.for_data_dir(tmp_path)
    svc.ensure(CONV)
    app = SimpleNamespace(state=SimpleNamespace(settings=settings, conversation_service=svc))
    yield SimpleNamespace(app=app)
    reset_persona_registries()


def _assemble(req, **kwargs):
    kwargs.setdefault("conversation_id", CONV)
    return asyncio.run(ContextAssembler(req).assemble(ContextRequest(**kwargs)))


def _bind_small_persona(req, prompt="S" * 10) -> None:
    reg = get_persona_registry(req.app.state.settings.data_dir)
    if reg.get("smol") is None:
        reg.create_user_persona(persona_id="smol", name="Smol", system_prompt=prompt)
    reg.bind("smol", conversation_id=CONV)


def test_empty_text_no_crash(req) -> None:
    out = _assemble(req, text="")
    assert out.user_text == ""
    assert out.system_prompt == CHAT_SYSTEM_PREFIX
    assert out.injected_blocks == []


def test_empty_history_default(req) -> None:
    out = _assemble(req, text="merhaba")
    assert out.history == []
    assert out.trace["history"]["turns"] == 0


def test_huge_history_trimmed_to_budget(req, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_history(request, conversation_id):
        return ([{"role": "user", "content": "x" * 1000} for _ in range(50)], 0, False)

    monkeypatch.setattr(assembler_mod, "async_llm_history_for_assemble", fake_history)
    monkeypatch.setenv("AKANA_CONTEXT_MAX_CHARS", "100")
    _bind_small_persona(req)
    out = _assemble(req, text="u" * 10)
    # system 10 + text 10 = 20 < 100; all history must be dropped (1000 each)
    assert out.history == []
    assert out.trace["budget"]["total_chars_after"] <= 100
    assert all(t["kind"] == "history" for t in out.trace["budget"]["trimmed"])


def test_budget_exact_boundary_no_trim(req, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_history(request, conversation_id):
        return ([{"role": "user", "content": "h" * 30}], 0, False)

    monkeypatch.setattr(assembler_mod, "async_llm_history_for_assemble", fake_history)
    # system 10 + history 30 + text 10 = 50; budget exactly 50 → NO trim (> not >=)
    monkeypatch.setenv("AKANA_CONTEXT_MAX_CHARS", "50")
    _bind_small_persona(req)
    out = _assemble(req, text="u" * 10)
    assert len(out.history) == 1  # stays at the exact boundary, not trimmed
    assert out.trace["budget"]["trimmed"] == []


def test_budget_one_over_boundary_trims(req, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_history(request, conversation_id):
        return ([{"role": "user", "content": "h" * 30}], 0, False)

    monkeypatch.setattr(assembler_mod, "async_llm_history_for_assemble", fake_history)
    monkeypatch.setenv("AKANA_CONTEXT_MAX_CHARS", "49")  # total 50 > 49
    _bind_small_persona(req)
    out = _assemble(req, text="u" * 10)
    assert out.history == []  # one char over → the oldest (only) message is dropped


def test_history_failure_propagates(req, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(request, conversation_id):
        raise RuntimeError("history db locked")

    monkeypatch.setattr(assembler_mod, "async_llm_history_for_assemble", boom)
    with pytest.raises(RuntimeError):
        _assemble(req, text="merhaba")


def test_memory_disabled_passthrough(req) -> None:
    # v1 in-prompt memory injection is retired; the assembler always passes
    # the raw user text through untouched and memory_trace is always empty.
    out = _assemble(req, text="merhaba")
    assert out.user_text == "merhaba"
    assert out.memory_trace == []
