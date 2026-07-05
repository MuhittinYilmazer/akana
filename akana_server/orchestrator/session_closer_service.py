"""SessionCloser server bridge — background cron for idle conversation summaries (M3.2).

`src/akana/memory/session_closer.py` requires an injected LLM (sync callable);
this module builds that bridge: the summariser is wired to the provider-aware
``llm_dispatch.complete_chat_with_usage`` on the main event loop via
``run_coroutine_threadsafe`` — while ``SessionCloser.close`` runs in a worker thread
the main loop stays free (no deadlock). If the memory layer is unavailable or a
summary fails, the cron logs silently; no errors ever leak into the server flow.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from akana_server.config import Settings
from akana_server.memory_core import get_memory_core
from akana_server.orchestrator import llm_dispatch  # noqa: F401  (re-exported for tests)
from akana_server.orchestrator._bridge_cron import (
    build_summarize as _build_summarize,
    poll_loop as _poll_loop,
    resolve_runtime as _resolve_runtime,
    start_task as _start_task,
    stop_task as _stop_task,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

log = logging.getLogger(__name__)

_TASK_ATTR = "session_closer_task"


async def run_once(settings: Settings) -> int:
    """Single scan: find idle conversations and stage each summary into the inbox.

    If K30 ``allow_direct`` ("remember without approval") is ON, staged synthesis
    candidates are promoted to permanent memory IMMEDIATELY without waiting in the
    inbox — design contract: "if the owner enables allow_direct, approvals are
    skipped entirely". The setting is read FRESH on every scan turn via
    ``load_memory_settings``; the toggle in Studio takes effect without a restart
    (SessionCloser stays pure: it always stages; the promote decision is made in
    this service layer).

    The Memory Studio ``session_summary`` toggle (also read FRESH here) is the
    user-facing on/off for summarization: when OFF, the scan is skipped entirely
    (no summary is staged), independent of the runtime ``session_closer_enabled``
    master switch. Reading it here (not in ``session_closer_active``) keeps the
    toggle live without a restart and leaves the cron loop's runtime gate intact.
    """
    from akana.memory import SessionCloser, find_idle_conversations
    from akana.memory.settings import load_memory_settings

    from akana_server.runtime_settings import get_runtime

    # Fresh read each scan (no restart needed): one load covers BOTH the session-summary
    # gate and the allow_direct promote decision below.
    mem_settings = load_memory_settings(settings.data_dir)
    if not mem_settings.session_summary:
        return 0  # Memory Studio "session summarization" toggle is OFF
    memory = get_memory_core(settings.data_dir)
    idle_ids = await asyncio.to_thread(
        find_idle_conversations,
        memory,
        idle_minutes=int(get_runtime("session_closer_idle_minutes", settings)),
        turn_threshold=int(get_runtime("session_closer_turn_threshold", settings)),
        char_threshold=int(get_runtime("session_closer_char_threshold", settings)),
    )
    if not idle_ids:
        return 0
    # allow_direct from the same fresh read above (env override in settings.py still wins).
    allow_direct = mem_settings.allow_direct
    lang = str(get_runtime("language", settings) or "en").strip().lower()
    closer = SessionCloser(
        memory,
        _build_summarize(settings, asyncio.get_running_loop()),
        language=lang if lang in ("tr", "en") else "en",
        max_chars=int(get_runtime("session_closer_max_chars", settings)),
    )
    curator = memory.make_curator() if allow_direct else None
    staged = 0
    promoted = 0
    for cid in idle_ids:
        candidates = await asyncio.to_thread(closer.close, cid)
        if not candidates:
            continue
        staged += len(candidates)
        if curator is not None:
            # Remember-without-approval: promote synthesis candidates to permanent
            # memory immediately, without waiting in the inbox (each atomic piece is
            # promoted SEPARATELY; conflict superseding + event broadcasting same).
            for candidate in candidates:
                try:
                    fact = await asyncio.to_thread(curator.promote, candidate.id)
                    if fact is not None:
                        promoted += 1
                except Exception:  # promote failure must not break the scan; staged entry remains
                    log.warning(
                        "session_closer: automatic promote failed (staged=%s)",
                        candidate.id,
                        exc_info=True,
                    )
    if staged:
        if allow_direct:
            log.info(
                "session_closer: %d/%d idle conversations summarised — %d promoted directly (allow_direct on)",
                staged,
                len(idle_ids),
                promoted,
            )
        else:
            log.info("session_closer: %d/%d idle conversations summarised (inbox)", staged, len(idle_ids))
    return staged


#: Check cadence for re-enabling when disabled (for runtime on/off toggling).
_DISABLED_CHECK_SECONDS = 60.0
_DEFAULT_ENABLED = True
_DEFAULT_INTERVAL = 300.0
_MIN_INTERVAL = 30.0


def session_closer_active(settings: Settings) -> bool:
    """RuntimeSettings resolution: enabled AND interval > 0 (on/off without restart)."""
    enabled = bool(
        _resolve_runtime("session_closer_enabled", settings, default=_DEFAULT_ENABLED)
    )
    interval = float(
        _resolve_runtime("session_closer_interval", settings, default=_DEFAULT_INTERVAL)
    )
    return enabled and interval > 0


def _interval_seconds(settings: Settings) -> float:
    interval = float(
        _resolve_runtime("session_closer_interval", settings, default=_DEFAULT_INTERVAL)
    )
    return max(_MIN_INTERVAL, interval)


def start_session_closer(app: FastAPI) -> None:
    """The loop is always set up; the active gate checks the runtime setting
    (can be enabled from settings without a restart even if disabled in the env)."""
    settings: Settings = app.state.settings
    _start_task(
        app,
        _TASK_ATTR,
        _poll_loop(
            settings,
            log=log,
            name="session_closer",
            is_active=session_closer_active,
            interval_seconds=_interval_seconds,
            disabled_check_seconds=_DISABLED_CHECK_SECONDS,
            run_once=run_once,
        ),
    )


async def stop_session_closer(app: FastAPI) -> None:
    await _stop_task(app, _TASK_ATTR)
