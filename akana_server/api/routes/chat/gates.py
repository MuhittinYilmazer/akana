"""Turn-start gate chain — intent → file → skill.

The second large seam extracted from the `chat/__init__.py` god-file: all the gates a
turn passes through before reaching the LLM (intent classification, file binding,
fast-path, skill injection). The pre-LLM command short-circuit was removed (see the
note above ``_fast_path_max_chars``) — there is no command step; the only
response-producing gate is ``_files_gate``. Both chat surfaces (blocking ``POST /chat``
and SSE ``POST /chat/stream``) consume the single entry ``_run_turn_gates``; there can
be no gate drift.

``__init__`` re-imports ``_run_turn_gates``/``_GateResult``. The test monkeypatch surface
(``complete_chat_with_usage``, ``plan_skill_turn``) continues to live in the PACKAGE
namespace: the readers below read these names late from the package at call time
(``_chatpkg`` deferred import) — so a ``setattr`` on ``routes.chat`` resolves to the same
object.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import logging
from dataclasses import dataclass

import ulid
from fastapi import HTTPException, Request

from akana_server.api.services import get_services
from akana_server.config import Settings
from akana_server.chat_context import effective_llm_settings
from akana_server.llm_settings import load_llm_settings, resolve_provider
from akana_server.multimodal import ImageStore, prepare_files
from akana_server.orchestrator.router import classify_intent
from akana_server.skills.turn_injection import (
    SkillTurnPlan,
    # `__init__` RE-EXPORTS this from `gates` (the test patch surface);
    # within gates it's read late at call time via `_chatpkg.plan_skill_turn`.
    plan_skill_turn,  # noqa: F401
)

from akana_server.api.routes.chat._base import _off_loop
from akana_server.api.routes.chat.models import ChatRequest, ChatResponse

log = logging.getLogger(__name__)


def _classify_turn_intent(text: str) -> str:
    """Classify a turn's intent ("chat"/"system_action").

    FULL AUTONOMY (user decision): the risk/approval gate is entirely removed —
    no request is blocked, no request asks for approval. The old hard 403 deny
    (``policy.rules.evaluate_user_text``) and the ``require_approval`` reflex were
    removed, so this is a thin wrapper over ``classify_intent``. The constant
    ``approval_required=False`` is set by the caller (``_run_turn_gates``) — both
    chat surfaces still carry that field on the wire for backward compatibility.
    """
    return classify_intent(text)


# -- File input (MultimodalEngine F1 → PHASE2 multi-type binding) -----------------------
#
# `ChatRequest.effective_file_ids` (file_ids + image_ids) → resolved per the active
# provider:
# * claude  → a `[Dosya: <absolute-path>]` line for each provider-native file
#   (the CLI's Read tool reads the path ITSELF: image/pdf/text — the content is
#   NOT EMBEDDED into the prompt, only a path reference is passed),
# * cursor / unsupported → without DROPPING the turn, a Turkish "this provider
#   can't read this file" note is added to the prompt block (the file isn't
#   silently dropped; one unsupported file doesn't abort the turn),
# * unknown/disabled/missing-on-disk id → `prepare_files` writes it to
#   `unsupported` (the turn isn't aborted, the user is honestly informed).
#
# NOTE: `prepare_files` does NOT RAISE UploadStoreError on a single file's error —
# the old single-file-400 behavior is replaced by a "don't drop the turn" contract.


def _image_store(request: Request) -> ImageStore:
    """Lazy app.state cache — the same seam as in the uploads route.

    ``image_store`` is a lazy cache (NOT in AppServices) → ``request`` is preserved
    (the cache write goes to app.state); only the ``settings`` read is taken from
    the typed container.
    """
    # Share deps._IMAGE_STORE_LOCK with the uploads dep so this seam and
    # get_image_store can't each build a DISTINCT store (with independent locks)
    # for the same process — the root of the concurrent-upload dedup race that
    # surfaced UNIQUE(sha256) IntegrityError + orphan files.
    from akana_server.api.deps import _IMAGE_STORE_LOCK

    store = getattr(request.app.state, "image_store", None)
    if store is None:
        with _IMAGE_STORE_LOCK:
            store = getattr(request.app.state, "image_store", None)
            if store is None:
                settings: Settings = get_services(request).settings
                store = ImageStore.for_settings(settings)
                request.app.state.image_store = store
    return store


def _kind_label(kind: str | None) -> str:
    """Convert a file kind to a short label (for the unsupported note)."""
    return {
        "image": "image",
        "pdf": "PDF",
        "docx": "Word document",
        "xlsx": "Excel spreadsheet",
        "text": "text file",
    }.get(str(kind or "").strip().lower(), "file")


def _file_block_line(kind: str | None, path: str) -> str:
    """Provider-native file path → prompt line (label per kind).

    Images keep the D16.B backward-compatible `[Görsel: <path>]` label (the
    assembler counters + existing tests look at this); other types use
    `[Dosya: <path>]`.
    """
    label = "Görsel" if str(kind or "").strip().lower() == "image" else "Dosya"
    return f"[{label}: {path}]"


async def _files_gate(
    request: Request, body: ChatRequest
) -> tuple[str, "ChatResponse | None"]:
    """effective_file_ids → (prompt file block, short-circuit response | None).

    claude provider-native files become `[Görsel/Dosya: <path>]` lines;
    unsupported files (cursor/unknown-provider) become `[Note: ...]` warning
    lines without dropping the turn. If no id is given, `("", None)`.

    A missing/disabled/missing-on-disk record (an unsupported carrying an
    UploadStore error code) preserves the D16.B contract → 400 (`error.code`
    carries the ImageStore code). When the provider can read no file at all (only
    "can't read" notes), an honest rejection is returned without reaching
    the LLM (action=file_unsupported, or image_unsupported in the image-only case
    for D16.B compatibility).
    """
    ids = body.effective_file_ids
    if not ids:
        return "", None
    services = get_services(request)
    settings: Settings = services.settings
    conv_id = (body.conversation_id or "").strip()
    if conv_id:
        llm = effective_llm_settings(request, conv_id)
    else:
        llm = services.llm_settings or load_llm_settings(settings.data_dir, settings)
    provider = resolve_provider(settings, llm)
    store = _image_store(request)
    # store.get + stat (sqlite/disk) — off the loop.
    prepared = await _off_loop(prepare_files, store, ids, provider)

    # Missing/disabled/missing-on-disk record → D16.B behavior is preserved: 400.
    for item in prepared.unsupported:
        code = item.get("code")
        if code:  # the unsupported coming from UploadStoreError (only these have a code)
            raise HTTPException(
                status_code=400,
                detail={"error": {"code": code, "message": item.get("reason")}},
            )

    lines = [_file_block_line(ref.get("kind"), ref["path"]) for ref in prepared.file_refs]
    notes: list[str] = []
    for item in prepared.unsupported:
        label = _kind_label(item.get("kind"))
        notes.append(
            f"[Note: the «{provider}» provider cannot read this {label}; "
            "to read it, switch to the claude provider under Settings → LLM.]"
        )

    if not prepared.file_refs and not prepared.inline_refs and prepared.unsupported:
        # The provider could read no file → an honest rejection without
        # reaching the LLM. NOTE: if there ARE inline_refs (gemini image/PDF) there's
        # NO short-circuit — the files are embedded as inline_data via
        # ``_add_turn_images`` and the turn flows normally.
        only_images = all(
            str(i.get("kind") or "").strip().lower() == "image"
            for i in prepared.unsupported
        )
        kinds = ", ".join(
            _kind_label(i.get("kind")) for i in prepared.unsupported
        )
        noun = "image input" if only_images else f"these file types ({kinds})"
        return "", ChatResponse(
            turn_id=str(ulid.new()),
            text=(
                f"I can't process the files: the active provider «{provider}» does "
                f"not support {noun}. To use files, switch to the claude provider "
                "under Settings → LLM."
            ),
            lang=body.lang,
            conversation_id=(body.conversation_id or "").strip() or str(ulid.new()),
            intent="system_action",
            action="image_unsupported" if only_images else "file_unsupported",
        )
    return "\n".join([*lines, *notes]), None


# -- (REMOVED) ReAct AUTONOMY — since `_react_autonomy_enabled` was always False,
# `_maybe_reasoning_turn` and all the reasoning/capability plumbing tied to it had
# become unreachable; deleted as a continuation of the WAVE 22 retirement.
# `classify_task` (the planner/router signal) stays live; the pre-LLM command
# short-circuit (new-chat/delete, aç:/youtube:) was REMOVED — every message is now
# an LLM turn. The natural-language autonomous tool-calling path is deliberately absent.


# -- FAST PATH (fast-path) --------------------------------------------------------------
#
# The turn-start skill suggestion (suggest_for_text) is unnecessary latency on
# short/simple messages ("play a song from YouTube"). On short messages that don't
# smell multi-step, the skill suggestion search runs with a 0.5s budget; on
# long/multi-step-smelling messages the full budget is used.

_FAST_PATH_DEFAULT_MAX_CHARS = 80
_FAST_SUGGEST_TIMEOUT_S = 0.5


def _fast_path_max_chars() -> int:
    """Fast-path short-message threshold — env ``AKANA_FAST_PATH_MAX_CHARS`` (0 = off)."""
    raw = os.environ.get("AKANA_FAST_PATH_MAX_CHARS", "").strip()
    if not raw:
        return _FAST_PATH_DEFAULT_MAX_CHARS
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning(
            "AKANA_FAST_PATH_MAX_CHARS=%r invalid; using default %s",
            raw,
            _FAST_PATH_DEFAULT_MAX_CHARS,
        )
        return _FAST_PATH_DEFAULT_MAX_CHARS


# Multi-step heuristic (moved here from the removed plan_act module — its only live
# consumer is the fast-path budget below). A rough smell test: bullet list, ordering
# connective, or 3+ sentences → use the full skill-suggestion budget, not the fast path.
_LIST_LINE_RE = re.compile(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+\S")

_STEP_CONNECTORS: tuple[str, ...] = (
    " sonra ",
    "sonra da",
    "ardından",
    "ardindan",
    "daha sonra",
    "akabinde",
    "bitince",
    "bittikten sonra",
    "tamamlanınca",
    "tamamlaninca",
    " and then ",
    " then ",
    "after that",
    "adım adım",
    "adim adim",
    "step by step",
)

_SENTENCE_SPLIT_RE = re.compile(r"[.!?;\n]+")


def looks_multi_step(text: str) -> bool:
    """Rough heuristic: bullet list, ordering connectives, or 3+ sentences."""
    raw = text.strip()
    if not raw:
        return False
    if _LIST_LINE_RE.search(raw):
        return True
    low = " ".join(raw.lower().split())
    if any(c in low for c in _STEP_CONNECTORS):
        return True
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(raw) if s.strip()]
    return len(sentences) >= 3 and len(low) >= 120


def _fast_path_active(text: str, thinking_mode: str) -> bool:
    """ThinkingMode + short-message heuristic → fast-path decision.

    * ``hizli`` → always (the user explicitly asked for speed),
    * ``normal`` → on messages below the threshold AND not smelling multi-step,
    * deep modes (``derin``/``yogun``/``azami``/``ultra``) → never (the full
      suggestion budget).

    This function only selects the skill suggestion budget.
    """
    if thinking_mode == "hizli":
        return True
    if thinking_mode != "normal":
        # derin/yogun/azami → every mode deeper than normal gets the full budget.
        return False
    limit = _fast_path_max_chars()
    if limit <= 0:
        return False
    t = (text or "").strip()
    return len(t) <= limit and not looks_multi_step(t)


# -- WI-1: per-turn skill injection --------------------------------------------------
#
# At the start of a turn, if SkillRegistry.suggest_for_text gives a strong match, the
# SKILL.md body (L2) is injected into the agent prompt as a [Yetenek: ...] block.
# FULL AUTONOMY: there is no approval gate — every strong-match skill (including any
# flagged requires_approval) is injected directly.
# (v0.1: the explicit `skill çalıştır:` work-mode command was removed — everything
# flows via chat-level injection; there's no separate skill_run machine.)


async def _skill_turn_gate(
    request: Request,
    body: ChatRequest,
    *,
    plan_task: "asyncio.Task[SkillTurnPlan] | None" = None,
) -> SkillTurnPlan | None:
    """The WI-1 gate: build the per-turn skill injection plan.

    If ``plan_task`` is given, the suggestion search was started in PARALLEL with the
    gate chain (a fast-path/latency optimization) — it is not recomputed.
    Failure guarantee: no failure breaks the turn — in the worst case ``None`` is
    returned and the turn continues without a skill.
    """
    from akana_server.api.routes import chat as _chatpkg

    settings: Settings = get_services(request).settings
    try:
        if plan_task is not None:
            return await plan_task
        return await _chatpkg.plan_skill_turn(settings, body.text)
    except Exception:  # defensive; plan_skill_turn should already swallow it
        log.warning(
            "skill injection plan could not be built; turn continues without a skill",
            exc_info=True,
        )
        return None


# -- Turn gate chain (blocking + SSE, a single gate) ------------------------------------


@dataclass(slots=True)
class _GateResult:
    """The gate chain's output — both chat surfaces consume this."""

    intent: str
    approval_required: bool
    body: ChatRequest
    response: ChatResponse | None = None
    skill_plan: SkillTurnPlan | None = None
    image_block: str = ""


async def _run_turn_gates(request: Request, body: ChatRequest) -> _GateResult:
    """Turn-start gate chain: intent → file → skill.

    A single dispatch point: blocking (`POST /chat`) and SSE (`POST /chat/stream`)
    use the same order; there can be no gate drift between the two surfaces. (The
    pre-LLM command gate was removed — every message is an LLM turn now; the only
    response-producing gate is ``_files_gate``.)

    Latency optimization (FAST PATH): on the fast-path the skill suggestion
    (suggest_for_text) budget drops to 0.5s. The gate duration is logged on every
    turn (a rough measurement).
    """
    from akana_server.api.routes import chat as _chatpkg

    t0 = time.perf_counter()
    # NOTE: we do NOT PIN body.conversation_id here. It was once normalized for a single
    # identity but had no observable benefit (every handler + the main turn generate
    # conv_id themselves) and it shadowed the "did the client provide a conv_id" signal —
    # conversation_delete reads it for the client-requirement (a clean 400 if empty;
    # otherwise a confusing 404 with a made-up ULID).
    # ``approval_required`` is a constant False (the approval gate was removed under
    # FULL AUTONOMY); it is still carried on the wire (ChatResponse + the SSE ``done``
    # event + the ``chat_done`` WS broadcast) for backward compatibility.
    intent = _classify_turn_intent(body.text)
    approval_required = False
    # Voice mode = the fastest possible response: this turn's ThinkingMode is dropped to
    # "hizli" → the suggestion budget is skipped (fast-path) AND on the claude provider
    # the effort drops to the lowest level (--effort low). The visible composer segment
    # doesn't change; only this turn's engine budget speeds up. The voice response
    # already gets a keep-it-short directive (user_for_llm below).
    if body.voice and body.thinking_mode != "hizli":
        body = body.model_copy(update={"thinking_mode": "hizli"})
    out = _GateResult(intent=intent, approval_required=approval_required, body=body)

    image_block, image_resp = await _files_gate(request, body)
    if image_resp is not None:
        out.response = image_resp
        return out
    out.image_block = image_block

    fast = _fast_path_active(body.text, body.thinking_mode)
    settings: Settings = get_services(request).settings

    # FULL AUTONOMY: every strong-match skill is injected; there is no approval gate.
    # On the fast path the suggestion search runs with a 0.5s budget; the suggestion
    # is started in parallel with the gate chain to hide its latency.
    suggest_task: asyncio.Task[SkillTurnPlan] = asyncio.create_task(
        _chatpkg.plan_skill_turn(
            settings,
            body.text,
            timeout_s=_FAST_SUGGEST_TIMEOUT_S if fast else None,
        )
    )
    out.skill_plan = await _skill_turn_gate(request, body, plan_task=suggest_task)
    log.info(
        "chat gates conv=%s mode=%s fast=%s ms=%d",
        (body.conversation_id or "-"),
        body.thinking_mode,
        fast,
        int((time.perf_counter() - t0) * 1000),
    )
    return out
