"""Uvicorn entrypoint."""

from __future__ import annotations

import asyncio
import logging
import sys

import uvicorn

# RE-EXPORT (not F401): uvicorn loads "akana_server.main:app" as a string below →
# `app` MUST be present in this module's namespace. ruff cannot see the string
# reference and would treat it as unused; removing it causes the server to fail with
# "Attribute 'app' not found" (polish regression).
from akana_server.api.app import app  # noqa: F401
from akana_server.config import load_settings


def main() -> None:
    settings = load_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO))
    # WINDOWS CRITICAL (audit C1): asyncio subprocess (LLM bridge / claude CLI spawn —
    # every chat turn) ONLY works on the Proactor event loop. When uvicorn[standard]
    # picks its own loop (often Selector), create_subprocess_exec raises
    # NotImplementedError → EVERY chat message fails. Force the Proactor policy and
    # pass loop="asyncio" to uvicorn so it does not override us with its own
    # implementation (uvloop is not available on Windows anyway).
    loop_impl = "auto"
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        loop_impl = "asyncio"
    uvicorn.run(
        "akana_server.main:app",
        host=settings.server_host,
        port=settings.server_port,
        reload=False,
        loop=loop_impl,
    )


if __name__ == "__main__":
    main()
