"""Regression — an extensionless ``Makefile`` is a known text file (accepted).

``ext_of`` special-cases extensionless ``Dockerfile``/``Makefile`` and returns
``"dockerfile"``/``"makefile"`` respectively. For that to actually be *accepted*
the derived pseudo-extension must also live in :data:`filekind.TEXT_EXTENSIONS`
(otherwise :meth:`UploadStore._validate` rejects it with
``UNSUPPORTED_EXTENSION`` — an HTTP 415). ``dockerfile`` was in the allowlist
but ``makefile`` was missing, so a file named ``Makefile`` was rejected. These
tests pin ``Makefile`` to the same accepted behaviour as ``Dockerfile``.
"""

from __future__ import annotations

from pathlib import Path

from akana_server.multimodal.filekind import TEXT_EXTENSIONS, ext_of
from akana_server.multimodal.store import ALL_ALLOWED_EXTENSIONS, UploadStore

_MAKEFILE_BODY = b"all:\n\techo build\n"


def test_makefile_is_a_known_text_extension() -> None:
    # A bare ``Makefile`` derives the ``makefile`` pseudo-extension, exactly
    # parallel to how ``Dockerfile`` derives ``dockerfile``.
    assert ext_of("Makefile") == "makefile"
    assert ext_of("Dockerfile") == "dockerfile"
    # ...and both must be in the allowlist so they are not rejected as unknown.
    assert "makefile" in TEXT_EXTENSIONS
    assert "makefile" in ALL_ALLOWED_EXTENSIONS
    assert "dockerfile" in ALL_ALLOWED_EXTENSIONS


def test_makefile_upload_is_accepted_as_text(tmp_path: Path) -> None:
    store = UploadStore(tmp_path)
    record, _ = store.save(_MAKEFILE_BODY, original_name="Makefile")
    # Classified in the text family (kind=text) — no UNSUPPORTED_EXTENSION 415.
    assert record.kind == "text"
    assert record.ext == "makefile"
