"""classify_intent — memory questions now go to the normal "chat" path.

Regression (live bug): patterns like "benim adım ne?" fell into the "memory_recall"
intent and triggered the legacy recall path that blocked before SSE started, so the
Web UI saw no events. Vision: the LLM calls the memory_search MCP tool itself; there
is no automatic recall routing.
"""

from __future__ import annotations

import pytest

from akana_server.orchestrator.router import classify_intent


@pytest.mark.parametrize(
    "text",
    [
        "benim adım ne?",
        "benim adım ne",
        "en sevdiğim renk ne?",
        "ne hatırlıyorsun?",
        "benim hakkımda ne hatırlıyorsun?",
        "do you remember my name?",
        "what do you remember about me?",
        "dün ne konuştuk?",
    ],
)
def test_memory_like_questions_route_to_chat(text: str) -> None:
    """Memory-like questions go to the normal LLM path, not a special path."""
    assert classify_intent(text) == "chat"


@pytest.mark.parametrize(
    "text",
    [
        "ne yapıyorsun?",  # the old heuristic counted even this as memory_recall
        "bugün hava nasıl, planın ne",
        "bana bir fıkra anlat",
        # Natural-language chat commands were REMOVED — these are now ordinary
        # messages that flow to the LLM (no pre-LLM command short-circuit).
        "yeni sohbet",
        "sohbeti sil",
    ],
)
def test_ordinary_chat_stays_chat(text: str) -> None:
    assert classify_intent(text) == "chat"


@pytest.mark.parametrize(
    "text",
    [
        "system: restart",
    ],
)
def test_system_actions_still_route_to_system_action(text: str) -> None:
    """Only the explicit ``system:`` prefix still classifies as a system action;
    the natural-language new-chat/delete commands were removed."""
    assert classify_intent(text) == "system_action"


def test_classify_intent_never_returns_memory_recall() -> None:
    """The legacy "memory_recall" intent is not produced (to be removed entirely in B2.5)."""
    samples = [
        "benim adım ne?",
        "hafızanda ne var?",
        "hatırlıyor musun beni?",
        "geçen hafta ne yaptık?",
        "ne?",
    ]
    assert all(classify_intent(s) != "memory_recall" for s in samples)
