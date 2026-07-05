"""ContextEngine F0 — assembles a chat turn's context through a SINGLE gate.

Problem: turn context used to be built in scattered places — the system prompt
(persona), the ``[Yetenek: ...]`` skill block (turn_injection), memory injection
(memory compile) and the conversation history (chat_max_turns window) were each
combined in different spots. F0 WRAPS these under :class:`ContextAssembler` (no moving, no
duplication): the existing pieces are combined in the same order and the same
shape; each component records in the trace what was injected / how many
characters / why it was written — this is where the answer to "why was the
context like this?" lives.

The only visible new feature (F0):

* persona.resolve() FIRST REAL BINDING — the channel/conversation persona
  enters the system prompt here; when there is no binding it returns the
  builtin ``akana`` (exactly ``CHAT_SYSTEM_PREFIX`` itself) and behavior does
  not change.
* ``GET /api/v1/context/preview?conversation_id=`` (bearer) — a preview of the
  assembled context (route in ``api/routes/chat.py``).
* The total context character budget lives in a SINGLE place
  (:func:`context_budget_chars`); on overflow the trimming order is: history
  first (oldest), then the skill block, system never.
"""

from __future__ import annotations

from akana_server.context.assembler import (
    DEFAULT_MAX_CONTEXT_CHARS,
    AssembledContext,
    ContextAssembler,
    ContextRequest,
    SummaryProvider,
    context_budget_chars,
)

__all__ = [
    "DEFAULT_MAX_CONTEXT_CHARS",
    "AssembledContext",
    "ContextAssembler",
    "ContextRequest",
    "SummaryProvider",
    "context_budget_chars",
]
