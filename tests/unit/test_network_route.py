"""NetworkEngine F0 — GET /api/v1/network/status (observability) + config.

Scope: the bearer requirement, the payload shape (config + breakers), setting
resolution from the runtime store, and breaker states reflecting into the snapshot.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app
from akana_server.network import load_network_config
from akana_server.network.guard import (
    global_registry,
    reset_global_registry,
)
from akana_server.runtime_settings import reset_runtime_stores

STATUS_URL = "/api/v1/network/status"


@pytest.fixture(autouse=True)
def _isolated():
    reset_global_registry()
    reset_runtime_stores()
    yield
    reset_global_registry()
    reset_runtime_stores()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_status_default_payload_shape(client: TestClient) -> None:
    r = client.get(STATUS_URL)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"config", "breakers"}
    cfg = body["config"]
    assert cfg["max_retries"] == 3
    assert cfg["breaker_threshold"] == 5
    assert cfg["retry_enabled"] is True
    assert cfg["breaker_enabled"] is True
    assert body["breakers"] == []  # no calls yet


def test_status_reflects_open_breaker(client: TestClient) -> None:
    # Manually trip a breaker → it must appear in the snapshot.
    br = global_registry().get("cursor")
    for _ in range(10):
        br.record_failure()
    r = client.get(STATUS_URL)
    body = r.json()
    names = {b["name"]: b for b in body["breakers"]}
    assert "cursor" in names
    assert names["cursor"]["state"] == "open"


def test_status_requires_bearer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AKANA_TOKEN", "secret-token")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    app = create_app()
    # The token gate applies only to PROXIED requests; X-Forwarded-For makes the
    # request look proxied so the configured token is actually enforced.
    proxied = {"X-Forwarded-For": "1.2.3.4"}
    with TestClient(app) as c:
        assert c.get(STATUS_URL, headers=proxied).status_code == 401
        ok = c.get(
            STATUS_URL,
            headers={**proxied, "Authorization": "Bearer secret-token"},
        )
        assert ok.status_code == 200


def test_runtime_override_changes_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A value written to the runtime_settings store reflects into config (no restart)."""
    from types import SimpleNamespace

    from akana_server.runtime_settings import get_store

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    get_store(data_dir).set("network_max_retries", 7)
    get_store(data_dir).set("network_breaker_threshold", 0)  # circuit breaker off

    settings = SimpleNamespace(data_dir=data_dir)
    cfg = load_network_config(settings)
    assert cfg.max_retries == 7
    assert cfg.breaker_enabled is False
