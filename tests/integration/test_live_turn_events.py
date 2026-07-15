"""End-to-end: a background schedule/task fire pushes a live ``turn_completed``
event to ``/ws/events`` — the fix for "the reminder's chat doesn't show up / the
task thread looks frozen until I refresh the page".

Real app + real lifespan (EventHub + schedule engine + task runner started), a
real ``/ws/events`` WebSocket client, and a FAKE ``complete_chat_aggregated`` so
no provider is needed. Proves the whole chain: REST fire → engine/runner → a
conversation is created + turns appended → EventHub broadcast → the browser's WS
client receives the event that drives the sidebar refresh + toast + log reload.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8766")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    # Silence the LLM: both the schedule engine and the task runner call
    # llm_dispatch.complete_chat_aggregated — return canned text so no provider is hit.
    from akana_server.orchestrator import llm_dispatch

    async def _fake(settings, user_text, **kwargs):
        return ("Here is your result.", {"prompt_tokens": 3, "completion_tokens": 5}, None)

    monkeypatch.setattr(llm_dispatch, "complete_chat_aggregated", _fake)
    with TestClient(create_app()) as c:
        yield c


def _drain_ready(ws):
    """The hub sends a {"type":"ready"} frame on connect — consume it."""
    first = ws.receive_json()
    assert first.get("type") == "ready"


def _await_event(ws, want_type, *, max_frames=10):
    for _ in range(max_frames):
        evt = ws.receive_json()
        if evt.get("type") == want_type:
            return evt
    raise AssertionError(f"did not receive a {want_type!r} event")


def test_schedule_run_pushes_turn_completed(client):
    # Create a reminder, connect the events socket, then fire it via the test button.
    created = client.post(
        "/api/v1/schedule",
        json={
            "title": "Test reminder",
            "prompt": "remind me",
            "kind": "once",
            "when": "2030-01-01T09:00",
            "delivery": {"mode": "thread"},
        },
    ).json()
    sid = created["schedule"]["id"]

    with client.websocket_connect("/ws/events") as ws:
        _drain_ready(ws)
        run = client.post(f"/api/v1/schedule/{sid}/run").json()
        assert run["ok"] is True
        evt = _await_event(ws, "turn_completed")
        # The event names the reminder's freshly-created conversation → the UI shows
        # the thread in the sidebar + toasts (status ok).
        assert evt["conversation_id"]
        assert evt["status"] == "ok"

