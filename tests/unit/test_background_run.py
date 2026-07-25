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


def test_job_is_tagged_and_hidden_from_the_reminder_list(tmp_path):
    """A background job is an implementation detail of "do it in the background", not a
    reminder the user set up: it must not fill «what are my reminders?» with spent rows."""
    t = _tools(tmp_path)
    t.handle_tool_call("background_run", {"instruction": "work", "title": "Job"})
    created = t.handle_tool_call(
        "schedule_create",
        {"kind": "once", "when": "in 2 hours", "message": "real reminder", "title": "Su iç"},
    )
    assert created.get("status") == "created", created
    assert ScheduleStore(tmp_path).load()[0].tag == "background"
    listed = t.handle_tool_call("schedule_list", {})
    titles = [s.get("title") for s in listed["schedules"]]
    assert listed["count"] == 1 and "Job" not in titles
    # …but they remain inspectable on request.
    assert t.handle_tool_call("schedule_list", {"include_background": True})["count"] == 2


# -- engine: the result (and the FAILURE) actually reach the chat --------------


def _run_engine(tmp_path, llm):
    """Run the due sweep with a patched LLM + a recording injection; return the messages
    that would land in the chat."""
    import asyncio
    from types import SimpleNamespace

    from akana_server import chat_injections
    from akana_server.orchestrator import llm_dispatch, memory_tools
    from akana_server.schedule import engine

    delivered: list[str] = []

    async def fake_deliver(app, s, conv_id, text, *, kind="schedule", title=""):
        delivered.append(text)
        return "delivered"

    orig_deliver = chat_injections.deliver_or_queue
    orig_llm = llm_dispatch.complete_chat_aggregated
    orig_mcp = memory_tools.mcp_servers_payload
    chat_injections.deliver_or_queue = fake_deliver  # engine imports it at call time
    llm_dispatch.complete_chat_aggregated = llm
    memory_tools.mcp_servers_payload = lambda *a, **k: {}
    try:
        item = ScheduleStore(tmp_path).load()[0]
        asyncio.run(
            engine.run_due_schedules(
                SimpleNamespace(data_dir=tmp_path),
                app=SimpleNamespace(state=SimpleNamespace()),
                now=parse_iso(item.next_run_at),
            )
        )
    finally:
        chat_injections.deliver_or_queue = orig_deliver
        llm_dispatch.complete_chat_aggregated = orig_llm
        memory_tools.mcp_servers_payload = orig_mcp
    return delivered


def test_result_reaches_the_chat_framed_as_a_finished_job(tmp_path):
    _tools(tmp_path).handle_tool_call("background_run", {"instruction": "x", "title": "Rapor"})

    async def ok(settings, prompt, **kw):
        return ("the answer", {}, None)

    [msg] = _run_engine(tmp_path, ok)
    assert "the answer" in msg
    # a job the user asked for is NOT a reminder they set
    assert "Reminder" not in msg and "Hatırlatma" not in msg
    assert "Rapor" in msg


def test_a_failed_job_is_reported_instead_of_vanishing(tmp_path):
    """THE bug this feature exists to prevent, one level down: background_run makes the
    model promise "the result will be posted here". If the run fails and we stay silent,
    that promise silently never arrives."""
    _tools(tmp_path).handle_tool_call("background_run", {"instruction": "x", "title": "Rapor"})

    async def boom(settings, prompt, **kw):
        raise RuntimeError("provider exploded")

    [msg] = _run_engine(tmp_path, boom)
    assert "Rapor" in msg
    assert "provider exploded" in msg  # the user learns WHY, not just that it failed


def test_an_empty_result_is_reported_too(tmp_path):
    _tools(tmp_path).handle_tool_call("background_run", {"instruction": "x", "title": "Rapor"})

    async def empty(settings, prompt, **kw):
        return ("   ", {}, None)

    [msg] = _run_engine(tmp_path, empty)
    assert "Rapor" in msg and "empty" in msg.lower()


def test_background_turn_is_told_not_to_spawn_another(tmp_path):
    """The persona tells the model to hand turn-outliving work to background_run — inside
    the background run itself that would defer the work forever."""
    from akana_server.schedule import engine

    _tools(tmp_path).handle_tool_call("background_run", {"instruction": "x", "title": "J"})
    item = ScheduleStore(tmp_path).load()[0]
    prompt = engine._build_system_prompt(None, item) or ""
    assert "BACKGROUND JOB" in prompt and "do NOT call background_run again" in prompt


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
