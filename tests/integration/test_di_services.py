"""Step C — typed DI container (AppServices + get_services).

Locks down two things:

1. **The additive path is not broken**: in an app built with the real
   ``create_app``, ``get_services`` still builds a typed view from ``app.state``;
   the migrated ``/system/audit/tail`` route keeps working.
2. **The actual DI win (isolated test)**: a bare ``FastAPI`` + only the system
   router; FAKE services are injected via ``dependency_overrides[get_services]``.
   NO lifespan, NO ``app.state`` — the route still runs and reads from the
   injected ``settings.data_dir``. Proof of the migration: the route now depends
   on the injected container, not on ``request.app.state``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from akana_server.api.app import create_app
from akana_server.api.deps import require_akana_bearer
from akana_server.api.routes.system import router as system_router
from akana_server.api.services import AppServices, get_services
from akana_server.audit import write_event


@pytest.fixture
def real_client(monkeypatch, tmp_path):
    """Real app — shows the additive ``app.state`` path is not broken."""
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setattr(
        "akana_server.packs.host.AkanaPackHost.register_all", lambda self: []
    )
    app = create_app()
    with TestClient(app) as c:
        yield c, tmp_path


def _fake_services(data_dir) -> AppServices:
    """Only ``settings.data_dir`` is filled; the remaining fields are ``None``
    like the early path in production — the route only touches settings."""
    return AppServices(
        settings=SimpleNamespace(data_dir=data_dir),
        conversation_service=None,  # type: ignore[arg-type]
        event_hub=None,  # type: ignore[arg-type]
        llm_settings=None,  # type: ignore[arg-type]
        pack_host=None,  # type: ignore[arg-type]
    )


def test_real_app_audit_tail_still_works(real_client) -> None:
    client, data_dir = real_client
    write_event(data_dir, "chat", data={"text": "selam"})
    body = client.get("/api/v1/system/audit/tail").json()
    assert body["count"] == 1
    assert body["events"][0]["kind"] == "chat"


def test_route_runs_with_injected_fakes_no_lifespan(tmp_path) -> None:
    """DI win: the route runs with fake services WITHOUT setting up lifespan/app.state."""
    write_event(tmp_path, "policy_block", data={"rule": "x"})

    app = FastAPI()
    app.include_router(system_router, prefix="/api/v1")
    # override get_services → fake container; the bearer dep also wants app.state, so no-op it.
    app.dependency_overrides[get_services] = lambda: _fake_services(tmp_path)
    app.dependency_overrides[require_akana_bearer] = lambda: None

    with TestClient(app) as c:
        body = c.get("/api/v1/system/audit/tail").json()

    assert body["count"] == 1
    assert body["events"][0]["kind"] == "policy_block"


def test_get_services_reads_from_app_state() -> None:
    """``get_services`` moves fields from app.state into a typed view."""
    sentinel_settings = SimpleNamespace(data_dir="/tmp/x")
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=sentinel_settings))
    )
    services = get_services(request)  # type: ignore[arg-type]
    assert services.settings is sentinel_settings
    # Missing fields default to None defensively (so an early/half-built app does not blow up).
    assert services.pack_host is None
