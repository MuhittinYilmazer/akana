"""MemoryOrchestrator — the single entry point behind the ``memory.*`` tools.

Vision §11: every consumer (chat LLM, skill runner, Studio) goes through this
handler; none touches the stores directly. The orchestrator owns intent routing
(O1), scope + trust gating (O2/O3), budget enforcement (O6), the recall trace
with its ``explain_id`` (O8), and a rate limiter (O10).

Honesty over theatre: today retrieval is the façade's keyword/FTS recall, so
the executed strategy is reported as ``fts_first`` even when an intent *asks*
for ``vector_first``/``graph_first`` — those names resolve through the strategy
registry and fall back until the vector milestone registers real ones. The
registry is the open door (P-extensibility): adding vector search later is
``register_strategy("vector_first", fn)``, not a refactor.

The orchestrator never raises at the tool boundary — every failure returns an
``{"error": {...}}`` envelope the model can read and react to.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import ulid

from akana.memory.fusion import (
    blocks_to_items,
    filter_observed,
    filter_time,
    filter_types,
)
from akana.memory.mutate import forget as run_forget
from akana.memory.mutate import remember as run_remember
from akana.memory.recall import (
    _DEFAULT_LANGUAGE,
    _EPISODIC_SCORE,  # single source for the episodic block score/labels
    _ROLE_LABELS,
    Recall,
    RecallBlock,
    RecallResult,
    RecallTrace,
    episodic_turn_eligible,
)
from akana.memory.terms import recall_search_terms
from akana.memory.tools import (
    MEMORY_TOOLS,
    RememberRequest,
    SearchRequest,
    ToolValidationError,
    error_envelope,
    parse_time_bound,
    parse_tool_request,
)

if TYPE_CHECKING:
    from akana.memory import Memory

__all__ = ["MemoryOrchestrator", "OrchestratorSettings", "IntentProfile"]

log = logging.getLogger(__name__)

Strategy = Literal["graph_first", "vector_first", "fts_first", "rrf"]
StrategyFn = Callable[..., RecallResult]


@dataclass(frozen=True, slots=True)
class IntentProfile:
    """§11.2 row: which strategy an intent wants, with weights + budget."""

    strategy: Strategy
    graph_w: float
    vector_w: float
    fts_w: float
    budget: int


# §11.2 intent → strategy table. Reconfigurable per instance (the ranker door).
INTENT_PROFILES: dict[str, IntentProfile] = {
    "fact_lookup": IntentProfile("graph_first", 0.7, 0.2, 0.1, 200),
    "episodic": IntentProfile("fts_first", 0.1, 0.3, 0.6, 600),
    "timeline": IntentProfile("rrf", 0.3, 0.5, 0.2, 1200),
    "explore": IntentProfile("vector_first", 0.2, 0.7, 0.1, 1000),
    "skill_context": IntentProfile("graph_first", 0.5, 0.4, 0.1, 1500),
    "lesson_lookup": IntentProfile("graph_first", 0.6, 0.3, 0.1, 800),
    "concept_lookup": IntentProfile("rrf", 0.4, 0.4, 0.2, 600),
}
_DEFAULT_PROFILE = IntentProfile("rrf", 0.2, 0.3, 0.5, 800)


@dataclass(slots=True)
class OrchestratorSettings:
    include_related: bool = True
    related_cap: int = 5
    trace_cap: int = 256
    remember_trust: str = "inferred"
    # K30 promote_mode=inbox_only: the LLM's policy="direct"/supersedes requests
    # default to staging (a clamp against persistent-memory poisoning via
    # prompt-injection). The user/server may deliberately enable it.
    allow_direct: bool = False
    rate_limits: dict[str, int] = field(
        default_factory=lambda: {
            "memory.search": 100,  # the §11 O10 example
            "memory.remember": 60,
            "memory.forget": 30,
        }
    )


class MemoryOrchestrator:
    """Tool-call router + fusion shell over a :class:`~akana.memory.Memory`."""

    def __init__(
        self,
        memory: Memory,
        *,
        settings: OrchestratorSettings | None = None,
        intent_profiles: dict[str, IntentProfile] | None = None,
    ) -> None:
        self._memory = memory
        self._settings = settings or OrchestratorSettings()
        self._profiles = dict(INTENT_PROFILES if intent_profiles is None else intent_profiles)
        self._traces: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._calls: dict[str, deque[float]] = {}
        # audit C8: one MemoryOrchestrator on app.state is dispatched via asyncio.to_thread
        # for every tool call, so concurrent workers mutate _traces (OrderedDict) and _calls
        # (deque). Guard both containers — an unlocked popleft/popitem can IndexError-crash a
        # worker or miscount the rate window.
        self._state_lock = threading.Lock()
        # Strategy registry (O1/O5 door). Only the real one registers today;
        # unknown strategies fall back to it with a trace warning.
        self._strategies: dict[str, StrategyFn] = {"fts_first": self._fts_recall}

    # -- public surface --------------------------------------------------------

    def register_strategy(self, name: str, fn: StrategyFn) -> None:
        """Plug a retrieval strategy in (vector/graph arrive this way)."""
        self._strategies[name] = fn

    def _live_settings(self) -> OrchestratorSettings:
        """Refresh ``allow_direct`` (the UI's 'remember without approval' toggle) from
        the yaml at WRITE time.

        The MCP child is a long-lived subprocess; since ``OrchestratorSettings`` is
        built once at startup, a toggle change had no effect until a restart (the bug
        the user described as 'inbox on/off is broken'). Because writes are infrequent,
        a single yaml read on each ``memory.remember`` is cheap. If the read fails the
        current setting is preserved — the write path never breaks.
        """
        data_dir = getattr(self._memory, "_data_dir", None)
        if data_dir is None:
            return self._settings
        try:
            from dataclasses import replace

            from akana.memory.settings import _settings_path, load_memory_settings

            if not _settings_path(data_dir).is_file():
                # NO persistent preference (yaml) → the explicitly supplied
                # OrchestratorSettings is authoritative (programmatic use / tests) —
                # don't override it with the default. Saving the UI toggle creates the
                # yaml; live refresh applies from that point on.
                return self._settings
            fresh = load_memory_settings(data_dir).allow_direct
        except Exception:
            log.debug("allow_direct refresh failed; keeping current setting", exc_info=True)
            return self._settings
        if fresh == self._settings.allow_direct:
            return self._settings
        return replace(self._settings, allow_direct=fresh)

    def handle_tool_call(
        self,
        name: str,
        args: dict[str, Any] | None,
        *,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Dispatch one tool call; always returns a dict, never raises."""
        if name not in MEMORY_TOOLS:
            return error_envelope(
                name, "unknown_tool", f"unknown tool {name!r}; available: {', '.join(MEMORY_TOOLS)}"
            )
        if not self._allow(name):
            return error_envelope(
                name, "rate_limited", f"{name} rate limit exceeded; retry in a minute"
            )
        try:
            req = parse_tool_request(name, args)
        except ToolValidationError as e:
            return error_envelope(name, "invalid_request", str(e))
        try:
            if isinstance(req, SearchRequest):
                return self._search(req, conversation_id)
            if isinstance(req, RememberRequest):
                return run_remember(self._memory, self._live_settings(), req, conversation_id)
            # audit C9: memory.forget deliberately is NOT gated by allow_direct. A durable
            # forget/supersede works even in K30 inbox_only mode — this is intended (and
            # tested: test_forget_retract_and_audit / test_forget_supersede run with the
            # default allow_direct=False). forget is soft/reversible via the ledger, so the
            # prompt-injection surface is low-impact; gating it would break a real feature.
            # The proper "forget-request approval inbox" would be a separate feature.
            return run_forget(self._memory, req)
        except Exception as e:  # the tool boundary must never crash the host
            log.exception("memory tool %s failed", name)
            return error_envelope(name, "internal_error", f"{type(e).__name__}: {e}")

    # -- memory.search ---------------------------------------------------------

    def _fts_recall(self, **kw: Any) -> RecallResult:
        return self._memory.recall(kw.pop("query"), **kw)

    def _resolve_strategy(self, requested: str) -> tuple[str, StrategyFn, bool]:
        fn = self._strategies.get(requested)
        if fn is not None:
            return requested, fn, False
        # The requested strategy is not registered (e.g. graph_first is not implemented yet).
        # When vector is ON, rrf is registered → fall back to rrf instead of plain
        # fts_first, so that graph-wanting intents (fact_lookup/lesson_lookup) also get
        # semantic recall. When vector is OFF, there is no rrf either → fts_first (old behaviour).
        rrf = self._strategies.get("rrf")
        if rrf is not None:
            return "rrf", rrf, True
        return "fts_first", self._strategies["fts_first"], True

    def _search(self, req: SearchRequest, conversation_id: str | None) -> dict[str, Any]:
        t0 = time.perf_counter()
        warnings: list[str] = []
        # 2026-06-14 (user decision): search runs over ALL of memory by default.
        # scope narrows only when EXPLICITLY given in the call; there is no automatic
        # narrowing to the current conversation (silent narrowing was filtering out the right result).
        conv = req.scope.conversation_id
        profile = self._profiles.get(req.intent or "", _DEFAULT_PROFILE)
        budget = req.budget_tokens or profile.budget
        # topic is length-capped at the boundary (ToolScope); clip again here so the
        # combined query respects SearchRequest.query's 2000-char ceiling even if the
        # model packs both to their limits.
        query = f"{req.query} {req.scope.topic}"[:2000] if req.scope.topic else req.query

        # Time bounds are resolved in one place (parse_time_bound: ISO / relative /
        # Turkish natural-language expression). as_of takes the "end" edge — "dün" = the
        # memory state as of the end of yesterday; observed_* are the two ends of the observation window.
        _time_hint = "use ISO-8601, 'relative:<n><h|d|w>' or Turkish ('dün', 'geçen hafta', 'mart ayında')"
        as_of: str | None = None
        if req.as_of:
            as_of = parse_time_bound(req.as_of, edge="end")
            if as_of is None:
                return error_envelope(
                    "memory.search",
                    "invalid_request",
                    f"as_of: cannot parse {req.as_of!r}; {_time_hint}",
                )
        observed_from: str | None = None
        observed_to: str | None = None
        if req.observed_from:
            observed_from = parse_time_bound(req.observed_from, edge="start")
            if observed_from is None:
                return error_envelope(
                    "memory.search",
                    "invalid_request",
                    f"observed_from: cannot parse {req.observed_from!r}; {_time_hint}",
                )
        if req.observed_to:
            observed_to = parse_time_bound(req.observed_to, edge="end")
            if observed_to is None:
                return error_envelope(
                    "memory.search",
                    "invalid_request",
                    f"observed_to: cannot parse {req.observed_to!r}; {_time_hint}",
                )
        # time_range is subject to the same contract as as_of/observed_*: a malformed
        # bound is not silently swallowed (previously the filter no-op'd and the model trusted it wrongly).
        time_from: str | None = None
        time_to: str | None = None
        if req.time_range is not None:
            if req.time_range.from_:
                time_from = parse_time_bound(req.time_range.from_, edge="start")
                if time_from is None:
                    return error_envelope(
                        "memory.search",
                        "invalid_request",
                        f"time_range.from: cannot parse {req.time_range.from_!r}; {_time_hint}",
                    )
            if req.time_range.to:
                time_to = parse_time_bound(req.time_range.to, edge="end")
                if time_to is None:
                    return error_envelope(
                        "memory.search",
                        "invalid_request",
                        f"time_range.to: cannot parse {req.time_range.to!r}; {_time_hint}",
                    )
        # Inverted window (from > to): not silently swallowed, like a malformed format.
        # The bounds parsed but logically produce an empty intersection — a warning is
        # left so the model can tell "memory is empty" apart from "my window is nonsense"
        # (consistent with the code's own "a malformed bound is not silently swallowed" principle).
        if observed_from and observed_to and observed_from > observed_to:
            warnings.append(
                f"observed window inverted (from {observed_from} > to {observed_to}); "
                "result will be empty"
            )
        if time_from and time_to and time_from > time_to:
            warnings.append(
                f"time_range inverted (from {time_from} > to {time_to}); "
                "result will be empty"
            )
        if req.rerank != "off":
            warnings.append("rerank=cross_encoder unavailable until the vector milestone")

        q0 = time.perf_counter()
        if as_of is not None:
            # Time-travel (D): the keyword strategy is skipped — the semantic
            # side comes from the validity-window query, the episodic side is
            # post-filtered to ts <= as_of. Same fusion/filter line afterwards.
            executed = "as_of"
            result = self._as_of_recall(
                query=query,
                as_of=as_of,
                conversation_id=conv,
                min_trust=req.min_trust,
                limit=req.k,
                budget_tokens=budget,
            )
        else:
            executed, strategy_fn, fell_back = self._resolve_strategy(profile.strategy)
            if fell_back:
                warnings.append(
                    f"strategy {profile.strategy!r} not registered yet; fell back to {executed!r}"
                )
            result = strategy_fn(
                query=query,
                conversation_id=conv,
                min_trust=req.min_trust,
                limit=req.k,
                budget_tokens=budget,
            )
        fts_ms = int((time.perf_counter() - q0) * 1000)
        rt = result.trace
        # costs/stage: attribute the measured strategy time to the EXECUTED strategy.
        # Previously it was written to fts_ms for EVERY strategy with vector_ms=0 fixed →
        # even when vector actually ran, the trace showed "fts_query / vector_ms:0" and
        # created a 'vector off' illusion (user report). Degrade is now logged in vector_recall.
        _costs = {"graph_ms": 0, "vector_ms": 0, "fts_ms": 0, "rerank_ms": 0}
        if executed == "vector_first":
            _costs["vector_ms"] = fts_ms
            _query_stage = "vector_query"
        elif executed == "graph_first":
            _costs["graph_ms"] = fts_ms
            _query_stage = "graph_query"
        elif executed == "rrf":
            _costs["vector_ms"] = _costs["fts_ms"] = fts_ms  # fusion: vector + fts
            _query_stage = "rrf_query"
        else:  # fts_first / as_of
            _costs["fts_ms"] = fts_ms
            _query_stage = "fts_query"

        pairs = blocks_to_items(
            self._memory,
            result,
            include_related=self._settings.include_related,
            related_cap=self._settings.related_cap,
        )
        kept, type_dropped = filter_types(pairs, req.types)
        kept, time_dropped = filter_time(kept, time_from, time_to)
        # Bi-temporal observation filter (one place): the same post-filter on both the
        # normal and the as_of path — observed_at on facts, ts on turns.
        kept, observed_dropped = filter_observed(kept, observed_from, observed_to)
        items = [item for item, _fact in kept]

        # B (user decision: everything goes through the inbox, no auto-promote): in
        # addition to the approved results, also return PENDING (inbox) info matching the
        # query, tagged 'awaiting approval' → instead of saying 'I don't know', the
        # assistant can say 'awaiting approval in your inbox: …' (the 'I don't know but
        # it's in memory' fix).
        pending = self._pending_matches(
            query, conversation_id=conv, min_trust=req.min_trust, limit=req.k
        )
        if pending:
            warnings.append(
                f"{len(pending)} related item(s) are AWAITING APPROVAL in the inbox "
                "(not yet durable) — see the 'pending' field; remind the user to "
                "approve them if needed"
            )

        explain_id = str(ulid.new())
        total_ms = int((time.perf_counter() - t0) * 1000)
        stages: list[dict[str, Any]] = [
            {"stage": "scope_filter", "conversation_id": conv, "note": "store-level"},
            {
                "stage": _query_stage,
                "semantic_candidates": rt.semantic_candidates,
                "episodic_candidates": rt.episodic_candidates,
                "ms": fts_ms,
            },
            {"stage": "fusion", "method": "score_sort", "merged": rt.merged},
            {"stage": "trust_filter", "floor": req.min_trust, "note": "store-level"},
            {"stage": "type_filter", "dropped": type_dropped},
            {"stage": "time_filter", "dropped": time_dropped},
            {
                "stage": "observed_filter",
                "from": observed_from,
                "to": observed_to,
                "dropped": observed_dropped,
            },
            {"stage": "budget_trim", "kept": rt.returned, "dropped": rt.dropped_for_budget},
        ]
        trace_out: dict[str, Any] = {
            "strategy": executed,
            "requested_strategy": profile.strategy,
            "weights": {"graph": profile.graph_w, "vector": profile.vector_w, "fts": profile.fts_w},
            "costs_ms": _costs,
            "budget_tokens": budget,
            "candidates_examined": rt.semantic_candidates + rt.episodic_candidates,
            "filtered_by_trust": 0,  # store-level gate; the counter arrives with the vector milestone
            "filtered_by_scope": 0,
            "stages": stages,
        }
        self._store_trace(
            {
                "explain_id": explain_id,
                "tool": "memory.search",
                "request": req.model_dump(exclude_none=True, by_alias=True),
                **trace_out,
                "result_ids": [item["id"] for item in items],
                "warnings": warnings,
                "used_ids": [],
                "total_ms": total_ms,
            }
        )
        return {
            "items": items,
            "pending": pending,
            "explain_id": explain_id,
            "trace": trace_out,
            "warnings": warnings,
        }

    def _pending_matches(
        self,
        query: str,
        *,
        conversation_id: str | None = None,
        min_trust: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """PENDING (inbox) facts matching the query — tagged 'onay_bekliyor'.

        User decision: everything goes through the inbox (no auto-promote). If recall
        only sees APPROVED facts, then for info the user stated but hasn't approved yet,
        the assistant says 'I don't know' (the 'I don't know but it's in memory'
        complaint). Surfacing and tagging these → the assistant can say 'awaiting
        approval in your inbox: …'.

        Scope-leak fix: pending rows are subject to the SAME scope/trust gate as approved
        recall. If ``conversation_id`` is given EXPLICITLY (no silent narrowing —
        consistent with _search's scope principle), only that conversation's (or
        conversation-independent) candidates are returned; otherwise an unapproved
        candidate specific to conversation A would show up in a search in conversation B.
        The ``min_trust`` floor is applied the same way (trust_rank) as on the approved side.
        """
        from akana.memory.semantic import trust_rank
        from akana.memory.terms import fold_text, recall_search_terms

        terms = [fold_text(t) for t in (recall_search_terms(query) or [query]) if t]
        if not terms:
            return []
        floor = trust_rank(min_trust) if min_trust else None
        out: list[dict[str, Any]] = []
        for s in self._memory.staging.list_pending(limit=200):
            # Scope gate: if a scope is given, filter out a candidate bound to another
            # conversation (conversation-independent candidates — conversation_id is None — appear in every scope).
            if (
                conversation_id is not None
                and s.conversation_id is not None
                and s.conversation_id != conversation_id
            ):
                continue
            # Trust floor: the same threshold as approved recall.
            if floor is not None and trust_rank(s.trust) < floor:
                continue
            if any(t in fold_text(f"{s.key} {s.value}") for t in terms):
                out.append(
                    {"id": s.id, "key": s.key, "value": s.value, "status": "onay_bekliyor"}
                )
                if len(out) >= limit:
                    break
        return out

    def _as_of_recall(
        self,
        *,
        query: str,
        as_of: str,
        conversation_id: str | None,
        min_trust: str | None,
        limit: int,
        budget_tokens: int,
    ) -> RecallResult:
        """Time-travel retrieval: blocks for the memory state *at* ``as_of``.

        Facts come from :meth:`SemanticStore.facts_as_of` (validity window in
        SQL, invalidated rows included when they were valid back then); turns
        are the normal keyword search post-filtered to ``ts <= as_of``. Blocks
        reuse recall's scoring and its fuse/budget helpers so downstream fusion
        behaves exactly like a live search.
        """
        blocks: list[RecallBlock] = []
        facts = self._memory.semantic.facts_as_of(
            query, as_of, min_trust=min_trust, limit=limit
        )
        for fact in facts:
            blocks.append(
                RecallBlock(
                    kind="semantic",
                    text=f"{fact.key}: {fact.value}",
                    ts=fact.ts_last,
                    score=0.7 + min(0.25, fact.importance * 0.25),  # recall's formula
                    trust=fact.trust,
                    source_ids=(fact.id,),
                )
            )
        turns = [
            t
            for t in self._memory.search_turns(
                query, conversation_id=conversation_id, limit=limit
            )
            if t.ts <= as_of and episodic_turn_eligible(t.role)
        ]
        # _ROLE_LABELS is language-keyed ({"en": {...}, "tr": {...}}); like the
        # live path (_collect_episodic) resolve the inner dict first, else every
        # lookup misses and turns render with the raw role ('[user]' not '[You]').
        labels = _ROLE_LABELS.get(_DEFAULT_LANGUAGE, _ROLE_LABELS[_DEFAULT_LANGUAGE])
        for turn in turns:
            role = labels.get(turn.role, turn.role)
            blocks.append(
                RecallBlock(
                    kind="episodic",
                    text=f"[{role}] {turn.text[:600]}",
                    ts=turn.ts,
                    conversation_id=turn.conversation_id,
                    score=_EPISODIC_SCORE,
                    source_ids=(turn.id,),
                )
            )
        unique = Recall._fuse(blocks)  # noqa: SLF001 - shared on purpose (same semantics)
        considered = unique[: limit * 2]
        out, total_tokens = Recall._apply_budget(considered, budget_tokens)  # noqa: SLF001
        trace = RecallTrace(
            query=query,
            terms=tuple(recall_search_terms(query) or [query]),
            min_trust=min_trust,
            conversation_id=conversation_id,
            semantic_candidates=len(facts),
            episodic_candidates=len(turns),
            merged=len(unique),
            returned=len(out),
            dropped_for_budget=max(0, len(considered) - len(out)),
            total_tokens=total_tokens,
            budget_tokens=budget_tokens,
        )
        return RecallResult(blocks=out, trace=trace)

    # -- plumbing ----------------------------------------------------------------

    def _allow(self, tool: str) -> bool:
        """O10: sliding-window rate limit per tool."""
        limit = self._settings.rate_limits.get(tool)
        if not limit or limit <= 0:
            return True
        now = time.monotonic()
        with self._state_lock:
            window = self._calls.setdefault(tool, deque())
            while window and window[0] <= now - 60.0:
                window.popleft()
            if len(window) >= limit:
                return False
            window.append(now)
            return True

    def _store_trace(self, trace: dict[str, Any]) -> None:
        with self._state_lock:
            self._traces[trace["explain_id"]] = trace
            while len(self._traces) > max(1, self._settings.trace_cap):
                self._traces.popitem(last=False)
