"""Memory staging API (v2): pending list + approve/reject (promote/discard).

The v1 staging mutation API (POST create/promote/discard, DELETE) was retired in
the v2 migration. In v2 candidates are produced by the LLM capture pipeline (there is
NO public create endpoint); so the tests seed a candidate directly into the staging
store via ``stage()``, then drive the v2 routes:
``GET /memory/staging`` + ``POST .../approve`` + ``POST .../reject``.
The store is the same in-process ``get_memory_core(data_dir)`` singleton as the app.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from akana.memory.staging import FactCandidate
from akana_server.api.app import create_app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8766")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    app = create_app()
    with TestClient(app) as c:
        yield c


def _stage(client: TestClient, key: str, value: str) -> str:
    """Seed a pending candidate (there is no public create endpoint in v2).

    To write to the SAME instance the route READS, we first fire a request so that
    ``_ensure_memory_stack`` sets up ``app.state.memory_core``; then we ``stage()``
    directly into that instance's staging store.
    (Since the path derived by hand may resolve differently from the app's effective
    data_dir, ``get_memory_core(tmp_path)`` does not guarantee the SAME instance.)
    """
    client.get("/api/v1/memory/staging?status=pending")
    mem = client.app.state.memory_core
    staged = mem.staging.stage(
        FactCandidate(key=key, value=value, trust="user_statement", reason="test")
    )
    return staged.id


def test_staging_list_and_approve(client: TestClient) -> None:
    sid = _stage(client, "kahve tercihi", "espresso sever")

    listed = client.get("/api/v1/memory/staging?status=pending")
    assert listed.status_code == 200
    body = listed.json()
    assert sid in [i["id"] for i in body["items"]]
    assert body["pending_count"] >= 1

    approved = client.post(f"/api/v1/memory/staging/{sid}/approve")
    assert approved.status_code == 200
    data = approved.json()
    assert data["status"] == "promoted"
    assert data["fact_id"]  # a persistent fact was produced

    # An approved candidate drops out of the pending list; in 'all' it shows as promoted.
    after = client.get("/api/v1/memory/staging?status=pending").json()
    assert sid not in [i["id"] for i in after["items"]]
    all_items = client.get("/api/v1/memory/staging?status=all").json()["items"]
    by_id = {i["id"]: i for i in all_items}
    assert by_id[sid]["status"] == "promoted"
    assert by_id[sid]["promoted_fact_id"] == data["fact_id"]


def test_staging_reject(client: TestClient) -> None:
    sid = _stage(client, "gereksiz aday", "x")

    rejected = client.post(f"/api/v1/memory/staging/{sid}/reject")
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"

    # A rejected candidate drops out of the pending list (not written to persistent memory).
    after = client.get("/api/v1/memory/staging?status=pending").json()
    assert sid not in [i["id"] for i in after["items"]]


def test_staging_action_on_unknown_is_404(client: TestClient) -> None:
    assert client.post("/api/v1/memory/staging/yok-boyle-id/approve").status_code == 404
    assert client.post("/api/v1/memory/staging/yok-boyle-id/reject").status_code == 404


def test_staging_approve_then_action_conflicts_409(client: TestClient) -> None:
    sid = _stage(client, "bir kez onaylanır", "v")
    assert client.post(f"/api/v1/memory/staging/{sid}/approve").status_code == 200

    # No longer 'pending' → both approve AND reject again return 409 NOT_ACTIONABLE.
    assert client.post(f"/api/v1/memory/staging/{sid}/approve").status_code == 409
    assert client.post(f"/api/v1/memory/staging/{sid}/reject").status_code == 409
