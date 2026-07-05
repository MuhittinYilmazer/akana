"""Cursor subagent nesting: ``parent_id`` must reach the wire tool_call payload.

The Cursor bridge (cursor_bridge/lib.mjs) expands a completed subagent's inner
conversation steps into child wire events carrying ``parent_id`` (the task's
call_id), so the frontend nests them exactly like Claude's subagent groups
(see akana-chat-render.js: subagentBodyFor). ``CursorStreamDecoder.feed`` must
pass ``parent_id`` through untouched for both "start" and "end" phases, and
default to ``None`` for ordinary (non-nested) tool events.
"""

from __future__ import annotations

from akana_server.orchestrator.base import CursorStreamDecoder


def test_feed_tool_start_carries_parent_id() -> None:
    dec = CursorStreamDecoder(model="composer-2")
    out = dec.feed(
        {"ev": "tool", "phase": "start", "call_id": "c0", "parent_id": "t0", "name": "read", "args": {}}
    )
    assert len(out) == 1
    assert out[0]["tool_call"]["parent_id"] == "t0"
    assert out[0]["tool_call"]["id"] == "c0"
    assert dec.tool_calls[0]["parent_id"] == "t0"


def test_feed_tool_end_carries_parent_id() -> None:
    dec = CursorStreamDecoder(model="composer-2")
    dec.feed({"ev": "tool", "phase": "start", "call_id": "c0", "parent_id": "t0", "name": "read", "args": {}})
    out = dec.feed(
        {
            "ev": "tool",
            "phase": "end",
            "call_id": "c0",
            "parent_id": "t0",
            "name": "read",
            "result": {"success": {}},
            "status": "completed",
        }
    )
    assert len(out) == 1
    assert out[0]["tool_call"]["parent_id"] == "t0"


def test_feed_normal_tool_has_no_parent_id() -> None:
    dec = CursorStreamDecoder(model="composer-2")
    out = dec.feed({"ev": "tool", "phase": "start", "call_id": "c1", "name": "shell", "args": {}})
    assert out[0]["tool_call"]["parent_id"] is None
