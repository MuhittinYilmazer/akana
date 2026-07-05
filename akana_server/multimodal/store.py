"""UploadStore — multi-type file upload layer (MultimodalEngine F1).

In F0 only IMAGES were accepted; F1 opens ALL file types:

* **image** (png/jpg/jpeg/webp/gif) — magic-bytes + EXIF stripping (as before).
* **text/code/configuration/data** (txt/md/py/js/ts/json/yaml/csv/log/...) —
  extension allowlist + a "looks like text" heuristic (NUL byte rejection).
* **document** (pdf/docx/xlsx/pptx/zip) — extension + magic-bytes (``%PDF-`` / ZIP).

Files are written under ``<data_dir>/uploads/`` with a ULID name
(``<ulid>.<ext>``); the meta record is kept in ``<data_dir>/db/multimodal.db``
(the F0 schema was extended with a ``kind`` column; migration is done via
``ALTER TABLE``). The connection pattern is the same as ``schedule/store.py``:
WAL, ``busy_timeout``, a ``threading.Lock`` per operation + a short-lived
connection.

Security/integrity boundaries (all inside ``save``, source-independent):

* **Magic-bytes** — image and pdf/docx/xlsx are validated FROM CONTENT; content
  named ``.pdf`` that does not carry ``%PDF-`` is rejected. For text types a
  binary leak is filtered out with the NUL-byte heuristic.
* **Extension allowlist** — extensions outside image ∪ text ∪ document are
  rejected.
* **Size limit** — ``Settings.upload_max_mb`` (env ``AKANA_UPLOAD_MAX_MB``).
* **EXIF stripping** — applied to images ONLY (location metadata is dropped
  before it touches the disk). Text/document bytes are stored as-is.
* **Dedup** — over sha256 (the STRIPPED content for images).

Append-only: rows are not deleted; every mutation writes an event to
``image_events``. There is no DELETE — a record can only be disabled via
``disable``.

Backward compatibility: the ``ImageStore``/``ImageRecord`` names are aliases for
``UploadStore``/``UploadRecord`` (F0 callers keep working).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import ulid

from akana_server.config import Settings
from akana_server.multimodal.exif import strip_location_metadata
from akana_server.multimodal.filekind import (
    BINARY_DOC_EXTENSIONS,
    TEXT_EXTENSIONS,
    ext_of,
    looks_like_text,
    sniff_binary_kind,
)
from akana_server.multimodal.sniff import (
    FORMAT_EXTENSIONS,
    FORMAT_MEDIA_TYPES,
    image_dimensions,
    sniff_format,
)
from akana_server.timeutil import iso_now

#: Accepted IMAGE name extensions (both jpg/jpeg map to the jpeg format).
IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "webp", "gif"})
#: F0 backward-compat name.
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS

#: All allowed extensions (image ∪ text ∪ document).
ALL_ALLOWED_EXTENSIONS = (
    IMAGE_EXTENSIONS | TEXT_EXTENSIONS | frozenset(BINARY_DOC_EXTENSIONS)
)

#: kind → representative MIME (for the Content-Type when serving documents/text).
_KIND_MEDIA_TYPES = {
    "text": "text/plain; charset=utf-8",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "zip": "application/zip",
}

DEFAULT_UPLOAD_MAX_MB = 10.0

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS images (
    id TEXT PRIMARY KEY,
    -- DEDUP CONTRACT (intentional): UNIQUE(sha256) deduplicates ACROSS ALL types.
    -- If the same bytes are uploaded again under a different name/extension/kind,
    -- NO new row is created — the FIRST record + ``dedup_hit=True`` is returned
    -- (the new ext/kind is discarded). Content identity is the SOLE source of
    -- identity; the name/extension does not change the content, so this is
    -- accepted by design (not a leak/collision). To make the behaviour
    -- type-aware, move the key to the (sha256, kind) pair — but that requires
    -- migrating existing rows and changes the dedup expectation (regression).
    sha256 TEXT NOT NULL UNIQUE,
    format TEXT NOT NULL,
    ext TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    size_bytes INTEGER NOT NULL,
    file_name TEXT NOT NULL,
    original_name TEXT,
    exif_stripped INTEGER NOT NULL DEFAULT 0,
    exif_note TEXT,
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_images_sha ON images(sha256);

CREATE TABLE IF NOT EXISTS image_events (
    image_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    action TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (image_id, seq)
);
"""


class UploadStoreError(Exception):
    """Validation/state error; ``code`` is mapped to an HTTP code in the route layer."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


#: F0 backward-compat name.
ImageStoreError = UploadStoreError


@dataclass(frozen=True, slots=True)
class UploadRecord:
    id: str
    sha256: str
    #: For images the sniffed format (png/jpeg/...); for text/documents same as ``kind``.
    format: str
    ext: str
    width: int | None
    height: int | None
    size_bytes: int
    file_name: str
    original_name: str | None
    exif_stripped: bool
    exif_note: str | None
    disabled: bool
    created_at: str
    #: "image" | "text" | "pdf" | "docx" | "xlsx" | "pptx" | "zip".
    kind: str = "image"

    @property
    def is_image(self) -> bool:
        return self.kind == "image"

    @property
    def media_type(self) -> str:
        if self.kind == "image":
            return FORMAT_MEDIA_TYPES.get(self.format, "application/octet-stream")
        return _KIND_MEDIA_TYPES.get(self.kind, "application/octet-stream")

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


#: F0 backward-compat name.
ImageRecord = UploadRecord


def _iso_now() -> str:
    return iso_now()


def _row_to_record(row: sqlite3.Row) -> UploadRecord:
    keys = row.keys()
    kind = row["kind"] if "kind" in keys else "image"
    return UploadRecord(
        id=row["id"],
        sha256=row["sha256"],
        format=row["format"],
        ext=row["ext"],
        width=row["width"],
        height=row["height"],
        size_bytes=int(row["size_bytes"]),
        file_name=row["file_name"],
        original_name=row["original_name"],
        exif_stripped=bool(row["exif_stripped"]),
        exif_note=row["exif_note"],
        disabled=bool(row["disabled"]),
        created_at=row["created_at"],
        kind=kind or "image",
    )


class UploadStore:
    """Durable file store: validate → strip EXIF (on images) → write to disk → meta record."""

    def __init__(self, data_dir: Path, *, max_mb: float = DEFAULT_UPLOAD_MAX_MB) -> None:
        self._data_dir = Path(data_dir).resolve()
        self._uploads_dir = self._data_dir / "uploads"
        self._uploads_dir.mkdir(parents=True, exist_ok=True)
        db_dir = self._data_dir / "db"
        db_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = db_dir / "multimodal.db"
        self._max_bytes = max(1, int(float(max_mb) * 1024 * 1024))
        self._lock = threading.Lock()
        self._init_db()

    @classmethod
    def for_settings(cls, settings: Settings) -> UploadStore:
        # RuntimeSettings: the limit is resolved at construction TIME; a runtime
        # settings PUT drops app.state.image_store → it is rebuilt with the new limit.
        from akana_server.runtime_settings import get_runtime

        max_mb = float(get_runtime("upload_max_mb", settings))
        return cls(settings.data_dir, max_mb=max_mb)

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    def file_path(self, record: UploadRecord) -> Path:
        return self._uploads_dir / record.file_name

    # -- db plumbing ----------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=10000")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                # F0 → F1 migration: the ``kind`` column is added if missing (old
                # rows assume "image"; consistent with the image-only F0 world).
                cols = {r["name"] for r in conn.execute("PRAGMA table_info(images)")}
                if "kind" not in cols:
                    conn.execute(
                        "ALTER TABLE images ADD COLUMN kind TEXT NOT NULL "
                        "DEFAULT 'image'"
                    )
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _atomic_write(dest: Path, data: bytes) -> None:
        """Write ``data`` to ``dest`` ATOMICALLY (temp in the same dir → ``os.replace``).

        A half-written file never appears under the ``dest`` name: either the
        full content or nothing. The temp file is opened in the SAME directory
        (``os.replace`` is atomic only within the same filesystem). If an error
        occurs before the replace, the temp file is cleaned up (no orphan left).
        fsync is intentionally skipped (the replace preserves metadata ordering;
        a durability/cost trade-off for F1)."""
        fd, tmp_name = tempfile.mkstemp(
            dir=str(dest.parent), prefix=f".{dest.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp_name, dest)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:  # pragma: no cover - temp already gone
                pass
            raise

    @staticmethod
    def _unlink_quiet(path: Path) -> None:
        """Best-effort remove of a just-written orphan file (never raises).

        Used by ``save`` to reclaim the ``<ulid>.<ext>`` file when the metadata
        INSERT/commit does not land (UNIQUE(sha256) race or commit failure), so a
        file is never left on disk without a committed row."""
        try:
            os.unlink(path)
        except OSError:  # pragma: no cover - already gone / never created
            pass

    @staticmethod
    def _append_event(
        conn: sqlite3.Connection,
        image_id: str,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO image_events (image_id, seq, timestamp, action, payload)"
            " SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?"
            " FROM image_events WHERE image_id = ?",
            (
                image_id,
                _iso_now(),
                action,
                json.dumps(payload or {}, ensure_ascii=False, default=str),
                image_id,
            ),
        )

    # -- validation -----------------------------------------------------------

    def _validate(
        self, data: bytes, original_name: str | None
    ) -> tuple[str, str, str]:
        """Size + extension + magic-bytes.

        Returns ``(kind, format, ext)``:

        * image    → ``("image", <sniff_format>, <canonical ext>)``
        * text     → ``("text", "text", <name extension | "txt">)``
        * document → ``("pdf"|"docx"|"xlsx", <kind>, <kind>)``
        """
        if not data:
            raise UploadStoreError("empty file", code="EMPTY_FILE")
        if len(data) > self._max_bytes:
            raise UploadStoreError(
                f"file too large ({len(data)} bytes > {self._max_bytes} byte limit)",
                code="FILE_TOO_LARGE",
            )

        ext = ext_of(original_name)
        # If an extension is present and OUTSIDE the allowlist, reject (even if the content is valid).
        if ext and ext not in ALL_ALLOWED_EXTENSIONS:
            raise UploadStoreError(
                f"unsupported extension: .{ext}",
                code="UNSUPPORTED_EXTENSION",
            )

        # 1) Is it an image? (from content)
        fmt = sniff_format(data)
        if fmt is not None and fmt in FORMAT_EXTENSIONS:
            # If an extension is present it must be an image extension (e.g. reject a png named .py)
            if ext and ext not in IMAGE_EXTENSIONS:
                raise UploadStoreError(
                    f"content is an image but the extension is not an image one: .{ext}",
                    code="UNSUPPORTED_EXTENSION",
                )
            return "image", fmt, FORMAT_EXTENSIONS[fmt]

        # 2) Is it a document? (pdf / zip-based docx/xlsx)
        binary_kind = sniff_binary_kind(data)
        if binary_kind == "pdf":
            if ext and ext != "pdf":
                raise UploadStoreError(
                    f"content is PDF but the extension is .{ext}", code="UNSUPPORTED_MEDIA"
                )
            return "pdf", "pdf", "pdf"
        if binary_kind == "zip":
            # docx/xlsx share the ZIP signature; the distinction comes from the extension.
            if ext in BINARY_DOC_EXTENSIONS and ext != "pdf":
                doc_kind = BINARY_DOC_EXTENSIONS[ext]
                return doc_kind, doc_kind, doc_kind
            raise UploadStoreError(
                "ZIP/OOXML content requires a docx/xlsx extension "
                f"(received extension: .{ext or '?'})",
                code="UNSUPPORTED_MEDIA",
            )

        # 3) Is it text? (extension allowed + NUL-byte heuristic)
        if ext in TEXT_EXTENSIONS:
            if not looks_like_text(data):
                raise UploadStoreError(
                    f"extension .{ext} is text but the content looks binary "
                    "(NUL byte) — rejected",
                    code="UNSUPPORTED_MEDIA",
                )
            return "text", "text", ext or "txt"

        # 4) Extensionless but text-like? (lenient accept: code/text paste)
        if not ext and looks_like_text(data):
            return "text", "text", "txt"

        raise UploadStoreError(
            "content did not match a supported type "
            "(image/text/pdf/docx/xlsx)",
            code="UNSUPPORTED_MEDIA",
        )

    # -- record -----------------------------------------------------------------

    def save(
        self, data: bytes, *, original_name: str | None = None
    ) -> tuple[UploadRecord, bool]:
        """Validate + strip EXIF (on images) + save; returns ``(record, dedup_hit)``.

        If the same content (stripped for images) was saved before, no new
        file/row is created — the existing record and ``dedup_hit=True`` are
        returned.
        """
        kind, fmt, ext = self._validate(data, original_name)

        if kind == "image":
            result = strip_location_metadata(data, fmt)
            clean = result.data
            exif_stripped = result.stripped
            exif_note = result.note
        else:
            clean = data
            exif_stripped = False
            exif_note = None

        sha = hashlib.sha256(clean).hexdigest()

        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM images WHERE sha256 = ?", (sha,)
                ).fetchone()
                if row is not None:
                    record = _row_to_record(row)
                    self._append_event(
                        conn,
                        record.id,
                        "dedup_hit",
                        {"original_name": original_name},
                    )
                    conn.commit()
                    return record, True

                image_id = str(ulid.new())
                file_name = f"{image_id}.{ext}"
                if kind == "image":
                    dims = image_dimensions(clean, fmt)
                    width = dims[0] if dims else None
                    height = dims[1] if dims else None
                else:
                    width = height = None
                now = _iso_now()
                record = UploadRecord(
                    id=image_id,
                    sha256=sha,
                    format=fmt,
                    ext=ext,
                    width=width,
                    height=height,
                    size_bytes=len(clean),
                    file_name=file_name,
                    original_name=original_name,
                    exif_stripped=exif_stripped,
                    exif_note=exif_note,
                    disabled=False,
                    created_at=now,
                    kind=kind,
                )
                # ATOMIC write: first write to a temp file in the same dir, then
                # move it into place with ``os.replace``. The old ``write_bytes``
                # was not atomic — a crash mid-write left a TRUNCATED file;
                # sha-dedup could never match/clean up that half file (permanent
                # garbage + silently corrupt content). ``os.replace`` is atomic
                # within the same filesystem.
                #
                # BUG (LOW, ordering) + BUG (MED, dedup race): the file is written
                # BEFORE the INSERT+commit, so a crash / commit failure / a losing
                # UNIQUE(sha256) INSERT would otherwise leave the ``<ulid>.<ext>``
                # file on disk with NO committed row — an orphan sha-dedup can
                # never reclaim. Every failure path below therefore unlinks the
                # just-written file, and the INSERT is tolerant of a concurrent
                # duplicate (two UploadStore instances with independent locks can
                # race the sha256 uniqueness — see deps.get_image_store).
                dest = self._uploads_dir / file_name
                self._atomic_write(dest, clean)
                try:
                    conn.execute(
                        "INSERT INTO images (id, sha256, format, ext, width, height,"
                        " size_bytes, file_name, original_name, exif_stripped,"
                        " exif_note, disabled, created_at, kind)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                        (
                            record.id,
                            record.sha256,
                            record.format,
                            record.ext,
                            record.width,
                            record.height,
                            record.size_bytes,
                            record.file_name,
                            record.original_name,
                            1 if record.exif_stripped else 0,
                            record.exif_note,
                            record.created_at,
                            record.kind,
                        ),
                    )
                    self._append_event(
                        conn,
                        record.id,
                        "created",
                        {
                            "kind": kind,
                            "format": fmt,
                            "size_bytes": record.size_bytes,
                            "exif_stripped": record.exif_stripped,
                            "exif_note": record.exif_note,
                        },
                    )
                    conn.commit()
                except sqlite3.IntegrityError:
                    # UNIQUE(sha256) collision: another store instance/process
                    # committed the SAME content between our SELECT-miss and this
                    # INSERT. Honour the dedup contract — drop our orphan file and
                    # return the existing row (dedup_hit=True), no HTTP 500.
                    conn.rollback()
                    self._unlink_quiet(dest)
                    row = conn.execute(
                        "SELECT * FROM images WHERE sha256 = ?", (sha,)
                    ).fetchone()
                    if row is not None:
                        existing = _row_to_record(row)
                        self._append_event(
                            conn,
                            existing.id,
                            "dedup_hit",
                            {"original_name": original_name},
                        )
                        conn.commit()
                        return existing, True
                    raise
                except BaseException:
                    # Any other failure (commit I/O error, disk full, crash-path
                    # rollback) must not leave the written file behind with no
                    # committed row — remove the orphan before propagating.
                    self._unlink_quiet(dest)
                    raise
                return record, False
            finally:
                conn.close()

    def get(self, image_id: str) -> UploadRecord | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM images WHERE id = ?", (image_id,)
                ).fetchone()
            finally:
                conn.close()
        return _row_to_record(row) if row else None

    def list(self, *, limit: int = 100) -> list[UploadRecord]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM images ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            finally:
                conn.close()
        return [_row_to_record(r) for r in rows]

    def disable(self, image_id: str) -> bool:
        """Append-only disable: the row/file is not deleted, ``disabled=1`` is set."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT disabled FROM images WHERE id = ?", (image_id,)
                ).fetchone()
                if row is None or row["disabled"]:
                    return False
                conn.execute(
                    "UPDATE images SET disabled = 1 WHERE id = ?", (image_id,)
                )
                self._append_event(conn, image_id, "disabled")
                conn.commit()
                return True
            finally:
                conn.close()

    def events(self, image_id: str) -> list[dict[str, Any]]:
        """Append-only event log, in insertion order (test/observability)."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM image_events WHERE image_id = ? ORDER BY seq ASC",
                    (image_id,),
                ).fetchall()
            finally:
                conn.close()
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                payload = json.loads(r["payload"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            out.append(
                {
                    "image_id": r["image_id"],
                    "seq": int(r["seq"]),
                    "timestamp": r["timestamp"],
                    "action": r["action"],
                    "payload": payload if isinstance(payload, dict) else {},
                }
            )
        return out


#: F0 backward-compat name.
ImageStore = UploadStore
