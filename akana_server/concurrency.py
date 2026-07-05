"""Small event-loop concurrency helpers, shared and dependency-free.

Lives at the package root (no imports into ``api/routes``, ``orchestrator`` or
``voice``) so every layer — chat routes, the LLM providers and the voice
bridges — can offload blocking work without copy-pasting the wrapper or taking
a cross-layer dependency.
"""

from __future__ import annotations

import asyncio
import functools


async def off_loop(fn, *args, **kwargs):
    """P0 live stability: move synchronous sqlite/file side effects off the loop.

    If every synchronous write on a hot path (policy.db ``busy_timeout=10000``,
    audit.jsonl, ledger, memory.db persist) runs ON the event loop, then under
    disk slowness / a concurrent writer the WHOLE server (every endpoint + WS)
    freezes until the write finishes — which the user sees as "the page crashed
    while the response was streaming". This wrapper moves the side effect to a
    worker thread; the loop keeps serving SSE/WS.
    """
    return await asyncio.to_thread(functools.partial(fn, *args, **kwargs))
