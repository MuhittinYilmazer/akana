"""Memory management API — thin HTTP surface over the clean ``src/akana`` core.

Staging inbox review, fact CRUD, orchestrated recall, owner settings and stats —
all on the new ``memory.db`` (``akana.memory.Memory`` façade), mounted under
``/api/v1/memory/*``. The app keeps a single lazy ``Memory`` instance on
``app.state`` (built on first request) so ``app.py`` never imports ``akana``.
"""

from __future__ import annotations

# `from akana.memory import …` (below, at module scope) resolves to src/akana via
# the SINGLE bootstrap in akana_server/__init__.py, which runs before this route
# module is imported. No per-module sys.path surgery here (the scattered
# "PERMANENT" preamble is gone; see _akana_src_bootstrap for the one mechanism).
import asyncio
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from akana.memory import (
    Memory,
    MemoryOrchestrator,
    SemanticFact,
    StagedFact,
    Trust,
    VectorStore,
)
from akana.memory.settings import (
    MemorySettings,
    load_memory_settings,
    save_memory_settings,
)
from akana.memory.tools import (
    MEMORY_TOOLS,
    RememberKind,
    derive_key,
    ensure_kind_prefix,
    kind_from_key,
)

from akana_server.api.deps import require_akana_bearer
from akana_server.api.errors import http_error
from akana_server.config import Settings

router = APIRouter(tags=["memory"])

_SETTINGS_FILE = "memory_settings.yaml"  # mirrors akana.memory.settings._FILE_NAME
_STAGING_STATUSES = frozenset({"pending", "promoted", "rejected", "all"})

# Guards the LAZY creation of app.state.memory_build_lock (the fallback path when the
# lifespan hasn't run, e.g. a test wiring app.state by hand). The lifespan creates the
# build lock eagerly, so this only fires when it didn't — but it makes even that path
# mint exactly ONE lock, closing the first-touch double-VectorIndexer race (#19).
_BUILD_LOCK_INIT = threading.Lock()

# Orchestrator error-code → HTTP status. Single source shared by call_memory_tool
# and recall (audit C29: recall previously hard-mapped every error to 400, so a
# rate_limited returned 400 instead of 429 and internal_error was masked as 400).
_TOOL_ERR_STATUS = {
    "unknown_tool": 404,
    "invalid_request": 400,
    "rate_limited": 429,
    "internal_error": 500,
}


# -- request bodies --------------------------------------------------------------


class FactCreateRequest(BaseModel):
    value: str = Field(..., min_length=1, max_length=8000)
    key: str | None = Field(default=None, max_length=256)
    kind: RememberKind = "fact"
    trust: Trust = "user_statement"
    # Provenance detail (free text: tool name / URL / conversation id). The origin
    # derives from the trust ladder; if detail is not provided it falls to the
    # extractor.
    source_detail: str | None = Field(default=None, max_length=512)


class FactPatchRequest(BaseModel):
    new_value: str = Field(..., min_length=1, max_length=8000)
    mode: Literal["supersede", "correct"] = "supersede"


class MemorySettingsPatch(BaseModel):
    allow_direct: bool | None = None
    auto_capture: bool | None = None
    session_summary: bool | None = None
    vector: Literal["auto", "on", "off"] | None = None
    embed_backend: Literal["local", "ollama", "off"] | None = None
    ollama_url: str | None = None
    embed_model: str | None = None


# -- lazy app.state singletons (app.py stays akana-free) -------------------------


def _data_dir(request: Request) -> Path:
    settings: Settings = request.app.state.settings
    return settings.data_dir


def _cached_memory_stack(
    request: Request,
) -> tuple[Memory, MemoryOrchestrator] | None:
    """The already-built stack if present, else ``None`` (no build)."""
    mem = getattr(request.app.state, "memory_core", None)
    orch = getattr(request.app.state, "memory_orchestrator", None)
    if isinstance(mem, Memory) and isinstance(orch, MemoryOrchestrator):
        return mem, orch
    return None


def _stack_build_lock(request: Request) -> threading.Lock:
    """One process-wide build lock on ``app.state`` (threading, not asyncio: the
    build runs both on the event loop's worker thread and on put_settings' rebuild
    thread, so an asyncio.Lock would not serialize them).

    The lifespan creates this lock eagerly, so the common path is a plain read. The
    lazy fallback below (for contexts where the lifespan didn't run) is serialised by
    ``_BUILD_LOCK_INIT`` and re-checks under it, so two concurrent first-touch requests
    can never each mint a DIFFERENT lock and both run the build → a second, never-
    detached VectorIndexer subscribed to the same Memory bus (#19 subscriber leak).
    """
    # threading.Lock is a factory, not a type — so no isinstance() guard; None-check
    # is enough (app.state only ever holds a Lock here).
    lock = getattr(request.app.state, "memory_build_lock", None)
    if lock is None:
        with _BUILD_LOCK_INIT:
            lock = getattr(request.app.state, "memory_build_lock", None)
            if lock is None:
                lock = threading.Lock()
                request.app.state.memory_build_lock = lock
    return lock


def _build_memory_stack_unlocked(
    request: Request,
) -> tuple[Memory, MemoryOrchestrator]:
    """The actual build — caller MUST already hold _stack_build_lock."""
    cached = _cached_memory_stack(request)
    if cached is not None:
        return cached
    from akana.memory.mcp import build_orchestrator

    from akana_server.memory_core import get_memory_core

    # A0: a SINGLE in-process Memory — the SAME instance as the turn_writer/persist
    # path. (Otherwise two separate Memory objects over the same memory.db: separate
    # ledger/indexer/subscriber/cache → while consolidation writes, the route could
    # see stale data.)
    data_dir = _data_dir(request)
    mem = get_memory_core(data_dir)
    _, orch, indexer = build_orchestrator(data_dir, memory=mem)
    request.app.state.memory_core = mem
    request.app.state.memory_orchestrator = orch
    request.app.state.memory_indexer = indexer
    return mem, orch


def _build_memory_stack_locked(
    request: Request,
) -> tuple[Memory, MemoryOrchestrator]:
    """Build (or return) the stack under the build lock — safe to call from a
    worker thread. The lock is what stops a first-touch request racing the
    put_settings rebuild into subscribing a SECOND VectorIndexer that is then
    never detached (#19 subscriber leak).
    """
    with _stack_build_lock(request):
        return _build_memory_stack_unlocked(request)


async def ensure_memory_stack(
    request: Request,
) -> tuple[Memory, MemoryOrchestrator]:
    """Cached stack, or build it OFF the event loop.

    The first memory-touching request (vector enabled, model not yet on disk, or an
    embed-backend change forcing a full re-embed) runs a ~220MB model download + a
    whole-corpus embed inside build_orchestrator → reindex. Doing that inline blocks
    the loop and freezes EVERY endpoint (chat/voice/SSE), so the build is pushed to a
    worker thread — same off-loop discipline the write paths already follow.
    """
    cached = _cached_memory_stack(request)
    if cached is not None:
        return cached
    return await asyncio.to_thread(_build_memory_stack_locked, request)


async def get_memory(request: Request) -> Memory:
    """The shared ``Memory`` façade — built on first use, kept on ``app.state``."""
    mem, _ = await ensure_memory_stack(request)
    return mem


def get_memory_settings(request: Request) -> MemorySettings:
    ms = getattr(request.app.state, "memory_settings", None)
    if not isinstance(ms, MemorySettings):
        ms = load_memory_settings(_data_dir(request))
        request.app.state.memory_settings = ms
    return ms


async def get_orchestrator(request: Request) -> MemoryOrchestrator:
    _, orch = await ensure_memory_stack(request)
    return orch


# -- serializers ------------------------------------------------------------------


def _staged_payload(s: StagedFact) -> dict[str, Any]:
    return {
        "id": s.id,
        "key": s.key,
        "value": s.value,
        "reason": s.reason,
        "trust": s.trust,
        "ts": s.ts,
        "conversation_id": s.conversation_id,
        "status": s.status,
        "promoted_fact_id": s.promoted_fact_id,
    }


def _fact_payload(f: SemanticFact) -> dict[str, Any]:
    return {
        "id": f.id,
        "key": f.key,
        "kind": kind_from_key(f.key),
        "value": f.value,
        "trust": f.trust,
        "confidence": f.confidence,
        "importance": f.importance,
        "anchored": f.anchored,
        "ts_first": f.ts_first,
        "ts_last": f.ts_last,
        "valid_from": f.valid_from,
        "invalidated_at": f.invalidated_at,
        "is_valid": f.is_valid,
        "source_turn_id": f.source_turn_id,
        "quote": f.quote,
        "extractor": f.extractor,
        # Salience (§13/M3.1): how many times recall returned it + when it was last used.
        "hit_count": f.hit_count,
        "last_hit_at": f.last_hit_at,
        # Provenance (citation-native): WHERE this record came from — UI badge+popover.
        "source": f.source,
    }


def _settings_payload(ms: MemorySettings, data_dir: Path) -> dict[str, Any]:
    return {
        **asdict(ms),
        "settings_path": str(Path(data_dir).expanduser() / _SETTINGS_FILE),
    }


# -- staging inbox -----------------------------------------------------------------


@router.get("/memory/staging", dependencies=[Depends(require_akana_bearer)])
async def list_staging(
    request: Request, status: str = "pending", limit: int = 50
) -> dict[str, Any]:
    if status not in _STAGING_STATUSES:
        raise http_error(
            422,
            "INVALID_STATUS",
            f"status must be one of {sorted(_STAGING_STATUSES)}, got {status!r}",
        )
    memory = await get_memory(request)
    staged = memory.staging.list_all(
        status=None if status == "all" else status,  # type: ignore[arg-type]
        limit=max(1, min(limit, 500)),
    )
    return {
        "items": [_staged_payload(s) for s in staged],
        "count": len(staged),
        "pending_count": memory.staging.count_pending(),
    }


@router.post(
    "/memory/staging/{staged_id}/approve",
    dependencies=[Depends(require_akana_bearer)],
)
async def approve_staged(request: Request, staged_id: str) -> dict[str, Any]:
    """Promote a pending candidate into durable memory (contradiction-aware)."""
    memory = await get_memory(request)
    staged = memory.staging.get(staged_id)
    if staged is None:
        raise http_error(404, "NOT_FOUND", f"no staged candidate {staged_id!r}")
    if staged.status != "pending":
        raise http_error(
            409, "NOT_ACTIONABLE", f"staged candidate already {staged.status}"
        )
    # Curator.promote: supersedes contradictions + mark_promoted + emits the
    # fact event (ledger/graph projector ride the façade's on_promote hook).
    # Off-loop: promote emits a fact event → the vector indexer embeds synchronously
    # (the first call lazy-downloads a ~220MB ONNX model). Running it on the event loop
    # would freeze the whole server, so push it to a worker thread (the memory store is
    # already accessed off-loop on the capture path).
    fact = await asyncio.to_thread(memory.make_curator().promote, staged_id)
    if fact is None:
        raise http_error(409, "NOT_ACTIONABLE", "candidate is no longer promotable")
    return {
        "status": "promoted",
        "staged_id": staged_id,
        "fact_id": fact.id,
        "key": fact.key,
    }


@router.post(
    "/memory/staging/{staged_id}/reject",
    dependencies=[Depends(require_akana_bearer)],
)
async def reject_staged(request: Request, staged_id: str) -> dict[str, Any]:
    memory = await get_memory(request)
    staged = memory.staging.get(staged_id)
    if staged is None:
        raise http_error(404, "NOT_FOUND", f"no staged candidate {staged_id!r}")
    if staged.status != "pending":
        raise http_error(
            409, "NOT_ACTIONABLE", f"staged candidate already {staged.status}"
        )
    if not memory.make_curator().reject(staged_id):
        raise http_error(409, "NOT_ACTIONABLE", "candidate is no longer rejectable")
    return {"status": "rejected", "staged_id": staged_id}


# -- facts ---------------------------------------------------------------------------


@router.get("/memory/facts", dependencies=[Depends(require_akana_bearer)])
async def list_facts(
    request: Request,
    q: str = "",
    limit: int = 50,
    offset: int = 0,
    include_invalidated: bool = False,
) -> dict[str, Any]:
    memory = await get_memory(request)
    lim = max(1, min(limit, 500))
    off = max(0, offset)
    query = q.strip()
    if query:
        # Search ranks by relevance; offset/total are not meaningful, so the
        # pager stays hidden and total mirrors the returned top-N slice.
        facts = memory.semantic.search(
            query, include_invalidated=include_invalidated, limit=lim
        )
        total = len(facts)
    else:
        facts = memory.semantic.list_facts(
            include_invalidated=include_invalidated, limit=lim, offset=off
        )
        total = memory.semantic.count_facts(include_invalidated=include_invalidated)
    return {
        "items": [_fact_payload(f) for f in facts],
        "count": len(facts),
        "total": total,
        "offset": off,
        "limit": lim,
    }


def _create_fact_sync(memory: Memory, *, key: str, body: FactCreateRequest) -> SemanticFact:
    """Contradiction-aware owner write (runs off-loop, see caller).

    A same-key DIFFERENT-value owner write must SUPERSEDE the existing valid fact,
    not pile a second contradictory valid row on top of it. Routed through the atomic
    primitive ``Memory.assert_fact_direct`` (audit C14) so find-contradictions +
    invalidate + upsert happen in ONE transaction — the old find→supersede→
    fall-through-to-remember_fact path left two conflicting valid rows on a lost race.
    Provenance: origin derives from the trust ladder, detail is the caller's free text.
    """
    _closed, new = memory.assert_fact_direct(
        key=key,
        value=body.value,
        trust=body.trust,
        extractor="api.memory",
        source_origin=body.trust,
        source_detail=body.source_detail,
    )
    return new


@router.post("/memory/facts", dependencies=[Depends(require_akana_bearer)])
async def create_fact(request: Request, body: FactCreateRequest) -> dict[str, Any]:
    """Owner write — direct to semantic memory (the user is the approver here)."""
    memory = await get_memory(request)
    key = (
        ensure_kind_prefix(body.key, body.kind)
        if body.key and body.key.strip()
        else derive_key(body.value, body.kind)
    )
    # Off-loop: the fact write emits → the vector indexer embeds synchronously (first call
    # downloads a ~220MB model); never block the event loop with it.
    fact = await asyncio.to_thread(_create_fact_sync, memory, key=key, body=body)
    return _fact_payload(fact)


@router.patch(
    "/memory/facts/{fact_id}", dependencies=[Depends(require_akana_bearer)]
)
async def patch_fact(
    request: Request, fact_id: str, body: FactPatchRequest
) -> dict[str, Any]:
    memory = await get_memory(request)
    existing = memory.get_fact(fact_id)
    if existing is None:
        raise http_error(404, "NOT_FOUND", f"no fact {fact_id!r}")

    if body.mode == "correct":
        # In-place typo/cleanup fix — not a temporal supersede. Routed through the
        # Memory façade (not memory.semantic directly) so the re-emitted `fact` event
        # re-embeds the vector and re-links the graph; a raw store write would leave
        # both stale until a manual reindex.
        # audit C30: correct_fact returns None for BOTH missing and already-invalidated
        # facts (its UPDATE is guarded by invalidated_at IS NULL). The fact was just
        # found above, so distinguish the two: an invalidated fact is 409 (not 404),
        # mirroring the supersede branch below.
        if not existing.is_valid:
            raise http_error(
                409, "NOT_ACTIONABLE", "fact already invalidated; cannot correct"
            )
        fact = await asyncio.to_thread(
            memory.correct_fact, fact_id, new_value=body.new_value
        )
        if fact is None:
            raise http_error(409, "NOT_ACTIONABLE", "fact is no longer correctable")
        return {"status": "corrected", "fact": _fact_payload(fact)}

    if not existing.is_valid:
        raise http_error(
            409, "NOT_ACTIONABLE", "fact already invalidated; cannot supersede"
        )
    # extractor → provenance detail derivation (the origin carries over from the
    # old record's trust)
    # Off-loop: supersede emits a new fact → synchronous embed (see approve_staged).
    result = await asyncio.to_thread(
        memory.supersede_fact, fact_id, new_value=body.new_value, extractor="api.memory"
    )
    if result is None:
        raise http_error(409, "NOT_ACTIONABLE", "fact is no longer supersedable")
    old, new = result
    return {"status": "superseded", "old_id": old.id, "fact": _fact_payload(new)}


@router.delete(
    "/memory/facts/{fact_id}", dependencies=[Depends(require_akana_bearer)]
)
async def delete_fact(
    request: Request, fact_id: str, hard: bool = False
) -> dict[str, Any]:
    memory = await get_memory(request)
    existing = memory.get_fact(fact_id)
    if existing is None:
        raise http_error(404, "NOT_FOUND", f"no fact {fact_id!r}")
    ok = memory.forget_fact(fact_id, hard=hard)
    if not ok:  # soft-forget on an already-closed validity window
        return {"status": "already_inactive", "fact_id": fact_id, "hard": hard}
    return {
        "status": "deleted" if hard else "invalidated",
        "fact_id": fact_id,
        "hard": hard,
    }


# -- recall (through the orchestrator, trace included) ------------------------------


@router.get("/memory/recall", dependencies=[Depends(require_akana_bearer)])
async def recall(
    request: Request,
    q: str = Query(..., min_length=1, max_length=2000),
    k: int = 12,
    as_of: str | None = Query(
        default=None,
        max_length=64,
        description=(
            "Time-travel (D): ISO-8601, 'relative:<n><h|d|w>', or Turkish natural "
            "language ('dün', 'geçen hafta', 'mart ayında'). Returns the state of "
            "memory as of that moment."
        ),
    ),
    observed_from: str | None = Query(
        default=None,
        max_length=64,
        description=(
            "Bi-temporal observation filter start: records observed after this "
            "moment. ISO, 'relative:7d', or Turkish natural language."
        ),
    ),
    observed_to: str | None = Query(
        default=None,
        max_length=64,
        description=(
            "Bi-temporal observation filter end: records observed up to this moment. "
            "ISO, 'relative:7d', or Turkish natural language (the end of the range is used)."
        ),
    ),
) -> dict[str, Any]:
    """Orchestrated recall (trace included). If ``as_of`` is given, it switches to
    the time-travel strategy — the validity window is evaluated in SQL and the
    value as of the ``as_of`` instant is returned instead of the most recent
    spelling. ``observed_from``/``observed_to`` filter by the observation
    (observed_at) range; both are resolved at a single point in the orchestrator."""
    orch = await get_orchestrator(request)
    args: dict[str, Any] = {"query": q, "k": max(1, min(k, 50))}
    for name, raw in (
        ("as_of", as_of),
        ("observed_from", observed_from),
        ("observed_to", observed_to),
    ):
        if raw is not None and raw.strip():
            args[name] = raw.strip()
    # Off-loop: with vector recall on, this embeds the query synchronously (first-call
    # model load/download); running it on the event loop would freeze the whole server
    # (same reasoning as approve_staged/create_fact above).
    result = await asyncio.to_thread(orch.handle_tool_call, "memory.search", args)
    err = result.get("error")
    if err:
        # audit C29: map the orchestrator error code through the shared status map —
        # a malformed as_of/observed_* is invalid_request → 400, but rate_limited → 429
        # and internal_error → 500 (previously all collapsed to 400).
        code = str(err.get("code", "recall_failed"))
        raise http_error(
            _TOOL_ERR_STATUS.get(code, 400),
            code.upper(),
            str(err.get("message", "memory.search failed")),
        )
    return result


# -- timeline (unified activity feed, newest first) ----------------------------------

# Ledger event.kind → human-readable title (timeline title). These are user-visible
# labels, so they are BILINGUAL and resolve by the active ``language`` runtime
# setting (en|tr), English default. Unknown kinds pass through raw.
_TIMELINE_TITLES: dict[str, dict[str, str]] = {
    "en": {
        "fact": "New fact",
        "fact_invalidated": "Fact invalidated",
        "turn": "Conversation turn",
        "conversation_reset": "Conversation reset",
        "memory.forget": "Forgotten",
        "memory.remember": "Remembered",
        "memory.usage": "Usage feedback",
    },
    "tr": {
        "fact": "Yeni bilgi",
        "fact_invalidated": "Bilgi geçersiz kılındı",
        "turn": "Konuşma turu",
        "conversation_reset": "Konuşma sıfırlandı",
        "memory.forget": "Unutuldu",
        "memory.remember": "Hatırlandı",
        "memory.usage": "Kullanım geri bildirimi",
    },
}

# Per-language detail templates that carry localized prose (the rest are pure data).
_TIMELINE_TURNS_DELETED = {"en": "{count} turns deleted", "tr": "{count} tur silindi"}

# Keys carrying the ref id inside ledger event.data (the first match wins).
_TIMELINE_REF_KEYS = ("fact_id", "turn_id", "target_id", "conversation_id", "explain_id")


def _timeline_title(kind: str, language: str = "en") -> str:
    lang = language if language in ("en", "tr") else "en"
    return _TIMELINE_TITLES[lang].get(kind, kind)


def _timeline_ref_id(data: dict[str, Any]) -> str | None:
    for k in _TIMELINE_REF_KEYS:
        v = data.get(k)
        if v:
            return str(v)
    return None


def _timeline_detail(kind: str, data: dict[str, Any], language: str = "en") -> str:
    """A one-line human-readable summary — woven from the data fields per kind.

    Localized prose follows the active ``language`` (en|tr, English default); the
    remaining fields are pure data (keys/values/ids) and stay as-is.
    """
    lang = language if language in ("en", "tr") else "en"
    if kind in ("fact", "memory.remember"):
        key, value = data.get("key"), data.get("value")
        if key and value:
            return f"{key}: {value}"
        return str(value or key or "")
    if kind == "fact_invalidated":
        if data.get("superseded_by"):
            return f"{data.get('key', '')}: {data.get('value', '')}".strip(": ")
        return "hard delete" if data.get("hard") else (str(data.get("key") or ""))
    if kind == "turn":
        return f"{data.get('role', '')} · {data.get('conversation_id', '')}".strip(" ·")
    if kind == "conversation_reset":
        return _TIMELINE_TURNS_DELETED[lang].format(count=data.get("count", 0))
    if kind == "memory.forget":
        return f"{data.get('mode', '')} · {data.get('outcome', '')}".strip(" ·")
    if kind == "memory.usage":
        return f"{len(data.get('used_ids') or [])} hit / {len(data.get('missed_ids') or [])} miss"
    return ""


@router.get("/memory/timeline", dependencies=[Depends(require_akana_bearer)])
async def memory_timeline(
    request: Request,
    limit: int = 100,
    kind: str | None = Query(default=None, max_length=64),
) -> dict[str, Any]:
    """Unified activity feed (newest first).

    Ledger events (``memory.ledger.read_all``) are mapped to timeline items: each
    is ``{ts, kind, title, detail, ref_id}``. If the ``kind`` query is given, the
    ledger read is filtered by that kind (matching the raw ``event.kind``).
    """
    memory = await get_memory(request)
    lim = max(1, min(limit, 500))
    kind_filter = (kind or "").strip() or None
    from akana_server.runtime_settings import resolve_language

    language = resolve_language(request.app.state.settings)
    events = memory.ledger.read_all(kind=kind_filter, limit=lim)
    items = [
        {
            "ts": ev.ts,
            "kind": ev.kind,
            "title": _timeline_title(ev.kind, language),
            "detail": _timeline_detail(ev.kind, ev.data, language),
            "ref_id": _timeline_ref_id(ev.data),
        }
        for ev in events
    ]
    # read_all(limit>0) already returns chronological (old→new); flip to newest first.
    items.reverse()
    return {"items": items, "count": len(items)}


# -- owner settings ------------------------------------------------------------------


@router.get("/memory/settings", dependencies=[Depends(require_akana_bearer)])
async def get_settings(request: Request) -> dict[str, Any]:
    return _settings_payload(get_memory_settings(request), _data_dir(request))


@router.put("/memory/settings", dependencies=[Depends(require_akana_bearer)])
async def put_settings(request: Request, body: MemorySettingsPatch) -> dict[str, Any]:
    data_dir = _data_dir(request)
    ms = load_memory_settings(data_dir)
    if body.allow_direct is not None:
        ms.allow_direct = body.allow_direct
    if body.auto_capture is not None:
        ms.auto_capture = body.auto_capture
    if body.session_summary is not None:
        ms.session_summary = body.session_summary
    if body.vector is not None:
        ms.vector = body.vector
    if body.embed_backend is not None:
        ms.embed_backend = body.embed_backend
    if body.ollama_url is not None and body.ollama_url.strip():
        ms.ollama_url = body.ollama_url.strip()
    if body.embed_model is not None and body.embed_model.strip():
        ms.embed_model = body.embed_model.strip()
    save_memory_settings(data_dir, ms)
    request.app.state.memory_settings = ms
    # Off-loop AND under the build lock: after an embed-backend/vector-mode change the
    # rebuild runs store.clear() + a full re-embed of every fact inline (first-call model
    # load/download on top) — inline on the loop it would freeze the whole server. The lock
    # (shared with ensure_memory_stack) is what keeps a concurrent first-touch request from
    # building its OWN stack against the same Memory singleton in the swap window and leaving
    # a second VectorIndexer subscribed forever (#19 subscriber leak).
    await asyncio.to_thread(_rebuild_memory_stack_locked, request)
    return _settings_payload(ms, data_dir)


def _rebuild_memory_stack_locked(request: Request) -> None:
    """Swap in a fresh stack for the new settings, atomically under the build lock.

    Build the NEW stack first (which subscribes the new indexer) and only THEN detach
    the old one, so at least one indexer is always attached to the in-process Memory
    singleton during the swap — a fact event emitted concurrently in the window is not
    lost (embedded twice at worst, idempotent on fact_id). The whole clear→swap→detach
    runs under _stack_build_lock so a first-touch build cannot interleave.
    """
    with _stack_build_lock(request):
        old_indexer = getattr(request.app.state, "memory_indexer", None)
        for key in ("memory_core", "memory_orchestrator", "memory_indexer"):
            if hasattr(request.app.state, key):
                delattr(request.app.state, key)
        _build_memory_stack_unlocked(request)  # rebuilds + subscribes the NEW VectorIndexer (lock already held)
        # Now detach the old indexer (idempotent + skipped if absent) → no duplicate-embed leak (#19).
        if old_indexer is not None and hasattr(old_indexer, "detach"):
            old_indexer.detach()


# -- stats ---------------------------------------------------------------------------


# -- HTTP loopback: generic MCP tool dispatcher ----------------------------------
#
# Phase 1 (full-A architecture): the src/akana/memory MCP subprocess will later
# delegate to this endpoint over HTTP (a shim). For now all 4 tools are served
# here: memory.search / memory.remember / memory.forget.
# For the MCP convention, tool names are accepted with a
# hyphen (`memory-search`) or a dot (`memory.search`);
# orchestrator.handle_tool_call sees both in dot format.


@router.post(
    "/memory/tool/{tool_name}",
    dependencies=[Depends(require_akana_bearer)],
)
async def call_memory_tool(
    request: Request,
    tool_name: str,
    body: dict[str, Any] | None = None,
    conversation_id: str | None = Query(
        default=None,
        max_length=128,
        description="Active conversation id for scoping the tool call (optional).",
    ),
) -> dict[str, Any]:
    """MCP tool dispatcher — the single entry for the HTTP loopback.

    ``tool_name``: ``memory.search`` / ``memory.remember`` / ``memory.forget``.
    For the MCP convention, the hyphenated form (``memory-search``) is also accepted.

    The body is free JSON per the tool's input_schema; the orchestrator validates
    it and returns an ``error`` envelope on failure (it never falls to 500 —
    invalid_request → 400, rate_limited → 429, unknown_tool → 404).
    """
    # The MCP shim sends tool names like "memory-search"; the orchestrator expects
    # "memory.search". Normalize to a single standard.
    canonical = tool_name.replace("-", ".")
    if canonical not in MEMORY_TOOLS:
        raise http_error(
            404,
            "UNKNOWN_TOOL",
            f"unknown tool {tool_name!r}; available: {', '.join(MEMORY_TOOLS)}",
        )
    orch = await get_orchestrator(request)
    # Off-loop: memory.search/remember can trigger a synchronous vector embed (first-call
    # model load/download) — never block the event loop with it (see approve_staged above).
    result = await asyncio.to_thread(
        orch.handle_tool_call,
        canonical,
        body or {},
        conversation_id=conversation_id,
    )
    err = result.get("error") if isinstance(result, dict) else None
    if err:
        code = str(err.get("code", "tool_error"))
        message = str(err.get("message", "tool failed"))
        raise http_error(_TOOL_ERR_STATUS.get(code, 400), code.upper(), message)
    return result


@router.get(
    "/memory/tools",
    dependencies=[Depends(require_akana_bearer)],
)
async def list_memory_tools() -> dict[str, Any]:
    """Tool inventory for the MCP shim — schema included. This lets the subprocess
    shim discover the tool list at startup without having to fetch it per call."""
    from akana.memory.tools import tool_schemas

    return {"tools": tool_schemas()}


def _vector_health(request: Request, embeddings: int) -> dict[str, Any]:
    """Vector recall health summary — show 'is it working' at a glance (this used to
    require a script/trace): is it on, which backend/model, is the embedder reachable."""
    ms = get_memory_settings(request)
    backend = ms.embed_backend
    indexer = getattr(request.app.state, "memory_indexer", None)
    # The local embedder loads its ONNX model LAZILY on the first embed, so an indexer
    # is wired (and fastembed imports) even when the configured model is invalid or the
    # download failed. The first embed then trips the shared breaker permanently-off
    # (or into rolling cooldowns) and every recall silently degrades to keyword-only.
    # Fold that breaker state into active/available so /memory/stats stops reporting a
    # dead vector layer as healthy. (_health has no public reader; access defensively.)
    health = getattr(indexer, "_health", None) if indexer is not None else None
    breaker_ok = health.active() if health is not None else True
    breaker_permanent = bool(getattr(health, "_permanent", False))
    try:
        models = VectorStore.for_data_dir(_data_dir(request)).distinct_models()
    except Exception:
        models = []
    if ms.vector == "off" or backend == "off":
        available = False
    elif backend == "ollama":
        try:
            from akana.memory.embed import is_available

            available = is_available(ms.ollama_url)
        except Exception:
            available = False
    else:  # local (fastembed)
        import importlib.util

        available = importlib.util.find_spec("fastembed") is not None
    # A permanent trip (missing/typo'd model, failed first download) means the embedder
    # is not actually usable, whatever the import check says.
    available = available and not breaker_permanent
    if ms.vector == "off" or backend == "off":
        status = "off"
    elif breaker_permanent:
        status = "degraded"  # embedder unusable this process → keyword-only recall
    elif not breaker_ok:
        status = "cooldown"  # embedding paused, retrying → keyword-only meanwhile
    elif not available:
        status = "unavailable"
    else:
        status = "active"
    return {
        "active": indexer is not None and ms.vector != "off" and breaker_ok,
        "mode": ms.vector,
        "backend": backend,
        "available": available,
        "status": status,  # machine token; the UI localizes it
        "embeddings": embeddings,
        "models": models,
    }


@router.get("/memory/stats", dependencies=[Depends(require_akana_bearer)])
async def memory_stats(request: Request) -> dict[str, Any]:
    memory = await get_memory(request)
    # audit C27/C28: compute every metric with COUNT(*) instead of hydrating rows.
    # Previously this read up to 50k full fact rows just to len() them (and both
    # counts plateaued past the 50k LIMIT) and counted only the newest 200
    # conversations (so >200 conversations froze the count and dropped older turns).
    # Off-loop: the COUNTs are cheap but still touch sqlite shared with the MCP subprocess.
    total_facts = await asyncio.to_thread(
        memory.semantic.count_facts, include_invalidated=True
    )
    valid_facts = await asyncio.to_thread(
        memory.semantic.count_facts, include_invalidated=False
    )
    conversations = await asyncio.to_thread(memory.conversation_count)
    turns = await asyncio.to_thread(memory.turn_count)
    def _read_vector_embeddings() -> int:
        try:
            return VectorStore.for_data_dir(_data_dir(request)).count()
        except Exception:  # embeddings table unreadable → report 0, never 500
            return 0

    vector_embeddings = await asyncio.to_thread(_read_vector_embeddings)
    # BUG (loop-freeze): _vector_health runs a synchronous httpx probe (embed.is_available,
    # 1.5s timeout) plus sync sqlite reads (distinct_models); when embed_backend=ollama and
    # the daemon is down this blocks the whole asyncio server for up to 1.5s — and the UI
    # polls /memory/stats repeatedly per turn. Offload it like the COUNT(*) queries above.
    vector = await asyncio.to_thread(_vector_health, request, vector_embeddings)
    return {
        "facts": total_facts,
        "valid_facts": valid_facts,
        "turns": turns,
        "conversations": conversations,
        "staging_pending": memory.staging.count_pending(),
        "vector_embeddings": vector_embeddings,
        "vector": vector,
        "ledger_path": str(memory.ledger.path),
    }
