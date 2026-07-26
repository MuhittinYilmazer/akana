"""The upload size ceiling must be enforced WHILE READING, not after the body is on disk.

FastAPI resolves ``await request.form()`` BEFORE the router's bearer dependency, and
Starlette streams a multipart FILE part straight into a ``SpooledTemporaryFile`` that rolls
onto real disk past ~1 MB (``max_part_size`` bounds only NON-file parts). So the handler's
``file.read(max_bytes + 1)`` ceiling and the auth check both used to run only after the
ENTIRE body — any size, from any local caller, token or not — had already been written to
a temp file. These tests count the bytes that reach the spool.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app


@pytest.fixture
def spool_writes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Bytes actually written into Starlette's multipart spool for this test."""
    import starlette.formparsers as fp

    written: list[int] = []
    real = fp.SpooledTemporaryFile

    class _Counting(real):  # type: ignore[misc, valid-type]
        def write(self, data):  # type: ignore[no-untyped-def]
            written.append(len(data))
            return super().write(data)

    monkeypatch.setattr(fp, "SpooledTemporaryFile", _Counting)
    return written


def _app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, token: str = ""):
    from akana_server.runtime_settings import reset_runtime_stores

    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", token)
    monkeypatch.setenv("CURSOR_API_KEY", "")
    monkeypatch.setenv("AKANA_UPLOAD_MAX_MB", "1")
    monkeypatch.delenv("AKANA_UPLOADS_ENABLED", raising=False)
    reset_runtime_stores()
    return create_app()


#: 8 MB against a 1 MB ceiling.
_OVERSIZE = b"a" * (8 * 1024 * 1024)
#: The ceiling plus the framing slack the route allows for multipart boundaries.
_CEILING = 1 * 1024 * 1024 + 64 * 1024


def test_oversize_body_is_not_fully_spooled_to_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spool_writes: list[int]
) -> None:
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/uploads", files={"file": ("big.txt", _OVERSIZE, "text/plain")}
        )
    assert r.status_code == 413, r.text
    assert r.json()["detail"]["error"]["code"] == "FILE_TOO_LARGE"
    assert sum(spool_writes) <= _CEILING, (
        f"{sum(spool_writes)} bytes hit the disk spool for a rejected upload"
    )


def test_unauthenticated_oversize_body_is_rejected_before_it_is_spooled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spool_writes: list[int]
) -> None:
    """A caller with a WRONG token must not be able to make the server write anything.

    The bearer gate is a router dependency, which FastAPI resolves only AFTER the form is
    parsed — so a 401 used to arrive with the whole payload already on disk.
    """
    app = _app(monkeypatch, tmp_path, token="s3cret")
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/uploads",
            files={"file": ("big.txt", _OVERSIZE, "text/plain")},
            headers={
                "Authorization": "Bearer WRONG",
                # A proxied shape, otherwise the loopback owner-skip lets the peer through.
                "X-Forwarded-For": "203.0.113.9",
            },
        )
    assert r.status_code == 401, r.text
    assert sum(spool_writes) == 0, (
        f"{sum(spool_writes)} bytes were spooled for an unauthenticated request"
    )


def test_declared_content_length_over_the_limit_is_rejected_early(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spool_writes: list[int]
) -> None:
    """When the client declares the size up front, nothing needs to be read at all."""
    app = _app(monkeypatch, tmp_path)
    body = (
        b"--b\r\n"
        b'Content-Disposition: form-data; name="file"; filename="big.txt"\r\n'
        b"Content-Type: text/plain\r\n\r\n" + _OVERSIZE + b"\r\n--b--\r\n"
    )
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/uploads",
            content=body,
            headers={"Content-Type": "multipart/form-data; boundary=b"},
        )
    assert r.status_code == 413, r.text
    assert sum(spool_writes) == 0


def test_upload_within_the_limit_still_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ceiling must leave a file at the configured size alone (framing slack)."""
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/uploads",
            files={"file": ("ok.txt", b"x" * (900 * 1024), "text/plain")},
        )
    assert r.status_code == 200, r.text
    assert r.json()["image"]["kind"] == "text"
