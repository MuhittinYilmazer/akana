"""Gemini native function-calling tool module — shared by both the text and voice surfaces.

Both ``gemini_provider`` (text chat, Phase 3) and ``voice/gemini_live`` (full-duplex
voice) share the SAME tool set: a single function-declaration list, a single dispatch,
and a single function-response builder. NOT MCP — Gemini native function-calling (the
gemini text/voice surface does not speak MCP); tools dispatch into the in-process
``Memory`` core.

Memory tools:
- ``memory_search`` (READ): fused recall → compact text. Safe, high-value.
- ``save_memory`` (WRITE): K30-SAFE. Does NOT write a durable fact directly — it
  places the candidate in the staging inbox (``policy="stage"`` + ``allow_direct=False``);
  the user confirms later. This prevents memory poisoning via prompt-injection or
  misunderstanding.
- ``memory_forget`` (WRITE): forget / partial-supersede a record. Reaches parity with
  the ``memory.forget`` MCP tool (claude/cursor). audit C9: forget is intentionally NOT
  ``allow_direct``-gated — it is soft/reversible via the ledger, so it works even in K30
  inbox_only mode; the declaration is DERIVED single-source from the MCP schema so the
  native and MCP surfaces never diverge (target_id + mode/new_value for partial forget).

Secure-vault tools — the full read/write/delete surface (``vault_list`` / ``vault_get``
/ ``vault_get_credential`` / ``vault_set`` / ``vault_set_credential`` / ``vault_delete``
/ ``vault_delete_credential``) — are merged in from
:mod:`akana_server.orchestrator.vault_tools`: declarations append to ``GEMINI_TOOL_DECLS``
(so every provider surface picks them up) and dispatch falls through to
``dispatch_vault_tool``.

DEFENSIVE: every dispatch path converts errors to clean text (never raises) —
a tool error breaks neither the text turn nor the voice session; the model reads the
result.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from akana_server.orchestrator.vault_tools import VAULT_TOOL_DECLS, dispatch_vault_tool

if TYPE_CHECKING:
    from akana_server.config import Settings

log = logging.getLogger(__name__)


#: MCP memory tool name ↔ native function-calling name. Native uses underscores
#: (``memory_search``/``save_memory``/``memory_forget``) to match the other native
#: tools; the MCP surface uses the dotted ``memory.*`` names. Kept here as the single
#: source of the mapping so the drift-guard test can assert native == MCP.
MEMORY_TOOL_NAME_MAP: dict[str, str] = {
    "memory_search": "memory.search",
    "save_memory": "memory.remember",
    "memory_forget": "memory.forget",
}


def _forget_decl() -> dict[str, Any]:
    """``memory_forget`` declaration — DERIVED single-source from the MCP schema
    (``memory.forget``'s ``input_schema`` → ``parameters``; the JSON-Schema body is
    identical, only the tool name differs). This mirrors how ``VAULT_TOOL_DECLS`` is
    derived from ``vault_schemas()`` so the native and MCP surfaces never diverge."""
    from akana.memory.tools import tool_schemas

    schema = next(s for s in tool_schemas() if s["name"] == "memory.forget")
    return {
        "name": "memory_forget",
        "description": schema.get("description", ""),
        "parameters": schema["input_schema"],
    }


#: Gemini native function declarations (provider-agnostic; shared by text and live).
#: ``memory_search`` is read-only; ``save_memory`` writes to the staging inbox (K30-safe).
GEMINI_TOOL_DECLS: list[dict[str, Any]] = [
    {
        "name": "memory_search",
        "description": (
            "Search Akana's long-term memory: facts about the user, past "
            "conversations, preferences. Call it when the user asks things like "
            "'do you remember', 'what did I say', 'what was X', 'my …'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The topic/question to search for (in the user's language).",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "save_memory",
        "description": (
            "Save a note/fact to long-term memory — call it when the user says "
            "'remember this', 'note that', 'keep in mind'. The note is not written "
            "directly; it lands in the memory inbox for the user to approve."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The info/fact to remember (in the user's language, one sentence).",
                }
            },
            "required": ["text"],
        },
    },
    # memory_forget: parity with the memory.forget MCP tool, derived single-source
    # from the MCP schema (target_id + mode/new_value for partial forget). C9-safe.
    _forget_decl(),
    # Secure-vault read/write/delete tools (vault_list / vault_get / vault_get_credential
    # / vault_set / vault_set_credential / vault_delete / vault_delete_credential),
    # derived single-source from the MCP schemas so the in-process surface matches
    # the claude MCP surface exactly.
    *VAULT_TOOL_DECLS,
]


def _memory_search(settings: Settings, conv_id: str | None, query: str) -> str:
    """``memory_search`` dispatch — fused recall → compact text (read by the model)."""
    from akana_server.memory_core import get_memory_core

    mem = get_memory_core(settings.data_dir)
    result = mem.recall(query, conversation_id=conv_id, limit=6, budget_tokens=1000)
    blocks = getattr(result, "blocks", None) or []
    lines: list[str] = []
    for b in blocks[:6]:
        text = (getattr(b, "text", "") or "").strip()
        if text:
            lines.append(f"- {text}")
    return "\n".join(lines) if lines else "No matching records found in memory."


def _save_memory(settings: Settings, conv_id: str | None, text: str) -> str:
    """``save_memory`` dispatch — place the candidate in the staging inbox (K30-safe).

    Uses the HIGHEST-LEVEL entry point: ``Memory.make_orchestrator().handle_tool_call``
    calling the ``memory.remember`` tool with ``policy="stage"`` (default). The
    orchestrator is built with ``OrchestratorSettings(allow_direct=False)`` → the
    request goes to the staging inbox (the user confirms later); ``handle_tool_call``
    NEVER raises at the tool boundary, it returns ``{"status": "staged", ...}``. This
    way a durable write from voice/text without explicit approval does not hit the K30
    clamp — candidates accumulate in the inbox.
    """
    from akana_server.memory_core import get_memory_core
    from akana.memory.orchestrator import OrchestratorSettings

    mem = get_memory_core(settings.data_dir)
    orch = mem.make_orchestrator(settings=OrchestratorSettings(allow_direct=False))
    result = orch.handle_tool_call(
        "memory.remember",
        {"content": text, "kind": "fact", "policy": "stage"},
        conversation_id=conv_id,
    )
    # handle_tool_call returns a dict: successful stage {"status":"staged",...},
    # or validation/internal error {"error":{...}}. Convert both to a clean message.
    if isinstance(result, dict) and result.get("error"):
        return "I couldn't save the note; could you try again?"
    return "The note was added to the memory inbox; you can approve it from the Memory screen."


def _memory_forget(settings: Settings, conv_id: str | None, args: dict[str, Any]) -> str:
    """``memory_forget`` dispatch — forget / partial-supersede a record.

    Uses the SAME safe entry point as ``_save_memory``: ``Memory.make_orchestrator()
    .handle_tool_call`` with the ``memory.forget`` tool. audit C9: forget is
    deliberately NOT ``allow_direct``-gated in the orchestrator — a durable
    forget/supersede works even under the K30 inbox_only clamp (it is soft/reversible
    via the ledger). So we keep the orchestrator's existing forget path AS-IS (no new
    direct-delete path is opened); the ``allow_direct=False`` build is intentional and
    harmless here. ``handle_tool_call`` never raises at the tool boundary — it returns
    ``{"status": ...}`` or an ``{"error": ...}`` envelope; both convert to clean text."""
    from akana_server.memory_core import get_memory_core
    from akana.memory.orchestrator import OrchestratorSettings

    target_id = str(args.get("target_id") or "").strip()
    if not target_id:
        return "Which record should I forget? A target_id is required."
    payload: dict[str, Any] = {"target_id": target_id}
    mode = str(args.get("mode") or "").strip()
    if mode:
        payload["mode"] = mode
    new_value = args.get("new_value")
    if new_value is not None and str(new_value).strip():
        payload["new_value"] = str(new_value)
    reason = args.get("reason")
    if reason is not None and str(reason).strip():
        payload["reason"] = str(reason)

    mem = get_memory_core(settings.data_dir)
    orch = mem.make_orchestrator(settings=OrchestratorSettings(allow_direct=False))
    result = orch.handle_tool_call("memory.forget", payload, conversation_id=conv_id)
    if isinstance(result, dict) and result.get("error"):
        return "I couldn't forget that record; please check the record id and try again."
    return "Done — that memory has been forgotten."


def dispatch_gemini_tool(
    settings: Settings, conv_id: str | None, name: str, args: dict[str, Any] | None
) -> str:
    """Gemini function-call → string result. DEFENSIVE: every error converts to clean
    text (never breaks the turn or the session; the model reads/voices the result)."""
    args = args or {}
    try:
        if name == "memory_search":
            query = str(args.get("query") or "").strip()
            if not query:
                return "The search query is empty."
            return _memory_search(settings, conv_id, query)
        if name == "save_memory":
            text = str(args.get("text") or "").strip()
            if not text:
                return "The note to save is empty."
            return _save_memory(settings, conv_id, text)
        if name == "memory_forget":
            return _memory_forget(settings, conv_id, args)
        vault_out = dispatch_vault_tool(settings, conv_id, name, args)
        if vault_out is not None:
            return vault_out
        return f"Unknown tool: {name}"
    except Exception:  # pragma: no cover - a tool error must not break the turn/session
        log.warning("gemini tool '%s' dispatch error", name, exc_info=True)
        return "The memory tool is unavailable right now."


def _function_response(fc: Any, result: str) -> Any:
    """Function-call → google-genai ``FunctionResponse`` (if available) / plain dict.

    The Live surface sends this via ``send_tool_response(function_responses=[...])``;
    the text surface wraps it in ``Part(function_response=...)`` and appends it to
    contents."""
    fid = getattr(fc, "id", None)
    name = getattr(fc, "name", "") or ""
    try:  # pragma: no cover - in environments where the SDK is installed
        from google.genai import types

        return types.FunctionResponse(id=fid, name=name, response={"result": result})
    except Exception:  # pragma: no cover - no SDK → dict fallback
        return {"id": fid, "name": name, "response": {"result": result}}


__all__ = [
    "GEMINI_TOOL_DECLS",
    "MEMORY_TOOL_NAME_MAP",
    "dispatch_gemini_tool",
    "_function_response",
    "_memory_search",
    "_save_memory",
    "_memory_forget",
]
