"""Memory façade M2 surface: recall()."""

from __future__ import annotations

from pathlib import Path

import pytest

from akana.memory import Memory


@pytest.fixture()
def mem(tmp_path: Path) -> Memory:
    return Memory.for_data_dir(tmp_path)


def test_recall_returns_result_with_trace(mem: Memory) -> None:
    mem.assert_fact_direct(key="favori_içecek", value="kahve", trust="user_statement")
    result = mem.recall("kahve")
    assert result.blocks
    assert result.trace.query == "kahve"
