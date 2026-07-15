"""Observability dashboard — GET ``/observability/summary`` (bearer-protected).

Akana already has a real observability BACKEND (``akana_server/observability/``:
in-process metrics registry, error taxonomy, failure capture, turn tracing) plus
a circuit-breaker registry (``akana_server/network/breaker.py``) and a JSONL audit
log (``akana_server/audit.py``) — but no single place that answers "how is the
server doing right now?" in one read. This route is a pure READ-SIDE aggregator:
it does not write anything, it only assembles four already-existing sources into
one JSON document for the Settings → Observability panel:

* ``metrics``  — the in-process counters/timers (``observability.registry.snapshot()``,
  the SAME data already exposed at ``/system/metrics`` — reused, not duplicated).
* ``usage``    — provider-usage totals aggregated from PERSISTED conversation turns
  (``ConversationService``/episodic store). See ``_usage_summary`` for the important
  caveat: turn-level ``usage`` is stored as ``{prompt, completion, cost_usd?}`` with
  **no provider field** (``src/akana/memory/episodic.py`` — ``EpisodicTurn.usage``),
  so per-provider token breakdown is NOT available from persisted history; this
  endpoint aggregates totals across all providers and says so explicitly
  (``provider_attribution: false`` + a human-readable ``note``).
* ``health``   — circuit breaker states (``network.guard.global_registry().snapshot()``,
  the SAME data already exposed at ``/network/status``) plus the currently active LLM
  provider name, resolved via the PUBLIC ``llm_settings.resolve_provider`` helper (the
  private ``llm_dispatch._active_provider`` is intentionally NOT imported here).
* ``audit``    — the last N audit events, reusing ``audit.read_tail`` (the SAME
  function backing ``/system/audit/tail`` — no re-parsing of the JSONL file here).

Bounded scan: conversation history can grow without bound, and this is a dashboard
read (called on a timer by the frontend), not an analytics warehouse query — see the
``_MAX_*`` constants below for the exact caps. The response always reports how much
was actually scanned so the numbers are legible as "recent window", not "all time".

Cost discipline: the frontend polls this every ~10s while the panel is open. Two
things keep that poll from stalling the event loop: (1) the handler is a plain
``def`` so FastAPI runs the whole (blocking, SQLite-touching) aggregation in its
threadpool instead of on the event loop, and (2) a short-TTL in-process cache
(``_SUMMARY_CACHE_TTL_S``) coalesces repeated polls / multiple tabs so back-to-back
requests return the last payload without rescanning. Tier-1 (turn count / window) is
also genuinely meta-only now — it reads ``ConversationMeta`` rows directly (one query)
instead of going through ``ConversationService.list_conversations`` (which wraps every
row via ``_newest_turn`` → a fresh SQLite connection PER conversation, an N+1 scan).

Registration: this router is wired into ``akana_server/api/app.py``
(``app.include_router(observability_routes.router, prefix="/api/v1")``), so the panel
is served at ``/api/v1/observability/summary``.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query

from akana_server.api.deps import require_akana_bearer
from akana_server.api.services import AppServices, get_services
from akana_server.audit import read_tail as audit_read_tail
from akana_server.conversation_service import ConversationService
from akana_server.llm_settings import load_llm_settings, resolve_provider
from akana_server.network.guard import global_registry
from akana_server.observability import registry
from akana_server.orchestrator.base import coerce_cost_usd, coerce_token_count

log = logging.getLogger(__name__)

router = APIRouter(tags=["observability"])

# -- scan bounds (dashboard read, not a warehouse query) -----------------------

#: Conversations considered for the "in window" turn count. Genuinely meta-only:
#: ``_list_conversation_meta`` reads ``ConversationMeta`` rows in a SINGLE query
#: (``message_count`` + ``updated_at`` are on the row already — no per-conversation
#: turn read). Mirrors ``ConversationStore.list``'s own 200 ceiling.
_MAX_CONVERSATIONS_SCANNED = 200

#: Of the in-window conversations, how many are actually opened to read per-turn
#: ``usage`` (token/cost) — this is the expensive part (one SQLite connection +
#: query per conversation), so it is capped tighter than the meta-only scan above.
_MAX_CONVERSATIONS_FOR_USAGE = 50

#: Per conversation, how many of its newest turns are inspected for ``usage``.
#: Assistant turns carry usage; a long-running conversation's oldest turns are
#: not worth paying for on every 10s panel refresh.
_MAX_MESSAGES_PER_CONVERSATION = 50

#: Default lookback window for the usage aggregation (days). Overridable via the
#: ``usage_days`` query parameter (see ``get_observability_summary``).
_DEFAULT_USAGE_WINDOW_DAYS = 7

#: Default number of audit events returned (mirrors ``/system/audit/tail``'s own
#: default; overridable via ``audit_limit``, same ``Query`` bounds style).
_DEFAULT_AUDIT_LIMIT = 50

#: In-process cache TTL for the assembled summary payload (seconds). Kept just
#: under the frontend's ~10s poll so back-to-back polls (or several open tabs)
#: return the last payload instead of re-running the SQLite scan every time.
_SUMMARY_CACHE_TTL_S = 8.0

#: {(data_dir, usage_days, audit_limit): (expiry_monotonic, payload)}. Keyed on the
#: data_dir so a single-user server reuses one slot in production while hermetic
#: tests (each a unique ``tmp_path``) never see each other's cached payload.
_summary_cache: dict[tuple[str, int, int], tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def _reset_summary_cache() -> None:
    """Drop the in-process summary cache (test hook + explicit safety valve)."""
    with _cache_lock:
        _summary_cache.clear()


def _parse_iso(ts: str | None) -> datetime | None:
    """Best-effort ISO-8601 parse → an AWARE (UTC) datetime; ``None`` on anything odd.

    Turn/conversation timestamps are our own ``iso_now()`` format (``...Z`` → aware),
    but this is a read-only diagnostics endpoint that must tolerate a legacy/hand-edited
    stamp. A tz-LESS stamp parses to a NAIVE datetime; comparing that against the aware
    ``cutoff`` (``ts >= cutoff``) raises ``TypeError`` — which, uncaught, 500s the
    endpoint forever (violating its "never 500 on a bad timestamp" contract). So a
    naive result is normalized to UTC (the project's canonical timezone — see
    ``akana_server/timeutil.py``: ``iso_now()`` emits UTC), and ``TypeError`` is caught
    alongside ``ValueError``. The caller treats ``None`` as "can't tell, include it".
    """
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _list_conversation_meta(conv_svc: ConversationService, *, limit: int) -> list[Any]:
    """Meta-only conversation rows — SINGLE query, no per-conversation turn read.

    ``ConversationService.list_conversations`` wraps every row via ``_wrap`` →
    ``_newest_turn`` → a fresh SQLite connection PER conversation (an N+1 scan that
    froze the event loop for ~0.5 s on 200 rows). Tier-1 of this dashboard only needs
    ``message_count`` + ``updated_at``, both already carried on the ``ConversationMeta``
    the store returns directly — so read the meta store once and skip the per-row turn
    query entirely. The store's own SQL already excludes deleted/archived rows, matching
    ``list_conversations``'s default view.
    """
    return conv_svc._meta_store.list(limit=limit, include_archived=False)


def _usage_summary(conv_svc: ConversationService, *, window_days: int) -> dict[str, Any]:
    """Provider-usage totals aggregated from persisted conversations (bounded scan).

    Two-tier bound: (1) up to ``_MAX_CONVERSATIONS_SCANNED`` recently-updated
    conversations are read at the META level (genuinely cheap — one query, no message
    rows) to compute ``turns_total`` for the requested day window; (2) of THOSE, only
    the newest ``_MAX_CONVERSATIONS_FOR_USAGE`` are opened to read actual turn ``usage``
    for token/cost totals (each conversation is capped to its newest
    ``_MAX_MESSAGES_PER_CONVERSATION`` turns). This keeps the endpoint fast even
    with thousands of historical conversations, at the cost of the token/cost
    totals being a "recent window" figure rather than an all-time figure — which
    is the right tradeoff for a live dashboard tile, not a billing report.
    """
    cutoff = datetime.now(UTC) - timedelta(days=max(1, window_days))
    # Meta-only fetch: ordered newest-updated-first (ConversationStore.list). We use
    # ``updated_at`` (bumped on every turn write) as the activity stamp — the meta-only
    # equivalent of the old ``last_message_at`` derivation, without its per-row query.
    conversations = _list_conversation_meta(conv_svc, limit=_MAX_CONVERSATIONS_SCANNED)
    in_window = []
    for meta in conversations:
        ts = _parse_iso(meta.updated_at)
        if ts is None or ts >= cutoff:
            in_window.append(meta)
    turns_total = sum(max(0, m.message_count) for m in in_window)

    prompt_total = 0
    completion_total = 0
    cost_total = 0.0
    conversations_scanned = 0
    # Per-provider breakdown, keyed by the ``provider`` stamp llm_dispatch now writes
    # onto each turn's usage (``_done_tokens_block``). Turns predating the stamp have
    # no provider → they fall into the "unknown" bucket; ``provider_attribution`` is
    # True as soon as ANY turn in the window carries the stamp.
    per_provider: dict[str, dict[str, Any]] = {}
    attributed = False

    def _bucket(name: str) -> dict[str, Any]:
        return per_provider.setdefault(
            name, {"prompt": 0, "completion": 0, "cost_usd": 0.0, "turns": 0}
        )

    for meta in in_window[:_MAX_CONVERSATIONS_FOR_USAGE]:
        conversations_scanned += 1
        try:
            messages = conv_svc.list_messages(
                meta.id, limit=_MAX_MESSAGES_PER_CONVERSATION
            )
        except Exception:  # a single corrupt conversation must not break the summary
            log.debug(
                "observability: usage scan skipped conversation=%s", meta.id, exc_info=True
            )
            continue
        for m in messages:
            if m.role != "assistant" or not m.usage:
                continue
            p = coerce_token_count(m.usage.get("prompt"))
            c = coerce_token_count(m.usage.get("completion"))
            cost = coerce_cost_usd(m.usage.get("cost_usd"))
            prompt_total += p
            completion_total += c
            cost_total += cost
            prov = str(m.usage.get("provider") or "").strip().lower()
            if prov:
                attributed = True
            b = _bucket(prov or "unknown")
            b["prompt"] += p
            b["completion"] += c
            b["cost_usd"] = round(b["cost_usd"] + cost, 6)
            b["turns"] += 1

    return {
        "window_days": window_days,
        "conversations_in_window": len(in_window),
        "conversations_scanned_for_tokens": conversations_scanned,
        "turns_total": turns_total,
        "tokens": {
            "prompt": prompt_total,
            "completion": completion_total,
            "total": prompt_total + completion_total,
        },
        "cost_usd": round(cost_total, 6),
        # Per-provider breakdown from the turn ``provider`` stamp. ``None`` when NO
        # turn in the window carried the stamp (all legacy) — an "unknown"-only bucket
        # is not worth showing, so the frontend falls back to the aggregate tiles. When
        # attribution IS present, the "unknown" bucket (if any) surfaces the residual
        # legacy tokens alongside the real providers.
        "per_provider": per_provider if attributed else None,
        "provider_attribution": attributed,
        "note": (
            ""
            if attributed
            else "Older turns predate per-provider attribution; totals are aggregated "
            "across all providers. New turns are stamped and will break out per provider."
        ),
    }


def _health_summary(services: AppServices) -> dict[str, Any]:
    """Circuit-breaker snapshot + the active LLM provider (public resolver only)."""
    settings = services.settings
    llm = load_llm_settings(settings.data_dir, settings)
    return {
        # Public equivalent of the private llm_dispatch._active_provider — same
        # resolution order (persisted setting wins, else env, else "unconfigured").
        "active_provider": resolve_provider(settings, llm),
        "breakers": global_registry().snapshot(),
    }


def _build_summary(
    services: AppServices, *, usage_days: int, audit_limit: int
) -> dict[str, Any]:
    """Assemble the four-section summary payload (the blocking work — runs off-loop).

    ``ConversationService`` is built fresh from ``services.settings.data_dir`` rather
    than pulled from ``app.state`` — the panel must work even when the caller only
    wired ``app.state.settings`` (e.g. this route's own hermetic tests).
    """
    settings = services.settings
    conv_svc = ConversationService.for_data_dir(settings.data_dir)
    audit_events = audit_read_tail(settings.data_dir, limit=audit_limit)
    return {
        "metrics": registry.snapshot(),
        "usage": _usage_summary(conv_svc, window_days=usage_days),
        "health": _health_summary(services),
        "audit": {
            "count": len(audit_events),
            # Chronological (oldest→newest), same order as /system/audit/tail —
            # the frontend reverses for "newest first" display (a display concern,
            # not a data concern; keeps this endpoint's ordering contract identical
            # to the route it reuses read_tail from).
            "events": audit_events,
        },
    }


@router.get("/observability/summary", dependencies=[Depends(require_akana_bearer)])
def get_observability_summary(
    usage_days: int = Query(default=_DEFAULT_USAGE_WINDOW_DAYS, ge=1, le=90),
    audit_limit: int = Query(default=_DEFAULT_AUDIT_LIMIT, ge=1, le=500),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    """One-shot aggregation for the Settings → Observability panel.

    A PLAIN ``def`` on purpose: FastAPI runs a sync route in its threadpool, so the
    whole blocking aggregation (SQLite scans, audit-tail read) stays OFF the event
    loop — a 10s poll can never stall chat SSE / voice / WS the way the old
    ``async def`` body did. A short-TTL in-process cache (``_SUMMARY_CACHE_TTL_S``)
    coalesces repeated polls and multiple open tabs so the scan is not re-run on
    every request. Every sub-section still degrades independently (an empty/fresh
    ``data_dir`` returns zeros/empty lists, never a 500) — mirroring the sibling
    ``/network/status`` and ``/system/metrics`` routes.
    """
    settings = services.settings
    data_dir = str(getattr(settings, "data_dir", ""))
    cache_key = (data_dir, usage_days, audit_limit)
    now = time.monotonic()

    with _cache_lock:
        hit = _summary_cache.get(cache_key)
        if hit is not None and hit[0] > now:
            return hit[1]

    payload = _build_summary(services, usage_days=usage_days, audit_limit=audit_limit)

    with _cache_lock:
        # Opportunistically drop expired slots so a server whose panel is polled with
        # varying params can't grow the cache without bound (prod uses one key).
        expired = [k for k, (exp, _) in _summary_cache.items() if exp <= now]
        for k in expired:
            _summary_cache.pop(k, None)
        _summary_cache[cache_key] = (now + _SUMMARY_CACHE_TTL_S, payload)

    return payload


__all__ = ["router"]
