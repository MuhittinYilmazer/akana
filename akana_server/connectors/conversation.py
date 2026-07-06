"""ConnectorEngine F2 — channel chat ↔ Akana conversation bridge (pure helpers).

Three responsibilities, all channel-agnostic:

1. :class:`ChannelBindingStore` — persistent mapping of
   ``(connector_id, chat_id) → conversation_id``
   (``<data_dir>/connector_bindings.json``). ConversationService holds the
   conversation itself; only the pointer is stored here. The ``/yeni`` command
   changes the mapping; the old conversation stays intact in the web UI.
2. :func:`trim_history` — fits the history sent to the LLM into a character
   budget (newest turns win; at least the last message is always kept).
3. :func:`parse_command` — Telegram-local slash commands (``/yeni``, ``/durum``,
   ``/baglan``); the ``/yeni@AkanaBot`` group syntax is also recognised. This is
   the connector's own explicit-slash-command surface (the web chat's
   natural-language command short-circuit was removed — every web message is an
   LLM turn).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from pathlib import Path

__all__ = [
    "ChannelBindingStore",
    "HISTORY_BUDGET_ENV",
    "channel_title",
    "parse_command",
    "resolve_history_budget",
    "trim_history",
]

log = logging.getLogger(__name__)

#: Channel history character budget (≈ token*4). Configurable via env.
HISTORY_BUDGET_ENV = "AKANA_CONNECTOR_HISTORY_BUDGET"
_DEFAULT_HISTORY_BUDGET = 12_000

#: Channel id → display name (used for conversation titles).
_CHANNEL_LABELS = {"telegram": "Telegram"}

#: Recognised Telegram-local commands.
_COMMANDS = frozenset({"yeni", "durum", "baglan"})

_BINDINGS_FILENAME = "connector_bindings.json"

#: Cross-INSTANCE file locks keyed by the resolved bindings path. A per-instance
#: threading.Lock does NOT serialize writes between the router's own store and the
#: bind-API route's separate store over the same JSON file, so their read-modify-write
#: could interleave and lose a binding (last writer wins). Keying the lock on the file
#: path makes every ChannelBindingStore over the same file share one lock.
_PATH_LOCKS: dict[str, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    try:
        key = str(path.resolve())
    except OSError:  # pragma: no cover - resolve on an odd path
        key = str(path)
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[key] = lock
        return lock


def resolve_history_budget() -> int:
    raw = os.environ.get(HISTORY_BUDGET_ENV, "").strip()
    if not raw:
        return _DEFAULT_HISTORY_BUDGET
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning(
            "%s=%r is invalid; using default %s",
            HISTORY_BUDGET_ENV,
            raw,
            _DEFAULT_HISTORY_BUDGET,
        )
        return _DEFAULT_HISTORY_BUDGET


def channel_title(connector_id: str, sender_name: str, chat_id: str) -> str:
    """Title shown in the web UI conversation list: «Telegram: <name>»."""
    label = _CHANNEL_LABELS.get(connector_id, connector_id.capitalize() or "Channel")
    who = (sender_name or "").strip() or (chat_id or "").strip() or "?"
    return f"{label}: {who}"


def parse_command(text: str) -> str | None:
    """``/yeni`` → ``"yeni"``; unrecognised / non-command text → ``None``."""
    t = (text or "").strip()
    if not t.startswith("/"):
        return None
    head = t.split()[0]
    name = head[1:].split("@", 1)[0].strip().lower()
    return name if name in _COMMANDS else None


def trim_history(
    messages: list[dict[str, str]], *, max_chars: int
) -> list[dict[str, str]]:
    """Trim history from the end (newest first) to fit within the budget.

    ``max_chars <= 0`` means no budget limit. At least one (newest) message is
    always kept — even if that single message exceeds the budget — so the LLM
    never runs without context.
    """
    if max_chars <= 0:
        return list(messages)
    kept: list[dict[str, str]] = []
    total = 0
    for m in reversed(messages):
        size = len(str(m.get("content") or ""))
        if kept and total + size > max_chars:
            break
        kept.append(m)
        total += size
    kept.reverse()
    return kept


class ChannelBindingStore:
    """Persistent map of ``(connector_id, chat_id) → conversation_id``.

    Single JSON file; writes are locked even though the inbound router is the
    sole consumer (defensive for tests / server threads). A corrupt file starts
    fresh — the conversations themselves are safe in ConversationService.
    """

    def __init__(self, data_dir: Path) -> None:
        self._path = Path(data_dir) / _BINDINGS_FILENAME
        # Cross-instance lock keyed on the resolved file path (NOT a per-instance
        # Lock): the router and the bind-API route each build their own store over the
        # same file, and only a shared lock serializes their read-modify-write.
        self._lock = _lock_for(self._path)

    def _load(self) -> dict[str, dict[str, str]]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict[str, str]] = {}
        for connector_id, chats in raw.items():
            if isinstance(chats, dict):
                out[str(connector_id)] = {
                    str(k): str(v) for k, v in chats.items() if isinstance(v, str)
                }
        return out

    def _save(self, data: dict[str, dict[str, str]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # A UNIQUE temp name per write (pid + uuid): the fixed ``.json.tmp`` was shared
        # by every instance, so two concurrent saves collided on the same temp file —
        # on Windows the write_text/replace of one raised a sharing-violation
        # PermissionError against the other. A unique temp file makes the write private;
        # the shared path lock still serializes the replace onto the final file.
        tmp = self._path.with_name(
            f"{self._path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            tmp.replace(self._path)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    def get(self, connector_id: str, chat_id: str) -> str | None:
        with self._lock:
            return self._load().get(connector_id, {}).get(chat_id) or None

    def bind(self, connector_id: str, chat_id: str, conversation_id: str) -> None:
        with self._lock:
            data = self._load()
            data.setdefault(connector_id, {})[chat_id] = conversation_id
            self._save(data)

    def clear(self, connector_id: str, chat_id: str) -> None:
        with self._lock:
            data = self._load()
            if data.get(connector_id, {}).pop(chat_id, None) is not None:
                self._save(data)
