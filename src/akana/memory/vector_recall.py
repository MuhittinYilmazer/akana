"""Vector recall — the real ``vector_first`` and ``rrf`` strategies (Vision §11).

Until now the orchestrator's strategy registry only held ``fts_first``; intents
asking for ``vector_first``/``rrf`` fell back with a warning. This module fills
those slots without touching the orchestrator: factories build strategy
functions matching the registry signature, a :class:`VectorIndexer` rides the
Memory event seam (P8) to keep the embedding table in sync, and
:func:`enable_vector_recall` wires it all up as an explicit opt-in — pass no
embedder and nothing changes.

Fusion follows §11.3 option A: Reciprocal Rank Fusion with ``k_const=60``,
``score(item) = Σ_ranking 1 / (k + rank)``. Budget trimming *is*
:meth:`akana.memory.recall.Recall._apply_budget` (token-estimate greedy fill,
oversized blocks skipped) so budgets mean the same thing everywhere.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Callable, Protocol

from akana.memory.embed import Embedder, ModelNotFoundError
from akana.memory.events import MemoryEvent
from akana.memory.recall import Recall, RecallBlock, RecallResult, RecallTrace

# Package-internal reuse: the trust ladder must have exactly one home (P6).
from akana.memory.semantic import _trust_allowset
from akana.memory.vector import VectorStore

if TYPE_CHECKING:
    from akana.memory import Memory
    from akana.memory.orchestrator import MemoryOrchestrator

__all__ = [
    "VectorHealth",
    "VectorIndexer",
    "make_vector_strategy",
    "make_rrf_strategy",
    "enable_vector_recall",
]

log = logging.getLogger(__name__)

RRF_K_CONST = 60  # §11.3 option A default
#: After a transient embed failure the vector layer stays off this long —
#: one failed 10s call must not become a per-query retry storm.
EMBED_COOLDOWN_S = 120.0
_DEFAULT_MIN_TRUST = "inferred"  # K15, same floor as recall.py
# Candidate collection must see the *full* keyword ranking; the budget is
# applied once, after fusion (§11.4), not inside each source.
_UNTRIMMED_BUDGET = 1_000_000


class _StrategyFn(Protocol):
    def __call__(
        self,
        *,
        query: str,
        conversation_id: str | None = ...,
        min_trust: str | None = ...,
        limit: int = ...,
        budget_tokens: int = ...,
    ) -> RecallResult: ...


def _fact_text(key: str, value: str) -> str:
    """The one rendering of a fact we embed — matches recall's semantic blocks."""
    return f"{key}: {value}"


# Package-internal reuse, same rationale as ``_trust_allowset``: budget
# trimming must have exactly one home. A hand-copied mirror here once drifted
# from recall.py's semantics — an alias cannot.
_apply_budget = Recall._apply_budget


# -- health (circuit breaker) -----------------------------------------------------


class VectorHealth:
    """Shared circuit breaker for one vector wiring: embed must never crash recall.

    Strategies and the indexer consult :meth:`active` before touching the
    embedder and report outcomes via :meth:`record_failure` /
    :meth:`record_success`. A transient failure (timeout, connection refused,
    bad payload) trips a cooldown — the lexical path answers every query in
    the meantime, and embedding is retried only after ``cooldown_s``. A
    :class:`~akana.memory.embed.ModelNotFoundError` disables the layer for
    the rest of the process: retrying cannot install the model, the operator
    must ``ollama pull`` it. Each trip logs exactly one line; disabled
    queries are silent (no log spam, no network).
    """

    def __init__(
        self,
        *,
        cooldown_s: float = EMBED_COOLDOWN_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._disabled_until = 0.0
        self._permanent = False
        # audit C28: one VectorHealth is shared by the write thread (on_event) and the
        # recall threads (vector_first/rrf). Guard the read-modify-decision in
        # record_failure against a concurrent record_success so the breaker state can't tear.
        self._lock = threading.Lock()

    def active(self) -> bool:
        """May the embedder be called right now?"""
        with self._lock:
            return not self._permanent and self._clock() >= self._disabled_until

    def record_success(self) -> None:
        with self._lock:
            self._disabled_until = 0.0

    def record_failure(self, exc: Exception) -> None:
        if isinstance(exc, ModelNotFoundError):
            with self._lock:
                already = self._permanent
                self._permanent = True
            if not already:
                log.warning(
                    "embed model missing — vector recall disabled for this process, "
                    "keyword recall continues: %s",
                    exc,
                )
            return
        with self._lock:
            # transition only: one log line per trip, not per query
            transition = not self._permanent and self._clock() >= self._disabled_until
            self._disabled_until = self._clock() + self._cooldown_s
        if transition:
            log.warning(
                "embed call failed (%s); vector recall paused for %.0fs, "
                "keyword recall continues",
                exc,
                self._cooldown_s,
            )


# -- indexer (event seam) --------------------------------------------------------


class VectorIndexer:
    """Keeps the embedding table mirroring the fact store via memory events.

    Subscribe :meth:`on_event` with ``memory.subscribe`` — ``fact`` events
    index the new fact, ``fact_invalidated`` events drop its vector (a
    supersede emits both, so the table tracks only currently-valid facts).
    :meth:`reindex` backfills facts written before the indexer attached.

    Embedding failures never propagate: a write event whose embed fails is
    logged through ``health`` and skipped (the fact itself is already safely
    stored — only its vector is missing), and while the health gate is
    tripped no embed is even attempted.
    """

    def __init__(
        self,
        store: VectorStore,
        embedder: Embedder,
        *,
        health: VectorHealth | None = None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._health = health if health is not None else VectorHealth()
        #: the ``memory.subscribe`` unsubscribe callable (filled in by enable_vector_recall).
        self._unsubscribe: Callable[[], None] | None = None
        # audit C11: fact events whose embed is skipped while the health gate is tripped
        # (cooldown) are remembered here (fact_id -> latest key/value) and re-embedded when
        # the gate recovers — otherwise a correct/supersede during cooldown leaves the vector
        # permanently stale (record_success only clears the cooldown, it never replays).
        self._dirty: dict[str, tuple[str, str]] = {}
        self._dirty_lock = threading.Lock()

    def detach(self) -> None:
        """Remove this indexer's ``memory`` fact-event subscription (idempotent).

        On a rebuild over the single in-process ``Memory`` singleton (e.g. a stack
        rebuild on a settings change), if the old indexer's ``on_event`` stays
        subscribed, every fact is embedded N× and written N× to the vector table
        (#19 subscriber leak). Before a rebuild the old indexer is ``detach``ed → only
        one subscriber remains.
        """
        unsub = self._unsubscribe
        self._unsubscribe = None
        if unsub is not None:
            unsub()

    def on_event(self, event: MemoryEvent) -> None:
        fact_id = event.data.get("fact_id")
        if not fact_id:
            return
        fid = str(fact_id)
        if event.kind == "fact":
            key = str(event.data.get("key", ""))
            value = str(event.data.get("value", ""))
            if not self._health.active():
                # Vector layer cooling down/off; the fact store is unaffected. Remember
                # this fact so its embedding is refreshed when the gate recovers (C11).
                with self._dirty_lock:
                    self._dirty[fid] = (key, value)
                return
            try:
                self._store.index_fact(fid, _fact_text(key, value), self._embedder)
                self._health.record_success()
                with self._dirty_lock:
                    self._dirty.pop(fid, None)
                self._replay_dirty()  # gate is up → flush anything skipped during cooldown
            except Exception as e:  # a missing vector must never break a write
                self._health.record_failure(e)
                with self._dirty_lock:
                    self._dirty[fid] = (key, value)
        elif event.kind == "fact_invalidated":
            with self._dirty_lock:
                self._dirty.pop(fid, None)  # no longer valid → do not re-embed it
            self._store.delete(fid)  # no embed involved — always runs

    def _replay_dirty(self) -> None:
        """Re-embed facts whose update was skipped during a health cooldown (C11).

        Called after a successful embed re-opens the gate. If the gate re-trips
        mid-replay, the remaining facts are requeued for the next recovery.
        """
        with self._dirty_lock:
            pending = list(self._dirty.items())
            self._dirty.clear()
        for fid, (key, value) in pending:
            if not self._health.active():
                with self._dirty_lock:
                    self._dirty[fid] = (key, value)
                continue
            try:
                self._store.index_fact(fid, _fact_text(key, value), self._embedder)
                self._health.record_success()
            except Exception as e:
                self._health.record_failure(e)
                with self._dirty_lock:
                    self._dirty[fid] = (key, value)

    def reindex(
        self,
        memory: Memory,
        *,
        batch_size: int = 32,
        should_continue: Callable[[], bool] | None = None,
    ) -> int:
        """Backfill currently-valid facts in batches; returns how many landed.

        One embed call per batch (not per fact), so a large db backfills in
        len/batch_size round-trips. ``should_continue`` is checked before each
        batch — return ``False`` to stop cleanly (shutdown, user cancel). An
        embed failure logs through health and stops the backfill instead of
        raising; whatever was already indexed stays, live indexing resumes
        when health recovers.
        """
        facts = list(memory.semantic.list_all_facts())
        step = max(1, batch_size)
        n = 0
        for start in range(0, len(facts), step):
            if should_continue is not None and not should_continue():
                log.info("vector backfill interrupted: %d/%d fact(s) indexed", n, len(facts))
                break
            if not self._health.active():
                log.info(
                    "vector backfill stopped (embed disabled): %d/%d fact(s) indexed",
                    n,
                    len(facts),
                )
                break
            batch = facts[start : start + step]
            try:
                n += self._store.index_many(
                    [(f.id, _fact_text(f.key, f.value)) for f in batch], self._embedder
                )
                self._health.record_success()
            except Exception as e:  # degrade, don't raise: lexical recall still answers
                self._health.record_failure(e)
                log.warning(
                    "vector backfill batch failed (%s): %d/%d fact(s) indexed; "
                    "keyword recall continues",
                    e,
                    n,
                    len(facts),
                )
                break
        return n


# -- strategies --------------------------------------------------------------------


def _vector_blocks(
    memory: Memory,
    store: VectorStore,
    embedder: Embedder,
    query: str,
    *,
    min_trust: str | None,
    limit: int,
) -> tuple[list[RecallBlock], int]:
    """Cosine hits → trust-gated semantic blocks (best first).

    Returns ``(blocks, hits_examined)``. The store knows nothing about trust,
    so the gate applies here, from the fact's own fields. The search is pinned
    to ``embedder.name`` — vectors from other models share a table (and
    possibly a dimension) but never a space.
    """
    hits = store.search(
        embedder.embed([query])[0], limit=max(limit * 3, 10), model=embedder.name
    )
    allow = set(_trust_allowset(min_trust)) if min_trust else None
    blocks: list[RecallBlock] = []
    for fact_id, score in hits:
        fact = memory.get_fact(fact_id)
        if fact is None or not fact.is_valid:
            continue
        if allow is not None and fact.trust not in allow:
            continue
        blocks.append(
            RecallBlock(
                kind="semantic",
                text=_fact_text(fact.key, fact.value),
                ts=fact.ts_last,
                score=score,
                trust=fact.trust,
                source_ids=(fact.id,),
            )
        )
    return blocks, len(hits)


def make_vector_strategy(
    memory: Memory,
    store: VectorStore,
    embedder: Embedder,
    *,
    health: VectorHealth | None = None,
) -> _StrategyFn:
    """Build the ``vector_first`` strategy: embed the query, cosine-rank facts.

    Degrade contract: when the embedder is unavailable (health gate tripped)
    or the embed call fails, the query is answered by plain keyword recall —
    the caller always gets a result, never an exception.
    """
    _health = health if health is not None else VectorHealth()

    def vector_first(
        *,
        query: str,
        conversation_id: str | None = None,
        min_trust: str | None = _DEFAULT_MIN_TRUST,
        limit: int = 12,
        budget_tokens: int = 2000,
    ) -> RecallResult:
        vector_out: tuple[list[RecallBlock], int] | None = None
        if _health.active():
            try:
                vector_out = _vector_blocks(
                    memory, store, embedder, query,
                    min_trust=min_trust, limit=limit,
                )
                _health.record_success()
            except Exception as e:  # embed must never break recall
                _health.record_failure(e)
        if vector_out is None:  # degraded: the lexical path answers this query
            return memory.recall(
                query,
                conversation_id=conversation_id,
                min_trust=min_trust,
                limit=limit,
                budget_tokens=budget_tokens,
            )
        blocks, examined = vector_out
        considered = blocks[: limit * 2]
        out, total = _apply_budget(considered, budget_tokens)
        trace = RecallTrace(
            query=query,
            terms=(query,),  # the whole query is the unit of search (no tokenizing)
            min_trust=min_trust,
            conversation_id=conversation_id,
            semantic_candidates=examined,
            episodic_candidates=0,  # vector index covers facts only today
            merged=len(blocks),
            returned=len(out),
            dropped_for_budget=max(0, len(considered) - len(out)),
            total_tokens=total,
            budget_tokens=budget_tokens,
        )
        return RecallResult(blocks=out, trace=trace)

    return vector_first


def make_rrf_strategy(
    memory: Memory,
    store: VectorStore,
    embedder: Embedder,
    *,
    k_const: int = RRF_K_CONST,
    health: VectorHealth | None = None,
) -> _StrategyFn:
    """Build the ``rrf`` strategy: fuse keyword + vector rankings (§11.3 A).

    Degrade contract: an unavailable/failing embedder contributes an empty
    vector ranking — fusion then preserves the keyword order, so the result
    *is* the lexical answer (budget applied as usual), never an exception.
    """
    _health = health if health is not None else VectorHealth()

    def rrf(
        *,
        query: str,
        conversation_id: str | None = None,
        min_trust: str | None = _DEFAULT_MIN_TRUST,
        limit: int = 12,
        budget_tokens: int = 2000,
    ) -> RecallResult:
        keyword = memory.recall(
            query,
            conversation_id=conversation_id,
            min_trust=min_trust,
            limit=limit,
            budget_tokens=_UNTRIMMED_BUDGET,
        )
        vec_blocks: list[RecallBlock] = []
        vec_examined = 0
        if _health.active():
            try:
                vec_blocks, vec_examined = _vector_blocks(
                    memory, store, embedder, query,
                    min_trust=min_trust, limit=limit,
                )
                _health.record_success()
            except Exception as e:  # embed must never break recall
                _health.record_failure(e)

        scores: dict[str, float] = {}
        first_seen: dict[str, RecallBlock] = {}
        for ranking in (keyword.blocks, vec_blocks):
            for rank, block in enumerate(ranking, start=1):
                ident = block.source_ids[0] if block.source_ids else block.text[:120]
                scores[ident] = scores.get(ident, 0.0) + 1.0 / (k_const + rank)
                first_seen.setdefault(ident, block)
        fused = [
            replace(first_seen[ident], score=round(score, 6))
            for ident, score in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        ]

        considered = fused[: limit * 2]
        out, total = _apply_budget(considered, budget_tokens)
        trace = RecallTrace(
            query=query,
            terms=keyword.trace.terms,
            min_trust=min_trust,
            conversation_id=conversation_id,
            semantic_candidates=keyword.trace.semantic_candidates + vec_examined,
            episodic_candidates=keyword.trace.episodic_candidates,
            merged=len(fused),
            returned=len(out),
            dropped_for_budget=max(0, len(considered) - len(out)),
            total_tokens=total,
            budget_tokens=budget_tokens,
        )
        return RecallResult(blocks=out, trace=trace)

    return rrf


# -- wiring -------------------------------------------------------------------------


def enable_vector_recall(
    memory: Memory,
    orchestrator: MemoryOrchestrator,
    embedder: Embedder | None = None,
    *,
    store: VectorStore | None = None,
    health: VectorHealth | None = None,
) -> VectorIndexer | None:
    """Opt in to vector recall: register strategies + attach the live indexer.

    With ``embedder=None`` this is a guaranteed no-op returning ``None`` — no
    strategy registered, the orchestrator's fts fallback (and its warning)
    stays exactly as before. With an embedder, ``vector_first`` and ``rrf``
    land in the registry, and the returned :class:`VectorIndexer` is already
    subscribed to fact events. Call ``indexer.reindex(memory)`` once to
    backfill facts that predate the wiring. ``store`` defaults to an
    ``embeddings`` table inside the same ``memory.db`` (K11).

    Both strategies and the indexer share one :class:`VectorHealth` (pass
    ``health`` to inject a configured one): the first embed failure anywhere
    pauses the whole vector layer at once — no per-component retry storms —
    while keyword recall keeps answering.
    """
    if embedder is None:
        return None
    if store is None:
        db_path = memory._db_path  # package-internal: Memory exposes no db accessor
        if db_path is None:
            raise ValueError("memory has no db_path; pass an explicit VectorStore")
        store = VectorStore(db_path)
    shared = health if health is not None else VectorHealth()
    orchestrator.register_strategy(
        "vector_first", make_vector_strategy(memory, store, embedder, health=shared)
    )
    orchestrator.register_strategy(
        "rrf", make_rrf_strategy(memory, store, embedder, health=shared)
    )
    indexer = VectorIndexer(store, embedder, health=shared)
    # Store the unsubscribe callable so that on a rebuild over the same Memory,
    # indexer.detach() can remove the old subscription (otherwise subscribers pile up, #19).
    indexer._unsubscribe = memory.subscribe(indexer.on_event)
    return indexer
