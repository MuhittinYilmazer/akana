"""FileEngine F0 — append-only operation ledger (``<data_dir>/db/files.db``).

Pattern matches ``policy/audit.py`` + ``cost/ledger.py``: WAL schema,
``busy_timeout=10000``, per-operation ``threading.Lock``, and short-lived
connections. The ledger is **append-only** — there is no UPDATE/DELETE API
by design; each file operation is a single immutable row:
``(id, ts, op, path, ok, old_hash, new_hash, backup_path, detail)``.

For writes, ``old_hash`` (sha256 of the overwritten content; empty for new files)
+ ``new_hash`` + ``backup_path`` are stored — these rows are what F1 undo (undo) will
walk. A ledger failure NEVER breaks a file operation (callers wrap with try/except
— cost ledger style).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

from akana_server.timeutil import iso_now

import ulid

__all__ = [
    "FileOpLog",
    "get_file_oplog",
    "reset_file_oplogs",
]

log = logging.getLogger(__name__)

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS file_ops (
    id          TEXT PRIMARY KEY,
    ts          TEXT NOT NULL,
    op          TEXT NOT NULL,
    path        TEXT NOT NULL,
    ok          INTEGER NOT NULL DEFAULT 1,
    old_hash    TEXT NOT NULL DEFAULT '',
    new_hash    TEXT NOT NULL DEFAULT '',
    backup_path TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_file_ops_ts ON file_ops(ts);
CREATE INDEX IF NOT EXISTS idx_file_ops_op ON file_ops(op, ts);
"""


class FileOpLog:
    """Append + last-N read (F0). Rows are immutable; the only write path is append()."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path.resolve()
        self._lock = threading.Lock()
        self._init_db()

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> FileOpLog:
        db_dir = Path(data_dir) / "db"
        db_dir.mkdir(parents=True, exist_ok=True)
        return cls(db_dir / "files.db")

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=10000")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()

    # -- append (sole write path) ------------------------------------------------

    def append(
        self,
        *,
        op: str,
        path: str,
        ok: bool = True,
        old_hash: str = "",
        new_hash: str = "",
        backup_path: str = "",
        detail: str = "",
        ts: str | None = None,
    ) -> dict[str, Any]:
        """Write a single file operation as an immutable row; returns the written record."""
        record: dict[str, Any] = {
            "id": str(ulid.new()),
            "ts": ts or iso_now(),
            "op": str(op),
            "path": str(path),
            "ok": 1 if ok else 0,
            "old_hash": str(old_hash),
            "new_hash": str(new_hash),
            "backup_path": str(backup_path),
            "detail": str(detail),
        }
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO file_ops (id, ts, op, path, ok, old_hash, new_hash,"
                    " backup_path, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record["id"],
                        record["ts"],
                        record["op"],
                        record["path"],
                        record["ok"],
                        record["old_hash"],
                        record["new_hash"],
                        record["backup_path"],
                        record["detail"],
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        return record

    # -- read --------------------------------------------------------------------

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Last N operations, newest first (strict insertion order).

        Ordered by the implicit SQLite ``rowid`` (a monotonic insertion counter),
        NOT by the ULID ``id``: ``ulid.new()`` is not monotonic *within* a single
        millisecond, so two same-ms appends can sort by their random component and
        invert insertion order on a fast host. ``rowid`` has no such tie.
        """
        limit = max(1, min(int(limit), 500))
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM file_ops ORDER BY rowid DESC LIMIT ?", (limit,)
                ).fetchall()
            finally:
                conn.close()
        return [dict(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT COUNT(*) AS n FROM file_ops").fetchone()
            finally:
                conn.close()
        return int(row["n"] or 0)


# --------------------------------------------------------------------------- #
# Process-wide singletons (one ledger per data_dir) — policy.audit style
# --------------------------------------------------------------------------- #
_OPLOGS: dict[str, FileOpLog] = {}
_OPLOGS_LOCK = threading.Lock()


def get_file_oplog(settings: Any) -> FileOpLog | None:
    """Resolve ledger from Settings; returns None if data_dir is absent/corrupt (never raises)."""
    data_dir = getattr(settings, "data_dir", None)
    if data_dir is None:
        return None
    try:
        key = str(Path(data_dir).resolve())
        with _OPLOGS_LOCK:
            oplog = _OPLOGS.get(key)
            if oplog is None:
                oplog = FileOpLog.for_data_dir(Path(data_dir))
                _OPLOGS[key] = oplog
        return oplog
    except Exception:  # pragma: no cover - corrupt data_dir → no operation log
        log.debug("Could not initialize file oplog", exc_info=True)
        return None


def reset_file_oplogs() -> None:
    """Test isolation: clear the singleton cache."""
    with _OPLOGS_LOCK:
        _OPLOGS.clear()
