"""M4 façade wiring — durable ledger, global recall."""

from __future__ import annotations

from pathlib import Path

import pytest

from akana.memory import Memory, MemoryLedger


@pytest.fixture()
def mem(tmp_path: Path) -> Memory:
    return Memory.for_data_dir(tmp_path)


def test_for_data_dir_wires_ledger(mem: Memory) -> None:
    assert isinstance(mem.ledger, MemoryLedger)


def test_ledger_durably_records_mutations(mem: Memory) -> None:
    mem.assert_fact_direct(key="ad", value="Alice")
    mem.remember_turn(role="user", conversation_id="c1", text="merhaba")
    kinds = {e.kind for e in mem.ledger.read_all()}
    assert {"fact", "turn"} <= kinds


def test_attach_ledger_false_is_write_light(tmp_path: Path) -> None:
    mem = Memory.for_data_dir(tmp_path, attach_ledger=False)
    mem.assert_fact_direct(key="ad", value="Alice")
    # no subscriber attached at birth → the mutation was not logged
    assert mem.ledger.read_all() == []
