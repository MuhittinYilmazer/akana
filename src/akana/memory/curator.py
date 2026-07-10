"""Curator — staging → (user) → durable fact.

K30-style ``inbox_only`` review pipeline:

1. **stage** (via ``Memory.staging.stage``, done by the caller — e.g. the chat
   loop's own LLM-capture path or a heuristic extractor) puts a
   :class:`~akana.memory.staging.FactCandidate` in the inbox.
2. **inbox** lists what's pending review.
3. **promote** writes a staged candidate into semantic memory. If a *different*
   value already exists under the same key, that's a contradiction → the old
   fact is temporally superseded (invalidated + replaced), preserving history.
4. **reject** drops a candidate.

Depends only on the two stores, so it has no import cycle with the
:class:`Memory` façade and is trivially unit-testable.

An earlier version of this class also ran extraction itself (``capture``/
``stage_candidates``). That path had no production caller — the live LLM-capture
flow (``akana_server/memory_capture.py``) and the chat loop
(``akana_server/api/routes/chat/persist.py``) both stage candidates directly
via ``memory.staging.stage`` — so it was removed. Build a
:class:`~akana.memory.staging.FactCandidate` and call
``memory.staging.stage(candidate)`` for each fact you want reviewed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import ulid

from akana.memory.semantic import SemanticFact, SemanticStore
from akana.memory.staging import StagingStore

log = logging.getLogger(__name__)

OnPromote = Callable[[SemanticFact], None]
# (closed fact, replacing fact id | None) — supersede/archive notification.
OnInvalidate = Callable[[SemanticFact, str | None], None]


class Curator:
    """Review pipeline: stage (elsewhere) -> promote/reject into semantic memory."""

    def __init__(
        self,
        semantic: SemanticStore,
        staging: StagingStore,
        *,
        on_promote: OnPromote | None = None,
        on_invalidate: OnInvalidate | None = None,
    ) -> None:
        self._semantic = semantic
        self._staging = staging
        self._on_promote = on_promote
        self._on_invalidate = on_invalidate

    def _notify_invalidate(self, closed: SemanticFact, superseded_by: str | None) -> None:
        """Notify the event seam of an invalidation (vector/graph/ledger sync).

        The Curator writes to the store directly (it does not go through
        Memory.supersede_fact); without this hook the ``fact_invalidated`` event
        was never emitted, so the vector index kept the old embedding, the graph
        kept the old node, and the ledger never saw the invalidation.
        """
        if self._on_invalidate is not None:
            self._on_invalidate(closed, superseded_by)

    # -- review ----------------------------------------------------------------

    def promote(self, staged_id: str, *, supersede: bool = True) -> SemanticFact | None:
        """Approve a staged candidate into durable memory (contradiction-aware).

        CLAIM-FIRST (audit C0/C1): win the staging election via ``mark_promoted``
        BEFORE the durable write, so a concurrent reject or a double-promote can never
        leave a committed-but-unannounced durable fact — only the single winner writes
        and emits. The write itself is one atomic transaction
        (:meth:`SemanticStore.assert_fact`: find-contradictions + invalidate + upsert),
        so the old get→find→write→retry race (two writers → two conflicting valid rows)
        is gone and the b20 single-retry / mark_rejected-on-None fallback — which could
        silently DROP a user-approved value on a double lost race (audit C2) — is no
        longer needed. ``assert_fact`` always writes a valid fact.
        """
        s = self._staging.get(staged_id)
        if s is None or s.status != "pending":
            return None
        fact_id = str(ulid.new())
        if not self._staging.mark_promoted(staged_id, fact_id):
            # Lost the election (a concurrent reject/promote already decided this row) →
            # write nothing, emit nothing. No orphan durable fact, no trust-ladder divergence.
            return None
        try:
            closed, fact = self._semantic.assert_fact(
                key=s.key,
                value=s.value,
                trust=s.trust,  # type: ignore[arg-type]
                source_turn_id=s.source_turn_id,
                quote=s.quote,
                extractor=s.extractor,
                supersede=supersede,
                fact_id=fact_id,
            )
        except BaseException:
            # The durable write failed AFTER we won the staging claim (claim-first).
            # Without a revert the row stays 'promoted' with a dangling fact_id that was
            # never written, and re-approval is impossible (409) — an explicitly
            # user-approved fact silently lost. Release the claim (row -> pending, clear
            # the fact_id) so the user can retry, then re-raise so the route reports it.
            self._staging.revert_promotion(staged_id)
            raise
        if fact.id != fact_id:
            # assert_fact deduped onto a pre-existing valid fact with a DIFFERENT id
            # (the staged value already existed) → re-point the staging link at the real
            # durable id so promoted_fact_id never dangles (Group B review).
            self._staging.set_promoted_fact_id(staged_id, fact.id)
        for old in closed:
            self._notify_invalidate(old, fact.id)
        if self._on_promote is not None:
            self._on_promote(fact)
        return fact

    def reject(self, staged_id: str) -> bool:
        return self._staging.mark_rejected(staged_id)
