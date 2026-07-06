"""Bug-hunt regression: UploadStore.save durability + dedup-race tolerance.

Two upload-store durability bugs are covered here (batch "Upload store"):

* MED dedup race — concurrent identical-content uploads can race the UNLOCKED
  lazy ``app.state.image_store`` build (deps.get_image_store / chat gates
  ``_image_store``) and end up with TWO ``UploadStore`` instances that share the
  same ``multimodal.db`` but hold INDEPENDENT locks. Their ``save`` critical
  sections then interleave: both SELECT-miss, both write their ``<ulid>.<ext>``
  file, and both INSERT the same UNIQUE(sha256). Pre-fix the second INSERT raised
  an uncaught ``sqlite3.IntegrityError`` (HTTP 500) and left the loser's file on
  disk as an orphan. ``save`` now tolerates the collision: it rolls back, unlinks
  the just-written orphan, re-SELECTs the winner's row and returns it with
  ``dedup_hit=True``.

* LOW ordering — ``save`` writes the file BEFORE the DB commit, so a commit
  failure (or a crash) between the write and the commit left the file on disk
  with no committed row. ``save`` now removes the just-written file on ANY commit
  failure, so no orphan survives.

Both tests are hermetic (``tmp_path``, no network, no app wiring). Note:
``sqlite3.Connection`` is a C type whose ``execute``/``commit`` attributes are
READ-ONLY, so the fault is injected through a thin delegating proxy around the
connection ``save`` opens, not by monkeypatching the connection in place.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from akana_server.multimodal.store import UploadStore


def _png(w: int = 4, h: int = 4) -> bytes:
    """A minimal valid PNG (sniff/dims only look at the signature + IHDR)."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0dIHDR"
        + struct.pack(">II", w, h)
        + b"\x08\x06\x00\x00\x00"
        + b"\x00" * 4
        + b"\x00\x00\x00\x00IEND\xae\x42\x60\x82"
    )


def _uploaded_files(store: UploadStore) -> list[Path]:
    """Committed ``<ulid>.<ext>`` files under uploads/ (temp ``.tmp`` excluded)."""
    return [
        p
        for p in store._uploads_dir.iterdir()
        if p.is_file() and not p.name.endswith(".tmp")
    ]


class _MissCursor:
    """Stand-in cursor whose ``fetchone()`` reports a SELECT miss."""

    def fetchone(self):  # noqa: ANN001, ANN201
        return None


class _ConnProxy:
    """Delegating wrapper over a real ``sqlite3.Connection``.

    ``sqlite3.Connection.execute``/``commit`` are read-only C attributes, so a
    test can't monkeypatch them on the instance. This proxy intercepts those two
    calls (via optional hooks) and forwards everything else to the real
    connection unchanged.
    """

    def __init__(self, conn, *, on_execute=None, on_commit=None):  # noqa: ANN001
        self._conn = conn
        self._on_execute = on_execute
        self._on_commit = on_commit

    def execute(self, sql, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        if self._on_execute is not None:
            forced = self._on_execute(sql)
            if forced is not None:
                return forced
        return self._conn.execute(sql, *args, **kwargs)

    def commit(self):  # noqa: ANN201
        if self._on_commit is not None:
            self._on_commit()
        return self._conn.commit()

    def __getattr__(self, name):  # noqa: ANN001, ANN204
        # Called only for attributes not defined above (rollback/close/…).
        return getattr(self._conn, name)


def test_concurrent_duplicate_save_dedups_without_integrityerror_or_orphan(
    tmp_path: Path,
) -> None:
    """Two racing stores + identical bytes → dedup, no IntegrityError, no orphan.

    Reproduces the unlocked-lazy-init race deterministically: two ``UploadStore``
    instances (independent locks) on the SAME data dir. Store A commits the row.
    Store B's FIRST dedup SELECT is forced to miss (as if it ran before A
    committed), so B proceeds to write its file and INSERT — hitting UNIQUE(sha256).
    The fix must swallow the IntegrityError, drop B's orphan file, and return A's
    existing record with ``dedup_hit=True``.
    """
    store_a = UploadStore(tmp_path)
    store_b = UploadStore(tmp_path)  # separate instance → separate _lock
    data = _png()

    rec_a, dedup_a = store_a.save(data, original_name="a.png")
    assert dedup_a is False  # first ever save → new row

    # Force ONLY store B's first dedup SELECT to miss (the re-SELECT inside the
    # IntegrityError branch then runs for real and finds A's row).
    real_connect = store_b._connect
    state = {"missed": False}

    def _miss_first_sha_select(sql):  # noqa: ANN001, ANN202
        if not state["missed"] and "WHERE sha256" in sql:
            state["missed"] = True
            return _MissCursor()
        return None

    def _connect():  # noqa: ANN202
        return _ConnProxy(real_connect(), on_execute=_miss_first_sha_select)

    store_b._connect = _connect  # type: ignore[method-assign]

    # Must NOT raise sqlite3.IntegrityError; must dedup onto A's row.
    rec_b, dedup_b = store_b.save(data, original_name="b.png")

    assert dedup_b is True
    assert rec_b.id == rec_a.id  # same content → same (winner's) record
    assert rec_b.sha256 == rec_a.sha256
    # No orphan: exactly ONE committed file on disk (A's), B's write was reclaimed.
    files = _uploaded_files(store_a)
    assert len(files) == 1, f"expected 1 file, found {[p.name for p in files]}"
    assert files[0].name == rec_a.file_name


def test_commit_failure_leaves_no_orphan_file(tmp_path: Path) -> None:
    """A commit failure mid-save must remove the just-written file (no orphan).

    The file is written before the INSERT+commit; a failing commit previously
    left ``<ulid>.<ext>`` on disk with no committed row (sha-dedup could never
    reclaim it). The fix unlinks the written file on ANY commit failure.
    """
    store = UploadStore(tmp_path)
    data = _png()

    real_connect = store._connect

    def _boom() -> None:
        raise RuntimeError("simulated commit failure (disk full / I/O error)")

    def _connect():  # noqa: ANN202
        # Wrap AFTER real_connect() so the schema-setup commit already ran on the
        # real connection; only save()'s commit hits the boom.
        return _ConnProxy(real_connect(), on_commit=_boom)

    store._connect = _connect  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="simulated commit failure"):
        store.save(data, original_name="crash.png")

    # The write happened, but the failed commit's cleanup must have removed it.
    files = _uploaded_files(store)
    assert files == [], f"orphan file(s) left on disk: {[p.name for p in files]}"


def test_dedup_recreates_missing_backing_file(tmp_path: Path) -> None:
    """Dedup onto a row whose backing file was lost must recreate the file.

    If the uploads/<ulid>.<ext> file is removed (manual cleanup, a restore that
    copied the db but not uploads/, a crash) the row survives but the bytes do
    not. Dedup is keyed on content sha alone, so pre-fix a re-upload of the same
    bytes kept deduping onto the broken row and never recreated the file — the
    content was permanently unusable (400 FILE_MISSING / 410). save() now
    recreates the file when it is missing on a dedup hit.
    """
    store = UploadStore(tmp_path)
    data = _png()

    rec, dedup = store.save(data, original_name="a.png")
    assert dedup is False
    path = store.file_path(rec)
    assert path.exists()

    # Simulate the lost-file scenario: the row stays, the bytes disappear.
    path.unlink()
    assert not path.exists()

    rec2, dedup2 = store.save(data, original_name="a-again.png")

    assert dedup2 is True  # still the same row (content identity)
    assert rec2.id == rec.id
    assert store.file_path(rec2).exists()  # file was recreated
    assert store.file_path(rec2).read_bytes() == data

    # An event records the recreation (observability).
    actions = [e["action"] for e in store.events(rec.id)]
    assert "file_recreated" in actions
