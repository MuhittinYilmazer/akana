"""MemoryLedger — durable append-only JSONL replay log (P8)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from akana.memory.events import MemoryEvent
from akana.memory.ledger import MemoryLedger


@pytest.fixture()
def ledger(tmp_path: Path) -> MemoryLedger:
    return MemoryLedger.for_data_dir(tmp_path)


def test_append_then_read_all(ledger: MemoryLedger) -> None:
    ledger.append("turn", data={"turn_id": "t1"})
    ledger.append("fact", data={"key": "ad"})
    evs = ledger.read_all()
    assert [e.kind for e in evs] == ["turn", "fact"]  # write order preserved
    assert evs[1].data["key"] == "ad"


def test_record_event_roundtrip(ledger: MemoryLedger) -> None:
    ledger.record(MemoryEvent(kind="fact", ts="2026-01-01T00:00:00Z", data={"x": 1}))
    got = ledger.read_all()[0]
    assert (got.kind, got.ts, got.data) == ("fact", "2026-01-01T00:00:00Z", {"x": 1})


def test_read_all_filters_by_kind(ledger: MemoryLedger) -> None:
    ledger.append("turn")
    ledger.append("fact")
    ledger.append("turn")
    assert [e.kind for e in ledger.read_all(kind="turn")] == ["turn", "turn"]


def test_read_all_tail_limit(ledger: MemoryLedger) -> None:
    for i in range(5):
        ledger.append("turn", data={"i": i})
    assert [e.data["i"] for e in ledger.read_all(limit=2)] == [3, 4]


def test_missing_file_reads_empty(tmp_path: Path) -> None:
    assert MemoryLedger.for_data_dir(tmp_path).read_all() == []


def test_skips_corrupt_lines(ledger: MemoryLedger) -> None:
    ledger.append("turn", data={"ok": True})
    with ledger.path.open("a", encoding="utf-8") as f:
        f.write("not json\n\n")  # garbage + blank line between good records
    ledger.append("fact")
    assert [e.kind for e in ledger.read_all()] == ["turn", "fact"]


def test_unicode_preserved(ledger: MemoryLedger) -> None:
    ledger.append("fact", data={"value": "İstanbul çayı"})
    assert ledger.read_all()[0].data["value"] == "İstanbul çayı"


def test_count(ledger: MemoryLedger) -> None:
    ledger.append("turn")
    ledger.append("fact")
    assert ledger.count() == 2


# -- rotation -----------------------------------------------------------------


def _mk(tmp_path: Path, **kw) -> MemoryLedger:
    return MemoryLedger(tmp_path / "event_log.jsonl", **kw)


def test_rotation_triggers_with_small_max_bytes(tmp_path: Path) -> None:
    led = _mk(tmp_path, max_bytes=200, keep=3)
    for i in range(20):
        led.append("turn", data={"i": i})
    assert (tmp_path / "event_log.1.jsonl").is_file()  # archive naming
    # rotation happens pre-write, so the active file stays near the cap
    assert led.path.stat().st_size <= 200 + 100


def test_keep_limit_drops_oldest_archive(tmp_path: Path) -> None:
    led = _mk(tmp_path, max_bytes=1, keep=2)  # every append after the first rotates
    for i in range(6):
        led.append("turn", data={"i": i})
    assert (tmp_path / "event_log.1.jsonl").is_file()
    assert (tmp_path / "event_log.2.jsonl").is_file()
    assert not (tmp_path / "event_log.3.jsonl").exists()  # never exceeds keep
    # one event per file: only the newest keep+1 survive, chronological
    assert [e.data["i"] for e in led.read_all()] == [3, 4, 5]


def test_read_all_chronological_across_rotation(tmp_path: Path) -> None:
    led = _mk(tmp_path, max_bytes=150, keep=20)
    for i in range(30):
        led.append("turn", data={"i": i})
    assert len(led.paths()) > 1  # the log really did span files
    assert [e.data["i"] for e in led.read_all()] == list(range(30))


def test_tail_limit_spans_archives(tmp_path: Path) -> None:
    led = _mk(tmp_path, max_bytes=150, keep=20)
    for i in range(30):
        led.append("turn", data={"i": i})
    active_lines = led.path.read_bytes().count(b"\n")
    want = active_lines + 5  # forces the tail to dip into the archives
    assert [e.data["i"] for e in led.read_all(limit=want)] == list(
        range(30 - want, 30)
    )
    # limit larger than everything → full chronological history
    assert [e.data["i"] for e in led.read_all(limit=1000)] == list(range(30))


def test_tail_limit_with_kind_filter_across_archives(tmp_path: Path) -> None:
    led = _mk(tmp_path, max_bytes=120, keep=20)
    for i in range(20):
        led.append("turn" if i % 2 == 0 else "fact", data={"i": i})
    assert [e.data["i"] for e in led.read_all(kind="turn", limit=4)] == [12, 14, 16, 18]


def test_count_across_rotation(tmp_path: Path) -> None:
    led = _mk(tmp_path, max_bytes=150, keep=20)
    for i in range(30):
        led.append("turn", data={"i": i})
    assert led.count() == 30


def test_paths_lists_archives_then_active(tmp_path: Path) -> None:
    led = _mk(tmp_path, max_bytes=150, keep=20)
    assert led.paths() == []
    led.append("turn")
    assert led.paths() == [led.path]
    for i in range(30):
        led.append("turn", data={"i": i})
    ps = led.paths()
    n = len(ps) - 1
    assert n >= 1
    assert [p.name for p in ps] == [
        f"event_log.{i}.jsonl" for i in range(n, 0, -1)
    ] + ["event_log.jsonl"]


def test_tail_tolerates_torn_final_line(tmp_path: Path) -> None:
    led = _mk(tmp_path, max_bytes=150, keep=20)
    for i in range(10):
        led.append("turn", data={"i": i})
    with led.path.open("ab") as f:  # crash mid-write: no trailing newline
        f.write(b'{"kind":"turn","ts":"x","data":{"i":99')
    assert [e.data["i"] for e in led.read_all(limit=3)] == [7, 8, 9]
    assert [e.data["i"] for e in led.read_all()] == list(range(10))


def test_reads_skip_undecodable_bytes(tmp_path: Path) -> None:
    led = _mk(tmp_path)
    led.append("turn", data={"i": 0})
    with led.path.open("ab") as f:
        f.write(b"\xff\xfe broken\n")  # invalid UTF-8 between good records
    led.append("turn", data={"i": 1})
    assert [e.data["i"] for e in led.read_all(limit=10)] == [0, 1]
    assert [e.data["i"] for e in led.read_all()] == [0, 1]


def test_concurrent_append_smoke(tmp_path: Path) -> None:
    led = _mk(tmp_path, max_bytes=2048, keep=50)  # rotates a few times, loses nothing
    n_threads, per = 8, 25

    def worker(t: int) -> None:
        for i in range(per):
            led.append("turn", data={"t": t, "i": i})

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    evs = led.read_all()
    assert len(evs) == n_threads * per == led.count()
    for t in range(n_threads):  # per-thread order survives lock + O_APPEND
        assert [e.data["i"] for e in evs if e.data["t"] == t] == list(range(per))
