"""Shared primitives for the small typed-JSON settings stores.

``llm_settings`` and ``voice_preferences`` are both single-file, read-modify-write
JSON stores guarded against two-tab / two-device concurrent writes. They evolved
by copy-paste and carried byte-identical copies of the per-``data_dir`` lock
registry and the crash-safe atomic-write block; the pid+uuid unique-tmp fix had
to be applied to both. This module is the single home for those two primitives so
the fix (and any future write-path change) lives in one place.

Each store keeps its own dataclass, merge/validation and public API — only these
low-level mechanics are shared. Stores with genuinely different write semantics
(``runtime_settings/store.py`` in-object lock + cache, ``vault_crypto`` 0600 +
Windows retry, ``files/service`` backup + oplog) are deliberately NOT folded in
here.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

# Per-``data_dir`` process-wide lock registry. Serializes the read-modify-write
# across concurrent writers (two tabs/devices PATCHing at once): without a lock +
# with a shared ".json.tmp" name the ``os.replace`` steps race
# (FileNotFoundError → HTTP 500) and updates are lost.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def lock_for(data_dir: Path) -> threading.Lock:
    """The process-wide lock for a ``data_dir`` (created on first use)."""
    key = str(data_dir)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def write_json_atomic(path: Path, obj: Any) -> None:
    """Crash-safe JSON write: full write to a unique tmp, then ``os.replace``.

    Even on a crash / colliding write mid-write, the read side never sees half
    JSON (same pattern as ``runtime_settings/store.py``). The tmp name is UNIQUE
    (pid+uuid) so two concurrent writers do not share a tmp file and one's
    ``os.replace`` does not end up "not finding" (FileNotFoundError) the other's.
    On failure (disk full / read-only) the half tmp is cleaned up and the error
    re-raised — thanks to the atomic write the OLD file is preserved (the half
    file is never visible).
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
