"""Auto-capture (capture-from-chat) compatibility with no-confirm remember (allow_direct).

User bug: "even when no-confirm remember is ON, captured info lands in the inbox."
Root: _stage_candidates always wrote to staging (without checking allow_direct).
Fix: when allow_direct is on, stage + promote (persistent); when off, inbox (default)
— same as the memory.remember tool + session_closer.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from akana.memory import Memory
from akana.memory.settings import MemorySettings, save_memory_settings
from akana_server.api.routes.chat.persist import _stage_candidates


def _cand(key: str, value: str) -> SimpleNamespace:
    return SimpleNamespace(key=key, value=value, reason="llm_capture")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AKANA_MEMORY_ALLOW_DIRECT", raising=False)


def test_capture_allow_direct_off_stages_to_inbox(tmp_path: Path) -> None:
    mem = Memory.for_data_dir(tmp_path)
    save_memory_settings(tmp_path, MemorySettings(allow_direct=False))
    out = _stage_candidates(
        mem, [_cand("ad", "Alice")], conversation_id="c1"
    )
    assert out[0]["kind"] == "staging"  # waits for approval in the inbox
    assert mem.staging.count_pending() == 1


def test_capture_allow_direct_on_promotes_direct(tmp_path: Path) -> None:
    mem = Memory.for_data_dir(tmp_path)
    save_memory_settings(tmp_path, MemorySettings(allow_direct=True))
    out = _stage_candidates(
        mem, [_cand("ad", "Alice")], conversation_id="c1"
    )
    assert out[0]["kind"] == "stored"  # no-confirm remember: directly persistent
    assert mem.staging.count_pending() == 0  # does NOT wait in the inbox
    assert mem.staging.get(out[0]["id"]).status == "promoted"  # moved to persistent


def test_capture_skips_duplicate_of_pending(tmp_path: Path) -> None:
    """A capture candidate that exactly restates a PENDING inbox row is not staged again —
    so the same info doesn't land in the inbox twice on a later turn (the reported bug)."""
    mem = Memory.for_data_dir(tmp_path)
    save_memory_settings(tmp_path, MemorySettings(allow_direct=False))
    first = _stage_candidates(mem, [_cand("ad", "Alice")], conversation_id="c1")
    assert len(first) == 1
    assert mem.staging.count_pending() == 1
    # Identical capture on a later turn → skipped, no duplicate row.
    second = _stage_candidates(mem, [_cand("ad", "Alice")], conversation_id="c1")
    assert second == []
    assert mem.staging.count_pending() == 1


def test_capture_skips_duplicate_of_durable_fact(tmp_path: Path) -> None:
    """A candidate that restates an APPROVED (durable) fact is not re-staged — staging's own
    same-key dedup never sees durable facts, so this guard is what covers it."""
    mem = Memory.for_data_dir(tmp_path)
    save_memory_settings(tmp_path, MemorySettings(allow_direct=False))
    mem.assert_fact_direct(key="ad", value="Alice", trust="user_statement", extractor="test")
    out = _stage_candidates(mem, [_cand("ad", "Alice")], conversation_id="c1")
    assert out == []
    assert mem.staging.count_pending() == 0


def test_capture_allows_correction_with_new_value(tmp_path: Path) -> None:
    """A same-key/NEW-value correction is NOT a duplicate — it must still be captured."""
    mem = Memory.for_data_dir(tmp_path)
    save_memory_settings(tmp_path, MemorySettings(allow_direct=False))
    _stage_candidates(mem, [_cand("sehir", "Ankara")], conversation_id="c1")
    out = _stage_candidates(mem, [_cand("sehir", "İstanbul")], conversation_id="c1")
    assert len(out) == 1  # the correction goes through (staging supersedes the old value)
