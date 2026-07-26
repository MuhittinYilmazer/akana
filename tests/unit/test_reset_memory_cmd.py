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

    rc = reset_memory_cmd.run_reset_memory(assume_yes=True)

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

    rc = reset_memory_cmd.run_reset_memory(assume_yes=True)

    assert rc == 1
    out = capsys.readouterr().out
    assert "could not reset" in out.lower()


def test_reset_memory_asks_before_deleting_anything(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """It is one keystroke (or a tab-completion) away from erasing everything Akana ever
    learned. Answering "no" must leave every store exactly as it was."""
    _seed(data_dir)
    before = _counts(data_dir)

    asked: list[str] = []
    monkeypatch.setattr(reset_memory_cmd, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(
        reset_memory_cmd.io,
        "ask_yes_no",
        lambda prompt, **kw: (asked.append(prompt), False)[1],
    )

    rc = reset_memory_cmd.run_reset_memory()

    assert rc == 0
    assert asked, "reset-memory deleted the user's facts without asking"
    assert str(data_dir) in asked[0], f"the prompt does not name what it will wipe: {asked[0]!r}"
    assert _counts(data_dir) == before, "answering 'no' still cleared the stores"
    assert "cancel" in capsys.readouterr().out.lower()


def test_reset_memory_refuses_unattended_without_yes(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A scheduled run / pipeline has no terminal: the command must fail fast with the
    --yes hint, never block on input() and never destroy anything unasked."""
    _seed(data_dir)
    before = _counts(data_dir)

    def _must_not_prompt(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("reset-memory prompted with no terminal attached")

    monkeypatch.setattr(reset_memory_cmd, "_stdin_is_interactive", lambda: False)
    monkeypatch.setattr(reset_memory_cmd.io, "ask_yes_no", _must_not_prompt)

    rc = reset_memory_cmd.run_reset_memory()

    assert rc == 1
    assert _counts(data_dir) == before
    assert "--yes" in capsys.readouterr().out


def test_reset_memory_yes_flag_is_accepted(data_dir: Path) -> None:
    """`--yes` is the documented escape hatch for scripts — it must exist on the parser."""
    from akana_cli.main import build_parser

    assert build_parser().parse_args(["reset-memory", "--yes"]).yes is True
    assert build_parser().parse_args(["reset-memory"]).yes is False


def test_reset_memory_import_resolves_to_src_package() -> None:
    """The CLI-layer analogue of the shadow guard: these are the real src classes."""
    for cls in (StagingStore, SemanticStore, VectorStore, GraphStore):
        mod = cls.__module__
        assert mod.startswith("akana.memory"), f"{cls.__name__} came from {mod}, not src/akana"
