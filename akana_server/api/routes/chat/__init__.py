"""POST /api/v1/chat — text in, LLM out via Cursor SDK bridge.

This package ``__init__`` is intentionally THIN (ARCH-03): the 8 route handlers live in
``routes.py``; ``__init__`` only mounts that ``router`` and holds the backward-compat
RE-EXPORT hub (ARCH-01 patch surface). The re-exports below carry ``noqa:F401`` and MUST
NOT be removed — they keep ``routes.chat.X`` attribute paths alive for two consumers:
external accesses (``routes.chat.ChatRequest`` / ``routes.chat._sse_pack`` / the handler
names) and the deliberate test/voice monkeypatch surface that submodules read LATE via
an in-function ``_chatpkg`` import (``stream_user_chat`` / ``complete_chat_with_usage`` /
the persist writers / ``plan_skill_turn`` / ``_run_turn_gates`` / ``_start_detached_chat_turn``).
Removing a "dead-looking" re-export here already broke queue command-turn persist once
(B4, 8fd2b73) — the AttributeError was swallowed by a try/except.
"""

from __future__ import annotations

from akana_server.orchestrator.llm_dispatch import (  # noqa: F401
    LLMCallError,
    complete_chat_with_usage,
    # routes.py / turn_core read `_chatpkg.stream_user_chat` at call time (patch surface).
    stream_user_chat,
)
# The turn_writer functions must stay in the PACKAGE namespace (re-export):
# persist.py / chat_detached read `_chatpkg.persist_assistant_turn` AT CALL TIME (a patch
# surface) and tests monkeypatch `routes.chat.persist_assistant_turn`. B4 (8fd2b73)
# mistakenly thought this import was "dead" and removed it → the queue command-turn
# persist broke silently with AttributeError (swallowed by try/except) + two integration
# tests broke.
from akana_server.orchestrator.turn_writer import (  # noqa: F401
    persist_assistant_turn,
    persist_error_turn,
    persist_user_turn,
)
# propose_memory_captures is also re-exported in the PACKAGE namespace (same B4 reason):
# persist.py reads it from the package at call time, and tests monkeypatch
# `routes.chat.propose_memory_captures` (the "is background capture non-blocking" scenario).
from akana_server.memory_capture import (  # noqa: F401
    propose_memory_captures,
)

# The turn API schemas moved to chat/models.py (Step B2). They are re-imported so
# external accesses like `routes.chat.ChatRequest` + FastAPI body-annotations and the
# tests' monkeypatch surface keep working unchanged.
from akana_server.api.routes.chat.models import (  # noqa: F401
    ChatRequest,
    ChatResponse,
    TokenUsage,
)


# _off_loop + the low-level accessors live in chat/_base.py (Step B3/B4). The pure
# SSE/context helpers (_sse_pack/_sse_memory_use/_context_request) also live in _base
# now (the seam split) — they are re-exported here so external accesses
# (`routes.chat._sse_pack`) keep working unchanged.
from akana_server.api.routes.chat._base import (  # noqa: F401
    _active_cursor_model,
    _client_ip,
    _context_request,
    _off_loop,
    _resolve_tts_lang,
    _sse_memory_use,
    _sse_pack,
    build_context_assembler,
    guard_nonstreaming_turn,
    voice_turn_suffix,
)

# The persist/capture helpers moved to chat/persist.py (Step B4). They are
# RE-EXPORTED here so external accesses (`routes.chat._mirror_cursor_agent_meta`)
# + the late-import surface (`from ..chat import _persist_*`) keep working unchanged.
from akana_server.api.routes.chat.persist import (  # noqa: F401
    _MEMORY_CAPTURE_TIMEOUT_S,
    _accumulate_tool_call,
    _capture_memory_background,
    _mirror_cursor_agent_meta,
    _persist_assistant_turn_end,
    _persist_error_turn_end,
    _persist_user_turn_start,
    _record_tool_calls,
    _stage_memory_captures,
)

# Shared single-turn core (blocking POST /chat; the voice route rebases onto it later).
# It reads the patchable ``complete_chat_with_usage`` from this package at call time.
from akana_server.api.routes.chat.turn_core import (  # noqa: F401
    TurnError,
    run_nonstreaming_turn,
)


# The turn gate chain (policy → command → file → skill) moved to chat/gates.py
# (Step B4). __init__ only re-imports the entry points the two chat surfaces
# consume + the backward-compatible names. `plan_skill_turn` stays in the PACKAGE
# namespace: tests patch `routes.chat.plan_skill_turn` and gates.py reads it late
# from the package at call time.
from akana_server.api.routes.chat.gates import (  # noqa: F401
    _GateResult,
    _classify_turn_intent,
    _run_turn_gates,
    plan_skill_turn,
)


# The SSE stream + unbreakable-response (active turn) machinery moved to
# chat/streaming.py (Step B4-2). __init__ only re-imports the names consumed by the
# lifespan/conversations/voice hooks + the external/monkeypatch surface. Names patched in
# the PACKAGE namespace (stream_user_chat / _run_turn_gates /
# _abort_bridge_run_for_conversation / _start_detached_chat_turn) are read LATE from
# the package at call time inside the submodules — a setattr on `routes.chat` resolves
# to the same object.
from akana_server.api.routes.chat.streaming import (  # noqa: F401
    _ActiveTurn,
    _abort_bridge_run_for_conversation,
    _active_turns,
    _append_chunk,
    _broadcast_queue_updated,
    _cancel_active_turn_impl,
    _chat_cleanup_tombstones,
    _conversation_chat_usable,
    _extract_turn_id,
    _follow_turn,
    _is_turn_running,
    _maybe_drain_queue,
    _reset_cursor_bridge_for_conversation,
    _spawn_background,
    _sse_command_response,
    _start_detached_chat_turn,
    _stream_chat_response,
    cleanup_conversation_chat_state,
    shutdown_active_turns,
    shutdown_background_tasks,
)
# DISTINCT RuntimeError subtypes raised by the detached-turn starter: a race
# (TurnAlreadyRunning → 202/queue) and a deleted conversation (ConversationNotUsable →
# 404) are handled separately. They are defined in chat_detached; since the streaming
# facade doesn't re-export the classes, they are imported directly from the source for
# type identity.
from akana_server.api.routes.chat.chat_detached import (  # noqa: F401
    ConversationNotUsable,
    TurnAlreadyRunning,
)


# The 8 route handlers + their response models moved to chat/routes.py (ARCH-03). The
# router is mounted from there; the handler/model names are re-exported so external
# accesses (`routes.chat.post_chat` / `routes.chat.ConversationOut`) keep working.
from akana_server.api.routes.chat.routes import (  # noqa: F401
    ConversationOut,
    ConversationTurnOut,
    cancel_chat_active,
    get_chat_active,
    get_chat_queue,
    get_context_preview,
    get_conversation,
    post_chat,
    post_chat_stream,
    recover_chat_bridge,
    reset_conversation,
    router,
)
