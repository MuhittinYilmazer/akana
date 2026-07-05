"""Bug-hunt regression: the Files REST handlers must offload their blocking
filesystem IO OFF the asyncio event loop.

Finding (``akana-bughunt-providers-files-vault.md`` — batch "Event-loop blocking
in Files REST handlers"): ``GET /api/v1/files/list`` and ``GET /api/v1/files/read``
called ``FileService.list_dir`` / ``FileService.read_text`` — synchronous
filesystem IO — directly on the event loop, stalling the whole server on slow or
large IO. The fix wraps each sync call in ``asyncio.to_thread(...)`` (the same
idiom :mod:`akana_server.api.routes.uploads` already uses).

These tests are hermetic: no real FileService/allowlist is used. The route's
``get_file_service`` dependency is overridden with a synchronous fake, and
``asyncio.to_thread`` (as imported by the route module) is monkeypatched with a
spy that records the callable it was handed while still executing it — so we
assert the blocking work is dispatched off-loop AND that the endpoint still
returns the fake's result.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app
from akana_server.api.deps import get_file_service
from akana_server.api.routes import files as files_route

LIST_URL = "/api/v1/files/list"
READ_URL = "/api/v1/files/read"


class _FakeReadResult:
    """Minimal stand-in for :class:`akana_server.files.service.ReadResult`."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def to_payload(self) -> dict[str, Any]:
        return self._payload


class _SyncFileService:
    """Fake FileService whose IO methods are plain synchronous calls.

    Records its invocations so a test can confirm the handler actually reached
    the service (via the off-loop hop) rather than short-circuiting.
    """

    def __init__(self) -> None:
        self.list_calls: list[tuple[str, int]] = []
        self.read_calls: list[tuple[str, int]] = []

    def list_dir(self, path: str, depth: int = 1) -> list[dict[str, Any]]:
        self.list_calls.append((path, depth))
        return [{"path": f"{path}/a.txt", "name": "a.txt", "type": "file", "size": 3}]

    def read_text(self, path: str, max_bytes: int = 262_144) -> _FakeReadResult:
        self.read_calls.append((path, max_bytes))
        return _FakeReadResult(
            {"path": path, "text": "merhaba", "size": 7, "truncated": False}
        )


@pytest.fixture
def fake_svc() -> _SyncFileService:
    return _SyncFileService()


@pytest.fixture
def client(fake_svc: _SyncFileService):
    app = create_app()
    app.dependency_overrides[get_file_service] = lambda: fake_svc
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def to_thread_spy(monkeypatch: pytest.MonkeyPatch):
    """Spy on ``asyncio.to_thread`` AS SEEN BY THE ROUTE MODULE.

    The route does ``import asyncio``; patching ``files_route.asyncio.to_thread``
    intercepts the exact call site. The spy still awaits the real implementation
    so the endpoint behaves normally — it merely records the callable it offloads.
    """
    real_to_thread = asyncio.to_thread
    calls: list[Any] = []

    async def _spy(func, /, *args, **kwargs):
        calls.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(files_route.asyncio, "to_thread", _spy)
    return calls


def test_list_handler_offloads_list_dir_off_loop(
    client: TestClient, fake_svc: _SyncFileService, to_thread_spy: list[Any]
) -> None:
    r = client.get(LIST_URL, params={"path": "/root", "depth": 2})
    assert r.status_code == 200
    assert r.json()["count"] == 1
    # The blocking svc.list_dir must have been dispatched via asyncio.to_thread.
    assert fake_svc.list_dir in to_thread_spy
    # ...and the handler really used the service (not a short-circuit).
    assert fake_svc.list_calls == [("/root", 2)]


def test_read_handler_offloads_read_text_off_loop(
    client: TestClient, fake_svc: _SyncFileService, to_thread_spy: list[Any]
) -> None:
    r = client.get(READ_URL, params={"path": "/root/a.txt", "max_bytes": 10})
    assert r.status_code == 200
    assert r.json()["text"] == "merhaba"
    # The blocking svc.read_text must have been dispatched via asyncio.to_thread.
    assert fake_svc.read_text in to_thread_spy
    assert fake_svc.read_calls == [("/root/a.txt", 10)]


def test_handlers_do_not_call_sync_io_directly_on_loop(
    client: TestClient, fake_svc: _SyncFileService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``asyncio.to_thread`` is neutralised, the sync IO must NOT run inline.

    Replacing ``to_thread`` with a no-op that never calls its target proves the
    handlers route their blocking work exclusively through it: with the off-loop
    hop stubbed out, the (client-wired) fake service is never touched. A
    regression that called ``svc.list_dir`` / ``svc.read_text`` directly on the
    loop would still record a call and fail this test.
    """

    async def _swallow(func, /, *args, **kwargs):
        # Deliberately do NOT invoke ``func`` — simulate the off-loop boundary.
        return None

    monkeypatch.setattr(files_route.asyncio, "to_thread", _swallow)

    # With ``to_thread`` swallowed the handlers get ``None`` back and may 500
    # while post-processing it — irrelevant here; we only care that the blocking
    # service methods were never invoked on the loop. Raise-on-server-error is
    # disabled so that expected 500 does not abort the test.
    client_no_raise = TestClient(client.app, raise_server_exceptions=False)
    client_no_raise.get(LIST_URL, params={"path": "/root"})
    client_no_raise.get(READ_URL, params={"path": "/root/a.txt"})

    assert fake_svc.list_calls == []
    assert fake_svc.read_calls == []
