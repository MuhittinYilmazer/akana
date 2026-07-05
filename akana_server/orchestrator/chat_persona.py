"""Chat-mode instructions — Akana full-capability personal-assistant persona.

The content was moved to ``akana_server/persona/builtin.py`` with PersonaEngine
F0; this module is an import BRIDGE. ``llm_dispatch``/``claude_provider``
continue to obtain ``CHAT_SYSTEM_PREFIX`` from here — the single source of truth
is the persona module.
"""

from __future__ import annotations

from akana_server.persona.builtin import CHAT_SYSTEM_PREFIX

__all__ = ["CHAT_SYSTEM_PREFIX", "wrap_chat_user_message"]


def wrap_chat_user_message(user_text: str) -> str:
    """Prepend the full-capability persona to the chat agent's user message."""
    body = user_text.strip()
    if not body:
        return CHAT_SYSTEM_PREFIX
    return f"{CHAT_SYSTEM_PREFIX}\n\n{body}"
