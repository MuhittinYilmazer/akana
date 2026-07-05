"""FileEngine F0 — :class:`FileService`: root-allowlist safe file operations.

Security model (``tasks/project_checks`` pattern):

* **Root allowlist** — ``Settings.file_roots`` (env ``AKANA_FILE_ROOTS``,
  ``:``-delimited). Empty ⇒ service DISABLED: every operation is rejected with
  an explicit :class:`FileEngineNotConfigured`. Every path is checked against
  the allowlist AFTER resolve (symlink follow); ``..``/symlink escape is a
  ``PermissionError``.
* **Write gate (REMOVED)** — by FULL AUTONOMY decision, the old file_write
  risk/approval policy gate on :meth:`FileService.write_text` was removed;
  writes within the root allowlist are always permitted. ``write_text``'s
  return value still reports a fixed ``policy_decision: "allow"`` for API
  shape stability.
* **Atomic write + backup** — tmp file + ``os.replace``; if an existing file
  is about to be overwritten, a copy is made to ``<data_dir>/file_backups/``
  first (size-limited).
* **Operation log** — every operation is appended to the append-only
  ``db/files.db`` (:mod:`akana_server.files.oplog`); writes store the
  old+new content hash and backup path (undo F1 foundation). Log failure
  never breaks a file operation.

Workflow contract (note for F1): the workflow step type is DELIBERATELY absent in
this phase — in F1 ``file_read``/``file_write`` steps will call this service's
``read_text`` / ``write_text``; signatures are kept stable.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from akana_server.files.oplog import FileOpLog, get_file_oplog

__all__ = [
    "DEFAULT_LIST_DEPTH",
    "DEFAULT_MAX_READ_BYTES",
    "MAX_BACKUP_BYTES",
    "MAX_LIST_DEPTH",
    "MAX_LIST_ENTRIES",
    "MAX_READ_BYTES",
    "FileEngineNotConfigured",
    "FileService",
    "ReadResult",
]

log = logging.getLogger(__name__)

DEFAULT_MAX_READ_BYTES = 262_144  # 256 KiB — REST + toolbox default
MAX_READ_BYTES = 2_000_000
DEFAULT_LIST_DEPTH = 1
MAX_LIST_DEPTH = 5
MAX_LIST_ENTRIES = 500
#: Files exceeding this size skip backup (the write still proceeds;
#: a note is logged). Prevents the backup directory from growing unbounded.
MAX_BACKUP_BYTES = 5_000_000


class FileEngineNotConfigured(RuntimeError):
    """Allowlist is empty — FileService is intentionally disabled (explicit error)."""

    def __init__(self) -> None:
        super().__init__(
            "FileEngine is not configured: AKANA_FILE_ROOTS is empty —"
            " explicitly define the allowed file roots"
        )


@dataclass(slots=True)
class ReadResult:
    """``read_text`` output — includes truncation info (JSON-safe)."""

    path: str
    text: str
    size: int  # actual file size in bytes
    truncated: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "text": self.text,
            "size": self.size,
            "truncated": self.truncated,
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entry_type(p: Path) -> str:
    if p.is_symlink():
        return "symlink"
    if p.is_dir():
        return "dir"
    if p.is_file():
        return "file"
    return "other"


class FileService:
    """Root-allowlist file operations — every path is checked against the allowlist after resolve."""

    def __init__(
        self,
        roots: tuple[Path, ...] | list[Path] | tuple[str, ...] = (),
        *,
        data_dir: Path | None = None,
    ) -> None:
        self._roots = tuple(Path(r).resolve() for r in roots)
        self._data_dir = Path(data_dir) if data_dir is not None else None

    @classmethod
    def from_settings(cls, settings: Any) -> FileService:
        # RuntimeSettings: roots are resolved AT CONSTRUCTION TIME (runtime > env > default);
        # a runtime setting PUT drops app.state.file_service → reconstructed with new roots.
        from akana_server.runtime_settings import get_runtime

        roots = get_runtime("file_roots", settings) or ()
        return cls(roots, data_dir=getattr(settings, "data_dir", None))

    @property
    def configured(self) -> bool:
        return bool(self._roots)

    @property
    def roots(self) -> tuple[Path, ...]:
        return self._roots

    # -- root jail (project_checks pattern) --------------------------------------

    def _require_roots(self) -> None:
        if not self._roots:
            raise FileEngineNotConfigured()

    def _resolve_allowed(self, raw_path: str) -> Path:
        """Resolve path and assert it is INSIDE one of the allowlist roots.

        Symlinks are resolved BEFORE the root check — a link pointing outside
        the root is rejected. Escape raises :class:`PermissionError`.
        """
        raw = str(raw_path or "").strip()
        if not raw:
            raise ValueError("file path cannot be empty")
        path = Path(os.path.expanduser(raw)).resolve()
        for root in self._roots:
            if path == root or path.is_relative_to(root):
                return path
        raise PermissionError(
            f"files: path outside the allowlist: {path}"
            f" (allowed roots: {', '.join(str(r) for r in self._roots)})"
        )

    # -- operation log (failure must not break the operation) --------------------

    def _oplog(self) -> FileOpLog | None:
        if self._data_dir is None:
            return None
        from types import SimpleNamespace

        return get_file_oplog(SimpleNamespace(data_dir=self._data_dir))

    def _log(self, **kwargs: Any) -> None:
        try:
            oplog = self._oplog()
            if oplog is not None:
                oplog.append(**kwargs)
        except Exception:  # noqa: BLE001 — log failure must never break a file operation
            log.warning("file oplog could not be written (operation continuing)", exc_info=True)

    # -- read surface ------------------------------------------------------------

    def read_text(self, path: str, max_bytes: int = DEFAULT_MAX_READ_BYTES) -> ReadResult:
        """Read a file inside the allowlist as UTF-8 text (errors=replace).

        At most ``max_bytes`` bytes are read; if the file is larger, ``truncated=True``.
        """
        self._require_roots()
        p = self._resolve_allowed(path)
        if not p.is_file():
            raise FileNotFoundError(f"files: file not found: {p}")
        limit = max(1, min(int(max_bytes), MAX_READ_BYTES))
        size = p.stat().st_size
        with p.open("rb") as fh:
            data = fh.read(limit)
        result = ReadResult(
            path=str(p),
            text=data.decode("utf-8", errors="replace"),
            size=size,
            truncated=size > limit,
        )
        self._log(op="read", path=str(p), detail=f"{len(data)}/{size} bytes")
        return result

    def list_dir(self, path: str, depth: int = DEFAULT_LIST_DEPTH) -> list[dict[str, Any]]:
        """List directory contents with limited depth (symlink directories are NOT descended into).

        Each entry is ``{path, name, type, size}``; at most
        :data:`MAX_LIST_ENTRIES` entries returned (deterministically sorted).
        """
        self._require_roots()
        base = self._resolve_allowed(path)
        if not base.is_dir():
            raise NotADirectoryError(f"files: directory not found: {base}")
        max_depth = max(1, min(int(depth), MAX_LIST_DEPTH))
        entries: list[dict[str, Any]] = []

        def _walk(directory: Path, level: int) -> None:
            try:
                children = sorted(directory.iterdir(), key=lambda c: str(c))
            except OSError:
                return
            for child in children:
                if len(entries) >= MAX_LIST_ENTRIES:
                    return
                kind = _entry_type(child)
                try:
                    size = child.stat().st_size if kind == "file" else 0
                except OSError:
                    size = 0
                entries.append(
                    {"path": str(child), "name": child.name, "type": kind, "size": size}
                )
                # symlink directories are not descended into — prevents indirect
                # traversal outside the root
                if kind == "dir" and level < max_depth:
                    _walk(child, level + 1)

        _walk(base, 1)
        self._log(op="list", path=str(base), detail=f"depth={max_depth} → {len(entries)} entries")
        return entries

    def stat(self, path: str) -> dict[str, Any]:
        """Type + size + mtime for a single path (from within the allowlist)."""
        self._require_roots()
        p = self._resolve_allowed(path)
        if not p.exists():
            raise FileNotFoundError(f"files: path not found: {p}")
        st = p.stat()
        info = {
            "path": str(p),
            "name": p.name,
            "type": _entry_type(p),
            "size": st.st_size,
            "mtime": st.st_mtime,
        }
        self._log(op="stat", path=str(p))
        return info

    # -- write surface (atomic, with backup) --------------------------------------

    def _backup(self, p: Path, old_bytes: bytes) -> tuple[str, str]:
        """Copy the file about to be overwritten to ``<data_dir>/file_backups/``.

        Returns: ``(backup_path, note)`` — backup is skipped (with a note) if
        data_dir is absent or the file exceeds :data:`MAX_BACKUP_BYTES`.
        """
        if self._data_dir is None:
            return "", "backup skipped: no data_dir"
        if len(old_bytes) > MAX_BACKUP_BYTES:
            return "", f"backup skipped: file exceeds the {MAX_BACKUP_BYTES} byte limit"
        backup_dir = self._data_dir / "file_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S")
        backup_path = backup_dir / f"{stamp}__{_sha256(old_bytes)[:8]}__{p.name}"
        try:
            shutil.copy2(p, backup_path)
        except OSError as e:
            return "", f"backup could not be taken: {type(e).__name__}: {e}"
        return str(backup_path), ""

    def write_text(self, path: str, content: str) -> dict[str, Any]:
        """Atomic text write into the allowlist (with backup).

        Flow: root jail → if file exists: hash + backup → write to tmp + fsync →
        ``os.replace`` (atomic) → operation log (old/new hash — undo F1 foundation).
        FULL AUTONOMY: no risk/approval gate; ``policy_decision`` is fixed ``"allow"``.
        """
        self._require_roots()
        p = self._resolve_allowed(path)
        if p.is_dir():
            raise IsADirectoryError(f"files: target is a directory: {p}")
        text = str(content if content is not None else "")

        old_bytes: bytes | None = None
        backup_path, backup_note = "", ""
        if p.is_file():
            old_bytes = p.read_bytes()
            backup_path, backup_note = self._backup(p, old_bytes)

        p.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=p.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, p)  # atomic: either old content or new
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

        detail = f"{len(data)} bytes written; policy=allow"
        if backup_note:
            detail += f"; {backup_note}"
        self._log(
            op="write",
            path=str(p),
            ok=True,
            old_hash=_sha256(old_bytes) if old_bytes is not None else "",
            new_hash=_sha256(data),
            backup_path=backup_path,
            detail=detail,
        )
        return {
            "path": str(p),
            "bytes_written": len(data),
            "created": old_bytes is None,
            "backup_path": backup_path,
            "old_hash": _sha256(old_bytes) if old_bytes is not None else "",
            "new_hash": _sha256(data),
            "policy_decision": "allow",
        }
