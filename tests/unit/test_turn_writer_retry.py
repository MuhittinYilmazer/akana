"""turn_writer — bounded retry on a transient persist error (R2-B4).

Post-A5 single-writer v2; there is a bounded retry so that ``remember_turn`` does
not PERMANENTLY lose a turn on a transient error (a short ``database is locked``
exceeding busy_timeout, an IO hiccup). These tests verify the retry
(transient→recovery) and the exhaustion path (loud-log without raising) against
the real module with a fake memory_core.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from akana_server.orchestrator import turn_writer


class _FakeEpisodic:
    def get_turn(self, turn_id: str):  # noqa: ANN201
        return None  # always a "new turn" → meta increments once


class _FakeMeta:
    def __init__(self) -> None:
        self.user = 0
        self.assistant = 0

    def on_user_message(self, conv_id: str, text: str) -> None:
        self.user += 1

    def on_assistant_message(self, conv_id: str) -> None:
        self.assistant += 1


class _FakeMem:
    def __init__(self, fail_times: int, sink: dict) -> None:
        self.episodic = _FakeEpisodic()
        self.conversations_meta = _FakeMeta()
        self._fail_times = fail_times
        self._sink = sink

    def remember_turn(self, **kw):  # noqa: ANN003
        self._sink["calls"] += 1
        if self._sink["calls"] <= self._fail_times:
            raise RuntimeError("database is locked")
        self._sink["saved"] = kw


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(turn_writer, "_PERSIST_BACKOFF_S", 0.0)


def test_persist_user_turn_retries_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = {"calls": 0, "saved": None}
    mem = _FakeMem(fail_times=1, sink=sink)
    monkeypatch.setattr(
        "akana_server.memory_core.get_memory_core", lambda dd: mem
    )
    tid = turn_writer.persist_user_turn(
        conversation_id="c1", user_text="selam", turn_id="u1", data_dir=tmp_path
    )
    assert tid == "u1"
    assert sink["calls"] == 2, "first attempt failed → must be retried once"
    assert sink["saved"]["text"] == "selam"
    assert mem.conversations_meta.user == 1  # meta incremented only once


def test_persist_user_turn_all_attempts_fail_no_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = {"calls": 0, "saved": None}
    mem = _FakeMem(fail_times=99, sink=sink)  # always blow up
    monkeypatch.setattr(
        "akana_server.memory_core.get_memory_core", lambda dd: mem
    )
    # All attempts fail → still does NOT raise (the reply reaches the user), turn_id is returned.
    tid = turn_writer.persist_user_turn(
        conversation_id="c1", user_text="x", turn_id="u9", data_dir=tmp_path
    )
    assert tid == "u9"
    assert sink["calls"] == turn_writer._PERSIST_ATTEMPTS  # the full attempt count was tried
    assert mem.conversations_meta.user == 0  # no success → meta did not increment


def test_persist_assistant_turn_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = {"calls": 0, "saved": None}
    mem = _FakeMem(fail_times=2, sink=sink)
    monkeypatch.setattr(
        "akana_server.memory_core.get_memory_core", lambda dd: mem
    )
    tid = turn_writer.persist_assistant_turn(
        conversation_id="c1",
        assistant_text="yanit",
        user_turn_id="u1",
        assistant_turn_id="a1",
        data_dir=tmp_path,
    )
    assert tid == "a1"
    assert sink["calls"] == 3  # 2 failed + 1 succeeded
    assert sink["saved"]["text"] == "yanit"
    assert mem.conversations_meta.assistant == 1
