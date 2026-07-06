"""Packs REST surface — list, enable/disable hot-reload, rescan, auth."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app

URL = "/api/v1/packs"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8767")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    app = create_app()
    with TestClient(app) as c:  # lifespan runs register_all → app.state.pack_host
        yield c


def _find(client: TestClient, pack_id: str) -> dict:
    body = client.get(URL).json()
    return next(p for p in body["packs"] if p["id"] == pack_id)


def test_list_includes_ref_pack_enabled(client: TestClient) -> None:
    r = client.get(URL)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 1
    pack = next(p for p in body["packs"] if p["id"] == "user/pack-author-pack")
    assert pack["enabled"] is True
    assert pack["state"] == "enabled"
    assert pack["counts"]["skills"] > 0


def test_disable_then_enable_round_trip(client: TestClient) -> None:
    r = client.post(f"{URL}/disable", json={"pack_id": "user/pack-author-pack"})
    assert r.status_code == 200, r.text
    assert r.json()["pack"]["enabled"] is False
    assert _find(client, "user/pack-author-pack")["state"] == "disabled"

    r = client.post(f"{URL}/enable", json={"pack_id": "user/pack-author-pack"})
    assert r.status_code == 200, r.text
    assert r.json()["pack"]["enabled"] is True
    assert _find(client, "user/pack-author-pack")["state"] == "enabled"


def test_enable_unknown_pack_404(client: TestClient) -> None:
    r = client.post(f"{URL}/enable", json={"pack_id": "user/does-not-exist"})
    assert r.status_code == 404
    assert r.json()["detail"]["error"]["code"] == "PACK_NOT_FOUND"


def test_disable_missing_body_422(client: TestClient) -> None:
    assert client.post(f"{URL}/disable", json={}).status_code == 422


def test_rescan_returns_list(client: TestClient) -> None:
    r = client.post(f"{URL}/rescan")
    assert r.status_code == 200
    body = r.json()
    assert "added" in body and isinstance(body["added"], list)
    assert "removed" in body and isinstance(body["removed"], list)
    assert "updated" in body and isinstance(body["updated"], list)
    assert "packs" in body


# --------------------------------------------------------------------------- #
# MCP consent surface (the human-in-the-loop gate)                            #
# --------------------------------------------------------------------------- #

BROWSER = "user/browser-pack"


def test_consents_lists_pending_browser_mcp(client: TestClient) -> None:
    r = client.get(f"{URL}/consents")
    assert r.status_code == 200
    body = r.json()
    entry = next(c for c in body["consents"] if c["pack_id"] == BROWSER)
    # Enabling the pack must NOT auto-mount its MCP server — it stays pending.
    assert "browser" in entry["pending"]
    assert entry["mounted"] == []


def test_consent_mount_then_revoke_round_trip(client: TestClient) -> None:
    # Approve + mount.
    r = client.post(f"{URL}/consent", json={"pack_id": BROWSER})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["mounted"] == ["browser"]

    # Now it shows as mounted, not pending.
    entry = next(
        c for c in client.get(f"{URL}/consents").json()["consents"] if c["pack_id"] == BROWSER
    )
    assert entry["mounted"] == ["browser"]
    assert entry["pending"] == []

    # Revoke withdraws it.
    r = client.post(f"{URL}/consent/revoke", json={"pack_id": BROWSER})
    assert r.status_code == 200, r.text
    assert r.json()["removed"] == ["browser"]


def test_consent_unknown_pack_404(client: TestClient) -> None:
    r = client.post(f"{URL}/consent", json={"pack_id": "user/does-not-exist"})
    assert r.status_code == 404
    assert r.json()["detail"]["error"]["code"] == "PACK_NOT_FOUND"


def test_bearer_enforced(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "gizli-token")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    proxied = {"X-Forwarded-For": "1.2.3.4"}
    with TestClient(create_app()) as c:
        assert c.get(URL, headers=proxied).status_code == 401
        assert (
            c.get(URL, headers={**proxied, "Authorization": "Bearer gizli-token"}).status_code
            == 200
        )
