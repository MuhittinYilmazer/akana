"""Provider-neutral tool surface — OpenAI (Chat Completions) tools format.

``gemini_tools`` holds Gemini's native function-declaration shape (``{"name",
"description","parameters"}``); this module wraps the SAME two memory tools
(``memory_search`` + ``save_memory``) in OpenAI's tools JSON-schema shape
(``{"type":"function","function":{...}}``). The schema is derived from a SINGLE
source (``GEMINI_TOOL_DECLS``) → the two surfaces never diverge: adding a tool or
changing a parameter updates both. No redefinition.

Dispatch also comes from a SINGLE source: ``dispatch_gemini_tool`` is actually
the provider-neutral in-process memory dispatcher (it does nothing Gemini-specific —
it falls into the ``Memory`` core, K30-safe staging). Therefore ``dispatch_llm_tool``
reuses it DIRECTLY (memory logic lives in one place; see
gemini_tools._memory_search/_save_memory). Gemini's own path is left UNTOUCHED —
this module only serves the OpenAI side.

DEFENSIVE: dispatch converts every error to a clean message (never raises) — a
tool failure does not break the turn; the model reads the result.
(Same contract as gemini_tools.)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from akana_server.orchestrator.gemini_tools import (
    GEMINI_TOOL_DECLS,
    dispatch_gemini_tool,
)

if TYPE_CHECKING:
    from akana_server.config import Settings


def _to_openai_tool(decl: dict[str, Any]) -> dict[str, Any]:
    """Single Gemini function-declaration → OpenAI tools entry.

    Gemini ``{"name","description","parameters"}`` → OpenAI
    ``{"type":"function","function":{"name","description","parameters"}}``.
    The ``parameters`` JSON-schema body is identical in BOTH (type/properties/required)
    → it is carried as-is (shallow copy: the outer dict is fresh, the inner schema
    is shared — decls are read-only, no mutation)."""
    return {
        "type": "function",
        "function": {
            "name": decl["name"],
            "description": decl.get("description", ""),
            "parameters": decl.get("parameters", {"type": "object", "properties": {}}),
        },
    }


#: OpenAI tools (Chat Completions ``tools=[...]``) — DERIVED from
#: ``GEMINI_TOOL_DECLS`` (single source; no divergence). memory_search (read-only) +
#: save_memory (staging-inbox, K30-safe) — the same two tools as the gemini surface.
OPENAI_TOOL_DECLS: list[dict[str, Any]] = [_to_openai_tool(d) for d in GEMINI_TOOL_DECLS]


def dispatch_llm_tool(
    settings: Settings,
    conversation_id: str | None,
    name: str,
    args: dict[str, Any] | None,
) -> str:
    """Provider-neutral tool-call dispatch → string result (read by the model).

    Reuses the shared in-process memory dispatcher (``dispatch_gemini_tool``) —
    memory logic lives in ONE place (memory_search fused recall; save_memory writes
    to the staging inbox, not directly). DEFENSIVE: every error is converted to a
    clean message (the underlying dispatch never raises), so the OpenAI function-
    calling loop is never broken.

    Return type is JSON-serialisable (str). The signature is left open to returning
    a ``dict`` (for structured results in the future), but today the shared
    dispatcher returns str."""
    return dispatch_gemini_tool(settings, conversation_id, name, args)


__all__ = ["OPENAI_TOOL_DECLS", "dispatch_llm_tool"]
