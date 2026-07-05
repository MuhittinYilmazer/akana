"""Write-path handlers — ``memory.remember`` / ``memory.forget`` (§8.2–8.3).

Split out of the orchestrator (like :mod:`~akana.memory.fusion`) so the
router stays a router. These functions
own the K30 clamp — no unapproved durable writes: when ``allow_direct`` is off,
``policy="direct"`` and ``supersedes`` degrade to the staging inbox — plus the
supersede path and the forget audit trail. Pure functions over the façade.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from akana.memory.staging import FactCandidate
from akana.memory.tools import (
    ForgetRequest,
    RememberRequest,
    derive_key,
    ensure_kind_prefix,
    error_envelope,
)

if TYPE_CHECKING:
    from akana.memory import Memory
    from akana.memory.orchestrator import OrchestratorSettings

__all__ = ["remember", "forget"]

log = logging.getLogger(__name__)


def remember(
    memory: Memory,
    settings: OrchestratorSettings,
    req: RememberRequest,
    conversation_id: str | None,
) -> dict[str, Any]:
    conv = req.scope.conversation_id or conversation_id
    key = ensure_kind_prefix(req.key, req.kind) if req.key else derive_key(req.content, req.kind)
    trust = settings.remember_trust

    # When allow_direct is on, the user's intent is "remember without approval":
    # if staging was not explicitly requested, promote the default to direct (skip the inbox).
    if settings.allow_direct and req.policy == "stage" and not req.supersedes:
        req = req.model_copy(update={"policy": "direct"})

    wants_direct = bool(req.supersedes) or req.policy == "direct"
    if wants_direct and not settings.allow_direct:
        # K30 clamp: no unapproved durable writes — the request falls into the inbox.
        requested = "supersede" if req.supersedes else "direct"
        return _stage(memory, req, key=key, trust=trust, conv=conv, requested=requested)

    if req.supersedes:  # explicit replace beats the staging default
        result = memory.supersede_fact(
            req.supersedes,
            new_value=req.content,
            new_key=req.key and ensure_kind_prefix(req.key, req.kind),
            trust=trust,  # type: ignore[arg-type]
            source_turn_id=req.evidence.source_turn_id,
            quote=req.evidence.quote,
            extractor="memory.remember",
        )
        if result is None:
            return error_envelope(
                "memory.remember", "not_found", f"supersedes target {req.supersedes!r} not found or inactive"
            )
        old, new = result
        return {"status": "superseded", "old_id": old.id, "fact_id": new.id, "key": new.key}

    if req.policy == "direct":
        # Contradiction-aware direct write via the atomic primitive (audit C14). The old
        # find_contradictions → supersede → fall-through-to-remember_fact path left TWO
        # conflicting valid rows under one key when the supersede lost a race (the plain
        # remember_fact only dedups same-key SAME-value). assert_fact_direct invalidates
        # every same-key contradiction and upserts in ONE transaction, so a lost race can
        # no longer pile on a second valid row.
        closed, new = memory.assert_fact_direct(
            key=key,
            value=req.content,
            confidence=req.confidence if req.confidence is not None else 0.85,
            trust=trust,  # type: ignore[arg-type]
            source_turn_id=req.evidence.source_turn_id,
            quote=req.evidence.quote,
            extractor="memory.remember",
        )
        if closed:
            return {"status": "superseded", "old_id": closed[0].id, "fact_id": new.id, "key": new.key}
        return {"status": "stored", "fact_id": new.id, "key": new.key, "kind": req.kind}

    return _stage(memory, req, key=key, trust=trust, conv=conv, requested=None)


def _stage(
    memory: Memory,
    req: RememberRequest,
    *,
    key: str,
    trust: str,
    conv: str | None,
    requested: str | None,
) -> dict[str, Any]:
    """Stage a remember request; ``requested`` marks a K30-clamped direct/supersede."""
    reason = f"memory.remember kind={req.kind}"
    if requested:
        reason += f" requested={requested}"
        if req.supersedes:
            reason += f" supersedes={req.supersedes}"
    staged = memory.staging.stage(
        FactCandidate(
            key=key,
            value=req.content,
            reason=reason,
            trust=trust,
            source_turn_id=req.evidence.source_turn_id,
            quote=req.evidence.quote,
            extractor="memory.remember",
        ),
        conversation_id=conv,
    )
    out: dict[str, Any] = {
        "status": "staged",
        "staged_id": staged.id,
        "key": staged.key,
        "kind": req.kind,
        "note": "awaiting inbox approval (K30 promote_mode=inbox_only)",
    }
    if requested:
        out["requested_policy"] = requested
        out["note"] = "direct write disabled (K30 inbox_only) — dropped into approval queue"
    return out


def forget(memory: Memory, req: ForgetRequest) -> dict[str, Any]:
    # audit C9 (reviewed, intentionally NOT gated): a durable forget/supersede works even
    # in K30 inbox_only mode. This is intended and tested (test_forget_* run with the default
    # allow_direct=False); forget is soft/reversible via the ledger so the prompt-injection
    # surface is low-impact. A proper "forget-request approval inbox" is a separate feature.
    fact = memory.get_fact(req.target_id)
    if fact is None:
        staged = memory.staging.get(req.target_id)
        if staged is not None:
            if staged.status != "pending":
                return error_envelope(
                    "memory.forget", "not_actionable", f"staged candidate already {staged.status}"
                )
            memory.staging.mark_rejected(req.target_id)
            _audit(memory, req, outcome="rejected_staged")
            return {"status": "rejected_staged", "staged_id": req.target_id}
        return error_envelope("memory.forget", "not_found", f"no memory with id {req.target_id!r}")

    if req.mode == "supersede":
        result = memory.supersede_fact(req.target_id, new_value=req.new_value or "")
        if result is None:
            return error_envelope(
                "memory.forget", "not_actionable", "target already inactive; cannot supersede"
            )
        old, new = result
        _audit(memory, req, outcome="superseded", new_id=new.id)
        return {"status": "superseded", "old_id": old.id, "new_id": new.id}

    if not fact.is_valid:
        return {"status": "already_inactive", "fact_id": fact.id}
    memory.forget_fact(req.target_id, hard=False)
    _audit(memory, req, outcome=req.mode)
    out: dict[str, Any] = {"status": "forgotten", "mode": req.mode, "fact_id": req.target_id}
    if req.mode == "soft_delete":
        out["note"] = "grace-period hard delete deferred to F5; behaves like retract for now"
    if req.mode == "quarantine":
        out["note"] = "quarantine: excluded from recall, flagged in ledger"
    return out


def _audit(memory: Memory, req: ForgetRequest, *, outcome: str, new_id: str | None = None) -> None:
    try:
        memory.ledger.append(
            "memory.forget",
            data={
                "target_id": req.target_id,
                "mode": req.mode,
                "reason": req.reason,
                "outcome": outcome,
                **({"new_id": new_id} if new_id else {}),
            },
        )
    except Exception:  # the audit write must never break the main operation
        log.exception("forget audit append failed for %s", req.target_id)
