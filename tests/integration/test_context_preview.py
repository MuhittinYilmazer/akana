"""GET /api/v1/context/preview — compiled context preview (ContextEngine F0).

Contract: same compiler as a real turn, side-effect free (no record is CREATED
for an unknown conversation → 404), bearer-protected; persona binding is visible
in the preview.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app
from akana_server.memory_core import get_memory_core
from akana_server.persona.registry import reset_persona_registries

URL = "/api/v1/context/preview"
CONV = "conv-preview-1"

BASE_ENV = {
    "AKANA_TOKEN": "",
    "AKANA_PORT": "8766",
    "CURSOR_API_KEY": "",
}


@pytest.fixture
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    reset_persona_registries()
    yield tmp_path
    reset_persona_registries()


@pytest.fixture
def client(data_dir):
    app = create_app()
    with TestClient(app) as c:
        yield c


def _seed_conversation(data_dir) -> None:
    # The conversation list moved to the v2 store (3x memory consolidation); the
    # preview endpoint also reads from v2 → seed the same canonical store (memory.db).
    get_memory_core(data_dir).conversations_meta.ensure(CONV)


def test_conversation_id_zorunlu(client: TestClient) -> None:
    assert client.get(URL).status_code == 422  # query parameter missing
    r = client.get(URL, params={"conversation_id": "  "})
    assert r.status_code == 400
    assert r.json()["detail"]["error"]["code"] == "CONVERSATION_REQUIRED"


def test_bilinmeyen_konusma_404_ve_kayit_olusmaz(client: TestClient, data_dir) -> None:
    r = client.get(URL, params={"conversation_id": "yok-boyle-konusma"})
    assert r.status_code == 404
    # preview is side-effect free: no record is CREATED in the canonical v2 store
    assert get_memory_core(data_dir).conversations_meta.get("yok-boyle-konusma") is None


def test_default_onizleme_akana_personasi(client: TestClient, data_dir) -> None:
    _seed_conversation(data_dir)
    r = client.get(URL, params={"conversation_id": CONV, "text": "merhaba"})
    assert r.status_code == 200, r.text
    body = r.json()
    # Since the installed capability catalog (WI-2) is appended to the persona base,
    # the prompt is no longer the bare default → default=False (the catalog MUST be
    # SENT to the LLM; if it were None the client would add a plain prefix and the LLM
    # would not know its capabilities). The persona identity is still akana.
    assert body["persona"]["id"] == "akana" and body["persona"]["source"] == "builtin"
    assert body["persona"]["default"] is False
    # Since the persona title can be personalized (e.g. "[Akana — X's personal
    # assistant]"), we check the stable block start rather than a fixed title.
    assert "[Akana —" in body["system_prompt"]
    assert "[INSTALLED CAPABILITIES]" in body["system_prompt"]
    assert body["user_text"] == "merhaba"
    assert body["history"] == [] and body["dropped_turns"] == 0
    assert body["injected_blocks"] == []
    assert body["trace"]["budget"]["max_chars"] > 0


def test_persona_baglamasi_onizlemede_gorunur(client: TestClient, data_dir) -> None:
    _seed_conversation(data_dir)
    created = client.post(
        "/api/v1/personas",
        json={"id": "resmi", "name": "Resmî", "system_prompt": "Resmî ve kısa konuş."},
    )
    assert created.status_code == 201, created.text
    bound = client.put(
        "/api/v1/personas/resmi/bind", json={"conversation_id": CONV}
    )
    assert bound.status_code == 200, bound.text
    body = client.get(URL, params={"conversation_id": CONV}).json()
    assert body["persona"]["id"] == "resmi" and body["persona"]["default"] is False
    # Persona base + installed capability catalog (WI-2) are appended.
    assert body["system_prompt"].startswith("Resmî ve kısa konuş.")
    assert "[INSTALLED CAPABILITIES]" in body["system_prompt"]


def test_chat_turu_davranis_notr_ve_persona_baglanir(
    client: TestClient, data_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before/after: on a default turn the prompt sent to the LLM is exactly the
    user text. The system override is no longer ``None`` since the installed
    capability catalog (WI-2) is appended — even on default akana a prefix+catalog
    is sent (so the LLM knows its capabilities). When a persona is bound, the
    persona base + catalog are sent."""
    captured: dict[str, object] = {}

    async def fake_complete(settings, prompt, **kwargs):
        captured["prompt"] = prompt
        captured["system_prompt"] = kwargs.get("system_prompt")
        return "tamam", {"prompt_tokens": 1, "completion_tokens": 1, "tool_calls": []}

    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage", fake_complete
    )
    _seed_conversation(data_dir)

    r = client.post(
        "/api/v1/chat", json={"text": "merhaba dostum", "conversation_id": CONV}
    )
    assert r.status_code == 200, r.text
    assert captured["prompt"] == "merhaba dostum"  # byte-for-byte equal to old composition
    # Since the catalog (installed capabilities) is appended, even default akana sends an override.
    assert captured["system_prompt"] is not None
    assert "[INSTALLED CAPABILITIES]" in captured["system_prompt"]

    client.post(
        "/api/v1/personas",
        json={"id": "kuru", "name": "Kuru", "system_prompt": "Kuru espriyle konuş."},
    )
    client.put("/api/v1/personas/kuru/bind", json={"conversation_id": CONV})
    r = client.post(
        "/api/v1/chat", json={"text": "merhaba dostum", "conversation_id": CONV}
    )
    assert r.status_code == 200, r.text
    assert captured["system_prompt"].startswith("Kuru espriyle konuş.")
    assert "[INSTALLED CAPABILITIES]" in captured["system_prompt"]
    assert captured["prompt"] == "merhaba dostum"  # user text is unchanged


def test_bearer_korumasi(monkeypatch: pytest.MonkeyPatch, data_dir) -> None:
    monkeypatch.setenv("AKANA_TOKEN", "gizli-token")
    app = create_app()
    with TestClient(app) as c:
        assert (
            c.get(
                URL,
                params={"conversation_id": CONV},
                headers={"X-Forwarded-For": "1.2.3.4"},
            ).status_code
            == 401
        )
        _seed_conversation(data_dir)
        r = c.get(
            URL,
            params={"conversation_id": CONV},
            headers={"Authorization": "Bearer gizli-token", "X-Forwarded-For": "1.2.3.4"},
        )
        assert r.status_code == 200
