"""Shared primitives for the small typed-JSON settings stores.

``llm_settings`` and ``voice_preferences`` are both single-file, read-modify-write
JSON stores guarded against two-tab / two-device concurrent writes. They evolved
by copy-paste and carried byte-identical copies of the per-``data_dir`` lock
registry and the crash-safe atomic-write block; the pid+uuid unique-tmp fix had
to be applied to both. This module is the single home for those two primitives so
the fix (and any future write-path change) lives in one place.

Each store keeps its own dataclass, merge/validation and public API — only these
low-level mechanics are shared. Stores with genuinely different write semantics
(``runtime_settings/store.py`` in-object lock + cache, ``vault_crypto`` 0600 +
Windows retry, ``files/service`` backup + oplog) are deliberately NOT folded in
here.

Cross-process safety
--------------------
The original :func:`lock_for` returns a :class:`threading.Lock`, which serialises
read-modify-write only WITHIN a single interpreter. That is enough for the
settings stores (only the server touches them). It is NOT enough for the
``akana_schedule`` MCP server, which runs as a SEPARATE CHILD
PROCESS (``scripts/mcp_schedule.py``) yet
read-modify-writes the very same ``schedules.json`` that the
in-server schedule engine also writes. A threading lock is invisible
across process boundaries, so two processes each did ``load → modify → write`` and
the last writer clobbered the other's update (lost creates, lost ``next_run_at``
advances, lost ``done`` finalisations, quota bypass — and, on Windows, an outright
store WIPE when a transient sharing-violation ``PermissionError`` mid-``os.replace``
was mistaken for "no data").

:func:`file_lock` and :func:`cross_process_lock` add an OS-level advisory lock
(``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows) on a sidecar
``<file>.lock`` so a read-modify-write is atomic ACROSS processes too. They are
ADDITIVE and OPT-IN: existing threading-lock callers are completely unaffected
unless they explicitly wrap their mutation in :func:`cross_process_lock`. Only the
two multi-process stores adopt it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

try:  # fcntl is POSIX-only; the cross-process lock degrades to a no-op without it.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX (e.g. Windows)
    fcntl = None  # type: ignore[assignment]

try:  # msvcrt is Windows-only — the cross-process backend where fcntl is absent.
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows
    msvcrt = None  # type: ignore[assignment]

#: How long :func:`file_lock` will keep polling for the OS advisory lock before it
#: gives up and raises. A cross-process read-modify-write of one small JSON file is
#: sub-millisecond, so real contention windows are tiny; this ceiling only bounds a
#: pathological hang (a wedged peer holding the lock) so a caller fails loudly with a
#: clear error instead of blocking forever.
_LOCK_ACQUIRE_DEADLINE_S = 10.0

#: Poll interval while spinning for the advisory lock (non-blocking attempt + sleep).
_LOCK_POLL_S = 0.05

#: Windows ``os.replace`` (MoveFileEx) raises ``PermissionError`` (WinError 5 /
#: WinError 32 sharing violation) if ANY other process holds a handle open on the
#: destination — e.g. a peer process reading the store, or its own tmp mid-swap.
#: Retry the rename for up to this long so a brief concurrent open does not fail the
#: write. Mirrors ``akana_server.vault_crypto.write_private_bytes_atomic``. No-op on
#: POSIX, where replacing over an open file is always allowed and this never fires.
_WIN_REPLACE_DEADLINE_S = 1.5


# Per-``data_dir`` process-wide lock registry. Serializes the read-modify-write
# across concurrent writers (two tabs/devices PATCHing at once): without a lock +
# with a shared ".json.tmp" name the ``os.replace`` steps race
# (FileNotFoundError → HTTP 500) and updates are lost.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def lock_for(data_dir: Path) -> threading.Lock:
    """The process-wide lock for a ``data_dir`` (created on first use)."""
    key = str(data_dir)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


@contextlib.contextmanager
def file_lock(lock_path: Path | str):
    """Best-effort cross-process EXCLUSIVE lock on a sidecar file.

    Serialises the read-modify-write of a shared JSON store ACROSS processes — the
    in-server schedule engine on one side and the
    ``akana_schedule`` MCP child processes on the other, both mutating the same file.
    A :class:`threading.Lock` cannot do this (it is invisible across an interpreter
    boundary); an OS advisory lock can.

    Mechanism: open (create-if-missing) ``lock_path`` and take an advisory lock on
    it — ``fcntl.flock(LOCK_EX)`` on POSIX, ``msvcrt.locking`` on Windows. Both are
    tried NON-BLOCKING inside a bounded poll loop so the wait is uniformly capped on
    every platform (a plain blocking ``flock`` could hang forever); after
    :data:`_LOCK_ACQUIRE_DEADLINE_S` seconds we raise a clear :class:`TimeoutError`
    rather than block indefinitely.

    NOT reentrant — never nest two ``file_lock`` acquisitions on the SAME path in one
    process (the second would spin against the first and time out). Same-process
    threads are kept off each other's fd by pairing this with the in-process
    threading lock in :func:`cross_process_lock`. Degrades to a plain no-op only if
    NEITHER OS backend is importable (should never happen on a real OS).
    """
    path = Path(lock_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if fcntl is None and msvcrt is None:  # pragma: no cover - no OS lock backend
        yield
        return
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        deadline = time.monotonic() + _LOCK_ACQUIRE_DEADLINE_S
        while True:
            try:
                if fcntl is not None:
                    # LOCK_NB → raise BlockingIOError immediately if another process
                    # holds it, so the poll loop (not the kernel) owns the timeout.
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    # LK_NBLCK → non-blocking single-byte region lock; raises OSError
                    # if already held. (The range may extend past EOF — allowed.)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"could not acquire cross-process lock {path} within "
                        f"{_LOCK_ACQUIRE_DEADLINE_S:.0f}s — another process is holding "
                        "it (or a stale lock was left behind)"
                    ) from None
                time.sleep(_LOCK_POLL_S)
        yield
    finally:
        try:
            # Release ONLY if we actually acquired it — otherwise the unlock itself
            # raises and MASKS the original acquisition error (the real failure).
            if acquired:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                else:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)


@contextlib.contextmanager
def cross_process_lock(data_dir: Path | str, target_path: Path | str):
    """Guard a read-modify-write of ``target_path`` against BOTH threads and processes.

    Holds two locks, in a FIXED order to avoid any inversion:

    1. the in-process :func:`lock_for` threading lock for ``data_dir`` (taken FIRST),
       so two threads in THIS interpreter never both open the sidecar fd and spin
       against each other on the OS lock; and
    2. the cross-process :func:`file_lock` on ``<target_path>.lock`` (taken SECOND),
       so a peer PROCESS mutating the same file cannot interleave its load→write with
       ours.

    Wrap the WHOLE read-modify-write (the strict read, the in-memory edit, and the
    :func:`write_json_atomic` write) in a single ``with`` so the sequence is atomic
    end-to-end: no other thread or process can slip a write between our read and our
    replace, which is exactly what prevents the lost-update / store-wipe failure mode.

    This is the additive, opt-in helper the multi-process stores wrap their mutations
    (and reads) in; single-process callers keep using :func:`lock_for` directly and
    are unaffected.
    """
    tlock = lock_for(Path(data_dir))
    target = Path(target_path)
    sidecar = target.with_name(target.name + ".lock")
    with tlock:
        with file_lock(sidecar):
            yield


def write_json_atomic(path: Path, obj: Any) -> None:
    """Crash-safe JSON write: full write to a unique tmp, then ``os.replace``.

    Even on a crash / colliding write mid-write, the read side never sees half
    JSON (same pattern as ``runtime_settings/store.py``). The tmp name is UNIQUE
    (pid+uuid) so two concurrent writers do not share a tmp file and one's
    ``os.replace`` does not end up "not finding" (FileNotFoundError) the other's.
    On failure (disk full / read-only) the half tmp is cleaned up and the error
    re-raised — thanks to the atomic write the OLD file is preserved (the half
    file is never visible).

    Windows retry: ``os.replace`` (MoveFileEx) raises ``PermissionError`` (WinError
    5 / 32) if another process holds a handle open on the destination at the instant
    of the rename — a brief window that a cross-process peer can hit even under the
    file lock (e.g. a still-draining lock-free reader). The rename is retried to a
    short deadline (:data:`_WIN_REPLACE_DEADLINE_S`) so that transient sharing
    violation does not fail an otherwise-valid write. A NON-permission ``OSError``
    (disk full, read-only) is NOT retried — it is a real, persistent failure and is
    raised immediately. This mirrors ``vault_crypto.write_private_bytes_atomic`` and
    is a no-op on POSIX, where replacing over an open file always succeeds.
    """
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    deadline = time.monotonic() + _WIN_REPLACE_DEADLINE_S
    while True:
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # Transient Windows sharing violation — the destination is momentarily
            # open elsewhere. Back off briefly and retry until the deadline, then
            # give up (cleaning the tmp so we never leak a *.tmp beside the target).
            if time.monotonic() >= deadline:
                with contextlib.suppress(OSError):
                    tmp.unlink()
                raise
            time.sleep(0.05)
        except OSError:
            # A genuine, persistent error (disk full / read-only / bad path). Do not
            # spin on it — clean up and surface it right away.
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise


__all__ = [
    "lock_for",
    "file_lock",
    "cross_process_lock",
    "write_json_atomic",
]
