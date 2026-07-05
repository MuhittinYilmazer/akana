"""runtime_settings.store — ``<data_dir>/runtime_settings.json`` persistence.

Atomic writes (tmp + ``os.replace``) + mtime-cached reads. The process-wide
store registry (:func:`get_store`) and the active data_dir binding for env-only
consumers (:func:`bind_runtime_data_dir`) live here.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_FILE_NAME = "runtime_settings.json"


@dataclass
class RuntimeStore:
    """JSON file store; each read is refreshed via mtime, writes are atomic."""

    path: Path
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _cache: dict[str, Any] = field(default_factory=dict, repr=False)
    _cache_mtime: int = -1

    def _read_disk(self) -> dict[str, Any]:
        try:
            stat = self.path.stat()
        except OSError:
            self._cache, self._cache_mtime = {}, -1
            return {}
        if stat.st_mtime_ns == self._cache_mtime:
            return self._cache
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Cache the mtime of a corrupt/unreadable file: load() is called on
            # every chat turn / STT / voice request; if mtime does not change,
            # a permanently corrupt file would be re-read, re-parsed, and would
            # re-emit a warning on every load (constant disk I/O + log flood).
            # Keep the last-good value (cache) as a fallback; until the file
            # actually CHANGES, this early-exit prevents re-reading → exactly one
            # warning per corruption event.
            log.warning("runtime_settings.json could not be read; ignoring", exc_info=True)
            self._cache_mtime = stat.st_mtime_ns
            return self._cache
        data = raw if isinstance(raw, dict) else {}
        self._cache, self._cache_mtime = data, stat.st_mtime_ns
        return data

    def load(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._read_disk())

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.path)
        try:
            self._cache, self._cache_mtime = dict(data), self.path.stat().st_mtime_ns
        except OSError:
            self._cache_mtime = -1

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            data = dict(self._read_disk())
            data[key] = value
            self._write(data)

    def set_many(self, values: dict[str, Any]) -> None:
        """Persist multiple keys in ONE atomic write (single lock + os.replace).

        A per-key ``set()`` loop would leave earlier keys durably written if a
        later key's write raised (disk full, read-only data_dir) — a partial
        multi-field PUT that silently diverges from what the client thinks failed.
        This applies all keys or none: ``_write`` builds the full dict in memory
        then ``os.replace``s once, so any failure leaves the original file intact.
        """
        if not values:
            return
        with self._lock:
            data = dict(self._read_disk())
            data.update(values)
            self._write(data)

    def reset(self, key: str) -> bool:
        with self._lock:
            data = dict(self._read_disk())
            if key not in data:
                return False
            data.pop(key)
            self._write(data)
            return True


_STORES: dict[str, RuntimeStore] = {}
_STORES_LOCK = threading.Lock()
#: Process-wide active data_dir for env-only consumers (planner/context).
_ACTIVE_DATA_DIR: Path | None = None


def get_store(data_dir: Path | str) -> RuntimeStore:
    key = str(Path(data_dir).resolve())
    with _STORES_LOCK:
        store = _STORES.get(key)
        if store is None:
            store = RuntimeStore(Path(key) / _FILE_NAME)
            _STORES[key] = store
        return store


def bind_runtime_data_dir(data_dir: Path | str | None) -> None:
    """Bind the process-wide store — for modules that do not carry settings (lifespan)."""
    global _ACTIVE_DATA_DIR
    _ACTIVE_DATA_DIR = Path(data_dir).resolve() if data_dir is not None else None


def reset_runtime_stores() -> None:
    """Test isolation: clear the store cache and active binding."""
    global _ACTIVE_DATA_DIR
    with _STORES_LOCK:
        _STORES.clear()
    _ACTIVE_DATA_DIR = None
