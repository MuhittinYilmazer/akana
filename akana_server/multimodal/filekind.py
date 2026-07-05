"""Multi-type file classification — extension allowlist + magic-bytes validation.

:mod:`.sniff` detects only IMAGE formats (png/jpeg/gif/webp) from content. This
module adds NON-image types:

* **text** — plain text/code/configuration (txt/md/py/js/ts/json/yaml/csv/log/...).
  The content is free bytes; there are no magic-bytes. Validation is done with
  the extension + a "does it look like text" (no NUL byte) heuristic.
* **pdf** — the ``%PDF-`` signature.
* **docx/xlsx/pptx/zip** — OOXML/archive; a ZIP container (``PK\\x03\\x04``).
  They all carry the SAME ZIP signature; the distinction comes from the
  extension (the ``[Content_Types].xml`` in the content sits in the ZIP central
  directory and cannot be distinguished by a cheap magic read — extension + ZIP
  signature is enough assurance: a png with a wrong extension is still rejected
  with ``UNSUPPORTED_*``).

No text EXTRACTION is done: files are given to the provider NATIVELY (a file
path it will read itself); classification is only for accept/reject and the
``kind`` label.

Design contract (symmetric with images):

* OUTSIDE the extension allowlist → rejected (``UNSUPPORTED_EXTENSION``).
* If the content does not match the expected family (e.g. named ``.pdf`` but no
  ``%PDF-``) → rejected (``UNSUPPORTED_MEDIA``). For images this is
  :mod:`.sniff`, for non-text binary types it is ``sniff_binary_kind`` here.
* If a NUL byte is seen for a text type it is treated as a binary leak →
  rejected (a file named ``.txt`` but containing ELF/zip does not slip through).
"""

from __future__ import annotations

#: NON-image "text-like" extensions → read directly (extract.py).
#: All are part of the plain-text family (code/configuration/data); kind="text".
TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {
        "txt",
        "md",
        "markdown",
        "rst",
        "log",
        "csv",
        "tsv",
        "json",
        "jsonl",
        "ndjson",
        "yaml",
        "yml",
        "toml",
        "ini",
        "cfg",
        "conf",
        "env",
        "xml",
        # html/htm is SAFE because ``GET /uploads/{id}/raw`` (api/routes/uploads.py)
        # serves this content with ``Content-Type: text/plain`` +
        # ``Content-Disposition: attachment`` + ``X-Content-Type-Options: nosniff``
        # → it is not rendered in the browser (no stored-XSS surface). If those
        # headers change this invariant breaks; regression:
        # tests/unit/test_uploads_html_serving.py.
        "html",
        "htm",
        "css",
        # code
        "py",
        "pyi",
        "js",
        "jsx",
        "ts",
        "tsx",
        "mjs",
        "cjs",
        "sh",
        "bash",
        "zsh",
        "sql",
        "go",
        "rs",
        "java",
        "kt",
        "c",
        "h",
        "cpp",
        "hpp",
        "cc",
        "cs",
        "rb",
        "php",
        "swift",
        "lua",
        "pl",
        "r",
        "dart",
        "vue",
        "svelte",
        "dockerfile",
        "makefile",
    }
)

#: Binary document/archive types → extension + magic-bytes. Value = canonical "kind".
#: pdf: the ``%PDF-`` signature. docx/xlsx/zip: all are a ZIP container (same
#: signature); the distinction comes from the extension.
BINARY_DOC_EXTENSIONS: dict[str, str] = {
    "pdf": "pdf",
    "docx": "docx",
    "xlsx": "xlsx",
    "pptx": "pptx",
    "zip": "zip",
}

_PDF_SIGNATURE = b"%PDF-"
_ZIP_SIGNATURE = b"PK\x03\x04"
#: Empty ZIP / ZIP spanned/empty variant signatures (rare in OOXML output).
_ZIP_EMPTY = (b"PK\x05\x06", b"PK\x07\x08")


def ext_of(name: str | None) -> str:
    """Name extension (lowercase, no dot); empty string if none.

    Extensionless but known names like ``Dockerfile`` are also caught.
    """
    raw = (name or "").strip()
    if not raw:
        return ""
    base = raw.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." in base.rstrip("."):
        return base.rsplit(".", 1)[-1].lower()
    # extensionless special names
    if base.lower() in ("dockerfile", "makefile"):
        return base.lower()
    return ""


def looks_like_text(data: bytes, *, sample: int | None = None) -> bool:
    """Treat as text if there is no NUL byte; the ENTIRE content is scanned.

    Heuristic: if a NUL (``\\x00``) is present it is treated as binary (like
    ELF/zip/png). The old version scanned only the first 8192 bytes → a file
    with a clean head but a binary TAIL could slip through the text gate (named
    ``.txt`` but with ELF/zip appended at the end). Since files are size-limited
    in the upper layer, the full scan is bounded. Empty data is treated as
    "text" (the upper layer does the EMPTY check separately). If ``sample`` is
    given, only that many bytes are scanned (backward compat / caller optional);
    the default is ALL content.
    """
    if not data:
        return True
    chunk = data if sample is None else data[:sample]
    if b"\x00" in chunk:
        return False
    return True


def sniff_binary_kind(data: bytes) -> str | None:
    """PDF/OOXML(zip) magic-bytes; ``None`` if there is no match.

    docx and xlsx share the same ZIP signature → both return ``"zip"`` here; the
    real kind is clarified by the extension (see :func:`classify`).
    """
    if data.startswith(_PDF_SIGNATURE):
        return "pdf"
    if data.startswith(_ZIP_SIGNATURE) or data.startswith(_ZIP_EMPTY):
        return "zip"
    return None
