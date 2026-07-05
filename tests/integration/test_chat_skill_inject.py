"""WI-1 + WI-2 — skill injection on the chat path (HTTP level).

FULL AUTONOMY: no approval gate — a strongly matching skill (including those
marked ``requires_approval``) is injected without approval in a single turn.

No real LLM/Ghidra: the cursor client is monkeypatched; skills are written to a
temporary data_dir (fake registry content), MCP is fake.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app
from akana_server.skills.registry import reload_skills


def _write_skill(
    root: Path,
    skill_id: str,
    *,
    triggers: list[str],
    requires_approval: bool = False,
    tools_allowed: list[str] | None = None,
    body: str = "Adım adım uygula.",
) -> None:
    d = root / "skills" / skill_id
    d.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": skill_id,
        "version": 1,
        "title": f"{skill_id} başlığı",
        "description": f"{skill_id} açıklaması",
        "triggers": triggers,
        "risk": "medium",
        "requires_approval": requires_approval,
        "tools_allowed": tools_allowed or [],
    }
    lines = [
        f"id: {manifest['id']}",
        "version: 1",
        f"title: \"{manifest['title']}\"",
        f"description: \"{manifest['description']}\"",
        "triggers:",
        *[f"  - \"{t}\"" for t in triggers],
        "risk: medium",
        f"requires_approval: {'true' if requires_approval else 'false'}",
        "tools_allowed:" if tools_allowed else "tools_allowed: []",
        *[f"  - {t}" for t in (tools_allowed or [])],
    ]
    (d / "manifest.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (d / "SKILL.md").write_text(f"# {skill_id}\n\n{body}\n", encoding="utf-8")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8766")
    monkeypatch.setenv("CURSOR_API_KEY", "x")
    # Hermetic isolation: these tests measure the injection mechanics with
    # CONTROLLED fake skills (re_analyze/re_setup). create_app()'s lifespan
    # normally COPIES ALL discovered real packs into data_dir/skills
    # (SkillsAdapter) → it OVERWRITES the fake skills with the same id (re-pack's
    # real re_analyze/re_setup, with different triggers/body) and leaks
    # broad-trigger skills like daily_brief ("bugün" → wrong match to plain
    # chat). No-op register_all and leave the tmp data_dir with only the fake
    # skills this test writes.
    monkeypatch.setattr(
        "akana_server.packs.host.AkanaPackHost.register_all",
        lambda self: [],
    )
    _write_skill(
        tmp_path,
        "re_analyze",
        triggers=["şu exe'yi analiz et"],
        tools_allowed=["ghidra.analyze", "memory_remember"],
        body="Ghidra MCP araçlarıyla binary'yi analiz et ve raporla.",
    )
    _write_skill(
        tmp_path,
        "re_setup",
        triggers=["ghidra kur"],
        requires_approval=True,
        body="Ghidra + GhidraMCP kurulum playbook'u.",
    )
    reload_skills()
    app = create_app()
    with TestClient(app) as c:
        yield c
    reload_skills()


def _mock_blocking_llm(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_complete(settings, user_text, **kwargs):
        calls.append({"text": user_text, **kwargs})
        return "tamamdır.", {"prompt_tokens": 1, "completion_tokens": 1, "tool_calls": []}

    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage", fake_complete
    )
    return calls


def _parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
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


def _events_of(events, name: str) -> list[dict[str, Any]]:
    return [p for n, p in events if n == name]


# -- WI-1: suggest → inject (blocking) ----------------------------------------------


def test_blocking_trigger_injects_skill_body(client: TestClient, monkeypatch) -> None:
    calls = _mock_blocking_llm(monkeypatch)
    r = client.post("/api/v1/chat", json={"text": "şu exe'yi analiz et lütfen"})
    assert r.status_code == 200
    data = r.json()
    assert [s["id"] for s in data["skill_used"]] == ["re_analyze"]
    assert data["skill_used"][0]["status"] == "injected"
    assert data["skill_used"][0]["match_reason"] == "trigger_exact"
    # The SKILL.md body (L2) entered the agent prompt as a [Capability] block
    assert len(calls) == 1
    assert "[Capability: re_analyze" in calls[0]["text"]
    assert "Ghidra MCP araçlarıyla binary'yi analiz et" in calls[0]["text"]
    # ghidra not mounted → missing-tool signal
    assert data["skill_used"][0]["missing_tools"] == ["ghidra"]


def test_blocking_plain_chat_has_no_skill(client: TestClient, monkeypatch) -> None:
    calls = _mock_blocking_llm(monkeypatch)
    r = client.post("/api/v1/chat", json={"text": "bugün hava nasıl olur sence"})
    assert r.status_code == 200
    assert r.json()["skill_used"] == []
    assert "[Capability:" not in calls[0]["text"]


def test_blocking_registry_error_does_not_break_turn(
    client: TestClient, monkeypatch
) -> None:
    """Error resilience: even if the suggestion layer crashes, the turn flows normally."""
    calls = _mock_blocking_llm(monkeypatch)

    def boom(_data_dir):
        raise RuntimeError("registry çöktü")

    monkeypatch.setattr(
        "akana_server.skills.turn_injection.get_registry", boom
    )
    r = client.post("/api/v1/chat", json={"text": "şu exe'yi analiz et"})
    assert r.status_code == 200
    assert r.json()["skill_used"] == []
    assert r.json()["text"] == "tamamdır."
    assert len(calls) == 1


# -- WI-1: SSE path -----------------------------------------------------------------


def test_stream_emits_skill_used_event_and_injects(
    client: TestClient, monkeypatch
) -> None:
    async def fake_stream(settings, user_text, **kwargs) -> AsyncIterator[dict[str, Any]]:
        fake_stream.captured = user_text  # type: ignore[attr-defined]
        yield {"delta": "analiz bitti", "done": False}
        yield {
            "done": True,
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "tool_calls": []},
            "status": "finished",
            "tool_calls": [],
        }

    monkeypatch.setattr(
        "akana_server.api.routes.chat.stream_user_chat", fake_stream
    )
    with client.stream(
        "POST", "/api/v1/chat/stream", json={"text": "şu exe'yi analiz et"}
    ) as response:
        assert response.status_code == 200
        body = response.read().decode("utf-8")
    events = _parse_sse(body)
    skill_events = _events_of(events, "skill_used")
    assert skill_events and skill_events[0]["skills"][0]["id"] == "re_analyze"
    assert skill_events[0]["skills"][0]["status"] == "injected"
    done = _events_of(events, "done")[-1]
    assert done["skill_used"][0]["id"] == "re_analyze"
    assert "[Capability: re_analyze" in fake_stream.captured  # type: ignore[attr-defined]


# -- FULL AUTONOMY: a requires_approval skill is INJECTED directly without approval ---


def test_requires_approval_skill_injected_without_approval(
    client: TestClient, monkeypatch
) -> None:
    """Approval gate removed: 'ghidra kur' (re_setup, requires_approval=True) is
    injected without approval in a single turn and the LLM is called with the
    body in that same turn — no intermediate «approve» turn."""
    calls = _mock_blocking_llm(monkeypatch)
    r = client.post("/api/v1/chat", json={"text": "ghidra kur"})
    assert r.status_code == 200
    data = r.json()
    injected = [s for s in data["skill_used"] if s["status"] == "injected"]
    assert [s["id"] for s in injected] == ["re_setup"]
    # the body entered the agent prompt in a SINGLE turn
    assert len(calls) == 1
    assert "[Capability: re_setup" in calls[0]["text"]
    assert "kurulum playbook" in calls[0]["text"]
    # NO approval reflex
    assert not data.get("approval_required")
    assert data.get("action") != "skill_approval"


