"""Shared bridge/scheduling skeleton for the memory background crons.

``session_closer_service`` and ``summary_consolidation_service`` are two thin
FastAPI-lifespan wrappers around ``src/akana/memory`` passes that both need an
injected sync LLM callable and a runtime-toggleable poll loop. This module
factors out the parts that were byte-identical (or structurally identical)
between the two: the ``run_coroutine_threadsafe`` summarizer bridge, the
defensive runtime-setting resolver, and the active-gate poll loop with
start/stop task lifecycle.

Each service still owns its own ``run_once`` (the actual memory pass differs)
and its own public ``start_*``/``stop_*``/``*_active`` names — this module is
an internal implementation detail, not a new public surface.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Awaitable, Callable

from akana_server.config import Settings
from akana_server.orchestrator import llm_dispatch

if TYPE_CHECKING:
    from fastapi import FastAPI

#: complete_chat has its own internal timeout; this is only the worker-thread safety belt.
SUMMARIZE_HARD_TIMEOUT = 360.0


def build_summarize(settings: Settings, loop: asyncio.AbstractEventLoop) -> Callable[[str], str]:
    """Wire a sync ``summarize(prompt) -> text`` callable to the provider-aware
    ``llm_dispatch.complete_chat_with_usage`` on ``loop`` via
    ``run_coroutine_threadsafe`` — safe to call from a worker thread while the
    main loop stays free (no deadlock)."""

    def summarize(prompt: str) -> str:
        future = asyncio.run_coroutine_threadsafe(
            llm_dispatch.complete_chat_with_usage(
                settings, prompt, chat_mode=False, reuse_agent=False
            ),
            loop,
        )
        text, _usage = future.result(timeout=SUMMARIZE_HARD_TIMEOUT)
        return text

    return summarize


def resolve_runtime(key: str, settings: Settings, *, default: object) -> object:
    """Resolve a runtime setting defensively: normal path is ``get_runtime``;
    should resolution ever raise, fall back to the Settings attr (env-or-default)
    and finally the literal default — settings resolution must never break a cron."""
    try:
        from akana_server.runtime_settings import get_runtime

        return get_runtime(key, settings)
    except Exception:
        return getattr(settings, key, default)


async def poll_loop(
    settings: Settings,
    *,
    log: logging.Logger,
    name: str,
    is_active: Callable[[Settings], bool],
    interval_seconds: Callable[[Settings], float],
    disabled_check_seconds: float,
    run_once: Callable[[Settings], Awaitable[int]],
) -> None:
    """Active-gate poll loop shared by both crons: sleep for the active interval
    (or the disabled-check cadence when inactive), then re-check the gate and run
    one pass. Exceptions from ``run_once`` are logged and swallowed so the cron
    always retries on the next turn; cancellation propagates."""
    while True:
        active = is_active(settings)
        await asyncio.sleep(interval_seconds(settings) if active else disabled_check_seconds)
        try:
            if is_active(settings):
                await run_once(settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s: pass failed; will retry on next turn", name)


def start_task(app: "FastAPI", task_attr: str, coro: Awaitable[None]) -> None:
    """The loop is always set up; the active gate (inside the loop) checks the
    runtime setting so it can be enabled from settings without a restart even if
    disabled in the env."""
    setattr(app.state, task_attr, asyncio.create_task(coro))


async def stop_task(app: "FastAPI", task_attr: str) -> None:
    task = getattr(app.state, task_attr, None)
    if isinstance(task, asyncio.Task) and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
