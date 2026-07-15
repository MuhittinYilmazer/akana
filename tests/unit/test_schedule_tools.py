"""ScheduleEngine tool surface — ScheduleTools, NL datetime, MCP + native parity.

Hermetic: real ``ScheduleStore`` over a ``tmp_path`` data dir; ``now`` injected
for the natural-language parser; env toggled for the connector-enabled gate. Also
smoke-tests the ``schedule_mcp`` server and asserts the native decls/dispatch are
merged into the gemini/openai surfaces (single-source parity).
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from akana_server.orchestrator import gemini_tools as gt
from akana_server.orchestrator.llm_tools import OPENAI_TOOL_DECLS, dispatch_llm_tool
from akana_server.orchestrator.schedule_tools import (
    SCHEDULE_TOOL_DECLS,
    dispatch_schedule_tool,
)
from akana_server.schedule.store import TR_TZ, ScheduleValidationError, parse_iso
from akana_server.schedule.tools import ScheduleTools, resolve_once_when, schedule_schemas
from akana_server.schedule_mcp.mcp import McpServer, mcp_tool_list

NOW = datetime(2026, 7, 11, 10, 0, tzinfo=TR_TZ)

_NAMES = {"schedule_create", "schedule_list", "schedule_cancel", "schedule_update"}


def _settings(tmp_path):
    return SimpleNamespace(data_dir=tmp_path)


# --- natural-language datetime ----------------------------------------------


def test_resolve_iso_passthrough():
    assert resolve_once_when("2026-07-12T09:00", now=NOW) == "2026-07-12T09:00:00+03:00"


def test_resolve_tomorrow_english():
    assert resolve_once_when("tomorrow 09:00", now=NOW) == "2026-07-12T09:00:00+03:00"


def test_resolve_today_english():
    assert resolve_once_when("today 18:00", now=NOW) == "2026-07-11T18:00:00+03:00"


def test_resolve_turkish_yarin():
    assert resolve_once_when("yarın 08:30", now=NOW) == "2026-07-12T08:30:00+03:00"


def test_resolve_bare_time_future():
    assert resolve_once_when("18:00", now=NOW) == "2026-07-11T18:00:00+03:00"


def test_resolve_bare_time_past_rolls_tomorrow():
    assert resolve_once_when("09:00", now=NOW) == "2026-07-12T09:00:00+03:00"


def test_resolve_weekday_name():
    dt = parse_iso(resolve_once_when("monday 08:00", now=NOW))
    assert dt.weekday() == 0
    assert dt > NOW
    assert (dt.hour, dt.minute) == (8, 0)


def test_resolve_garbage_raises():
    with pytest.raises(ScheduleValidationError):
        resolve_once_when("sometime maybe", now=NOW)


# --- BUG 1: relative-time phrases (were rejected → None → error) -------------


def test_resolve_relative_turkish_minutes():
    # The owner's very first prompt: "1 dakika sonra ...". Must be now + 60s.
    assert resolve_once_when("1 dakika sonra", now=NOW) == "2026-07-11T10:01:00+03:00"


def test_resolve_relative_turkish_five_minutes():
    assert resolve_once_when("5 dakika sonra", now=NOW) == "2026-07-11T10:05:00+03:00"


def test_resolve_relative_turkish_one_hour():
    assert resolve_once_when("1 saat sonra", now=NOW) == "2026-07-11T11:00:00+03:00"


def test_resolve_relative_turkish_half_hour():
    assert resolve_once_when("yarım saat sonra", now=NOW) == "2026-07-11T10:30:00+03:00"


def test_resolve_relative_turkish_one_and_half_hours():
    assert resolve_once_when("bir buçuk saat sonra", now=NOW) == "2026-07-11T11:30:00+03:00"


def test_resolve_relative_english_minutes():
    assert resolve_once_when("in 5 minutes", now=NOW) == "2026-07-11T10:05:00+03:00"


def test_resolve_relative_english_an_hour():
    assert resolve_once_when("in an hour", now=NOW) == "2026-07-11T11:00:00+03:00"


# --- BUG 2: Turkish clock forms (were silently defaulting to 09:00) ----------


def test_resolve_turkish_apostrophe_hour():
    # "yarin 8'de" → 08:00 tomorrow (was wrongly 09:00 via the _DEFAULT_HOUR path).
    assert resolve_once_when("yarin 8'de", now=NOW) == "2026-07-12T08:00:00+03:00"


def test_resolve_turkish_dotted_time():
    assert resolve_once_when("yarın 18.30", now=NOW) == "2026-07-12T18:30:00+03:00"


def test_resolve_turkish_bare_hour_after_day():
    assert resolve_once_when("yarın 8", now=NOW) == "2026-07-12T08:00:00+03:00"


def test_resolve_turkish_evening_shift():
    # "akşam 7" → 19:00 (PM shift), "gece 11" → 23:00.
    assert resolve_once_when("bugün akşam 7", now=NOW) == "2026-07-11T19:00:00+03:00"
    assert resolve_once_when("bugün gece 11", now=NOW) == "2026-07-11T23:00:00+03:00"


def test_resolve_turkish_saat_form():
    assert resolve_once_when("yarın saat 8", now=NOW) == "2026-07-12T08:00:00+03:00"


def test_resolve_pure_day_word_keeps_default_nine():
    # No digits at all → the 09:00 default is intentionally kept.
    assert resolve_once_when("yarın", now=NOW) == "2026-07-12T09:00:00+03:00"


def test_resolve_unbindable_digits_raise_not_default():
    # Digits present but NOT a valid clock time → error, never a silent 09:00.
    with pytest.raises(ScheduleValidationError):
        resolve_once_when("yarın 30", now=NOW)


# --- ScheduleTools CRUD -----------------------------------------------------


def test_create_list_update_cancel_roundtrip(tmp_path):
    tools = ScheduleTools(tmp_path, created_by="assistant")
    created = tools.handle_tool_call(
        "schedule_create", {"title": "t", "prompt": "p", "kind": "daily", "when": "09:00"}
    )
    assert created["status"] == "created"
    assert created["schedule"]["created_by"] == "assistant"
    sid = created["schedule"]["id"]

    assert tools.handle_tool_call("schedule_list", {})["count"] == 1

    updated = tools.handle_tool_call(
        "schedule_update", {"id": sid, "title": "t2", "enabled": False}
    )
    assert updated["schedule"]["title"] == "t2"
    assert updated["schedule"]["enabled"] is False

    cancelled = tools.handle_tool_call("schedule_cancel", {"id": sid})
    assert cancelled["removed"] is True
    assert tools.handle_tool_call("schedule_list", {})["count"] == 0


def test_create_once_uses_natural_language(tmp_path):
    tools = ScheduleTools(tmp_path)
    out = tools.handle_tool_call(
        "schedule_create",
        {"title": "t", "prompt": "p", "kind": "once", "when": "tomorrow 09:00"},
    )
    assert out["status"] == "created"
    assert out["schedule"]["kind"] == "once"
    assert out["schedule"]["next_run_at"].endswith("09:00:00+03:00")


def test_create_invalid_returns_error_not_raise(tmp_path):
    out = ScheduleTools(tmp_path).handle_tool_call(
        "schedule_create", {"title": "t", "prompt": "", "kind": "daily", "when": "09:00"}
    )
    assert "error" in out


def test_unknown_tool_is_error(tmp_path):
    assert "error" in ScheduleTools(tmp_path).handle_tool_call("nope", {})


# --- BUG 3: a once resolved into the past is rejected (was fired instantly) --


def test_create_once_in_the_past_is_rejected(tmp_path):
    out = ScheduleTools(tmp_path).handle_tool_call(
        "schedule_create",
        {"title": "t", "message": "hi", "kind": "once", "when": "2020-01-01T00:00"},
    )
    assert "error" in out
    assert "past" in out["error"].lower()


def test_create_once_within_grace_is_nudged_to_future(tmp_path):
    """A time that is 'basically now' (within the 60s grace) is scheduled at now+5s,
    not rejected and not fired instantly."""
    from akana_server.schedule.store import now_tr, to_iso

    just_now = to_iso(now_tr())  # right now → inside the grace window
    out = ScheduleTools(tmp_path).handle_tool_call(
        "schedule_create",
        {"title": "t", "message": "hi", "kind": "once", "when": just_now},
    )
    assert out["status"] == "created"
    nxt = parse_iso(out["schedule"]["next_run_at"])
    assert nxt > now_tr()  # pushed into the (near) future, never the past


# --- BUG 9: verbatim message mode -------------------------------------------


def test_create_message_mode_stores_verbatim_and_no_prompt(tmp_path):
    out = ScheduleTools(tmp_path).handle_tool_call(
        "schedule_create",
        {"title": "su", "message": "Suyu iç", "kind": "daily", "when": "09:00"},
    )
    assert out["status"] == "created"
    sched = out["schedule"]
    assert sched["message"] == "Suyu iç"
    assert sched["prompt"] == ""


def test_create_rejects_both_prompt_and_message(tmp_path):
    out = ScheduleTools(tmp_path).handle_tool_call(
        "schedule_create",
        {"title": "t", "prompt": "p", "message": "m", "kind": "daily", "when": "09:00"},
    )
    assert "error" in out and "not both" in out["error"]


def test_create_rejects_neither_prompt_nor_message(tmp_path):
    out = ScheduleTools(tmp_path).handle_tool_call(
        "schedule_create", {"title": "t", "kind": "daily", "when": "09:00"}
    )
    assert "error" in out


# --- BUG 6: kind change on update requires a new 'when' ----------------------


def test_update_kind_without_when_errors(tmp_path):
    tools = ScheduleTools(tmp_path)
    sid = tools.handle_tool_call(
        "schedule_create", {"title": "t", "prompt": "p", "kind": "daily", "when": "09:00"}
    )["schedule"]["id"]
    out = tools.handle_tool_call("schedule_update", {"id": sid, "kind": "interval"})
    assert "error" in out
    # unchanged: still daily
    assert tools.handle_tool_call("schedule_list", {})["schedules"][0]["kind"] == "daily"


def test_update_kind_with_when_changes_kind(tmp_path):
    tools = ScheduleTools(tmp_path)
    sid = tools.handle_tool_call(
        "schedule_create", {"title": "t", "prompt": "p", "kind": "daily", "when": "09:00"}
    )["schedule"]["id"]
    out = tools.handle_tool_call(
        "schedule_update", {"id": sid, "kind": "interval", "when": "3600"}
    )
    assert out["status"] == "updated"
    assert out["schedule"]["kind"] == "interval"


# --- BUG 7: an unparseable weekday name on update is an error ----------------


def test_update_bad_weekday_name_errors(tmp_path):
    tools = ScheduleTools(tmp_path)
    sid = tools.handle_tool_call(
        "schedule_create",
        {"title": "t", "prompt": "p", "kind": "weekly", "when": "09:00", "weekday": 0},
    )["schedule"]["id"]
    out = tools.handle_tool_call(
        "schedule_update", {"id": sid, "weekday": "blursday"}
    )
    assert "error" in out
    # weekday unchanged (still Monday=0), NOT silently kept-while-reporting-success
    assert tools.handle_tool_call("schedule_list", {})["schedules"][0]["weekday"] == 0


# --- BUG 5: re-enabling a spent once without a new time errors ---------------


def test_update_reenable_spent_once_requires_new_when(tmp_path):
    from akana_server.schedule.store import ScheduleStore, now_tr, to_iso

    # A once that already fired: create it, then mark it ran (→ disabled, past time).
    store = ScheduleStore(tmp_path)
    past = parse_iso(to_iso(now_tr())).replace(microsecond=0)
    item = store.create(
        title="t", message="hi", kind="once", when=to_iso(past), now=past
    )
    store.mark_ran(item.id, status="ok", now=past)

    tools = ScheduleTools(tmp_path)
    # Re-enable with NO new 'when' → rejected (would fire instantly otherwise).
    out = tools.handle_tool_call("schedule_update", {"id": item.id, "enabled": True})
    assert "error" in out
    # Re-enable WITH a fresh future 'when' → allowed.
    ok = tools.handle_tool_call(
        "schedule_update",
        {"id": item.id, "enabled": True, "when": "in 2 hours"},
    )
    assert ok["status"] == "updated" and ok["schedule"]["enabled"] is True


# --- connector-enabled safety gate ------------------------------------------


def test_connector_delivery_blocked_when_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("AKANA_TELEGRAM_ENABLED", raising=False)
    out = ScheduleTools(tmp_path).handle_tool_call(
        "schedule_create",
        {
            "title": "t", "prompt": "p", "kind": "daily", "when": "09:00",
            "delivery_mode": "connector", "channel": "telegram", "chat_id": "1",
        },
    )
    assert "error" in out
    assert "not enabled" in out["error"]


def test_connector_delivery_allowed_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("AKANA_TELEGRAM_ENABLED", "1")
    out = ScheduleTools(tmp_path).handle_tool_call(
        "schedule_create",
        {
            "title": "t", "prompt": "p", "kind": "daily", "when": "09:00",
            "delivery_mode": "connector", "channel": "telegram", "chat_id": "1",
        },
    )
    assert out["status"] == "created"
    assert out["schedule"]["delivery"]["mode"] == "connector"


# --- MCP server smoke -------------------------------------------------------


def test_mcp_tool_list_exposes_schedule_tools():
    names = {t["name"] for t in mcp_tool_list()}
    assert names == _NAMES
    for t in mcp_tool_list():
        assert t["description"]
        assert t["inputSchema"]["type"] == "object"


def test_mcp_call_create_list_cancel(tmp_path):
    srv = McpServer(ScheduleTools(tmp_path, created_by="assistant"))

    def call(name, arguments):
        resp = srv.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        result = resp["result"]
        return result["isError"], json.loads(result["content"][0]["text"])

    is_err, payload = call("schedule_create", {"title": "t", "prompt": "p", "kind": "daily", "when": "09:00"})
    assert is_err is False and payload["status"] == "created"
    sid = payload["schedule"]["id"]

    is_err, payload = call("schedule_list", {})
    assert is_err is False and payload["count"] == 1

    is_err, payload = call("schedule_cancel", {"id": sid})
    assert is_err is False and payload["removed"] is True


def test_mcp_initialize_reports_server_info():
    srv = McpServer(ScheduleTools("."))
    resp = srv.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    assert resp["result"]["serverInfo"]["name"] == "akana-schedule"


# --- native (gemini/openai) parity ------------------------------------------


def test_decls_cover_full_surface():
    names = [d["name"] for d in SCHEDULE_TOOL_DECLS]
    assert set(names) == _NAMES
    # Derived single-source from the MCP schemas.
    assert {s["name"] for s in schedule_schemas()} == _NAMES
    create = next(d for d in SCHEDULE_TOOL_DECLS if d["name"] == "schedule_create")
    # 'prompt' is no longer required on its own — a schedule may instead carry a
    # verbatim 'message' (exactly one of the two, enforced at runtime).
    assert create["parameters"]["required"] == ["title", "kind", "when"]
    assert "message" in create["parameters"]["properties"]


def test_merged_into_gemini_and_openai_surfaces():
    gem = {d["name"] for d in gt.GEMINI_TOOL_DECLS}
    assert _NAMES <= gem
    oai = {d["function"]["name"] for d in OPENAI_TOOL_DECLS}
    assert _NAMES <= oai


def test_gemini_dispatch_routes_to_schedule(tmp_path):
    out = gt.dispatch_gemini_tool(_settings(tmp_path), "c", "schedule_list", {})
    assert "No schedules" in out


def test_llm_dispatch_routes_to_schedule(tmp_path):
    out = dispatch_llm_tool(_settings(tmp_path), "c", "schedule_list", {})
    assert "No schedules" in out


def test_dispatch_returns_none_for_non_schedule(tmp_path):
    assert dispatch_schedule_tool(_settings(tmp_path), "c", "vault_get", {}) is None


def test_dispatch_gated_off_by_setting(tmp_path, monkeypatch):
    """The schedule_tools_enabled gate applies to the NATIVE dispatch surface too
    (not just the MCP-spawn path) — so 'disabled' actually stops gemini/openai/
    ollama/voice from creating schedules, matching the setting's promise."""
    monkeypatch.setenv("AKANA_SCHEDULE_TOOLS", "0")
    out = dispatch_schedule_tool(_settings(tmp_path), "c", "schedule_list", {})
    assert out is not None and "turned off" in out.lower()
    # And nothing was created when create is attempted while disabled.
    out2 = dispatch_schedule_tool(
        _settings(tmp_path), "c", "schedule_create",
        {"title": "x", "prompt": "p", "kind": "once", "when": "2030-01-01T09:00"},
    )
    assert "turned off" in out2.lower()
