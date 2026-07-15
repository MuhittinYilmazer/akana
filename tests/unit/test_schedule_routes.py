"""ScheduleEngine REST routes — CRUD + run-now over a minimal mounted app.

Hermetic: a bare FastAPI app mounts only the schedule router; ``app.state.settings``
points at a ``tmp_path`` data dir with no token (loopback owner skips auth). The
run-now path monkeypatches the LLM + thread-append so no provider or ``memory.db``
is touched.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from akana_server.api.routes import schedule as schedule_routes
from akana_server.orchestrator import llm_dispatch, memory_tools
from akana_server.schedule import engine


@pytest.fixture
def client(tmp_path):
    app = FastAPI()
    app.state.settings = SimpleNamespace(data_dir=tmp_path, api_token="")
    app.include_router(schedule_routes.router, prefix="/api/v1")
    return TestClient(app)


def _create(client, **over):
    body = {"title": "t", "prompt": "p", "kind": "interval", "when": "3600"}
    body.update(over)
    return client.post("/api/v1/schedule", json=body)


def test_create_list_patch_delete(client):
    r = _create(client, delivery={"mode": "thread"})
    assert r.status_code == 200, r.text
    sid = r.json()["schedule"]["id"]

    assert client.get("/api/v1/schedule").json()["count"] == 1

    r = client.patch(f"/api/v1/schedule/{sid}", json={"title": "renamed"})
    assert r.status_code == 200
    assert r.json()["schedule"]["title"] == "renamed"

    r = client.delete(f"/api/v1/schedule/{sid}")
    assert r.status_code == 200 and r.json()["removed"] is True
    assert client.get("/api/v1/schedule").json()["count"] == 0


def test_create_validation_error_is_422(client):
    r = _create(client, prompt="")  # empty prompt is rejected
    assert r.status_code == 422
    assert r.json()["detail"]["error"]["code"] == "VALIDATION"


def test_create_message_mode_via_rest(client):
    """BUG 9: the REST create path accepts a verbatim `message` (no prompt)."""
    r = client.post(
        "/api/v1/schedule",
        json={"title": "su", "message": "Suyu iç", "kind": "daily", "when": "09:00"},
    )
    assert r.status_code == 200, r.text
    sched = r.json()["schedule"]
    assert sched["message"] == "Suyu iç" and sched["prompt"] == ""


def test_create_once_in_past_via_rest_is_422(client):
    """BUG 3: a once resolved into the past is rejected at the REST surface too."""
    r = client.post(
        "/api/v1/schedule",
        json={"title": "t", "message": "hi", "kind": "once", "when": "2020-01-01T00:00"},
    )
    assert r.status_code == 422


def test_create_bad_json_is_400(client):
    r = client.post(
        "/api/v1/schedule", content="not json", headers={"content-type": "application/json"}
    )
    assert r.status_code == 400


def test_patch_unknown_is_404(client):
    assert client.patch("/api/v1/schedule/nope", json={"title": "x"}).status_code == 404


def test_delete_unknown_is_404(client):
    assert client.delete("/api/v1/schedule/nope").status_code == 404


def test_run_unknown_is_404(client):
    assert client.post("/api/v1/schedule/nope/run").status_code == 404


def test_run_now_fires_and_delivers_to_thread(client, monkeypatch):
    sid = _create(client, prompt="do it").json()["schedule"]["id"]

    async def fake(settings, prompt, **kw):
        return ("RESULT", {}, None)

    monkeypatch.setattr(llm_dispatch, "complete_chat_aggregated", fake)
    monkeypatch.setattr(memory_tools, "mcp_servers_payload", lambda *a, **k: None)
    appended: list = []
    monkeypatch.setattr(engine, "_append_turn_pair", lambda *a: appended.append(a))

    class _Convs:
        def create(self, *, title=None):
            return SimpleNamespace(id="conv-1")

        def get(self, cid):
            return None

    client.app.state.conversation_service = _Convs()

    r = client.post(f"/api/v1/schedule/{sid}/run")
    assert r.status_code == 200, r.text
    assert r.json()["run"]["status"] == "ok"
    assert appended, "run-now should have delivered to a thread"
