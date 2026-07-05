"""SSE stream + unbreakable-response (active turn) machine — a THIN FACADE (the Step B4-2 split).

WHY A FACADE: this module had grown into a ~1619-line god-file (it tripped the
architecture guards twice). It was split into four cohesive modules by RESPONSIBILITY;
behavior is UNCHANGED — only the code moved:

  * ``chat_state``        — the running-turn registries + the ``_ActiveTurn`` buffer +
                            ``_DropOldestQueue`` + the busy/usable predicates.
  * ``chat_bridge``       — Cursor bridge abort/close/reset.
  * ``chat_commands_sse`` — the one-shot SSE triple for a command/plan response.
  * ``chat_producer``     — the live SSE producer (``_stream_chat_response``).
  * ``chat_detached``     — the client-independent turn machine (``_run_turn_detached``
                            + ``_follow_turn`` + drain + shutdown + cancel/cleanup).

This file re-exports ALL the symbols that USED to be imported via ``...chat.streaming``;
the existing ``from ...streaming import X`` call sites (``__init__``, ``persist``, the
in-function imports in ``_base``, ``test_streaming_bounded_queue``) keep working
unchanged. Symbol IDENTITY (the same function object) is preserved across the re-import
chain → the test monkeypatch surface in the package namespace (``routes.chat`` setattr)
is not broken.

LAYERING: this facade is now an upper-L node that imports the submodules DOWNWARD
(``chat_state`` < ``chat_bridge``/``chat_commands_sse`` < ``chat_producer`` <
``chat_detached`` < ``streaming`` < ``persist`` < ``__init__``). No submodule imports
the package itself (``__init__``) at module level — no cycle.
"""

from __future__ import annotations

from akana_server.api.routes.chat.chat_state import (
    _CANCEL_AWAIT_TIMEOUT,
    _TTS_QUEUE_MAX,
    _ActiveTurn,
    _DropOldestQueue,
    _active_turns,
    _append_chunk,
    _background_tasks,
    _broadcast_queue_updated,
    _cancel_nonstreaming_turn,
    _chat_cleanup_tombstones,
    _conversation_chat_usable,
    _cursor_breaker_open,
    _extract_turn_id,
    _is_turn_running,
    _nonstreaming_busy,
    _register_nonstreaming_turn,
    _release_nonstreaming_turn,
    _spawn_background,
    _synthetic_request,
)
from akana_server.api.routes.chat.chat_bridge import (
    _abort_bridge_run_for_conversation,
    _close_bridge_session,
    _reset_cursor_bridge_for_conversation,
)
from akana_server.api.routes.chat.chat_commands_sse import (
    _command_sse_chunks,
    _sse_command_response,
)
from akana_server.api.routes.chat.chat_producer import _stream_chat_response
from akana_server.api.routes.chat.chat_detached import (
    _cancel_active_turn_impl,
    _command_turn_gen,
    _follow_turn,
    _maybe_drain_queue,
    _run_turn_detached,
    _start_detached_chat_turn,
    _start_detached_command_turn,
    cleanup_conversation_chat_state,
    shutdown_active_turns,
    shutdown_background_tasks,
)

__all__ = [
    "_ActiveTurn",
    "_CANCEL_AWAIT_TIMEOUT",
    "_DropOldestQueue",
    "_TTS_QUEUE_MAX",
    "_abort_bridge_run_for_conversation",
    "_active_turns",
    "_append_chunk",
    "_background_tasks",
    "_broadcast_queue_updated",
    "_cancel_active_turn_impl",
    "_cancel_nonstreaming_turn",
    "_chat_cleanup_tombstones",
    "_close_bridge_session",
    "_command_sse_chunks",
    "_command_turn_gen",
    "_conversation_chat_usable",
    "_cursor_breaker_open",
    "_extract_turn_id",
    "_follow_turn",
    "_is_turn_running",
    "_maybe_drain_queue",
    "_nonstreaming_busy",
    "_register_nonstreaming_turn",
    "_release_nonstreaming_turn",
    "_reset_cursor_bridge_for_conversation",
    "_run_turn_detached",
    "_spawn_background",
    "_sse_command_response",
    "_start_detached_chat_turn",
    "_start_detached_command_turn",
    "_stream_chat_response",
    "_synthetic_request",
    "cleanup_conversation_chat_state",
    "shutdown_active_turns",
    "shutdown_background_tasks",
]
