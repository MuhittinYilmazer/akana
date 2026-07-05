"""Request-scoped LLM settings override (per-conversation model).

The global ``llm_settings.json`` remains the default; during an active conversation
turn the stored provider/model selection for that conversation is reflected to clients
via ``contextvars`` (``load_effective_llm_settings``).
"""

from __future__ import annotations

import contextvars
from typing import Any

from akana_server.config import Settings
from akana_server.llm_settings import LlmSettings, load_llm_settings

_conversation_llm: contextvars.ContextVar[LlmSettings | None] = contextvars.ContextVar(
    "conversation_llm", default=None
)


def set_conversation_llm(llm: LlmSettings | None) -> contextvars.Token[LlmSettings | None]:
    return _conversation_llm.set(llm)


def reset_conversation_llm(token: contextvars.Token[LlmSettings | None]) -> None:
    _conversation_llm.reset(token)


def get_conversation_llm() -> LlmSettings | None:
    """The per-turn LLM snapshot bound by ``bind_conversation_llm`` (None outside a turn)."""
    return _conversation_llm.get()


def load_effective_llm_settings(data_dir: Any, settings: Settings) -> LlmSettings:
    override = _conversation_llm.get()
    if override is not None:
        return override
    return load_llm_settings(data_dir, settings)
