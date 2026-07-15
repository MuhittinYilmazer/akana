"""ScheduleEngine native tools — in-process, provider-neutral (Gemini / OpenAI / Ollama).

Mirrors ``vault_tools`` for schedules: a declaration list (merged into
``GEMINI_TOOL_DECLS`` so every native provider surface picks it up) and a
string-returning dispatch (the model reads the result text).

The declarations are DERIVED from the SINGLE SOURCE
(``schedule.tools.schedule_schemas``) — the MCP (claude/cursor) and native
(gemini/openai) surfaces never diverge (same pattern as ``vault_tools`` deriving
from ``vault_schemas``).

Tools: ``schedule_create`` / ``schedule_list`` / ``schedule_cancel`` /
``schedule_update``.

DEFENSIVE: every dispatch path converts errors to clean text (never raises) — a
tool error breaks neither the text turn nor the voice session; the model reads
the result.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from akana_server.schedule.tools import schedule_schemas

if TYPE_CHECKING:
    from akana_server.config import Settings

log = logging.getLogger(__name__)

#: Names this module handles — dispatch returns ``None`` for anything else so the
#: shared gemini dispatcher can fall through to its own unknown-tool handling.
SCHEDULE_TOOL_NAMES = frozenset(
    {
        "schedule_create",
        "schedule_list",
        "schedule_cancel",
        "schedule_update",
    }
)

#: Gemini native function declarations — DERIVED from the MCP schemas
#: (``input_schema`` → ``parameters``; identical JSON-Schema body, only the key
#: name differs).
SCHEDULE_TOOL_DECLS: list[dict[str, Any]] = [
    {
        "name": s["name"],
        "description": s.get("description", ""),
        "parameters": s["input_schema"],
    }
    for s in schedule_schemas()
]


def _format_result(name: str, result: dict[str, Any]) -> str:
    """Turn a ScheduleTools result dict into compact text the model reads."""
    if result.get("error"):
        return f"Schedule request failed: {result['error']}"
    if name == "schedule_create":
        s = result.get("schedule", {})
        return (
            f"Scheduled «{s.get('title')}» ({s.get('kind')}); next run "
            f"{s.get('next_run_at')} (Turkey time). id={s.get('id')}"
        )
    if name == "schedule_list":
        items = result.get("schedules") or []
        if not items:
            return "No schedules are set."
        lines = [
            f"- {s['id']}: «{s.get('title')}» ({s.get('kind')}) next "
            f"{s.get('next_run_at')}" + ("" if s.get("enabled") else " [paused]")
            for s in items
        ]
        return "Schedules:\n" + "\n".join(lines)
    if name == "schedule_cancel":
        if result.get("removed"):
            return f"Cancelled schedule {result.get('id')}."
        return f"There is no schedule {result.get('id')} to cancel."
    if name == "schedule_update":
        s = result.get("schedule", {})
        return f"Updated schedule {s.get('id')}; next run {s.get('next_run_at')} (Turkey time)."
    return "Done."


def dispatch_schedule_tool(
    settings: "Settings", conv_id: str | None, name: str, args: dict[str, Any] | None
) -> str | None:
    """Schedule tool → string result, or ``None`` if ``name`` is not a schedule tool.

    Returning ``None`` lets the shared gemini dispatcher fall through to its own
    'unknown tool' handling. DEFENSIVE: a schedule tool never raises — every error
    is converted to clean text the model reads."""
    if name not in SCHEDULE_TOOL_NAMES:
        return None
    # Honor the ``schedule_tools_enabled`` gate on the NATIVE surface too. The decl
    # list is a module constant baked into every gemini/openai/ollama + voice turn,
    # so the MCP-spawn gate (claude/cursor/codex only) is not enough — without this
    # check the "disabled" setting is a no-op for the native providers, contradicting
    # the setting's own promise ("the model cannot create schedules").
    from akana_server.orchestrator.memory_tools import schedule_tools_enabled

    if not schedule_tools_enabled():
        return "Scheduling is turned off in settings; I can't create or change schedules."
    try:
        from akana_server.schedule.tools import ScheduleTools

        # Model-created schedules are tagged created_by="assistant". ``conv_id``
        # (the calling conversation) enables the SAME-CHAT default: a reminder
        # created mid-chat fires back into that same conversation.
        tools = ScheduleTools(
            settings.data_dir, created_by="assistant", origin_conversation=conv_id
        )
        result = tools.handle_tool_call(name, args or {})
        return _format_result(name, result)
    except Exception:  # pragma: no cover - a tool error must not break the turn/session
        log.warning("schedule tool '%s' dispatch error", name, exc_info=True)
        return "The schedule tool is currently unavailable."


__all__ = ["SCHEDULE_TOOL_DECLS", "SCHEDULE_TOOL_NAMES", "dispatch_schedule_tool"]
