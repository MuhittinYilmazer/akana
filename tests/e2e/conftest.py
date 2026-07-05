"""E2E smoke infrastructure — REAL lifespan app + fake LLM seams.

Philosophy: without forcing into application code (uvicorn subprocess not needed)
``create_app()`` + ``TestClient`` runs full lifespan; the only things faked
are the external world seams:

* LLM   → ``chat_routes.complete_chat_with_usage`` + ``llm_dispatch.…``
          (task handlers call via module) + ``stream_user_chat``
* Search → ``app.state.search_service`` (same seam as routes/search)

Isolation: each test runs with its own ``tmp_path`` data_dir; cron-like
services (session closer, telegram) are disabled via env.
Scheduler stays ON — first tick is 30s out so it won't enter the test window,
but the "missed" scan at startup runs for real (part of the journey).
"""

from __future__ import annotations

import concurrent.futures
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app
from akana_server.api.routes import chat as chat_routes
from akana_server.orchestrator import llm_dispatch
from akana_server.skills.registry import reload_skills

#: Plan-gate journey — response to be returned for planner decomposition prompt.
PLAN_LLM_TEXT = "- Eski logları yedekle\n- Logları sil\n- Raporu güncelle"  # Turkish literal preserved — asserted value

E2E_ENV = {
    "AKANA_TOKEN": "",  # bearer disabled — smoke surface kept simple
    "AKANA_PORT": "8766",
    "CURSOR_API_KEY": "",  # no LLM key → recall hybrid fallback
    "LLM_PROVIDER": "cursor",
    "AKANA_MEMORY_LLM_CAPTURE": "0",
    "AKANA_SESSION_CLOSER_ENABLED": "0",
    "AKANA_TELEGRAM_ENABLED": "0",
}


@pytest.fixture
def e2e_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Isolated data_dir + env with all external services disabled."""
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    for key, value in E2E_ENV.items():
        monkeypatch.setenv(key, value)
    return tmp_path


@pytest.fixture
def make_client(e2e_data_dir):
    """Real-lifespan app factory — lazy so tests can seed skills/store before
    the app OPENS."""
    clients: list[TestClient] = []
    reload_skills()

    def _make() -> TestClient:
        app = create_app()
        client = TestClient(app)
        client.__enter__()  # start lifespan
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.__exit__(None, None, None)
    reload_skills()  # don't leave tmp skill remnants in the global registry


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Deterministic fake LLM — records prompts.

    * plan/decomposition prompt → :data:`PLAN_LLM_TEXT` (plan-gate path)
    * everything else → ``yanit: <prompt start>``

    Both the function bound by the chat route under its own name and
    ``llm_dispatch`` called by task handlers via module are patched.
    """
    prompts: list[str] = []

    async def fake_complete(settings, prompt, **kwargs):
        prompts.append(prompt)
        if "[Akana plan modu]" in prompt or "[Akana decomposition modu]" in prompt:
            return PLAN_LLM_TEXT, {"prompt_tokens": 1, "completion_tokens": 1, "tool_calls": []}
        return f"yanit: {prompt[:80]}", {"prompt_tokens": 1, "completion_tokens": 1, "tool_calls": []}  # Turkish literal preserved — asserted value

    monkeypatch.setattr(chat_routes, "complete_chat_with_usage", fake_complete)
    monkeypatch.setattr(llm_dispatch, "complete_chat_with_usage", fake_complete)
    return prompts


@pytest.fixture
def fake_stream(monkeypatch: pytest.MonkeyPatch):
    """Fake ``stream_user_chat`` for the SSE path — injects a delta list."""

    state: dict[str, Any] = {"deltas": ["Merha", "ba, ", "ben Akana."], "prompts": []}  # Turkish literal preserved — asserted value

    async def fake_stream_chat(settings, user_text, **kwargs):
        state["prompts"].append(user_text)
        for piece in state["deltas"]:
            yield {"delta": piece, "done": False}
        yield {
            "done": True,
            "text": "".join(state["deltas"]),
            "usage": {"prompt_tokens": 2, "completion_tokens": 5, "tool_calls": []},
            "status": "finished",
            "tool_calls": [],
        }

    monkeypatch.setattr(chat_routes, "stream_user_chat", fake_stream_chat)
    return state


# -- helpers (imported by journey tests) -------------------------------------


def parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    """SSE body → [(event, data), ...] (same as existing integration tests)."""
    events: list[tuple[str, dict[str, Any]]] = []
    event_name = "message"
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
        if data_lines:
            try:
                payload = json.loads("\n".join(data_lines))
            except json.JSONDecodeError:
                payload = {"raw": "\n".join(data_lines)}
            events.append((event_name, payload))
    return events


def events_of(events: list[tuple[str, dict[str, Any]]], name: str) -> list[dict[str, Any]]:
    return [p for n, p in events if n == name]


def ws_receive_json(ws, timeout: float = 8.0) -> dict[str, Any]:
    """Receive a WS message with a time limit — prevents CI hanging if the event never arrives."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(ws.receive_json)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            pytest.fail(f"WS event did not arrive within {timeout}s")
