"""Conversation metadata — titles, archive state and per-thread JSON metadata.

The "which threads exist" layer over episodic turns. It carries the behaviour
contract the web UI relies on (ported from the legacy
``akana_server.memory_engine.conversations.ConversationService``):
auto-title from the first user message, archive/soft-delete, a free-form
``json_metadata`` column (the cursor bridge stores ``agent_id`` there),
keyword search across titles + turn texts, and the recent user/assistant
window the LLM prompt is built from.

Simplifications vs the legacy service: soft delete *is* ``archived=1`` (one
flag instead of ``archived_at``/``deleted_at``), and turns stay fully owned by
:class:`~akana.memory.episodic.EpisodicStore` — this store only keeps counters
and metadata in the shared ``memory.db`` (K11).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from akana.memory._time import iso_now
from akana.memory.episodic import EpisodicStore
from akana.memory.terms import escape_like, fold_text

log = logging.getLogger(__name__)

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT,
    created_at TEXT,
    updated_at TEXT,
    archived INTEGER DEFAULT 0,
    message_count INTEGER DEFAULT 0,
    json_metadata TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_conversations_updated
    ON conversations(archived, updated_at DESC);
"""

_AUTO_TITLE_MAX = 60
# A user/tool-supplied title is bounded too: while auto-title is clipped to 60, a
# manual patch was unbounded — a runaway (very long/pasted) title could bloat the
# conversation-list payload. A generous but finite ceiling.
_TITLE_MAX = 200


@dataclass(frozen=True, slots=True)
class ConversationMeta:
    """One conversation row; ``json_metadata`` is the parsed dict."""

    id: str
    title: str | None
    created_at: str
    updated_at: str
    archived: bool
    message_count: int
    json_metadata: dict[str, Any] = field(default_factory=dict)


class ConversationStore:
    """SQLite-backed conversation metadata (shares ``memory.db``).

    ``episodic`` is optional: without it the store still does CRUD/metadata,
    but :meth:`search` falls back to title-only and
    :meth:`recent_llm_messages` returns ``[]``.
    """

    def __init__(self, db_path: Path, *, episodic: EpisodicStore | None = None) -> None:
        self._path = db_path.resolve()
        self._episodic = episodic
        self._lock = threading.Lock()
        self._init_db()

    @classmethod
    def for_data_dir(
        cls, data_dir: Path, *, episodic: EpisodicStore | None = None
    ) -> ConversationStore:
        db_dir = data_dir / "db"
        db_dir.mkdir(parents=True, exist_ok=True)
        return cls(db_dir / "memory.db", episodic=episodic)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        # Two processes (server + MCP subprocess) share memory.db; without a
        # busy timeout a write-txn in the other process raises a raw
        # "database is locked" instead of waiting.
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

    @staticmethod
    def _iso_now() -> str:
        return iso_now()

    @staticmethod
    def _auto_title(text: str, *, max_len: int = _AUTO_TITLE_MAX) -> str | None:
        """First line of ``text`` squeezed to ``max_len`` chars; ``None`` if blank."""
        line = " ".join(text.strip().split())
        if not line:
            return None
        if len(line) <= max_len:
            return line
        return line[: max_len - 1].rstrip() + "…"

    @staticmethod
    def _parse_metadata(raw: object) -> dict[str, Any]:
        try:
            meta = json.loads(str(raw) if raw else "{}")
        except json.JSONDecodeError:
            return {}
        return meta if isinstance(meta, dict) else {}

    def _row_to_meta(self, r: sqlite3.Row) -> ConversationMeta:
        return ConversationMeta(
            id=str(r["id"]),
            title=r["title"],
            created_at=str(r["created_at"] or ""),
            updated_at=str(r["updated_at"] or ""),
            archived=bool(r["archived"]),
            message_count=int(r["message_count"] or 0),
            json_metadata=self._parse_metadata(r["json_metadata"]),
        )

    # -- CRUD ------------------------------------------------------------------

    def ensure(self, conversation_id: str) -> ConversationMeta:
        """Return the row for ``conversation_id``, creating an empty one if absent."""
        existing = self.get(conversation_id)
        if existing is not None:
            return existing
        now = self._iso_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO conversations
                    (id, title, created_at, updated_at, archived, message_count, json_metadata)
                    VALUES (?, NULL, ?, ?, 0, 0, '{}')
                    """,
                    (conversation_id, now, now),
                )
                conn.commit()
            finally:
                conn.close()
        meta = self.get(conversation_id)
        if meta is None:  # pragma: no cover - PK insert cannot silently vanish
            raise RuntimeError(f"failed to ensure conversation {conversation_id}")
        return meta

    def get(self, conversation_id: str) -> ConversationMeta | None:
        """Fetch one row (archived included — only :meth:`list` hides them).

        LOCK-FREE READ: in WAL mode a reader sees a consistent snapshot; the global
        ``self._lock`` only serializes WRITES. Previously reads also took the lock, so
        during streaming, when frequent append_turn writes held the lock, this read was
        QUEUED (user: /messages ~2s while streaming). A lock-free read runs concurrently;
        data safety is preserved by the WAL snapshot."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
        finally:
            conn.close()
        return self._row_to_meta(row) if row else None

    def list(
        self,
        *,
        limit: int = 50,
        include_archived: bool = False,
        archived_only: bool = False,
    ) -> list[ConversationMeta]:
        """Most recently updated first; archived rows hidden by default.

        Rows stamped ``json_metadata.deleted`` are filtered out IN SQL as well.
        Otherwise the LIMIT (200) FILLED UP with deleted/cached rows and OLDER but LIVE
        conversations spilled out of the window and DISAPPEARED from the list (user
        report: "my old conversations vanished" — 197 deleted rows pushed 13 live ones
        out of the 200 window). When the filtering happens in SQL, the LIMIT applies only
        to LIVE rows; even though the adapter also filters in Python, nothing is lost now.

        ``archived_only`` emits ``archived = 1`` so the Archived view's filter runs BEFORE
        the LIMIT — otherwise active rows fill the 200-row window and an archived
        conversation older than the newest 200 active ones silently vanishes from the
        Archived tab (and is unsearchable, so it can never be unarchived). Mirrors the
        pinned_only pre-limit filtering in the service.
        """
        lim = max(1, min(limit, 200))
        if archived_only:
            base = "archived = 1"
        elif include_archived:
            base = "1=1"
        else:
            base = "archived = 0"
        where = f"{base} AND COALESCE(json_extract(json_metadata, '$.deleted'), 0) = 0"
        # LOCK-FREE READ (WAL snapshot) — see get(): don't let streaming-time writes
        # queue the list fetch.
        conn = self._connect()
        try:
            rows = conn.execute(
                f"""
                SELECT * FROM conversations
                WHERE {where}
                ORDER BY updated_at DESC, rowid DESC
                LIMIT ?
                """,  # noqa: S608 - where is a static literal
                (lim,),
            ).fetchall()
        finally:
            conn.close()
        return [self._row_to_meta(r) for r in rows]

    def patch(
        self,
        conversation_id: str,
        *,
        title: str | None = None,
        archived: bool | None = None,
    ) -> ConversationMeta | None:
        """Update title and/or archived flag; ``None`` leaves a field untouched."""
        meta = self.get(conversation_id)
        if meta is None:
            return None
        new_title = title.strip()[:_TITLE_MAX] if title and title.strip() else meta.title
        new_archived = meta.archived if archived is None else bool(archived)
        now = self._iso_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE conversations SET title = ?, archived = ?, updated_at = ? "
                    "WHERE id = ?",
                    (new_title, 1 if new_archived else 0, now, conversation_id),
                )
                conn.commit()
            finally:
                conn.close()
        return self.get(conversation_id)

    def soft_delete(self, conversation_id: str) -> bool:
        """Hide a conversation (``archived=1``); turns stay in episodic memory."""
        if self.get(conversation_id) is None:
            return False
        now = self._iso_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE conversations SET archived = 1, updated_at = ? WHERE id = ?",
                    (now, conversation_id),
                )
                conn.commit()
            finally:
                conn.close()
        return True

    # -- JSON metadata (cursor bridge stores agent_id here) --------------

    def get_json_metadata(self, conversation_id: str) -> dict[str, Any]:
        meta = self.get(conversation_id)
        return dict(meta.json_metadata) if meta else {}

    def merge_json_metadata(
        self, conversation_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        """Shallow-merge keys into ``json_metadata`` (``None`` removes a key)."""
        self.ensure(conversation_id)
        now = self._iso_now()
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT json_metadata FROM conversations WHERE id = ?",
                    (conversation_id,),
                ).fetchone()
                if row is None:  # pragma: no cover - ensured above
                    return {}
                cur = self._parse_metadata(row["json_metadata"])
                for key, val in patch.items():
                    if val is None:
                        cur.pop(key, None)
                    else:
                        cur[key] = val
                conn.execute(
                    "UPDATE conversations SET json_metadata = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(cur, ensure_ascii=False), now, conversation_id),
                )
                conn.commit()
            finally:
                conn.close()
        return cur

    # -- message lifecycle hooks -------------------------------------------------

    def on_user_message(self, conversation_id: str, text: str) -> ConversationMeta:
        """After a user turn: ensure row, bump count, auto-title if untitled."""
        self.ensure(conversation_id)
        now = self._iso_now()
        auto = self._auto_title(text)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    UPDATE conversations SET
                        message_count = message_count + 1,
                        updated_at = ?,
                        title = CASE
                            WHEN (title IS NULL OR title = '') AND ? IS NOT NULL THEN ?
                            ELSE title
                        END
                    WHERE id = ?
                    """,
                    (now, auto, auto, conversation_id),
                )
                conn.commit()
            finally:
                conn.close()
        meta = self.get(conversation_id)
        assert meta is not None  # ensured above
        return meta

    def on_assistant_message(self, conversation_id: str) -> ConversationMeta:
        """After an assistant turn: ensure row, bump count, touch updated_at."""
        self.ensure(conversation_id)
        now = self._iso_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE conversations SET message_count = message_count + 1, "
                    "updated_at = ? WHERE id = ?",
                    (now, conversation_id),
                )
                conn.commit()
            finally:
                conn.close()
        meta = self.get(conversation_id)
        assert meta is not None  # ensured above
        return meta

    def set_llm_title(self, conversation_id: str, title: str) -> None:
        """Set an LLM-summarized title — but NEVER over a manual (user) title.

        The background chat-titler (``chat_titler.maybe_title_conversation``) calls this
        once per conversation to upgrade the truncation auto-title to a short LLM summary.
        Guard: if ``json_metadata.title_source`` is already ``"manual"`` (a user rename),
        the title is left untouched — an async summary must never clobber an explicit name.
        On write the title is clipped to ``_TITLE_MAX`` and the metadata is merged with
        ``{"title_source": "auto", "llm_titled": True}`` (mirrors the patch/merge style;
        the ``llm_titled`` flag makes the titler idempotent so it runs ONCE).
        """
        clean = title.strip()[:_TITLE_MAX] if title and title.strip() else ""
        if not clean:
            return
        meta = self.get(conversation_id)
        if meta is None:
            return
        if (meta.json_metadata or {}).get("title_source") == "manual":
            # A user rename owns the title — record that the titler ran (idempotent) but
            # do not change the title.
            self.merge_json_metadata(conversation_id, {"llm_titled": True})
            return
        now = self._iso_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                    (clean, now, conversation_id),
                )
                conn.commit()
            finally:
                conn.close()
        # Stamp source + idempotency flag (separate merge — keeps the SQL above minimal,
        # same as the manual-rename path in conversation_service.patch).
        self.merge_json_metadata(
            conversation_id, {"title_source": "auto", "llm_titled": True}
        )

    def reset_message_count(self, conversation_id: str) -> None:
        """Zero the message counter after a history clear. message_count only ever incremented,
        so without this it stayed stale after a reset and ``dropped_turns`` was wrong (b24)."""
        now = self._iso_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE conversations SET message_count = 0, updated_at = ? WHERE id = ?",
                    (now, conversation_id),
                )
                conn.commit()
            finally:
                conn.close()

    # -- search + LLM window -----------------------------------------------------

    def search(self, query: str, *, limit: int = 30) -> list[ConversationMeta]:
        """Find conversations by title or by matching turn text.

        Title matches first (SQL ``LIKE``), then conversations whose episodic
        turns match via FTS (when an :class:`EpisodicStore` was provided).
        Archived (= soft-deleted) conversations are excluded; results dedupe
        on conversation id.
        """
        q = query.strip()
        if not q:
            return []
        lim = max(1, min(limit, 100))
        out: list[ConversationMeta] = []
        seen: set[str] = set()
        # Turkish-folded matching: SQLite LIKE only folds ASCII, so an 'İstanbul'
        # title would never match '%istanbul%'. Both the title and the pattern go
        # through fold_text (same semantics as the semantic store's norm columns; a
        # per-row function is fine on a ≤200-row table).
        pattern = f"%{escape_like(fold_text(q))}%"
        with self._lock:
            conn = self._connect()
            try:
                conn.create_function("akana_fold", 1, fold_text, deterministic=True)
                rows = conn.execute(
                    # audit C31: exclude soft-DELETED rows too (not just archived), matching
                    # list() — soft_delete stamps json_metadata.deleted without setting archived,
                    # so without this the store-level search leaked deleted conversations.
                    """
                    SELECT * FROM conversations
                    WHERE archived = 0
                      AND COALESCE(json_extract(json_metadata, '$.deleted'), 0) = 0
                      AND akana_fold(IFNULL(title, '')) LIKE ? ESCAPE '\\'
                    ORDER BY updated_at DESC, rowid DESC
                    LIMIT ?
                    """,
                    (pattern, lim),
                ).fetchall()
            finally:
                conn.close()
        for r in rows:
            meta = self._row_to_meta(r)
            seen.add(meta.id)
            out.append(meta)
        if self._episodic is None or len(out) >= lim:
            return out[:lim]
        for turn in self._episodic.search_keyword(q, limit=lim):
            if turn.conversation_id in seen:
                continue
            seen.add(turn.conversation_id)
            meta = self.get(turn.conversation_id)
            if meta is None or meta.archived or meta.json_metadata.get("deleted"):
                continue  # audit C31: skip soft-deleted conversations here too
            out.append(meta)
            if len(out) >= lim:
                break
        return out

    def recent_llm_messages(
        self, conversation_id: str, *, max_turns: int = 20
    ) -> list[dict[str, str]]:
        """Last ``max_turns`` user/assistant messages, chronological, for the LLM."""
        if self._episodic is None:
            return []
        cap = max(1, max_turns)
        # audit C24: fetch the newest ``cap`` USER/ASSISTANT turns directly in SQL. The old
        # code fetched the newest 1000 turns of ALL roles then filtered — so >1000 consecutive
        # tool/system/error turns after the last exchange (a long agentic session) starved the
        # model of prior user/assistant context. Role-windowing in SQL fixes that.
        turns = self._episodic.list_conversation_recent(
            conversation_id, limit=cap, roles=("user", "assistant")
        )
        return [{"role": t.role, "content": t.text} for t in turns]


__all__ = ["ConversationMeta", "ConversationStore"]
