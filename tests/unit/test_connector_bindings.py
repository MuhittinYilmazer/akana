"""ChannelBindingStore cross-instance concurrency.

Two ChannelBindingStore instances over the SAME connector_bindings.json (the
router owns one; the bind-API route builds its own) must serialize their
read-modify-write. A per-instance lock did not: interleaved _load->mutate->_save
lost one update (last writer wins), and the shared fixed .json.tmp temp file
collided (Windows sharing-violation PermissionError). This pins both.
"""

from __future__ import annotations

import threading
from pathlib import Path

from akana_server.connectors.conversation import ChannelBindingStore


def test_two_instances_over_one_file_do_not_lose_bindings(tmp_path: Path) -> None:
    """Many concurrent binds from two separate stores over one file: EVERY binding
    must survive (no lost update). Old per-instance lock dropped bindings whose save
    was overwritten by the other instance's stale-load save."""
    store_a = ChannelBindingStore(tmp_path)  # the router's instance
    store_b = ChannelBindingStore(tmp_path)  # the bind-route's separate instance
    assert store_a._lock is store_b._lock  # cross-instance lock is shared by path

    n = 40
    start = threading.Barrier(2)

    def bind_via(store: ChannelBindingStore, prefix: str) -> None:
        start.wait()
        for i in range(n):
            store.bind("telegram", f"{prefix}-{i}", f"conv-{prefix}-{i}")

    t_a = threading.Thread(target=bind_via, args=(store_a, "a"))
    t_b = threading.Thread(target=bind_via, args=(store_b, "b"))
    t_a.start()
    t_b.start()
    t_a.join()
    t_b.join()

    # A fresh reader sees the union of BOTH threads' 40 bindings — none lost.
    reader = ChannelBindingStore(tmp_path)
    for i in range(n):
        assert reader.get("telegram", f"a-{i}") == f"conv-a-{i}"
        assert reader.get("telegram", f"b-{i}") == f"conv-b-{i}"

    # No leftover temp files (unique per-write temp names are cleaned up).
    assert not list(tmp_path.glob("connector_bindings.json*.tmp"))


def test_unique_temp_filename_per_write(tmp_path: Path) -> None:
    """The temp file must NOT be the fixed ``.json.tmp`` shared across instances —
    a shared temp path collides on Windows. Each write uses a pid+uuid temp name and
    removes it, so nothing persists between writes."""
    store = ChannelBindingStore(tmp_path)
    store.bind("telegram", "1", "conv-1")
    assert (tmp_path / "connector_bindings.json").exists()
    assert not (tmp_path / "connector_bindings.json.tmp").exists()
    assert not list(tmp_path.glob("*.tmp"))
