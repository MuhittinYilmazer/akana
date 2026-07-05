"""Files REST surface (FileEngine F0) — GET /api/v1/files/list + /files/read.

Scope: list/read within the allowlist, ``max_bytes`` truncation, path outside the
root → 403, missing path → 404, unconfigured service → 503, bearer enforcement, and
the write endpoint being DELIBERATELY absent in F0 (405/404).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app
from akana_server.files.oplog import reset_file_oplogs

LIST_URL = "/api/v1/files/list"
READ_URL = "/api/v1/files/read"


@pytest.fixture(autouse=True)
def _isolated_oplogs():
    reset_file_oplogs()
    yield
    reset_file_oplogs()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "root"
    r.mkdir()
    (r / "not.txt").write_text("merhaba dünya", encoding="utf-8")
    (r / "alt").mkdir()
    return r


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, root: Path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    monkeypatch.setenv("AKANA_FILE_ROOTS", str(root))
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_list_allowlist_icinden(client: TestClient, root: Path) -> None:
    r = client.get(LIST_URL, params={"path": str(root)})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert {e["name"] for e in body["entries"]} == {"alt", "not.txt"}


def test_read_allowlist_icinden(client: TestClient, root: Path) -> None:
    r = client.get(READ_URL, params={"path": str(root / "not.txt")})
    assert r.status_code == 200
    body = r.json()
    assert body["text"] == "merhaba dünya"
    assert body["truncated"] is False


def test_read_max_bytes_kesme(client: TestClient, root: Path) -> None:
    body = client.get(
        READ_URL, params={"path": str(root / "not.txt"), "max_bytes": 7}
    ).json()
    assert body["text"] == "merhaba"
    assert body["truncated"] is True


def test_kok_disindaki_yol_403(client: TestClient, tmp_path: Path) -> None:
    disarida = tmp_path / "disarida.txt"
    disarida.write_text("gizli", encoding="utf-8")
    r = client.get(READ_URL, params={"path": str(disarida)})
    assert r.status_code == 403
    assert r.json()["detail"]["error"]["code"] == "PATH_FORBIDDEN"
    assert client.get(LIST_URL, params={"path": str(tmp_path)}).status_code == 403


def test_olmayan_yol_404(client: TestClient, root: Path) -> None:
    r = client.get(READ_URL, params={"path": str(root / "yok.txt")})
    assert r.status_code == 404
    assert r.json()["detail"]["error"]["code"] == "PATH_NOT_FOUND"


def test_yapilandirilmamis_servis_503(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    monkeypatch.delenv("AKANA_FILE_ROOTS", raising=False)
    app = create_app()
    with TestClient(app) as c:
        r = c.get(READ_URL, params={"path": str(tmp_path / "x.txt")})
        assert r.status_code == 503
        assert r.json()["detail"]["error"]["code"] == "FILES_NOT_CONFIGURED"
        assert c.get(LIST_URL, params={"path": str(tmp_path)}).status_code == 503


def test_bearer_zorunlu_token_varsa(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, root: Path
) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AKANA_TOKEN", "sekret")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    monkeypatch.setenv("AKANA_FILE_ROOTS", str(root))
    app = create_app()
    # The token gate applies only to PROXIED requests; X-Forwarded-For makes the
    # request look proxied so the configured token is actually enforced.
    proxied = {"X-Forwarded-For": "1.2.3.4"}
    with TestClient(app) as c:
        assert (
            c.get(
                READ_URL, params={"path": str(root / "not.txt")}, headers=proxied
            ).status_code
            == 401
        )
        ok = c.get(
            READ_URL,
            params={"path": str(root / "not.txt")},
            headers={**proxied, "Authorization": "Bearer sekret"},
        )
        assert ok.status_code == 200


def test_yazma_ucu_f0_da_yok(client: TestClient, root: Path) -> None:
    # F0 contract: the REST surface is read-only — the write endpoint is intentionally not registered.
    r = client.post(
        "/api/v1/files/write",
        json={"path": str(root / "x.txt"), "content": "x"},
    )
    assert r.status_code in (404, 405)
