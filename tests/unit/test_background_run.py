"""``background_run`` — the tool that lets a promise of "I'll tell you when it's done"
actually be kept.

Akana's turn is one-shot: when the reply ends, nothing resumes the model. Before this
tool, a model that said "I'll get back to you when the job finishes" simply never did.
``background_run`` writes a one-off, due-in-seconds schedule bound to the calling
conversation; the engine sweep runs it as its own turn and injects the result back into
that chat (busy-safe), so the follow-up arrives by itself.

Locked here:
  • the tool reaches BOTH model surfaces (MCP child for claude/cursor/codex, native
    declarations + dispatch for gemini/openai/ollama) from the single schema source,
  • it creates a `once` job, due within seconds, delivered same-chat to the ORIGIN
    conversation (never a separate thread),
  • it refuses when there is no conversation to report back to,
  • an empty instruction is refused,
  • the dispatch text tells the model to end its turn rather than wait/poll.
"""

from __future__ import annotations

from pathlib import Path

from akana_server.orchestrator.schedule_tools import (
    SCHEDULE_TOOL_DECLS,
    SCHEDULE_TOOL_NAMES,
    dispatch_schedule_tool,
)
from akana_server.schedule.store import ScheduleStore, now_tr, parse_iso
from akana_server.schedule.tools import ScheduleTools, schedule_schemas
from akana_server.schedule_mcp.mcp import mcp_tool_list


def _tools(tmp_path: Path, conv: str | None = "conv-1") -> ScheduleTools:
    return ScheduleTools(tmp_path, created_by="assistant", origin_conversation=conv)


# -- surface reach ------------------------------------------------------------


def test_tool_reaches_every_model_surface():
    """One schema entry must light up MCP + native decls + native dispatch. The dispatch
    gate used to be a hardcoded name list, which silently dropped the new tool: the model
    saw the declaration, called it, and the call fell through as unknown."""
    assert "background_run" in {s["name"] for s in schedule_schemas()}
    assert "background_run" in {t["name"] for t in mcp_tool_list()}
    assert "background_run" in {d["name"] for d in SCHEDULE_TOOL_DECLS}
    assert "background_run" in SCHEDULE_TOOL_NAMES


def test_schema_tells_the_model_to_end_its_turn():
    schema = next(s for s in schedule_schemas() if s["name"] == "background_run")
    desc = schema["description"].lower()
    assert "instruction" in schema["input_schema"]["required"]
    # the whole point: do not promise a follow-up without calling this, and don't wait
    assert "cannot keep working" in desc or "cannot" in desc
    assert "poll" in desc


# -- job creation -------------------------------------------------------------


def test_creates_a_due_soon_same_chat_job(tmp_path):
    out = _tools(tmp_path).handle_tool_call(
        "background_run", {"instruction": "Uzun raporu özetle", "title": "Rapor"}
    )
    assert out["status"] == "started"
    item = ScheduleStore(tmp_path).load()[0]
    assert item.kind == "once"
    assert item.prompt == "Uzun raporu özetle"
    assert not (item.message or "")  # an LLM turn, NOT a verbatim reminder
    assert item.delivery.mode == "thread"
    assert item.delivery.same_chat is True
    assert item.delivery.conversation_id == "conv-1"
    # due within seconds — the next engine sweep picks it up (not a minute+ away)
    delta = (parse_iso(item.next_run_at) - now_tr()).total_seconds()
    assert 0 < delta <= 30, f"job should be due within seconds, got {delta}s"


def test_default_title_when_omitted(tmp_path):
    _tools(tmp_path).handle_tool_call("background_run", {"instruction": "do the thing"})
    assert ScheduleStore(tmp_path).load()[0].title


def test_refuses_without_a_conversation_to_report_to(tmp_path):
    out = _tools(tmp_path, conv=None).handle_tool_call(
        "background_run", {"instruction": "do the thing"}
    )
    assert "error" in out
    assert not ScheduleStore(tmp_path).load()  # nothing was written


def test_refuses_an_empty_instruction(tmp_path):
    out = _tools(tmp_path).handle_tool_call("background_run", {"instruction": "   "})
    assert "error" in out
    assert not ScheduleStore(tmp_path).load()


# -- native dispatch ----------------------------------------------------------


def test_native_dispatch_returns_end_your_turn_guidance(tmp_path):
    from types import SimpleNamespace

    text = dispatch_schedule_tool(
        SimpleNamespace(data_dir=tmp_path),
        "conv-9",
        "background_run",
        {"instruction": "summarize the logs", "title": "Logs"},
    )
    assert text is not None, "dispatch must handle background_run (not fall through)"
    low = text.lower()
    assert "started" in low and ("end your reply" in low or "do not wait" in low)
    item = ScheduleStore(tmp_path).load()[0]
    assert item.delivery.conversation_id == "conv-9"  # bound to the calling conversation
