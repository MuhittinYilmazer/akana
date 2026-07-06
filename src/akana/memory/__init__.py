"""Memory — the unified entry point over episodic + semantic stores.

One façade, one ``memory.db`` (K11). It owns id minting, wires the two stores
together and exposes a small, stable surface for the chat loop. It deliberately
does **not** import the driver layer: memory returns plain :class:`EpisodicTurn`
/ :class:`SemanticFact` values and the loop maps them to driver messages.

Layers (vision's "languages of memory"): **Episodic** (what happened, searchable)
and **Semantic** (durable facts with evidence/trust/validity) today; Procedural
(skills) plugs into the same seam in a later milestone.

**Event seam (P8/P2).** Every mutation emits a :class:`MemoryEvent` to any
subscriber (zero cost with none). The durable ledger and the graph projector
both ride this hook; a replay engine or timeline attaches the same way.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import ulid

from akana.memory._time import iso_now
from akana.memory.conversations import ConversationMeta, ConversationStore
from akana.memory.curator import Curator
from akana.memory.embed import Embedder, EmbeddingError, HashingEmbedder, OllamaEmbedder
from akana.memory.episodic import EpisodicStore, EpisodicTurn, Role
from akana.memory.events import MemoryEvent, Subscriber
from akana.memory.graph import GraphStore
from akana.memory.ledger import MemoryLedger
from akana.memory.orchestrator import IntentProfile, MemoryOrchestrator, OrchestratorSettings
from akana.memory.projector import GraphProjector
from akana.memory.recall import Recall, RecallBlock, RecallResult, RecallTrace
from akana.memory.semantic import (
    SOURCE_ORIGINS, TRUST_RANK, SemanticFact, SemanticStore, Trust, trust_rank,
)
from akana.memory.session_closer import (
    SessionCloser,
    find_idle_conversations,
    get_session_summary,
)
from akana.memory.summary_types import SummaryView
from akana.memory.summary_consolidation import SummaryConsolidator
from akana.memory.settings import MemorySettings, load_memory_settings, save_memory_settings
from akana.memory.staging import FactCandidate, StagedFact, StagingStore
from akana.memory.tools import MEMORY_TOOLS, tool_schemas
from akana.memory.vector import VectorStore
from akana.memory.vector_recall import (
    VectorIndexer, enable_vector_recall, make_rrf_strategy, make_vector_strategy,
)

__all__ = [
    # façade + tool surface
    "Memory", "MemoryOrchestrator", "OrchestratorSettings", "IntentProfile",
    "MEMORY_TOOLS", "tool_schemas",
    # events / ledger
    "MemoryEvent", "MemoryLedger",
    # stores
    "GraphStore", "EpisodicStore", "EpisodicTurn", "SemanticStore", "SemanticFact",
    "StagingStore", "StagedFact", "FactCandidate", "VectorStore",
    "ConversationStore", "ConversationMeta",
    # settings
    "MemorySettings", "load_memory_settings", "save_memory_settings",
    # curation
    "Curator",
    "SessionCloser", "find_idle_conversations", "get_session_summary", "SummaryView",
    "SummaryConsolidator",
    # recall
    "Recall", "RecallBlock", "RecallResult", "RecallTrace",
    # vector recall (F3)
    "Embedder", "EmbeddingError", "OllamaEmbedder", "HashingEmbedder",
    "VectorIndexer", "make_vector_strategy", "make_rrf_strategy", "enable_vector_recall",
    # scalars + trust ladder / provenance (the single canonical definition lives in semantic.py)
    "Role", "Trust", "TRUST_RANK", "trust_rank", "SOURCE_ORIGINS",
]

log = logging.getLogger(__name__)


def _new_id() -> str:
    return str(ulid.new())


def _iso_now() -> str:
    return iso_now()


class Memory:
    """Unified memory façade over the episodic and semantic stores."""

    def __init__(
        self,
        episodic: EpisodicStore,
        semantic: SemanticStore,
        *,
        staging: StagingStore | None = None,
        ledger: MemoryLedger | None = None,
        graph: GraphStore | None = None,
        db_path: Path | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self._episodic = episodic
        self._semantic = semantic
        self._recall = Recall(episodic, semantic)
        self._staging = staging
        self._ledger = ledger
        self._graph = graph
        self._db_path = db_path
        self._data_dir = data_dir
        self._subscribers: list[Subscriber] = []
        # U6: a lazy, best-effort VectorStore over this Memory's own memory.db, used to
        # cascade-delete a fact's embedding on EVERY delete path — independent of whether
        # a VectorIndexer happens to be subscribed. Embeddings otherwise leak whenever the
        # embedder is unresolved (vector/embed off, fastembed missing, ollama down): no
        # indexer subscribes, so nothing prunes the row. Built on first delete only.
        self._vector_store: VectorStore | None = None
        self._vector_store_built = False
        # b22: guards the lazy first-time construction of conversations_meta (and any other
        # lazy store) on this process-wide shared singleton, so concurrent first callers don't
        # each build a separate store. Only taken on the lazy-build path, never in the hot _emit.
        self._lazy_lock = threading.Lock()
        # ledger + graph attach from birth via the same event seam (P8/P2)
        if ledger is not None:
            self.subscribe(ledger.record)
        if graph is not None:
            self.subscribe(GraphProjector(graph).on_event)

    @classmethod
    def for_data_dir(cls, data_dir: Path, *, attach_ledger: bool = True) -> Memory:
        """Build a Memory on ``<data_dir>/db/memory.db`` with ledger and graph
        alongside. Ledger + graph projector subscribe from birth (P8/P2);
        ``attach_ledger=False`` gives a write-light instance.
        """
        data_dir = data_dir.resolve()
        db_dir = data_dir / "db"
        db_dir.mkdir(parents=True, exist_ok=True)
        db_path = db_dir / "memory.db"
        return cls(
            EpisodicStore(db_path),
            SemanticStore(db_path),
            staging=StagingStore(db_path),
            ledger=MemoryLedger.for_data_dir(data_dir) if attach_ledger else None,
            graph=GraphStore.for_data_dir(data_dir),
            db_path=db_path,
            data_dir=data_dir,
        )

    # -- stores (escape hatch for advanced callers / migrations) --------------

    @property
    def episodic(self) -> EpisodicStore:
        return self._episodic

    @property
    def semantic(self) -> SemanticStore:
        return self._semantic

    @property
    def staging(self) -> StagingStore:
        if self._staging is None:
            if self._db_path is None:
                raise RuntimeError("Memory has no staging store and no db_path to build one")
            self._staging = StagingStore(self._db_path)
        return self._staging

    @property
    def ledger(self) -> MemoryLedger:
        """The durable event ledger; lazily built + subscribed if absent."""
        if self._ledger is None:
            if self._data_dir is None:
                raise RuntimeError("Memory has no ledger and no data_dir to build one")
            self._ledger = MemoryLedger.for_data_dir(self._data_dir)
            self.subscribe(self._ledger.record)
        return self._ledger

    @property
    def graph(self) -> GraphStore:
        """The knowledge graph; lazily built + projector-subscribed if absent."""
        if self._graph is None:
            if self._data_dir is None:
                raise RuntimeError("Memory has no graph and no data_dir to build one")
            self._graph = GraphStore.for_data_dir(self._data_dir)
            self.subscribe(GraphProjector(self._graph).on_event)
        return self._graph

    @property
    def conversations_meta(self) -> ConversationStore:
        """Conversation metadata store; lazily built on the shared db.

        ``getattr`` instead of an ``__init__`` slot keeps construction untouched;
        the store shares ``memory.db`` and reuses this memory's episodic store
        for search + the recent-LLM-message window.
        """
        store: ConversationStore | None = getattr(self, "_conversations_meta", None)
        if store is None:
            if self._db_path is None:
                raise RuntimeError("Memory has no db_path to build a conversation store")
            # b22: double-checked lock — concurrent first callers must not each build a store.
            with self._lazy_lock:
                store = getattr(self, "_conversations_meta", None)
                if store is None:
                    store = ConversationStore(self._db_path, episodic=self._episodic)
                    self._conversations_meta = store
        return store

    def make_curator(self) -> Curator:
        """A Curator wired to this memory's stores, emitting promote events."""
        return Curator(
            self._semantic,
            self.staging,
            on_promote=self._emit_fact,
            on_invalidate=self._emit_fact_invalidated,
        )

    def make_orchestrator(
        self, *, settings: OrchestratorSettings | None = None
    ) -> MemoryOrchestrator:
        """The ``memory.*`` tool handler (Vision §11) serving this memory."""
        return MemoryOrchestrator(self, settings=settings)

    # -- event seam (P8 replay door) ------------------------------------------

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        """Register a mutation listener; returns an unsubscribe callable."""
        self._subscribers.append(callback)

        def _unsub() -> None:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

        return _unsub

    def _emit(self, kind: str, **data: Any) -> None:
        if not self._subscribers:
            return
        event = MemoryEvent(kind=kind, ts=_iso_now(), data=data)
        for cb in tuple(self._subscribers):
            try:
                cb(event)
            except Exception:  # a bad subscriber must never break a write
                log.exception("memory subscriber raised on %s event", kind)

    def _emit_fact(self, fact: SemanticFact) -> None:
        """Emit a ``fact`` event with all the graph projector needs to mirror it."""
        self._emit("fact", fact_id=fact.id, key=fact.key, value=fact.value, trust=fact.trust)

    def _emit_fact_invalidated(self, fact: SemanticFact, superseded_by: str | None = None) -> None:
        """Emit ``fact_invalidated`` for a closed fact (supersede/archive).

        Both ``supersede_fact`` and the Curator's direct-store path produce the same
        event shape from here; the vector indexer/graph projector/ledger are all fed
        from one seam.
        """
        data: dict[str, Any] = {"fact_id": fact.id, "key": fact.key, "value": fact.value}
        if superseded_by:
            data["superseded_by"] = superseded_by
        self._drop_embedding(fact.id)
        self._emit("fact_invalidated", **data)

    def _drop_embedding(self, fact_id: str) -> None:
        """Cascade-delete a fact's embedding from the vector table (U6).

        Best-effort and idempotent by fact_id: a still-subscribed VectorIndexer also
        deletes the same row, so a double delete is harmless. The store is built lazily
        over this Memory's own memory.db so cascade works even when no indexer is wired
        (vector/embed off, fastembed missing, ollama down). Vector cleanup must never
        break a fact write, so every failure is swallowed at debug level.
        """
        if self._db_path is None:
            return
        try:
            if not self._vector_store_built:
                self._vector_store = VectorStore(self._db_path)
                self._vector_store_built = True
            if self._vector_store is not None:
                self._vector_store.delete(fact_id)
        except Exception:
            log.debug("embedding cascade-delete failed for %s", fact_id, exc_info=True)

    # -- episodic: what happened ----------------------------------------------

    def remember_turn(
        self,
        *,
        conversation_id: str,
        role: Role,
        text: str,
        turn_id: str | None = None,
        lang: str | None = None,
        importance: float | None = None,
        tool_call_id: str | None = None,
        duration_ms: int | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        file_ids: list[str] | None = None,
        usage: dict[str, Any] | None = None,
        ask_user: dict[str, Any] | None = None,
    ) -> EpisodicTurn:
        turn = self._episodic.append_turn(
            turn_id=turn_id or _new_id(),
            conversation_id=conversation_id,
            role=role,
            text=text,
            lang=lang,
            importance=importance,
            tool_call_id=tool_call_id,
            duration_ms=duration_ms,
            tool_calls=tool_calls,
            file_ids=file_ids,
            usage=usage,
            ask_user=ask_user,
        )
        self._emit("turn", turn_id=turn.id, conversation_id=conversation_id, role=role)
        return turn

    def recent_turns(
        self, conversation_id: str, *, limit: int = 200
    ) -> list[EpisodicTurn]:
        # Must be the NEWEST ``limit`` turns (ASC). ``list_conversation`` returns the
        # OLDEST 1000 then slices — so past 1000 turns the newest messages never arrive
        # and the session summarizer's anchor freezes forever (silent memory loss). The
        # ``_recent`` variant fetches ``ts DESC LIMIT ?`` then flips to ASC. For short
        # conversations (< limit) both are identical.
        return self._episodic.list_conversation_recent(conversation_id, limit=limit)

    def search_turns(
        self, query: str, *, conversation_id: str | None = None, limit: int = 20
    ) -> list[EpisodicTurn]:
        return self._episodic.search_keyword(
            query, conversation_id=conversation_id, limit=limit
        )

    def conversations(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self._episodic.list_conversation_ids(limit=limit)

    def conversation_count(self) -> int:
        """Total distinct conversations (uncapped — for stats; audit C27)."""
        return self._episodic.count_conversations()

    def turn_count(self) -> int:
        """Total turn rows across all conversations (uncapped — for stats; audit C27)."""
        return self._episodic.count_turns()

    def reset_conversation(self, conversation_id: str) -> int:
        count = self._episodic.delete_conversation(conversation_id)
        # b24: also zero the conversation's message_count so dropped_turns is correct after a
        # history clear (the counter only ever incremented → a stale value showed a wrong
        # "N old messages dropped" notice). Best-effort; never break the reset.
        if self._db_path is not None:
            try:
                self.conversations_meta.reset_message_count(conversation_id)
            except Exception:
                log.debug(
                    "reset_conversation: message_count reset failed for %s",
                    conversation_id,
                    exc_info=True,
                )
        self._emit("conversation_reset", conversation_id=conversation_id, count=count)
        return count

    # -- semantic: what is true -----------------------------------------------

    def assert_fact_direct(
        self,
        *,
        key: str,
        value: str,
        confidence: float = 0.85,
        importance: float = 0.7,
        anchored: bool = False,
        trust: Trust = "inferred",
        source_turn_id: str | None = None,
        quote: str | None = None,
        extractor: str | None = None,
        source_origin: str | None = None,
        source_detail: str | None = None,
        observed_at: str | None = None,
    ) -> tuple[list[SemanticFact], SemanticFact]:
        """Contradiction-aware owner/direct write via the atomic store primitive,
        emitting vector/graph/ledger events AFTER commit (audit C14).

        Replaces the old find_contradictions → supersede → fall-through-to-remember_fact
        dance on the direct-write paths, which on a lost supersede race left two
        conflicting valid rows under one key. ``SemanticStore.assert_fact`` does the
        find + invalidate + upsert atomically, so the loser can no longer add a second
        valid row. Returns ``(closed, new)``.
        """
        closed, new = self._semantic.assert_fact(
            key=key,
            value=value,
            confidence=confidence,
            importance=importance,
            anchored=anchored,
            trust=trust,
            source_turn_id=source_turn_id,
            quote=quote,
            extractor=extractor,
            source_origin=source_origin,
            source_detail=source_detail,
            observed_at=observed_at,
            supersede=True,
        )
        for old in closed:
            self._emit_fact_invalidated(old, superseded_by=new.id)
        self._emit_fact(new)
        return closed, new

    def list_facts(
        self,
        *,
        min_trust: str | None = None,
        limit: int = 50,
    ) -> list[SemanticFact]:
        """Query-independent fact list (importance DESC, ts_last DESC).

        For a per-session memory snapshot (Gemini Live ``system_instruction``): the
        user's most important/recent durable facts, without requiring a query."""
        return self._semantic.list_facts(min_trust=min_trust, limit=limit)

    def get_fact(self, fact_id: str) -> SemanticFact | None:
        return self._semantic.get_fact(fact_id)

    def correct_fact(
        self,
        fact_id: str,
        *,
        new_value: str,
        importance: float | None = None,
    ) -> SemanticFact | None:
        """In-place value fix (typo/cleanup) routed through the event seam.

        The store rewrites the value under the SAME id; without re-emitting a
        ``fact`` event the vector index keeps the OLD embedding and the graph
        keeps the old node (only a manual reindex would heal them). Re-emitting
        re-drives ``VectorIndexer.index_fact`` (which upserts by fact_id) and
        ``GraphProjector.link_fact`` so every subscriber re-syncs.
        """
        fact = self._semantic.correct_fact(
            fact_id, new_value=new_value, importance=importance
        )
        if fact is not None:
            self._emit_fact(fact)
        return fact

    def supersede_fact(
        self,
        fact_id: str,
        *,
        new_value: str,
        new_key: str | None = None,
        trust: Trust | None = None,
        source_turn_id: str | None = None,
        quote: str | None = None,
        extractor: str | None = None,
        source_origin: str | None = None,
        source_detail: str | None = None,
        observed_at: str | None = None,
    ) -> tuple[SemanticFact, SemanticFact] | None:
        result = self._semantic.supersede_fact(
            fact_id,
            new_value=new_value,
            new_key=new_key,
            trust=trust,
            source_turn_id=source_turn_id,
            quote=quote,
            extractor=extractor,
            source_origin=source_origin,
            source_detail=source_detail,
            observed_at=observed_at,
        )
        if result is not None:
            old, new = result
            self._emit_fact_invalidated(old, superseded_by=new.id)
            self._emit_fact(new)
        return result

    def forget_fact(self, fact_id: str, *, hard: bool = False) -> bool:
        """Invalidate a fact (default, replay-safe) or hard-delete it."""
        if hard:
            ok = self._semantic.delete_fact(fact_id)
        else:
            ok = self._semantic.invalidate_fact(fact_id) is not None
        if ok:
            # U6: cascade the embedding regardless of whether an indexer is subscribed.
            # forget_fact emits the raw event (not via _emit_fact_invalidated), so drop
            # here directly. Both hard and soft forget shrink the table — matching the
            # indexer's contract that it tracks only currently-valid facts.
            self._drop_embedding(fact_id)
            self._emit("fact_invalidated", fact_id=fact_id, hard=hard)
        return ok

    # -- recall: bring the past to bear (M2) ----------------------------------

    def recall(
        self,
        query: str,
        *,
        conversation_id: str | None = None,
        min_trust: str | None = "inferred",
        limit: int = 12,
        budget_tokens: int = 2000,
        language: str = "en",
    ) -> RecallResult:
        """Fuse semantic + episodic memory for ``query`` (with an explain trace).

        ``language`` picks the episodic role labels ("You"/"Akana"/"System" vs
        "Sen"/"Akana"/"Sistem") rendered into recalled turn text; English by
        default, Turkish only when a caller explicitly opts in.
        """
        return self._recall.recall(
            query,
            conversation_id=conversation_id,
            min_trust=min_trust,
            limit=limit,
            budget_tokens=budget_tokens,
            language=language,
        )
