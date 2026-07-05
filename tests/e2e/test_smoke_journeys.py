"""Golden journeys — end-to-end smoke with real lifespan + fake LLM.

Each journey enters from the user surface (HTTP/SSE/WS), goes through the real
app wiring (store/services set up in the lifespan), and validates the
persistent side effect (persist, WS event, store record). Nothing is faked
except the LLM/search — an integration break blows up here, not in front of
the user.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.e2e.conftest import events_of, parse_sse

pytestmark = pytest.mark.e2e

CHAT_URL = "/api/v1/chat"


def _chat(client: TestClient, text: str, **extra) -> dict[str, Any]:
    r = client.post(CHAT_URL, json={"text": text, **extra})
    assert r.status_code == 200, r.text
    return r.json()


# -- (a) blocking chat → reply + persist ----------------------------------------------


def test_blocking_chat_replies_and_persists(
    client: TestClient, fake_llm: list[str]
) -> None:
    body = _chat(client, "merhaba, bugün nasılsın?")
    assert body["text"].startswith("yanit:")
    conv_id = body["conversation_id"]
    assert conv_id

    # conversation appears in the list + messages landed in the episodic archive
    listed = client.get("/api/v1/conversations").json()["conversations"]
    assert conv_id in [c["id"] for c in listed]
    messages = client.get(f"/api/v1/conversations/{conv_id}/messages").json()[
        "messages"
    ]
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant"], f"roles {roles}"
    assert messages[0]["content"] == "merhaba, bugün nasılsın?"
    assert messages[1]["content"] == body["text"]


# -- (b) SSE chat stream → deltas + final -------------------------------------------


def test_sse_chat_streams_deltas_and_persists(client: TestClient, fake_stream) -> None:
    conv_id = client.post("/api/v1/conversations", json={"title": "SSE smoke"}).json()[
        "id"
    ]
    with client.stream(
        "POST",
        f"{CHAT_URL}/stream",
        json={"text": "kendini tanıt", "conversation_id": conv_id},
    ) as response:
        assert response.status_code == 200
        body = response.read().decode("utf-8")

    events = parse_sse(body)
    deltas = [p.get("text", "") for p in events_of(events, "delta")]
    assert "".join(deltas) == "Merhaba, ben Akana."
    done = events_of(events, "done")
    assert done and done[-1]["text"] == "Merhaba, ben Akana."

    messages = client.get(f"/api/v1/conversations/{conv_id}/messages").json()[
        "messages"
    ]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "Merhaba, ben Akana."


# -- (d) skill injection → skill_used populated ------------------------------------------


def _write_skill(root: Path, skill_id: str, trigger: str) -> None:
    d = root / "skills" / skill_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.yaml").write_text(
        "\n".join(
            [
                f"id: {skill_id}",
                "version: 1",
                f'title: "{skill_id} smoke"',
                f'description: "{skill_id} e2e smoke yeteneği"',
                "triggers:",
                f'  - "{trigger}"',
                "risk: low",
                "requires_approval: false",
                "tools_allowed: []",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (d / "SKILL.md").write_text(
        f"# {skill_id}\n\nSmoke raporunu şablona göre derle.\n", encoding="utf-8"
    )


def test_skill_trigger_injects_into_turn(
    e2e_data_dir: Path, make_client, fake_llm: list[str]
) -> None:
    _write_skill(e2e_data_dir, "smoke_rapor", trigger="smoke raporu hazırla")
    from akana_server.skills.registry import reload_skills

    reload_skills()
    client = make_client()

    body = _chat(client, "smoke raporu hazırla lütfen")
    assert [s["id"] for s in body["skill_used"]] == ["smoke_rapor"]
    assert body["skill_used"][0]["status"] == "injected"
    # the SKILL.md body made it into the agent prompt
    assert any("[Capability: smoke_rapor" in p for p in fake_llm)
    assert any("Smoke raporunu şablona göre derle" in p for p in fake_llm)


# -- (g) settings: PUT llm-settings → system/status reflection --------------------------


def test_llm_settings_provider_roundtrip(client: TestClient) -> None:
    before = client.get("/api/v1/system/status").json()
    assert before["chat_path"] == "cursor"

    r = client.put("/api/v1/system/llm-settings", json={"provider": "claude"})
    assert r.status_code == 200, r.text
    assert r.json()["active_provider"] == "claude"

    after = client.get("/api/v1/system/status").json()
    assert after["chat_path"] == "claude"
    assert after["model"]["provider"] == "claude"
    assert after["model"]["active_tag"] == after["model"]["claude_tag"]


# -- (h) memory (v2): create fact → find via recall ---------------------------------------
# The v1 /api/v1/memory routes were retired (3× memory merge); chat+memory live in
# a single canonical v2 store. This journey now exercises the v2 surface end-to-end.


def test_memory_create_and_recall(client: TestClient) -> None:
    r = client.post(
        "/api/v1/memory/facts", json={"value": "favori rengim: mor", "kind": "fact"}
    )
    assert r.status_code == 200, r.text

    recall = client.get("/api/v1/memory/recall", params={"q": "favori rengim"}).json()
    joined = " ".join(it.get("summary", "") for it in recall["items"])
    assert "mor" in joined.lower(), f"recall does not contain 'mor': {recall}"
