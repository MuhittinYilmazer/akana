"""Unit tests for ``python akana.py reset-memory`` (akana_cli.reset_memory_cmd).

Regression cover for [cli:arch:0]: the command used to be dead on arrival —
``from akana.memory.graph import GraphStore`` raised ModuleNotFoundError (the
root ``akana.py`` launcher shadowed the ``akana`` package) and a blanket
``except Exception`` swallowed it, so the command printed success while clearing
NOTHING on every machine. These tests would have caught that: they assert the
import resolves AND that every store's data is actually gone afterwards.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# Resolves to src/akana via the central bootstrap (tests/conftest.py); if the
# launcher shadow ever wins again this import fails at collection time.
from akana.memory.graph import GraphStore
from akana.memory.semantic import SemanticStore
from akana.memory.staging import FactCandidate, StagingStore
from akana.memory.vector import VectorStore

from akana_cli import reset_memory_cmd


def _seed(data_dir: Path) -> None:
    """Populate all four stores so a real reset has something to clear."""
    StagingStore.for_data_dir(data_dir).stage(
        FactCandidate(key="pref.color", value="blue", reason="test")
    )
    SemanticStore.for_data_dir(data_dir).upsert_fact(
        fact_id="f1", key="pref.color", value="blue"
    )
    GraphStore.for_data_dir(data_dir).link_fact(key="pref.color", value="blue")
    # VectorStore.index_fact needs an embedder; seeding staging/semantic/graph is
    # enough to prove the reset clears real rows. Vector clear() is exercised by
    # the command itself and asserted to return 0 (empty) below.


def _counts(data_dir: Path) -> dict[str, int]:
    return {
        "staging": StagingStore.for_data_dir(data_dir).count_pending(),
        "semantic": SemanticStore.for_data_dir(data_dir).count_facts(),
        "vector": VectorStore.for_data_dir(data_dir).count(),
        "graph": len(GraphStore.for_data_dir(data_dir).snapshot().get("nodes", [])),
    }


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "akana-data"
    monkeypatch.setenv("AKANA_DATA_DIR", str(d))
    # Keep the "server may be running" probe hermetic — it scans the real port.
    monkeypatch.setattr(reset_memory_cmd, "_server_might_be_running", lambda: False)
    return d


def test_reset_memory_no_db_is_a_clean_noop(data_dir: Path) -> None:
    """With no memory.db yet, the command reports nothing to reset and exits 0."""
    rc = reset_memory_cmd.run_reset_memory()
    assert rc == 0
    assert not (data_dir / "db" / "memory.db").exists()


def test_reset_memory_actually_clears_every_store(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real bug: seed the stores, run the command, assert the data is GONE."""
    _seed(data_dir)
    seeded = _counts(data_dir)
    assert seeded["staging"] >= 1 and seeded["semantic"] >= 1 and seeded["graph"] >= 1, (
        f"seeding failed to create rows: {seeded}"
    )

    rc = reset_memory_cmd.run_reset_memory()

    assert rc == 0
    after = _counts(data_dir)
    assert after == {"staging": 0, "semantic": 0, "vector": 0, "graph": 0}, (
        f"reset-memory left rows behind: {after} (this is the dead-on-arrival bug)"
    )
    # Honest success message — not the old "No files to reset." lie.
    out = capsys.readouterr().out
    assert "cleared" in out.lower()


def test_reset_memory_surfaces_store_failure_as_nonzero(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A real sqlite failure must FAIL loudly (exit 1), not be swallowed as success.

    Guards the narrowed ``except (sqlite3.Error, OSError)``: the old blanket
    ``except Exception`` turned every failure — including the shadow import error
    — into a warn line + exit 0.
    """
    _seed(data_dir)

    def _boom(self: StagingStore) -> int:  # noqa: ANN001
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(StagingStore, "clear", _boom)

    rc = reset_memory_cmd.run_reset_memory()

    assert rc == 1
    out = capsys.readouterr().out
    assert "could not reset" in out.lower()


def test_reset_memory_import_resolves_to_src_package() -> None:
    """The CLI-layer analogue of the shadow guard: these are the real src classes."""
    for cls in (StagingStore, SemanticStore, VectorStore, GraphStore):
        mod = cls.__module__
        assert mod.startswith("akana.memory"), f"{cls.__name__} came from {mod}, not src/akana"
