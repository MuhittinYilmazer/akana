"""Unit tests for the shared JSON-store primitives — the CROSS-PROCESS safety net.

These cover the pieces that make ``schedules.json`` (and any future shared JSON
store) safe to read-modify-write from more than one PROCESS at a time (the
in-server schedule engine AND the ``akana_schedule`` MCP child process):

* :func:`akana_server.json_store.file_lock` — the OS advisory lock (``fcntl.flock``
  on POSIX, ``msvcrt.locking`` on Windows) taken on a sidecar ``<file>.lock``.
* :func:`akana_server.json_store.write_json_atomic` — now with a Windows
  ``os.replace`` retry so a transient sharing violation does not fail a valid write.

"Two processes" are simulated with two THREADS each taking the lock through its OWN
file descriptor: both ``flock`` (per open-file-description) and ``msvcrt.locking``
(per file HANDLE) mutually exclude across descriptors even inside one process, so a
two-thread race is a faithful stand-in for a two-process race — and it deliberately
BYPASSES the in-process threading lock (:func:`lock_for`), which is invisible across
processes and must not be what is under test here.
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from akana_server import json_store
from akana_server.json_store import (
    cross_process_lock,
    file_lock,
    lock_for,
    write_json_atomic,
)


# --------------------------------------------------------------------------- #
# file_lock — mutual exclusion + bounded acquire
# --------------------------------------------------------------------------- #


def test_file_lock_is_exclusive_across_descriptors(tmp_path) -> None:
    """A second acquisition (its own fd) BLOCKS until the first releases.

    This is the cross-process guarantee expressed within one process: the two
    ``file_lock`` calls open independent descriptors, and the OS lock keeps them
    strictly ordered — exactly what stops a peer process interleaving its write.
    """
    lock = tmp_path / "excl.lock"
    got: list[float] = []

    def contender() -> None:
        with file_lock(lock):
            got.append(time.monotonic())

    with file_lock(lock):
        th = threading.Thread(target=contender)
        th.start()
        th.join(timeout=0.3)
        assert got == []  # still blocked while we hold the lock
        time.sleep(0.05)
    th.join(timeout=5)
    assert len(got) == 1  # acquired immediately after we released


def test_file_lock_times_out_with_clear_error(tmp_path, monkeypatch) -> None:
    """When the lock cannot be had in time, raise a clear ``TimeoutError``.

    The deadline is shrunk so the test is fast; the real ceiling only bounds a
    pathological hang so a caller fails loudly instead of blocking forever."""
    monkeypatch.setattr(json_store, "_LOCK_ACQUIRE_DEADLINE_S", 0.2)
    lock = tmp_path / "timeout.lock"
    holding = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with file_lock(lock):
            holding.set()
            release.wait(timeout=3)

    th = threading.Thread(target=holder)
    th.start()
    try:
        assert holding.wait(timeout=3)
        with pytest.raises(TimeoutError):
            with file_lock(lock):
                pass
    finally:
        release.set()
        th.join(timeout=3)


def test_file_lock_serializes_concurrent_rmw_no_lost_writes(tmp_path) -> None:
    """Two "processes" racing read-modify-write under the file lock lose NOTHING.

    Each thread appends its own N items through its OWN lock fd (no shared threading
    lock). If the lock did not mutually exclude, the widened read→write window would
    let one thread's write clobber the other's and the final count would fall short.
    A correct lock keeps all 2*N appends.
    """
    data = tmp_path / "shared.json"
    write_json_atomic(data, {"items": []})
    lock = data.with_name(data.name + ".lock")
    n = 40

    def worker(tag: str) -> None:
        for i in range(n):
            with file_lock(lock):
                items = json.loads(data.read_text(encoding="utf-8"))["items"]
                items.append(f"{tag}-{i}")
                time.sleep(0.0004)  # widen the RMW window to force interleaving
                write_json_atomic(data, {"items": items})

    threads = [threading.Thread(target=worker, args=(t,)) for t in ("A", "B")]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=30)

    final = json.loads(data.read_text(encoding="utf-8"))["items"]
    assert len(final) == 2 * n  # no lost updates
    assert sorted(final) == sorted(
        f"{t}-{i}" for t in ("A", "B") for i in range(n)
    )


def test_unlocked_concurrent_rmw_loses_a_write_control(tmp_path) -> None:
    """Control: the SAME race WITHOUT the lock provably loses an update.

    Two threads read the identical empty snapshot (synchronised on a barrier), each
    append their own item, then write. With no mutual exclusion the second writer
    clobbers the first, so exactly ONE append survives. This is the lost-update bug
    the file lock exists to prevent — and it confirms the positive test above would
    actually catch a broken lock (rather than passing vacuously)."""
    data = tmp_path / "shared_unlocked.json"
    write_json_atomic(data, {"items": []})
    barrier = threading.Barrier(2)

    def rmw(tag: str) -> None:
        items = json.loads(data.read_text(encoding="utf-8"))["items"]
        barrier.wait(timeout=5)  # both threads now hold the same empty snapshot
        items.append(tag)
        write_json_atomic(data, {"items": items})

    threads = [threading.Thread(target=rmw, args=(t,)) for t in ("A", "B")]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=5)

    survived = json.loads(data.read_text(encoding="utf-8"))["items"]
    assert len(survived) == 1  # one update lost to the clobber (no lock)


# --------------------------------------------------------------------------- #
# write_json_atomic — Windows os.replace retry
# --------------------------------------------------------------------------- #


def test_write_json_atomic_retries_replace_on_permission_error(
    tmp_path, monkeypatch
) -> None:
    """A transient ``PermissionError`` from ``os.replace`` is retried, not fatal.

    On Windows the rename raises ``PermissionError`` (WinError 5 / 32) if the
    destination is momentarily open in another process; the write must survive that
    by retrying the rename to a short deadline."""
    target = tmp_path / "retry.json"
    calls = {"n": 0}
    real_replace = os.replace

    def flaky_replace(src, dst, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(5, "Access is denied (simulated sharing violation)")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", flaky_replace)
    write_json_atomic(target, {"ok": True})

    assert calls["n"] == 2  # failed once, retried, then succeeded
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}
    assert list(tmp_path.glob("*.tmp")) == []  # tmp swapped in, none left behind


def test_write_json_atomic_raises_immediately_on_non_permission_oserror(
    tmp_path, monkeypatch
) -> None:
    """A genuine, persistent OSError (e.g. disk full) is NOT retried — surface it.

    Only a Windows sharing violation (``PermissionError``) is transient; anything
    else is a real failure and must be raised at once (after cleaning the tmp so no
    ``*.tmp`` is leaked next to the target)."""
    target = tmp_path / "enospc.json"

    def always_enospc(src, dst, *a, **k):
        raise OSError(28, "No space left on device (simulated)")

    monkeypatch.setattr(os, "replace", always_enospc)
    with pytest.raises(OSError):
        write_json_atomic(target, {"nope": 1})
    assert list(tmp_path.glob("*.tmp")) == []  # tmp cleaned up on failure


# --------------------------------------------------------------------------- #
# cross_process_lock — holds BOTH the threading lock and the file lock
# --------------------------------------------------------------------------- #


def test_cross_process_lock_holds_the_inprocess_lock(tmp_path) -> None:
    """``cross_process_lock`` takes the per-data_dir threading lock too.

    While it is held, the SAME ``lock_for(data_dir)`` cannot be acquired by another
    thread — proving the in-process half is engaged (the file-lock half is exercised
    by the tests above)."""
    target = tmp_path / "sub" / "store.json"
    tlock = lock_for(tmp_path)
    with cross_process_lock(tmp_path, target):
        assert tlock.acquire(blocking=False) is False  # held by the guard
    assert tlock.acquire(blocking=False) is True  # released on exit
    tlock.release()
