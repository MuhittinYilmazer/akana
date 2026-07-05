"""Regression — an uploaded ``.html``/``.htm`` is NOT EXECUTABLE when served raw.

The ``html``/``htm`` extensions are in :data:`filekind.TEXT_EXTENSIONS` (accepted).
This is safe ONLY because ``GET /uploads/{id}/raw`` preserves the following
invariants:

* ``Content-Type: text/plain`` (NOT ``text/html`` — the browser does not render it),
* ``Content-Disposition: attachment`` (does not open inline, is downloaded),
* ``X-Content-Type-Options: nosniff`` (the browser cannot inspect the content and
  promote it to HTML).

Together these three close the stored-XSS surface. If the serving layer silently
drops these headers (e.g. if the ``media_type`` derivation changes) the HTML could
become executable in the browser — this test catches that regression.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app


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


_HTML_BODY = b"<html><body><script>alert(document.cookie)</script></body></html>"


@pytest.mark.parametrize("name", ["sayfa.html", "sayfa.htm"])
def test_html_upload_raw_servis_calistirilamaz(client: TestClient, name: str) -> None:
    up = client.post(
        "/api/v1/uploads",
        files={"file": (name, _HTML_BODY, "text/html")},
    )
    assert up.status_code == 200, up.text
    img = up.json()["image"]
    # HTML is classified in the text family (kind=text) — not a renderable type.
    assert img["kind"] == "text"

    raw = client.get(f"/api/v1/uploads/{img['id']}/raw")
    assert raw.status_code == 200, raw.text
    # 1) Content-Type text/plain — NEVER text/html (the browser does not render HTML).
    ctype = raw.headers["content-type"]
    assert ctype.startswith("text/plain")
    assert "html" not in ctype
    # 2) attachment — does not open inline (is downloaded).
    assert raw.headers["content-disposition"].startswith("attachment;")
    # 3) nosniff — the browser cannot inspect the content and promote it to HTML.
    assert raw.headers["x-content-type-options"] == "nosniff"
    # The content is returned verbatim (unchanged but in a non-executable form).
    assert raw.content == _HTML_BODY
