"""PR-T1 — tool gateway audit + recent endpoint."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app
from akana_server.audit import read_tail
from akana_server.tools.gateway import (
    list_recent_tool_calls,
    record_tool_call,
    reset_recent_for_tests,
)

MOCK_TOOL_CALL = {
    "id": "call-abc",
    "name": "shell",
    "phase": "start",
    "args": {"command": "pytest -q"},
    "status": None,
}


@pytest.fixture(autouse=True)
def _clear_recent() -> None:
    reset_recent_for_tests()
    yield
    reset_recent_for_tests()


def test_record_tool_call_audit(tmp_path: Path) -> None:
    record_tool_call(
        tmp_path,
        MOCK_TOOL_CALL,
        turn_id="01TURN",
        conv_id="01CONV",
        mode="stream",
    )

    events = read_tail(tmp_path, limit=10)
    assert len(events) == 1
    assert events[0]["kind"] == "tool_call"
    assert events[0]["turn_id"] == "01TURN"
    assert events[0]["conv_id"] == "01CONV"
    assert events[0]["data"]["tool"]["name"] == "shell"
    assert events[0]["data"]["mode"] == "stream"

    recent = list_recent_tool_calls(limit=20)
    assert len(recent) == 1
    assert recent[0]["call"]["name"] == "shell"


def test_tool_name_from_toolname_and_nested(tmp_path: Path) -> None:
    """Cursor SDK sends toolCall.toolName, not toolCall.name."""
    cases = [
        ({"id": "1", "toolName": "Shell"}, "Shell"),
        ({"id": "2", "toolCall": {"toolName": "grep"}}, "grep"),
        (
            {"id": "3", "toolCall": {"providerIdentifier": "akana_memory", "toolName": "memory_search"}},
            "akana_memory/memory_search",
        ),
        ({"id": "4", "function": {"name": "read_file"}}, "read_file"),
    ]
    for call, expected in cases:
        record_tool_call(tmp_path, call, turn_id="01T", conv_id="01C")
        events = read_tail(tmp_path, limit=1)
        assert events[-1]["data"]["tool"]["name"] == expected, call


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8766")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_tools_recent_endpoint(client: TestClient, tmp_path: Path) -> None:
    record_tool_call(
        tmp_path,
        MOCK_TOOL_CALL,
        turn_id="01TURN2",
        conv_id="01CONV2",
    )
    r = client.get("/api/v1/tools/recent?limit=20")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["tools"][0]["call"]["name"] == "shell"

