"""SummaryConsolidation server bridge — background cron for cross-session summary
consolidation (M3.3).

`src/akana/memory/summary_consolidation.py` requires an injected LLM (sync
callable); this module builds that bridge exactly like ``session_closer_service``:
the summariser is wired to the provider-aware ``llm_dispatch.complete_chat_with_usage``
on the main event loop via ``run_coroutine_threadsafe`` — while the consolidation pass
runs in a worker thread the main loop stays free (no deadlock). Consolidation is pure
housekeeping, so the loop runs on a LONG interval (default hourly). If the memory layer
is unavailable or a merge fails, the cron logs silently; no errors ever leak into the
server flow.

Runtime setting (resolved fresh each turn via the registered schema entry; if
resolution ever fails this falls back to the Settings attr / env / default
defensively — the same try/except pattern as ``session_closer_service``):

* ``summary_consolidation_enabled`` (bool, default True)
* ``summary_consolidation_interval`` (float seconds, default 3600 = hourly; 0 = off)
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
    resolve_runtime as _runtime,
    start_task as _start_task,
    stop_task as _stop_task,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

log = logging.getLogger(__name__)

_TASK_ATTR = "summary_consolidation_task"

#: Defaults used when the runtime schema entry is not yet registered (env override
#: still wins via getattr on Settings). Consolidation is housekeeping → hourly.
_DEFAULT_ENABLED = True
_DEFAULT_INTERVAL = 3600.0
#: Floor so a misconfigured tiny interval can't busy-loop the housekeeping pass.
_MIN_INTERVAL = 300.0
#: Check cadence for re-enabling when disabled (runtime on/off without restart).
_DISABLED_CHECK_SECONDS = 60.0


def consolidation_active(settings: Settings) -> bool:
    """Resolution: enabled AND interval > 0 (runtime on/off without restart)."""
    enabled = bool(_runtime("summary_consolidation_enabled", settings, default=_DEFAULT_ENABLED))
    interval = float(_runtime("summary_consolidation_interval", settings, default=_DEFAULT_INTERVAL))
    return enabled and interval > 0


def _interval_seconds(settings: Settings) -> float:
    interval = float(
        _runtime("summary_consolidation_interval", settings, default=_DEFAULT_INTERVAL)
    )
    return max(_MIN_INTERVAL, interval)


async def run_once(settings: Settings) -> int:
    """Single pass: group overlapping pending session summaries and stage one
    consolidated topic candidate per group. Returns the number staged.

    The consolidator always STAGES (synthesis); promotion/superseding stays a curator/
    inbox decision like the rest of the system (consolidation candidates carry
    ``source_fact_ids`` and are dedup-exempt). Errors are swallowed inside the
    consolidator; this layer only orchestrates the thread hop + language resolution.

    Gated by the Memory Studio ``session_summary`` toggle (read FRESH each pass, no
    restart): consolidation produces summary-derived inbox items, so when the user turns
    session summarization OFF it is skipped entirely — independent of the
    ``summary_consolidation_enabled`` runtime switch. This keeps the one toggle meaning
    "no session-summary inbox activity" (mirrors ``session_closer_service.run_once``).
    """
    from akana.memory.settings import load_memory_settings
    from akana.memory.summary_consolidation import SummaryConsolidator

    if not load_memory_settings(settings.data_dir).session_summary:
        return 0  # Memory Studio "session summarization" toggle is OFF
    memory = get_memory_core(settings.data_dir)
    lang = str(_runtime("language", settings, default="en") or "en").strip().lower()
    min_overlap = int(_runtime("summary_consolidation_min_overlap", settings, default=2))
    consolidator = SummaryConsolidator(
        memory,
        _build_summarize(settings, asyncio.get_running_loop()),
        language=lang if lang in ("tr", "en") else "en",
        min_overlap=min_overlap,
    )
    staged = await asyncio.to_thread(consolidator.consolidate)
    if staged:
        log.info("summary_consolidation: %d topic candidate(s) staged (inbox)", len(staged))
    return len(staged)


def start_summary_consolidation(app: FastAPI) -> None:
    """The loop is always set up; the active gate checks the runtime setting (can be
    enabled from settings without a restart even if disabled in the env)."""
    settings: Settings = app.state.settings
    _start_task(
        app,
        _TASK_ATTR,
        _poll_loop(
            settings,
            log=log,
            name="summary_consolidation",
            is_active=consolidation_active,
            interval_seconds=_interval_seconds,
            disabled_check_seconds=_DISABLED_CHECK_SECONDS,
            run_once=run_once,
        ),
    )


async def stop_summary_consolidation(app: FastAPI) -> None:
    await _stop_task(app, _TASK_ATTR)
