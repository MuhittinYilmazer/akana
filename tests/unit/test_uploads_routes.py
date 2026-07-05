"""Uploads REST surface — /api/v1/uploads (POST multipart + GET meta/raw, bearer)."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app


def _png_chunk(ctype: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + ctype
        + data
        + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
    )


def make_png(width: int = 2, height: int = 1) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xab\xcd\xef" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    monkeypatch.delenv("AKANA_UPLOADS_ENABLED", raising=False)
    monkeypatch.delenv("AKANA_UPLOAD_MAX_MB", raising=False)
    app = create_app()
    with TestClient(app) as c:
        yield c


def _upload(client: TestClient, data: bytes, name: str = "a.png", mime: str = "image/png"):
    return client.post("/api/v1/uploads", files={"file": (name, data, mime)})


def test_upload_ok_meta_doner(client: TestClient) -> None:
    r = _upload(client, make_png())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dedup"] is False
    img = body["image"]
    assert img["format"] == "png"
    assert (img["width"], img["height"]) == (2, 1)
    assert img["id"]
    assert "file_name" not in img  # the ULID disk name never leaks to the surface
    # F1 meta: kind + provider-native info
    assert img["kind"] == "image"
    assert img["path"].endswith(".png")
    # gemini native: image is read via inline_data → True; openai vision (image_url) → True
    assert img["provider_native"] == {
        "claude": True,
        "cursor": True,
        "gemini": True,
        "openai": True,
    }


def test_upload_metin_dosyasi_kind_text(client: TestClient) -> None:
    r = client.post(
        "/api/v1/uploads",
        files={"file": ("not.txt", b"merhaba dunya", "text/plain")},
    )
    assert r.status_code == 200, r.text
    img = r.json()["image"]
    assert img["kind"] == "text"
    assert img["provider_native"]["claude"] is True
    # gemini/openai: text type is not native (not inlined, no file-reading tool)
    assert img["provider_native"]["gemini"] is False
    assert img["provider_native"]["openai"] is False


def test_upload_pdf_kind_pdf(client: TestClient) -> None:
    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF"
    r = client.post(
        "/api/v1/uploads",
        files={"file": ("r.pdf", pdf, "application/pdf")},
    )
    assert r.status_code == 200, r.text
    img = r.json()["image"]
    assert img["kind"] == "pdf"
    # gemini: PDF is read natively via inline_data → True; openai: PDF is now inlined
    # via a ``file`` content part (file_data data-URI) → True
    assert img["provider_native"]["gemini"] is True
    assert img["provider_native"]["openai"] is True


def test_upload_docx_kind_docx(client: TestClient) -> None:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", b"<Types/>")
        zf.writestr("word/document.xml", b"<doc/>")
    r = client.post(
        "/api/v1/uploads",
        files={"file": ("b.docx", buf.getvalue(), "application/octet-stream")},
    )
    assert r.status_code == 200, r.text
    img = r.json()["image"]
    assert img["kind"] == "docx"
    # gemini: docx is not native (not image/PDF) → False
    assert img["provider_native"]["gemini"] is False


def test_upload_izinsiz_uzanti_exe_415(client: TestClient) -> None:
    r = client.post(
        "/api/v1/uploads",
        files={"file": ("v.exe", b"MZbinari", "application/octet-stream")},
    )
    assert r.status_code == 415
    assert r.json()["detail"]["error"]["code"] == "UNSUPPORTED_EXTENSION"


def test_upload_tekrar_dedup_true(client: TestClient) -> None:
    first = _upload(client, make_png()).json()["image"]["id"]
    r = _upload(client, make_png(), name="kopya.png")
    assert r.status_code == 200
    assert r.json()["dedup"] is True
    assert r.json()["image"]["id"] == first


def test_upload_magic_bytes_reddi_415(client: TestClient) -> None:
    r = _upload(client, b"<html>sahte</html>", name="masum.png")
    assert r.status_code == 415
    assert r.json()["detail"]["error"]["code"] == "UNSUPPORTED_MEDIA"


def test_upload_izinsiz_uzanti_415(client: TestClient) -> None:
    r = _upload(client, make_png(), name="resim.bmp")
    assert r.status_code == 415
    assert r.json()["detail"]["error"]["code"] == "UNSUPPORTED_EXTENSION"


def test_upload_boyut_siniri_413(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    monkeypatch.setenv("AKANA_UPLOAD_MAX_MB", "0.001")  # ~1048 bytes
    app = create_app()
    with TestClient(app) as c:
        import random

        noise = zlib.compress(bytes(random.Random(0).randbytes(4096)))
        big = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
            + _png_chunk(b"IDAT", noise)
            + _png_chunk(b"IEND", b"")
        )
        r = _upload(c, big)
        assert r.status_code == 413
        assert r.json()["detail"]["error"]["code"] == "FILE_TOO_LARGE"


def test_uploads_disable_bayragi_403(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    monkeypatch.setenv("AKANA_UPLOADS_ENABLED", "0")
    app = create_app()
    with TestClient(app) as c:
        r = _upload(c, make_png())
        assert r.status_code == 403
        assert r.json()["detail"]["error"]["code"] == "UPLOADS_DISABLED"


def test_get_meta_ve_404(client: TestClient) -> None:
    image_id = _upload(client, make_png()).json()["image"]["id"]
    r = client.get(f"/api/v1/uploads/{image_id}")
    assert r.status_code == 200
    assert r.json()["image"]["id"] == image_id
    assert client.get("/api/v1/uploads/yok-boyle-id").status_code == 404


def test_get_raw_guvenli_basliklar(client: TestClient) -> None:
    up = _upload(client, make_png()).json()["image"]
    r = client.get(f"/api/v1/uploads/{up['id']}/raw")
    assert r.status_code == 200
    assert r.content == make_png()  # the EXIF-free PNG is returned as-is
    assert r.headers["content-type"].startswith("image/png")
    cd = r.headers["content-disposition"]
    assert cd.startswith("attachment;")
    assert f"{up['id']}.png" in cd  # server-generated name, not the user's name
    assert r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/v1/uploads/yok/raw").status_code == 404


def test_delete_yok_405(client: TestClient) -> None:
    image_id = _upload(client, make_png()).json()["image"]["id"]
    assert client.delete(f"/api/v1/uploads/{image_id}").status_code == 405


def test_bearer_zorunlu(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "sekret")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    app = create_app()
    # The token gate applies only to PROXIED requests; X-Forwarded-For makes the
    # request look proxied so the configured token is actually enforced.
    proxied = {"X-Forwarded-For": "1.2.3.4"}
    with TestClient(app) as c:
        assert (
            c.post(
                "/api/v1/uploads",
                files={"file": ("a.png", make_png(), "image/png")},
                headers=proxied,
            ).status_code
            == 401
        )
        ok = c.post(
            "/api/v1/uploads",
            files={"file": ("a.png", make_png(), "image/png")},
            headers={**proxied, "Authorization": "Bearer sekret"},
        )
        assert ok.status_code == 200
        image_id = ok.json()["image"]["id"]
        assert c.get(f"/api/v1/uploads/{image_id}", headers=proxied).status_code == 401
        assert (
            c.get(
                f"/api/v1/uploads/{image_id}/raw",
                headers={**proxied, "Authorization": "Bearer sekret"},
            ).status_code
            == 200
        )
