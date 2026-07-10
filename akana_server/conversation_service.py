"""Single unified conversation service — canonical store ``db/memory.db``.

Conversation list/archive/messages are read and written via the ``src/akana/memory``
core (``get_memory_core`` → ``Memory``). All methods called by routes and the chat
loop (list/get/create/ensure/patch/delete/message/meta + ``on_*``) are exposed from
a single surface.

Design:

* **Single instance.** ``get_memory_core(data_dir)`` reuses the single in-process
  ``Memory`` instance (single SQLite writer set).
* **Meta derivation.** ``ConversationMeta`` carries 7 fields; the 10 fields expected
  by the route (title_source/preview/pinned/archived_at/last_message_at) are derived
  in :class:`_Meta` (pinned/title_source from ``json_metadata``; preview/last_message_at
  from the newest turn).
* **Delete ≠ archive.** Deletion is a ``json_metadata.deleted`` stamp (excluded from
  list+archive); archiving is separate (``archived``) and remains accessible.
* **on_* hooks.** In the hot path, ``turn_writer`` bumps meta directly via
  ``conversations_meta.on_*``; these hooks delegate to the meta store for service-level
  callers (connector/test) — no double-counting (the hot path does not call these hooks).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import ulid

from akana_server.memory_core import get_memory_core

if TYPE_CHECKING:  # type-only: get_memory_core lazy-imports at runtime
    from akana.memory import Memory
    from akana.memory.conversations import ConversationMeta, ConversationStore
    from akana.memory.episodic import EpisodicStore, EpisodicTurn


def _preview(text: str, *, max_len: int = 120) -> str:
    """Same truncation as ``ConversationService._preview`` (collapse whitespace)."""
    line = " ".join(text.strip().split())
    if len(line) <= max_len:
        return line
    return line[: max_len - 1].rstrip() + "…"


@dataclass(frozen=True, slots=True)
class _Meta:
    """View carrying the same 10 fields as ``ConversationMeta``.

    ``conversations._meta_out`` reads these fields; the service derives the fields
    missing from ``ConversationMeta`` (title_source/preview/pinned/archived_at/
    last_message_at) here, so the route layer never sees store internals.
    """

    id: str
    title: str
    title_source: str
    preview: str | None
    pinned: bool
    archived_at: str | None
    created_at: str
    updated_at: str
    last_message_at: str | None
    message_count: int


@dataclass(frozen=True, slots=True)
class _Message:
    """Fields read by the route's ``/messages`` handler (``MessageOut`` shape).

    ``EpisodicTurn`` now carries ``tool_calls``/``file_ids``/``usage`` → tool cards,
    attachments, and token/cost info are preserved on reload.
    """

    id: str
    conversation_id: str
    role: str
    content: str
    created_at: str
    file_ids: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    #: On a question turn: the structured AskUser payload so the interactive card
    #: re-renders on reload / chat switch; ``None`` otherwise.
    ask_user: dict[str, Any] | None = None


class ConversationService:
    """Single canonical conversation service — operates via ``memory.db``
    (list/archive/message/meta). No separate ``episodic.db``; connects through ``Memory``.

    Exposed surface: ``list_conversations``/``create``/``search``/``get``/``patch``/
    ``ensure``/``soft_delete``/``list_messages``/``recent_llm_messages``/
    ``get_json_metadata``/``merge_json_metadata``.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = Path(data_dir)
        self._memory: Memory = get_memory_core(data_dir)

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> ConversationService:
        return cls(data_dir)

    @property
    def _meta_store(self) -> ConversationStore:
        return self._memory.conversations_meta

    @property
    def _episodic(self) -> EpisodicStore:
        return self._memory.episodic

    # -- meta derivation -------------------------------------------------------

    def _newest_turn(self, conversation_id: str) -> EpisodicTurn | None:
        """Fetch the newest USER/ASSISTANT turn (for preview + last_message_at) — SINGLE row.

        Previously fetched the full conversation (1000 rows) and took ``[-1]``;
        calling this for every sidebar row in ``list_conversations`` caused N×M
        queries (N+1 problem). This reads exactly 1 row per conversation.

        Contract: a failed turn (``role="error"``, persisted by ``turn_writer`` so the
        UI can re-render the error card) must NOT become the conversation's latest-message
        preview — the write path deliberately does not bump ``updated_at`` for it, so an
        unfiltered ``newest_turn`` would pin the failure text + timestamp on a row that
        still sorts by its old activity. Window to user/assistant roles in SQL (same role
        set as ``recent_llm_messages``) so error/system/tool turns never drive the sidebar.
        """
        turns = self._episodic.list_conversation_recent(
            conversation_id, limit=1, roles=("user", "assistant")
        )
        return turns[-1] if turns else None

    def _default_title(self) -> str:
        """Localized fallback for an untitled conversation.

        Follows the unified ``language`` runtime setting (the same source that drives
        voice/persona/UI), so a brand-new chat reads "New chat" in English mode and
        "Yeni sohbet" in Turkish — instead of a hardcoded Turkish string. The runtime
        store read is mtime-cached and only runs for the rare untitled row.
        """
        from types import SimpleNamespace

        from akana_server.runtime_settings import get_runtime

        lang = (
            str(get_runtime("language", SimpleNamespace(data_dir=self._data_dir)) or "en")
            .strip()
            .lower()
        )
        return "Yeni sohbet" if lang == "tr" else "New chat"

    def _wrap(self, meta: ConversationMeta) -> _Meta:
        """``ConversationMeta`` → :class:`_Meta` (10 alan)."""
        jm = meta.json_metadata or {}
        title = meta.title or self._default_title()  # untitled → language-aware fallback
        pinned = bool(jm.get("pinned", False))
        title_source = jm.get("title_source") or ("manual" if meta.title else "auto")
        newest = self._newest_turn(meta.id)
        if newest is not None:
            preview: str | None = _preview(newest.text)
            last_message_at: str | None = newest.ts
        else:
            preview = jm.get("preview") or None
            last_message_at = meta.updated_at
        archived_at = meta.updated_at if meta.archived else None
        return _Meta(
            id=meta.id,
            title=title,
            title_source=str(title_source),
            preview=preview,
            pinned=pinned,
            archived_at=archived_at,
            created_at=meta.created_at,
            updated_at=meta.updated_at,
            last_message_at=last_message_at,
            message_count=meta.message_count,
        )

    # -- route surface ---------------------------------------------------------

    def list_conversations(
        self,
        *,
        limit: int = 50,
        include_archived: bool = False,
        pinned_only: bool = False,
        archived_only: bool = False,
    ) -> list[_Meta]:
        # Rows with a 'deleted' stamp appear in neither the list nor the archive.
        # Since they are filtered AFTER the store LIMIT, we fetch from a ceiling (200),
        # filter, then trim to the requested limit. The pinned_only filter must run
        # BEFORE the limit slice — otherwise the newest ``limit`` rows fill the window
        # first and any pinned conversation older than that window silently vanishes
        # from the pinned view (b2h-#0).
        #
        # archived_only has the SAME hazard: the Archived tab must filter to archived
        # rows in SQL (store archived_only) BEFORE the 200 ceiling, else an archived
        # conversation older than the newest 200 active ones drops out of the mixed
        # window and — since search hard-codes WHERE archived=0 — becomes invisible and
        # un-unarchivable. So push it down to the store rather than filtering in Python.
        rows = self._meta_store.list(
            limit=200,
            include_archived=include_archived,
            archived_only=archived_only,
        )
        rows = [r for r in rows if not (r.json_metadata or {}).get("deleted")]
        metas = [self._wrap(r) for r in rows]
        if pinned_only:
            metas = [m for m in metas if m.pinned]
        return metas[: max(1, limit)]

    def create(self, *, title: str | None = None) -> _Meta:
        """No explicit ``create`` → generate a ULID, ``ensure`` the row, then ``patch`` the title."""
        cid = str(ulid.new())
        self._meta_store.ensure(cid)
        t = title.strip() if title and title.strip() else None
        if t is not None:
            self._meta_store.patch(cid, title=t)
            self._meta_store.merge_json_metadata(cid, {"title_source": "manual"})
        meta = self._meta_store.get(cid)
        assert meta is not None  # ensured above
        return self._wrap(meta)

    def ensure(self, conversation_id: str) -> _Meta:
        """Fetch the row, or create an empty one if absent (chat loop calls this before the first turn)."""
        meta = self._meta_store.ensure(conversation_id)
        return self._wrap(meta)

    def search(self, query: str, *, limit: int = 30) -> list[dict[str, Any]]:
        """Exact keys matching ``search``: conversation_id/title/preview/match_turn_id.

        Delegates to ``ConversationStore.search``, which matches conversation TITLES
        (SQL LIKE with Turkish folding) FIRST and then turn text via FTS — a keyword-only
        turn search misses renamed/LLM-titled conversations whose title words don't occur
        in any message, so a title the user plainly sees in the list was unfindable (b2h-#1).
        """
        q = query.strip()
        if not q:
            return []
        out: list[dict[str, Any]] = []
        for meta in self._meta_store.search(q, limit=limit):
            if (meta.json_metadata or {}).get("deleted"):
                continue
            wrapped = self._wrap(meta)
            out.append(
                {
                    "conversation_id": meta.id,
                    "title": wrapped.title,
                    "preview": wrapped.preview,
                    "match_turn_id": None,
                }
            )
        return out

    def get(self, conversation_id: str) -> _Meta | None:
        """Returns None for deleted (``deleted``-stamped) rows; ARCHIVED rows are VISIBLE.

        Behavior: deleted → ``get`` returns None (404); archived remains accessible
        (can be viewed + deleted).
        """
        meta = self._meta_store.get(conversation_id)
        if meta is None or (meta.json_metadata or {}).get("deleted"):
            return None
        return self._wrap(meta)

    def patch(
        self,
        conversation_id: str,
        *,
        title: str | None = None,
        pinned: bool | None = None,
        archived: bool | None = None,
    ) -> _Meta | None:
        if self.get(conversation_id) is None:
            return None
        if pinned is not None:
            self._meta_store.merge_json_metadata(conversation_id, {"pinned": bool(pinned)})
        if title is not None or archived is not None:
            patch_title = title if (title and title.strip()) else None
            self._meta_store.patch(
                conversation_id,
                title=patch_title,
                archived=archived,
            )
            if patch_title is not None:
                self._meta_store.merge_json_metadata(
                    conversation_id, {"title_source": "manual"}
                )
        meta = self._meta_store.get(conversation_id)
        if meta is None:
            return None
        return self._wrap(meta)

    def set_llm_title(self, conversation_id: str, title: str) -> None:
        """Set an LLM-summarized title (background chat-titler) — never over a manual one.

        Thin delegate to ``ConversationStore.set_llm_title``: the store guards against a
        ``title_source="manual"`` title, clips to the title ceiling, and stamps
        ``{"title_source":"auto","llm_titled":True}`` (idempotency flag). Kept on the
        service surface so the titler goes through the same access layer as the routes.
        """
        self._meta_store.set_llm_title(conversation_id, title)

    def soft_delete(self, conversation_id: str) -> bool:
        """Delete = ``json_metadata.deleted`` stamp (separate from archive; truly hides the row)."""
        meta = self._meta_store.get(conversation_id)
        if meta is None or (meta.json_metadata or {}).get("deleted"):
            return False
        self._meta_store.merge_json_metadata(conversation_id, {"deleted": True})
        return True

    def _conversation_exists(self, conversation_id: str) -> bool:
        """True if and only if a meta row EXISTS (soft-delete INCLUDED).

        Contract: "does the row exist (even if deleted)". ``_meta_store.get()``
        returns the row REGARDLESS of the deleted flag (only ``get()`` returns None
        for deleted) → existence = row is present. Tombstone detection
        (``persist._conversation_tombstoned``) relies on this: ``ensure()`` + turn
        writes are blocked on soft-deleted conversations (otherwise a deleted
        conversation would be silently resurrected as a hidden record in the list).
        """
        return self._meta_store.get(conversation_id) is not None

    # -- JSON metadata (cursor/claude bridge stores agent id here) --------------

    def get_json_metadata(self, conversation_id: str) -> dict[str, Any]:
        return self._meta_store.get_json_metadata(conversation_id)

    def merge_json_metadata(
        self, conversation_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        return self._meta_store.merge_json_metadata(conversation_id, patch)

    # -- LLM context window ----------------------------------------------------

    def recent_llm_messages(
        self, conversation_id: str, *, max_turns: int
    ) -> list[dict[str, str]]:
        """Last ``max_turns`` user/assistant messages (chronological) — LLM context window."""
        return self._meta_store.recent_llm_messages(
            conversation_id, max_turns=max_turns
        )

    def list_messages(
        self,
        conversation_id: str,
        *,
        limit: int = 500,
        before_ts: str | None = None,
        before_id: str | None = None,
    ) -> list[_Message]:
        """``EpisodicStore.list_conversation_recent`` (newest N, ASC) → ``MessageOut`` shape.

        The NEWEST ``limit`` turns are fetched in SQL (``ts DESC LIMIT``); the
        ``before_ts`` pagination predicate is also pushed to SQL. Previously the
        OLDEST 1000 were fetched and sliced in Python → newest messages were missing
        for conversations with 1000+ turns. When ``before_id`` is also provided, a
        keyset cursor is used (no boundary-loss at same-ms edges).
        """
        lim = max(1, min(limit, 1000))
        turns = self._episodic.list_conversation_recent(
            conversation_id, limit=lim, before_ts=before_ts, before_id=before_id
        )
        return [
            _Message(
                id=t.id,
                conversation_id=t.conversation_id,
                role=t.role,
                content=t.text,
                created_at=t.ts,
                file_ids=list(t.file_ids),
                tool_calls=list(t.tool_calls),
                usage=dict(t.usage) if t.usage else None,
                ask_user=dict(t.ask_user) if t.ask_user else None,
            )
            for t in turns
        ]


__all__ = ["ConversationService"]
