"""Bug-hunt regression: a non-positive idle timeout must mean "wait indefinitely".

``combine_cap`` yields ``0`` as the documented "no idle ceiling" sentinel (the
disabled knob, e.g. ``CURSOR_BRIDGE_TIMEOUT=0``). ``read_ndjson_line`` used to
pass that ``0`` straight to ``asyncio.wait_for``, which raises ``TimeoutError``
IMMEDIATELY (before any bytes arrive) → every cursor turn failed with a false
504 the instant it started — the exact opposite of the "0 = disabled" contract.
``read_ndjson_line`` now mirrors ``claude_provider._read_line``'s guard: treat a
non-positive timeout as "wait indefinitely".
"""

from __future__ import annotations

import asyncio

import pytest

from akana_server.orchestrator.base import read_ndjson_line


def test_read_ndjson_line_timeout_zero_waits_instead_of_instant_timeout() -> None:
    """timeout=0 must NOT instantly raise TimeoutError — it means "no ceiling".

    Feed the line only AFTER a tiny delay: the pre-fix code would have raised
    ``TimeoutError`` before that delay elapsed; the fixed code waits and returns
    the line. Fast + deterministic: fakes are created inside the running loop.
    """

    async def run() -> bytes:
        reader = asyncio.StreamReader()

        async def _feed_later() -> None:
            # The line arrives AFTER read_ndjson_line is already awaiting, so a
            # timeout=0 that mapped to wait_for would have fired long before this.
            await asyncio.sleep(0.02)
            reader.feed_data(b'{"ev":"done"}\n')

        feeder = asyncio.create_task(_feed_later())
        try:
            # timeout=0 → "wait indefinitely"; must return the delayed line.
            return await read_ndjson_line(reader, 0)
        finally:
            await feeder

    line = asyncio.run(run())
    assert line == b'{"ev":"done"}\n'


def test_read_ndjson_line_negative_timeout_also_waits() -> None:
    """A negative timeout is likewise treated as "wait indefinitely" (<= 0)."""

    async def run() -> bytes:
        reader = asyncio.StreamReader()

        async def _feed_later() -> None:
            await asyncio.sleep(0.02)
            reader.feed_data(b'{"ev":"delta","text":"hi"}\n')

        feeder = asyncio.create_task(_feed_later())
        try:
            return await read_ndjson_line(reader, -1.0)
        finally:
            await feeder

    line = asyncio.run(run())
    assert line == b'{"ev":"delta","text":"hi"}\n'


def test_read_ndjson_line_positive_timeout_still_enforced() -> None:
    """A positive timeout is still honored: a silent stream raises TimeoutError."""

    async def run() -> None:
        reader = asyncio.StreamReader()  # never fed → stays silent
        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await read_ndjson_line(reader, 0.02)

    asyncio.run(run())
