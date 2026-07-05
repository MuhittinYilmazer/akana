"""Minimal intent routing (F1) — expanded in later phases."""

from __future__ import annotations

from typing import Literal

Intent = Literal["chat", "system_action"]


def classify_intent(text: str) -> Intent:
    """Route user text to a handler class.

    Memory-like questions ("what is my name?" etc.) now flow as "chat":
    the LLM calls the memory_search MCP tool itself using the persona directive.

    Natural-language chat commands (new-chat/delete) were removed — every message
    is an LLM turn now; only the explicit ``system:`` prefix still classifies as a
    system action.
    """
    stripped = text.strip()
    low = stripped.lower()
    if low.startswith("system:"):
        return "system_action"
    return "chat"


