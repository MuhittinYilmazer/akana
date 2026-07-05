"""Durable ledger — the P8 replay door made real.

In M1 the façade grew an in-memory event seam (:class:`MemoryEvent` +
subscribers). This is the durable end of that seam: a thread-safe, append-only
JSONL log at ``<data_dir>/event_log.jsonl``. Attached as a subscriber, it
captures the full mutation lifecycle (turns, facts, invalidations, resets) so
the history can be replayed, audited, or projected without touching the live
stores. Ported from the legacy ``event_log.py`` (F2).

It only *consumes* events; it never emits, so wiring it in can't loop.

Rotation
--------
The active file is size-capped (``max_bytes``, default 8 MiB). Before each
write — inside the lock, via one cheap ``os.stat`` — an oversized active file
is renamed to ``event_log.1.jsonl``; existing archives shift ``1 → 2 → …`` up
to ``keep`` (default 3) and the oldest archive is dropped. The write then
lands in a fresh active file.

Two-process note (server + MCP subprocess sharing the path): the rename is
atomic, but a process still holding an old fd keeps appending — via
``O_APPEND`` — into the now-archived file. Such lines simply land in the
archive and stay readable in order, which is acceptable; nothing interleaves
or tears. The window is tiny anyway because each :meth:`record` opens a fresh
fd per line. A failed rotation is logged and NEVER blocks the write itself.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Iterator
from pathlib import Path

from akana.memory._time import iso_now
from akana.memory.events import MemoryEvent

log = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 8 * 1024 * 1024  # rotate the active file past 8 MiB
DEFAULT_KEEP = 3  # archives retained: event_log.1.jsonl … event_log.3.jsonl

_TAIL_BLOCK = 64 * 1024  # backwards-read block size
_COUNT_BLOCK = 1024 * 1024  # newline-count block size


class MemoryLedger:
    """Thread-safe append-only JSONL writer/reader for memory events.

    ``max_bytes``/``keep`` control rotation (see module docstring). ``keep=0``
    means an oversized active file is simply dropped on rotation.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        keep: int = DEFAULT_KEEP,
    ) -> None:
        self._path = path.resolve()
        self._lock = threading.Lock()
        self._max_bytes = int(max_bytes)
        self._keep = max(0, int(keep))

    @classmethod
    def for_data_dir(
        cls,
        data_dir: Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        keep: int = DEFAULT_KEEP,
    ) -> MemoryLedger:
        return cls(data_dir / "event_log.jsonl", max_bytes=max_bytes, keep=keep)

    @property
    def path(self) -> Path:
        return self._path

    def paths(self) -> list[Path]:
        """Existing ledger files, oldest first (archives ``keep → 1``, then
        the active file) — i.e. the order :meth:`read_all` replays them.
        Meant for Studio/stats; files may vanish between this call and a
        ``stat`` if another process rotates."""
        out = [
            p
            for i in range(self._keep, 0, -1)
            if (p := self._archive_path(i)).is_file()
        ]
        if self._path.is_file():
            out.append(self._path)
        return out

    def _archive_path(self, i: int) -> Path:
        return self._path.with_name(f"{self._path.stem}.{i}{self._path.suffix}")

    @staticmethod
    def _iso_now() -> str:
        return iso_now()

    # -- write ----------------------------------------------------------------

    def record(self, event: MemoryEvent) -> MemoryEvent:
        """Append one event (the subscriber entry point)."""
        line = (
            json.dumps(
                {"kind": event.kind, "ts": event.ts, "data": event.data},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # One O_APPEND os.write per line: atomic-enough across the server
            # process AND the MCP subprocess sharing this file (no interleave).
            data = line.encode("utf-8")
            with self._lock:
                try:
                    self._maybe_rotate()
                except Exception as e:  # rotation must never block the write
                    log.warning("ledger rotation failed %s: %s", self._path, e)
                fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
        except OSError as e:  # a failed ledger write must never break a memory write
            log.warning("ledger append failed %s: %s", self._path, e)
        return event

    def append(
        self, kind: str, *, data: dict | None = None, ts: str | None = None
    ) -> MemoryEvent:
        """Convenience: build + record an event from primitives."""
        return self.record(
            MemoryEvent(kind=kind, ts=ts or self._iso_now(), data=data or {})
        )

    def _maybe_rotate(self) -> None:
        """Rotate the active file once it exceeds ``max_bytes``.

        Caller holds the lock. One ``os.stat`` on the hot path; the shift
        ``event_log.jsonl → .1 → .2 → …`` uses atomic ``os.replace`` (the
        rename onto ``.keep`` drops the oldest archive).
        """
        try:
            size = os.stat(self._path).st_size
        except OSError:  # no active file yet — nothing to rotate
            return
        if size <= self._max_bytes:
            return
        if self._keep <= 0:
            self._path.unlink(missing_ok=True)
            return
        for i in range(self._keep - 1, 0, -1):
            src = self._archive_path(i)
            if src.is_file():
                os.replace(src, self._archive_path(i + 1))
        os.replace(self._path, self._archive_path(1))

    # -- read / replay --------------------------------------------------------

    def read_all(
        self, *, kind: str | None = None, limit: int | None = None
    ) -> list[MemoryEvent]:
        """Replay the log in write order; optionally filter by kind / tail.

        ``limit=None`` reads everything — archives oldest-first, then the
        active file (full replay). With a positive ``limit`` the files are
        scanned *backwards* from the active tail (block-wise seek, decode-safe)
        and only as far as needed, so explain-style calls never parse the whole
        history; the result is still chronological.
        """
        if limit is None or limit <= 0:
            out: list[MemoryEvent] = []
            for p in self.paths():
                self._read_forward(p, kind, out)
            return out

        newest_first: list[MemoryEvent] = []
        for p in reversed(self.paths()):  # active, then archives 1 → keep
            try:
                for raw in self._iter_lines_backwards(p):
                    try:
                        text = raw.decode("utf-8")
                    except UnicodeDecodeError:  # torn multi-byte tail etc.
                        continue
                    ev = self._parse_line(text, kind)
                    if ev is None:
                        continue
                    newest_first.append(ev)
                    if len(newest_first) >= limit:
                        newest_first.reverse()
                        return newest_first
            except OSError as e:  # file rotated away mid-read — keep going
                log.warning("ledger tail read failed %s: %s", p, e)
        newest_first.reverse()
        return newest_first

    def count(self) -> int:
        """Cheap line count over active + archives (binary newline scan; no
        JSON parsing, so blank/corrupt lines count too)."""
        total = 0
        for p in self.paths():
            try:
                with p.open("rb") as f:
                    while chunk := f.read(_COUNT_BLOCK):
                        total += chunk.count(b"\n")
            except OSError as e:
                log.warning("ledger count failed %s: %s", p, e)
        return total

    # -- parse helpers ----------------------------------------------------------

    def _read_forward(
        self, path: Path, kind: str | None, out: list[MemoryEvent]
    ) -> None:
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    ev = self._parse_line(line, kind)
                    if ev is not None:
                        out.append(ev)
        except OSError as e:
            log.warning("ledger read failed %s: %s", path, e)

    @staticmethod
    def _parse_line(line: str, kind: str | None) -> MemoryEvent | None:
        line = line.strip()
        if not line:
            return None
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(raw, dict):
            return None
        if kind is not None and raw.get("kind") != kind:
            return None
        return MemoryEvent(
            kind=str(raw.get("kind", "")),
            ts=str(raw.get("ts", "")),
            data=raw.get("data") if isinstance(raw.get("data"), dict) else {},
        )

    @staticmethod
    def _iter_lines_backwards(path: Path) -> Iterator[bytes]:
        """Yield complete lines (sans ``\\n``) last-to-first, block-wise.

        Splitting on ``b"\\n"`` is UTF-8 safe (0x0A never occurs inside a
        multi-byte sequence); decoding is the caller's job.
        """
        with path.open("rb") as f:
            pos = f.seek(0, os.SEEK_END)
            buf = b""
            while pos > 0:
                step = min(_TAIL_BLOCK, pos)
                pos -= step
                f.seek(pos)
                lines = (f.read(step) + buf).split(b"\n")
                buf = lines[0]  # head fragment — completed by the next block
                yield from reversed(lines[1:])
            if buf:
                yield buf
