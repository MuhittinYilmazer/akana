"""Low-level helpers shared from the chat-turn god-file (Step B3).

Only primitive helpers that are independent of the chat internals and used by
BOTH ``__init__`` (the streaming/persist path) AND ``commands.py`` live here.
This lets the command handlers move to a separate module without creating a
``commands.py`` ↔ ``__init__`` circular import.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
from typing import Any, Callable

from fastapi import Request

from akana_server.concurrency import off_loop
from akana_server.config import Settings
from akana_server.context import ContextRequest
from akana_server.llm_settings import resolve_cursor_model_tag
from akana_server.skills.turn_injection import SkillTurnPlan


def _sse_pack(event: str, data: dict[str, Any]) -> str:
    """Encode one SSE message. Newlines in data are escaped via JSON."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _sse_memory_use(items: list[dict[str, Any]]) -> bytes:
    if not items:
        return b""
    return _sse_pack(
        "memory_use",
        {"items": items, "count": len(items)},
    ).encode("utf-8")


def _context_request(
    body: Any,
    conv_id: str,
    *,
    skill_plan: SkillTurnPlan | None = None,
    image_block: str = "",
) -> ContextRequest:
    """ChatRequest → ContextEngine input (blocking + SSE, the same gate).

    ``body`` is a ``models.ChatRequest`` (typed ``Any`` here to keep ``_base`` a true
    L0 leaf — it must carry no intra-package import; see the chat-boundaries guard).

    NOTE: the memory injection and skill prepend formula now live inside
    :class:`ContextAssembler` (ContextEngine F0) — here only the inputs are
    gathered, the composition rule is not duplicated.
    """
    return ContextRequest(
        text=body.text,
        conversation_id=conv_id,
        channel="web",
        skill_block=skill_plan.prompt_block if skill_plan is not None else "",
        skill_entries=skill_plan.used_payload() if skill_plan is not None else [],
        image_block=image_block,
    )


def _resolve_tts_lang(value: str | None) -> str | None:
    """Normalize `?tts=` query: `tr|en|auto` -> language, `false|off|""` -> None."""
    if not value:
        return None
    v = value.strip().lower()
    if v in ("0", "false", "no", "off"):
        return None
    if v == "auto":
        return "auto"
    if v in ("tr", "en"):
        return v
    return "auto"


async def _off_loop(fn, *args, **kwargs):
    """P0 live stability: move synchronous sqlite/file side effects off the loop.

    Thin re-export of :func:`akana_server.concurrency.off_loop` under the name
    the chat modules import; see that function for the full rationale.
    """
    return await off_loop(fn, *args, **kwargs)


def _client_ip(request: Request) -> str | None:
    try:
        return request.client.host if request.client else None
    except AttributeError:
        return None


def _active_cursor_model(request: Request) -> str:
    settings: Settings = request.app.state.settings
    from akana_server.llm_context import load_effective_llm_settings

    llm = load_effective_llm_settings(settings.data_dir, settings)
    return resolve_cursor_model_tag(settings, llm)


def build_context_assembler(request: Request):
    """ContextAssembler with the prior-context recall seam (B) wired in.

    When ``session_summary_inject_enabled`` is on (default) the assembler folds the
    active chat's rolling session summary back into the turn as a compact «Prior
    context» block, so a long chat resumes with its earlier decisions/open items
    even after the older turns scroll out of the window. Toggle off (or any
    resolution failure) → a bare assembler, i.e. the pre-B behavior.

    The provider is a thin ``conversation_id → SummaryView | None`` lookup; the
    assembler runs it OFF the event loop (its own ``to_thread`` fan-out), so the
    per-turn sqlite read never blocks the loop.

    The compaction summarizer (C) is intentionally left unwired: it is invoked
    inline on the event loop, so bridging it to the live LLM there would deadlock —
    overflow keeps dropping the oldest turns until an async-safe extractive
    summarizer lands.
    """
    from akana_server.context import ContextAssembler

    settings: Settings = request.app.state.settings
    provider = None
    inject_cap: int | None = None
    try:
        from akana_server.runtime_settings import get_runtime

        if bool(get_runtime("session_summary_inject_enabled", settings)):
            from akana.memory import get_session_summary
            from akana_server.memory_core import get_memory_core

            def provider(conversation_id: str):
                return get_session_summary(
                    get_memory_core(settings.data_dir), conversation_id
                )

            inject_cap = int(get_runtime("session_summary_inject_max_chars", settings))
    except Exception:  # recall is an enhancement — never block turn assembly
        provider = None
        inject_cap = None
    return ContextAssembler(
        request, summary_provider=provider, summary_inject_max_chars=inject_cap
    )


# -- voice-mode turn directive (text-chat voice path) ------------------------- #
# Appended to the user turn (LLM prompt only — the stored message stays clean) when
# body.voice is set. Body = the EDITABLE, bilingual voice directive from the persona
# registry (user override or the language default), so editing Settings → Persona →
# "Voice-mode directive" actually governs text-chat voice replies and the directive
# follows the language picker (the fix for "English mode still replies Turkish" in
# voice mode). The opening-words line is a STREAMING-only mechanic — keep the first
# words flowing before tool calls so the user is not left in silence — so it is added
# on the SSE path only (the blocking path returns in one piece, no silence to fill).

_VOICE_OPENING_EN = (
    "OPENING REQUIRED: produce the FIRST words of your reply BEFORE any tool calls "
    "— start with a 1-3 word natural opener ('Sure,', 'One sec,', 'On it,', 'Got "
    "it,'); this is critical so the user is not left waiting in silence. Then call "
    "your tools and complete the answer. Never skip the opener."
)
_VOICE_OPENING_TR = (
    "AÇILIŞ ZORUNLU: yanıtının İLK kelimelerini araç çağrılarından ÖNCE üret — "
    "'Tabii,', 'Bir dakika,', 'Hemen bakıyorum,', 'Olur,' gibi 1-3 kelimelik doğal "
    "bir açılışla başla; kullanıcının sessizlikte beklememesi için kritik. Sonra "
    "araçları çağır ve cevabı tamamla. Açılışı asla atlama."
)


def _voice_language(settings: Settings) -> str:
    """Active prompt language (``en`` | ``tr``) from runtime ``language``; any
    failure → ``"en"`` (English-first default)."""
    try:
        from akana_server.runtime_settings import get_runtime

        lang = str(get_runtime("language", settings) or "en").strip().lower()
        return lang if lang in ("tr", "en") else "en"
    except Exception:
        return "en"


def voice_turn_suffix(settings: Settings, *, streaming: bool) -> str:
    """The ``[mode: voice]`` directive block appended to the user turn in voice mode.

    Body = the editable, bilingual voice directive (persona registry: user override
    or the language default) so Settings → Persona → "Voice-mode directive" governs
    text-chat voice replies and the language picker is honoured. On the streaming
    (SSE) path a bilingual opening-words line is appended too. Defensive: never
    raises — at worst it returns the bare ``[mode: voice]`` tag (the persona still
    carries its own brief voice hint, so voice never breaks on a persona/DB error).
    """
    language = _voice_language(settings)
    directive = ""
    try:
        from akana_server.persona.registry import get_persona_registry

        directive = (
            get_persona_registry(settings.data_dir).get_voice_directive() or ""
        ).strip()
    except Exception:
        directive = ""
    parts = ["[mode: voice]"]
    if directive:
        parts.append(directive)
    if streaming:
        parts.append(_VOICE_OPENING_TR if language == "tr" else _VOICE_OPENING_EN)
    return "\n".join(parts)


def guard_nonstreaming_turn(get_conv_id: Callable[[dict[str, Any]], Any]):
    """DECORATOR wrapping a blocking/voice handler with busy-registry register/release.

    Convergence A #2 (busy) + #3 (cancel). Why NOT a dependency: tests and the queue
    drain call the handler DIRECTLY (bypassing routing) → a FastAPI yield-dependency
    would not run. Because the decorator wraps the function ITSELF, it works for both a
    direct call and HTTP. Since ``functools.wraps`` preserves ``__wrapped__``, FastAPI
    reads the injection signature from the wrapped fn; the wrapper passes the injected
    args through unchanged.

    ``get_conv_id(arguments)`` extracts conv_id from the call arguments bound to the
    signature. An empty/new conv (None) → no registration (a unique ULID can't clash).
    A concurrent second turn on an existing conv → 409 TURN_BUSY; the release runs on
    every exit (exception/cancel). The registry functions are lazily imported at call
    time (when the module is fully loaded) → no ``_base`` ↔ ``streaming`` module-level
    cycle.
    """

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            from akana_server.api.routes.chat.streaming import (
                _register_nonstreaming_turn,
                _release_nonstreaming_turn,
            )

            request = None
            conv_id = None
            try:
                bound = sig.bind_partial(*args, **kwargs)
                request = bound.arguments.get("request")
                conv_id = get_conv_id(bound.arguments)
            except TypeError:
                pass
            app = getattr(request, "app", None)
            token = (
                _register_nonstreaming_turn(app, conv_id) if app is not None else None
            )
            cancelled = False
            try:
                return await fn(*args, **kwargs)
            except asyncio.CancelledError:
                # b8: STOP cancelled this blocking/voice turn → by contract STOP PRESERVES the
                # queue (does not auto-run the next message). Record it so the finally skips the
                # drain, matching the streaming STOP path (status != 'cancelled' gate).
                cancelled = True
                raise
            finally:
                if app is not None:
                    _release_nonstreaming_turn(app, conv_id, token)
                    # #8 queue drain: when a blocking/voice turn finishes NORMALLY, drain the
                    # next queued message (symmetric with the streaming finally). Otherwise a
                    # message queued with 202 after a voice/blocking turn hangs forever. Skipped
                    # on STOP (cancelled) so STOP does not auto-run the queue (b8).
                    if conv_id and not cancelled:
                        from akana_server.api.routes.chat.streaming import (
                            _maybe_drain_queue,
                            _spawn_background,
                        )

                        _spawn_background(app, _maybe_drain_queue(app, conv_id))

        return wrapper

    return deco
