"""Region REST surfaces — boundary-value / 4xx parameter tests.

Additional edge tests for QUERY parameter validation (422) of the files/uploads
routes, plus bearer and disabled-record behaviors (closes the gaps in the
existing route tests).
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app
from akana_server.files.oplog import reset_file_oplogs


@pytest.fixture(autouse=True)
def _isolated():
    reset_file_oplogs()
    yield
    reset_file_oplogs()


def _png_chunk(ctype: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + ctype
        + data
        + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
    )


def _png() -> bytes:
    ihdr = struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0)
    raw = b"\x00" + b"\xab\xcd\xef" * 2
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "root"
    r.mkdir()
    (r / "n.txt").write_text("abc", encoding="utf-8")
    return r


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, root: Path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    monkeypatch.setenv("AKANA_FILE_ROOTS", str(root))
    monkeypatch.delenv("AKANA_UPLOADS_ENABLED", raising=False)
    app = create_app()
    with TestClient(app) as c:
        yield c


# -- files query param validation -----------------------------------------------------


def test_files_read_bos_path_422(client: TestClient) -> None:
    # path min_length=1 → empty is 422.
    assert client.get("/api/v1/files/read", params={"path": ""}).status_code == 422


def test_files_read_max_bytes_sinir_422(client: TestClient, root: Path) -> None:
    p = {"path": str(root / "n.txt")}
    assert client.get("/api/v1/files/read", params={**p, "max_bytes": 0}).status_code == 422
    assert (
        client.get(
            "/api/v1/files/read", params={**p, "max_bytes": 9_999_999}
        ).status_code
        == 422
    )


def test_files_list_depth_sinir_422(client: TestClient, root: Path) -> None:
    p = {"path": str(root)}
    assert client.get("/api/v1/files/list", params={**p, "depth": 0}).status_code == 422
    assert client.get("/api/v1/files/list", params={**p, "depth": 99}).status_code == 422


def test_files_path_eksik_422(client: TestClient) -> None:
    assert client.get("/api/v1/files/list").status_code == 422


# -- uploads disabled-record raw behavior ---------------------------------------------


def test_upload_pasif_kayit_raw_410(client: TestClient) -> None:
    img = client.post(
        "/api/v1/uploads", files={"file": ("a.png", _png(), "image/png")}
    ).json()["image"]
    # disable the store directly (REST disable is not exposed in F0).
    store = client.app.state.image_store
    assert store.disable(img["id"]) is True
    r = client.get(f"/api/v1/uploads/{img['id']}/raw")
    assert r.status_code == 410
    assert r.json()["detail"]["error"]["code"] == "IMAGE_DISABLED"


def test_upload_disk_silinince_raw_410(client: TestClient) -> None:
    img = client.post(
        "/api/v1/uploads", files={"file": ("a.png", _png(), "image/png")}
    ).json()["image"]
    store = client.app.state.image_store
    rec = store.get(img["id"])
    store.file_path(rec).unlink()
    r = client.get(f"/api/v1/uploads/{img['id']}/raw")
    assert r.status_code == 410
    assert r.json()["detail"]["error"]["code"] == "FILE_MISSING"


def test_upload_bos_dosya_422(client: TestClient) -> None:
    r = client.post("/api/v1/uploads", files={"file": ("bos.png", b"", "image/png")})
    assert r.status_code == 422
    assert r.json()["detail"]["error"]["code"] == "EMPTY_FILE"
