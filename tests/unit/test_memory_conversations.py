"""ConversationStore: CRUD, auto-title, json_metadata, search, LLM window."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from akana.memory import Memory
from akana.memory.conversations import ConversationMeta, ConversationStore
from akana.memory.episodic import EpisodicStore


@pytest.fixture()
def episodic(tmp_path: Path) -> EpisodicStore:
    return EpisodicStore(tmp_path / "memory.db")


@pytest.fixture()
def store(tmp_path: Path, episodic: EpisodicStore) -> ConversationStore:
    return ConversationStore(tmp_path / "memory.db", episodic=episodic)


# -- CRUD ---------------------------------------------------------------------


def test_ensure_creates_empty_row(store: ConversationStore) -> None:
    meta = store.ensure("c1")
    assert meta.id == "c1"
    assert meta.title is None
    assert meta.archived is False
    assert meta.message_count == 0
    assert meta.json_metadata == {}
    assert meta.created_at and meta.updated_at


def test_ensure_is_idempotent(store: ConversationStore) -> None:
    first = store.ensure("c1")
    store.on_user_message("c1", "merhaba")
    again = store.ensure("c1")
    assert again.created_at == first.created_at
    assert again.message_count == 1  # existing row untouched by ensure


def test_get_missing_returns_none(store: ConversationStore) -> None:
    assert store.get("nope") is None


def test_patch_title_and_archived(store: ConversationStore) -> None:
    store.ensure("c1")
    meta = store.patch("c1", title="  Plan  ", archived=True)
    assert meta is not None
    assert meta.title == "Plan"
    assert meta.archived is True
    # None leaves fields untouched; empty title keeps the old one
    meta = store.patch("c1", title="   ", archived=False)
    assert meta is not None
    assert meta.title == "Plan"
    assert meta.archived is False


def test_patch_missing_returns_none(store: ConversationStore) -> None:
    assert store.patch("ghost", title="x") is None


def test_soft_delete_archives(store: ConversationStore) -> None:
    store.ensure("c1")
    assert store.soft_delete("c1") is True
    meta = store.get("c1")
    assert meta is not None and meta.archived is True  # row survives (soft)
    assert [m.id for m in store.list()] == []
    assert [m.id for m in store.list(include_archived=True)] == ["c1"]
    assert store.soft_delete("ghost") is False


def test_list_orders_by_updated_at_desc_and_limits(store: ConversationStore) -> None:
    for cid in ("c1", "c2", "c3"):
        store.ensure(cid)
        time.sleep(0.005)  # distinct millisecond timestamps
    store.on_user_message("c1", "en taze bu")  # c1 bumps to the top
    assert [m.id for m in store.list()] == ["c1", "c3", "c2"]
    assert [m.id for m in store.list(limit=2)] == ["c1", "c3"]


def test_list_archived_only_survives_newer_active_beyond_ceiling(
    store: ConversationStore,
) -> None:
    """archived_only must filter to archived rows in SQL BEFORE the 200-row ceiling.

    Regression: the Archived view fetched a MIXED active+archived window (include_archived
    → base '1=1') trimmed to the ceiling by updated_at; an archived conversation older than
    the newest 200 active ones fell out of the window and — search hard-codes archived=0 —
    became invisible and un-unarchivable. With archived_only the archived row is always in
    the result regardless of how many newer active conversations exist.
    """
    store.ensure("arch")
    store.patch("arch", archived=True)  # oldest, archived
    for i in range(201):  # more than the 200 store ceiling, all newer + active
        store.ensure(f"active-{i:03d}")
    got = store.list(limit=50, archived_only=True)
    assert [m.id for m in got] == ["arch"]  # only the archived one, and it's present
    # Sanity: the mixed window (old behavior) would NOT contain it — the archived row is
    # older than 200 active rows, so it spills out of the ceiling.
    mixed = store.list(limit=50, include_archived=True)
    assert "arch" not in {m.id for m in mixed}


# -- auto-title + message hooks -------------------------------------------------


def test_on_user_message_sets_auto_title_once(store: ConversationStore) -> None:
    meta = store.on_user_message("c1", "  kahve   makinesi\nbozuldu  ")
    assert meta.title == "kahve makinesi bozuldu"  # whitespace squeezed
    assert meta.message_count == 1
    meta = store.on_user_message("c1", "başka bir konu")
    assert meta.title == "kahve makinesi bozuldu"  # first title sticks
    assert meta.message_count == 2


def test_auto_title_truncates_to_60_chars(store: ConversationStore) -> None:
    meta = store.on_user_message("c1", "a" * 200)
    assert meta.title is not None
    assert len(meta.title) <= 60
    assert meta.title.endswith("…")


def test_blank_user_text_leaves_title_empty(store: ConversationStore) -> None:
    meta = store.on_user_message("c1", "   \n  ")
    assert meta.title is None
    assert meta.message_count == 1


def test_user_set_title_not_overwritten_by_auto(store: ConversationStore) -> None:
    store.ensure("c1")
    store.patch("c1", title="Benim başlığım")
    meta = store.on_user_message("c1", "ilk mesaj")
    assert meta.title == "Benim başlığım"


def test_on_assistant_message_bumps_count(store: ConversationStore) -> None:
    store.on_user_message("c1", "soru")
    meta = store.on_assistant_message("c1")
    assert meta.message_count == 2
    assert meta.title == "soru"  # untouched


# -- json metadata ---------------------------------------------------------------


def test_merge_and_get_json_metadata(store: ConversationStore) -> None:
    # merge auto-ensures the row (cursor bridge may write before first turn)
    merged = store.merge_json_metadata("c1", {"agent_id": "agent-1", "x": 1})
    assert merged == {"agent_id": "agent-1", "x": 1}
    merged = store.merge_json_metadata("c1", {"x": None, "y": "z"})  # None removes
    assert merged == {"agent_id": "agent-1", "y": "z"}
    assert store.get_json_metadata("c1") == {"agent_id": "agent-1", "y": "z"}
    assert store.get_json_metadata("ghost") == {}
    meta = store.get("c1")
    assert meta is not None and meta.json_metadata["agent_id"] == "agent-1"


# -- search ----------------------------------------------------------------------


def test_search_matches_title_and_turn_text(
    store: ConversationStore, episodic: EpisodicStore
) -> None:
    store.ensure("c-title")
    store.patch("c-title", title="Kahve planı")
    store.ensure("c-turns")
    episodic.append_turn(
        turn_id="t1", conversation_id="c-turns", role="user",
        text="bugün kahve makinesi bozuldu",
    )
    hits = store.search("kahve")
    assert {m.id for m in hits} == {"c-title", "c-turns"}


def test_search_excludes_archived_and_dedupes(
    store: ConversationStore, episodic: EpisodicStore
) -> None:
    store.on_user_message("c1", "kahve sohbeti")  # title mentions kahve
    episodic.append_turn(
        turn_id="t1", conversation_id="c1", role="user", text="kahve sohbeti"
    )
    assert [m.id for m in store.search("kahve")] == ["c1"]  # deduped
    store.soft_delete("c1")
    assert store.search("kahve") == []
    assert store.search("   ") == []


def test_search_without_episodic_is_title_only(tmp_path: Path) -> None:
    solo = ConversationStore(tmp_path / "memory.db")
    solo.ensure("c1")
    solo.patch("c1", title="Kahve planı")
    assert [m.id for m in solo.search("kahve")] == ["c1"]
    assert solo.recent_llm_messages("c1") == []


# -- recent LLM window -------------------------------------------------------------


def test_recent_llm_messages_filters_and_caps(
    store: ConversationStore, episodic: EpisodicStore
) -> None:
    turns = [
        ("user", "q1"), ("assistant", "a1"), ("tool", "tool çıktısı"),
        ("user", "q2"), ("system", "sys"), ("assistant", "a2"),
    ]
    for i, (role, text) in enumerate(turns):
        episodic.append_turn(
            turn_id=f"t{i}", conversation_id="c1", role=role, text=text,  # type: ignore[arg-type]
            ts=f"2026-06-01T10:00:{i:02d}.000Z",
        )
    msgs = store.recent_llm_messages("c1", max_turns=3)
    assert msgs == [
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
    ]
    assert store.recent_llm_messages("empty") == []


def test_recent_llm_messages_excludes_error_role(
    store: ConversationStore, episodic: EpisodicStore
) -> None:
    """Failed turns are persisted as ``role="error"`` (so the UI can re-render the
    error card after F5), but they must NEVER enter the LLM history window — otherwise
    a stored "model unavailable" string would pollute a later turn's context. Only
    user/assistant turns flow to the model; the error turn between them is dropped."""
    turns = [
        ("user", "q1"),
        ("error", "LLM_UNAVAILABLE: model is currently unavailable"),
        ("user", "q2"),
        ("assistant", "a2"),
    ]
    for i, (role, text) in enumerate(turns):
        episodic.append_turn(
            turn_id=f"e{i}", conversation_id="cerr", role=role, text=text,  # type: ignore[arg-type]
            ts=f"2026-06-02T10:00:{i:02d}.000Z",
        )
    msgs = store.recent_llm_messages("cerr", max_turns=10)
    assert msgs == [
        {"role": "user", "content": "q1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
    ]
    assert all(m["role"] != "error" for m in msgs)


def test_recent_llm_messages_returns_newest_window_beyond_1000(
    store: ConversationStore, episodic: EpisodicStore
) -> None:
    """In a 1000+ turn conversation the NEWEST N turns must be returned — last turn included (R3-#2).

    Regression: the old code used ``list_conversation(limit=1000)`` with ``ts ASC`` to pull
    the OLDEST 1000 and then sliced ``[-cap:]`` → in an 1100-turn conversation the newest ~100
    turns (the model context) NEVER arrived. Now ``list_conversation_recent`` pulls the newest
    window in SQL (``ts DESC``); the newest message (msg-01099) must arrive.
    """
    total = 1100
    for i in range(total):
        role = "user" if i % 2 == 0 else "assistant"
        episodic.append_turn(
            turn_id=f"m{i:05d}",
            conversation_id="big",
            role=role,  # type: ignore[arg-type]
            text=f"msg-{i:05d}",
            # Monotonically increasing, ms-resolution ts → chronological order is unambiguous.
            ts=f"2026-06-01T{10 + i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}.{i % 1000:03d}Z",
        )
    msgs = store.recent_llm_messages("big", max_turns=10)
    # The newest 10 user/assistant messages, in chronological (ASC) order.
    assert [m["content"] for m in msgs] == [f"msg-{i:05d}" for i in range(total - 10, total)]
    # The most critical point: the real last turn (which the old code MISSED) is here.
    assert msgs[-1] == {"role": "assistant", "content": "msg-01099"}


# -- façade wiring ------------------------------------------------------------------


def test_memory_conversations_meta_property(tmp_path: Path) -> None:
    mem = Memory.for_data_dir(tmp_path)
    store = mem.conversations_meta
    assert isinstance(store, ConversationStore)
    assert mem.conversations_meta is store  # cached
    # shares memory.db with episodic: turn text is searchable through the store
    mem.remember_turn(role="user", conversation_id="c1", text="kahve içtik")
    store.on_user_message("c1", "kahve içtik")
    meta = store.get("c1")
    assert isinstance(meta, ConversationMeta)
    assert meta.title == "kahve içtik"
    assert {m.id for m in store.search("kahve")} == {"c1"}


def test_search_title_turkish_fold_insensitive(store: ConversationStore) -> None:
    """Title search is Turkish case-folding: an 'İstanbul' title is found with 'istanbul'.

    SQLite LIKE folds ASCII only; since in the raw column 'İ' ≠ 'i', Turkish
    uppercase titles never matched a lowercase query.
    """
    store.ensure("c1")
    store.patch("c1", title="İstanbul gezisi planı")
    assert [m.id for m in store.search("istanbul")] == ["c1"]
    assert [m.id for m in store.search("İSTANBUL")] == ["c1"]
    assert [m.id for m in store.search("GEZİSİ")] == ["c1"]
    # LIKE wildcards are still literal (escaping preserved)
    assert store.search("%stanbul%") == []
