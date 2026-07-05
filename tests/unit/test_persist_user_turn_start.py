"""``_persist_user_turn_start`` resilience tests (multi-chat message-loss bug).

Reported bug: "write a message in A → open new chat B → write in B → go back to A,
the message you wrote in A is not showing". The backend leg: the early user-turn persist
(``_persist_user_turn_start``) was SILENTLY dropping the message when the
``_conversation_chat_usable`` gate returned False. That gate conflates two SEPARATE cases:

* (a) the conversation was DELETED (tombstone / soft-delete) → writing forbidden (so it isn't resurrected),
* (b) the conversation has NOT yet been ``ensure``d (fresh, not visible on the server) → the message
  must not be lost; it should be ensured and persisted.

These tests nail both cases: a fresh conv IS written to; a tombstoned/soft-deleted one is
NOT written to and is NOT resurrected.

NOTE: the sync ``def`` + ``asyncio.run(...)`` idiom is intentional — the fast path
(``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1``) cannot load pytest-asyncio; the other async unit
tests in this repo (see test_detached_turn_primitives.py) use the same wrapper.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from akana_server.api.routes.chat.persist import _persist_user_turn_start
from akana_server.conversation_service import ConversationService
from akana_server.memory_core import get_memory_core

FRESH_CID = "01CONVFRESHNOTYETSEEN000000"
TOMB_CID = "01CONVTOMBSTONEDMEMSET00000"
SOFT_CID = "01CONVSOFTDELETEDROW0000000"


def _fake_request(tmp_path: Path, *, tombstones: set[str] | None = None):
    """A minimal fake request carrying ``request.app.state``.

    ``_persist_user_turn_start`` reads only ``app.state.{memory,conversation_service,
    settings}`` and (for tombstone classification) ``app.state.chat_cleanup_tombstones``.
    Even though settings is not a real ``Settings``, the turn_writer resolves the data_dir
    from ``memory_service._data_dir`` → the persist still goes to ``tmp_path/db/memory.db``.
    """
    # PRODUCTION REALITY: app.state.conversation_service = ConversationServiceV2Adapter
    # (api/app.py:204). This fixture used to use the v1 ``ConversationService.for_data_dir``
    # → the adapter's missing ``_conversation_exists`` override (since super
    # __init__ was skipped there is no ``self._lock`` → AttributeError → the ``except`` swallows it →
    # a soft-delete was mistaken for "not a tombstone" and RESURRECTED) was NOT SEEN by these tests.
    # Switching to the adapter makes the soft-delete resurrect test nail the real production path.
    state = SimpleNamespace(
        conversation_service=ConversationService(tmp_path),
        settings=SimpleNamespace(data_dir=tmp_path),
        chat_cleanup_tombstones=set(tombstones or set()),
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_persists_user_turn_for_fresh_conversation(tmp_path: Path) -> None:
    """Fresh (never ensured) conv: the user turn is PERSISTED and the conv becomes visible."""
    request = _fake_request(tmp_path)
    svc: ConversationService = request.app.state.conversation_service
    # Precondition: the conversation is not visible on the server (the "not yet visible" state that triggers the bug).
    assert svc.get(FRESH_CID) is None

    async def run() -> str:
        return await _persist_user_turn_start(
            request,
            conversation_id=FRESH_CID,
            user_text="A sohbetinde yazdığım mesaj kaybolmamalı",
            lang="tr",
            user_turn_id="01USERTURNFRESH000000000000",
        )

    turn_id = asyncio.run(run())

    assert turn_id == "01USERTURNFRESH000000000000"
    mem = get_memory_core(tmp_path)
    persisted = mem.episodic.get_turn(turn_id)
    assert persisted is not None, "user turn silently dropped on a fresh conv (bug)"
    assert persisted.role == "user"
    assert persisted.text == "A sohbetinde yazdığım mesaj kaybolmamalı"
    assert persisted.conversation_id == FRESH_CID
    # ensured → the conversation is now visible/usable.
    assert svc.get(FRESH_CID) is not None


def test_ok_out_signals_true_on_success(tmp_path: Path) -> None:
    """D5: a confirmed write appends ``True`` to ``ok_out``."""
    request = _fake_request(tmp_path)
    ok: list[bool] = []

    async def run() -> str:
        return await _persist_user_turn_start(
            request,
            conversation_id=FRESH_CID,
            user_text="kaydedildi",
            lang="tr",
            user_turn_id="01USEROK00000000000000000000",
            ok_out=ok,
        )

    turn_id = asyncio.run(run())
    assert turn_id == "01USEROK00000000000000000000"
    assert ok == [True]


def test_ok_out_signals_false_when_persist_raises(tmp_path: Path, monkeypatch) -> None:
    """D5: a SWALLOWED persist failure must append ``False`` so the caller does NOT mark the
    user turn persisted — otherwise the assistant turn lands with no preceding user turn."""
    import akana_server.api.routes.chat as chatpkg

    request = _fake_request(tmp_path)

    def _boom(**_kwargs: object) -> str:
        raise RuntimeError("disk full")

    monkeypatch.setattr(chatpkg, "persist_user_turn", _boom)
    ok: list[bool] = []

    async def run() -> str:
        return await _persist_user_turn_start(
            request,
            conversation_id=FRESH_CID,
            user_text="yazılamadı",
            lang="tr",
            user_turn_id="01USERFAIL000000000000000000",
            ok_out=ok,
        )

    turn_id = asyncio.run(run())
    # turn id still returned (the stream is not broken) but ok_out=False → caller must retry.
    assert turn_id == "01USERFAIL000000000000000000"
    assert ok == [False]


def test_skips_persist_for_in_memory_tombstone(tmp_path: Path) -> None:
    """Conv in the in-memory cleanup tombstone set: persist is NOT performed."""
    request = _fake_request(tmp_path, tombstones={TOMB_CID})

    async def run() -> str:
        return await _persist_user_turn_start(
            request,
            conversation_id=TOMB_CID,
            user_text="silinmiş konuşmaya yazma",
            lang="tr",
            user_turn_id="01USERTURNTOMB0000000000000",
        )

    turn_id = asyncio.run(run())

    # A fake turn id is still returned (so as not to break the turn) BUT nothing is written to disk.
    assert turn_id == "01USERTURNTOMB0000000000000"
    mem = get_memory_core(tmp_path)
    assert mem.episodic.get_turn(turn_id) is None


def test_skips_persist_and_no_resurrect_for_soft_deleted(tmp_path: Path) -> None:
    """Soft-deleted conv (row exists, ``deleted_at`` populated): NO write + NO resurrect."""
    request = _fake_request(tmp_path)
    svc: ConversationService = request.app.state.conversation_service
    svc.ensure(SOFT_CID)
    assert svc.soft_delete(SOFT_CID) is True
    # After soft-delete: get() returns None but the row EXISTS (the resurrect risk is here).
    assert svc.get(SOFT_CID) is None

    async def run() -> str:
        return await _persist_user_turn_start(
            request,
            conversation_id=SOFT_CID,
            user_text="soft-delete edilmiş konuşma resurrect olmamalı",
            lang="tr",
            user_turn_id="01USERTURNSOFT0000000000000",
        )

    turn_id = asyncio.run(run())

    assert turn_id == "01USERTURNSOFT0000000000000"
    mem = get_memory_core(tmp_path)
    assert mem.episodic.get_turn(turn_id) is None
    # Critical: the deleted conversation was NOT resurrected.
    assert svc.get(SOFT_CID) is None
