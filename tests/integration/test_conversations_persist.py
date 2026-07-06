"""Conversation archive API and persistence."""

from __future__ import annotations


import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app
from akana_server.conversation_service import ConversationService


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8766")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_create_and_list_conversations(client: TestClient) -> None:
    r = client.post("/api/v1/conversations", json={"title": "Planlama"})
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "Planlama"
    cid = body["id"]

    listed = client.get("/api/v1/conversations")
    assert listed.status_code == 200
    ids = [c["id"] for c in listed.json()["conversations"]]
    assert cid in ids


def test_untitled_conversation_title_follows_language(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug: a just-created (untitled) conversation showed the hardcoded Turkish
    "Yeni sohbet" even in English mode. The fallback now follows the unified
    ``language`` runtime setting (same picker that drives voice/persona/UI)."""
    from akana_server.runtime_settings.store import get_store, reset_runtime_stores

    monkeypatch.delenv("AKANA_LANGUAGE", raising=False)
    reset_runtime_stores()
    svc = ConversationService.for_data_dir(tmp_path)
    cid = svc.create().id  # no title → title_source="auto"

    # English-first default (no stored language) → English fallback.
    meta_en = svc.get(cid)
    assert meta_en is not None
    assert meta_en.title == "New chat"
    assert meta_en.title_source == "auto"

    # Flip the language picker to Turkish → Turkish fallback for the SAME row.
    get_store(tmp_path).set("language", "tr")
    meta_tr = svc.get(cid)
    assert meta_tr is not None
    assert meta_tr.title == "Yeni sohbet"

    reset_runtime_stores()


def test_turn_pair_updates_meta(client: TestClient, tmp_path) -> None:
    svc = ConversationService.for_data_dir(tmp_path)
    cid = svc.create().id
    user_id = "01USER00000000000000000000"
    asst_id = "01ASST00000000000000000000"
    svc._episodic.append_turn(
        turn_id=user_id,
        conversation_id=cid,
        role="user",
        text="Merhaba dünya",
    )
    svc._episodic.append_turn(
        turn_id=asst_id,
        conversation_id=cid,
        role="assistant",
        text="Selam!",
    )
    # Production meta bump (turn_writer pattern): user then assistant on the store.
    svc._meta_store.on_user_message(cid, "Merhaba dünya")
    svc._meta_store.on_assistant_message(cid)

    meta = svc.get(cid)
    assert meta is not None
    assert meta.message_count == 2
    assert "Merhaba" in (meta.title or "")

    messages = svc.list_messages(cid)
    assert len(messages) == 2
    assert messages[0].role == "user"


async def _mock_complete_chat(*_args, **_kwargs):
    return "Mock assistant reply.", {
        "prompt_tokens": 3,
        "completion_tokens": 5,
        "tool_calls": [],
    }


async def _mock_complete_chat_with_tools(*_args, **_kwargs):
    return "Aracı çağırdım.", {
        "prompt_tokens": 3,
        "completion_tokens": 5,
        "tool_calls": [
            {
                "id": "call_1",
                "name": "read_file",
                "phase": "end",
                "status": "ok",
                "args": {"path": "notes.md"},
                "result": "dosya içeriği",
            },
            {
                "id": "call_2",
                "name": "list_dir",
                "phase": "end",
                "status": "ok",
                "args": {"path": "."},
                "result": "a\nb\nc",
            },
        ],
    }


def test_two_chat_turns_persist_in_same_conversation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage",
        _mock_complete_chat,
    )
    created = client.post("/api/v1/conversations", json={"title": "Multi turn"})
    cid = created.json()["id"]

    for user_text in ("İlk soru", "İkinci soru"):
        r = client.post(
            "/api/v1/chat",
            json={"text": user_text, "conversation_id": cid},
        )
        assert r.status_code == 200
        assert r.json()["conversation_id"] == cid

    messages = client.get(f"/api/v1/conversations/{cid}/messages")
    assert messages.status_code == 200
    payload = messages.json()
    assert len(payload["messages"]) == 4
    assert [m["role"] for m in payload["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


def test_chat_turn_persists_and_reloads_via_messages(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage",
        _mock_complete_chat,
    )
    created = client.post("/api/v1/conversations", json={"title": "Persist test"})
    assert created.status_code == 200
    cid = created.json()["id"]

    chat = client.post(
        "/api/v1/chat",
        json={"text": "Kalıcı kullanıcı sorusu", "conversation_id": cid},
    )
    assert chat.status_code == 200
    body = chat.json()
    assert body["text"] == "Mock assistant reply."
    assert body["conversation_id"] == cid

    messages = client.get(f"/api/v1/conversations/{cid}/messages")
    assert messages.status_code == 200
    payload = messages.json()
    assert payload["conversation_id"] == cid
    roles = [m["role"] for m in payload["messages"]]
    assert roles == ["user", "assistant"]
    assert payload["messages"][0]["content"] == "Kalıcı kullanıcı sorusu"
    assert payload["messages"][1]["content"] == "Mock assistant reply."


def test_tool_calls_persist_and_reload_via_messages(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGRESSION: tool calls persist ON THE SERVER + /messages returns them.

    Bug: tool cards were only written to localStorage on the client SSE ``done``
    event. When a concurrent 2nd conversation finished IN THE BACKGROUND (the user
    is looking at another conversation) that turn's ``done`` never reached the client
    → the cards were lost forever. They are now written to the ``turns.tool_calls``
    column together with the assistant turn and /messages returns them; the client
    does NOT need to be connected.
    """
    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage",
        _mock_complete_chat_with_tools,
    )
    created = client.post("/api/v1/conversations", json={"title": "Araçlı tur"})
    cid = created.json()["id"]

    chat = client.post(
        "/api/v1/chat",
        json={"text": "dosyayı oku", "conversation_id": cid},
    )
    assert chat.status_code == 200
    assert len(chat.json()["tool_calls"]) == 2

    # Without the client ever connecting to the ``done`` event, a fresh /messages call
    # (simulating the turn that finished in the background) must return the tool calls in full.
    messages = client.get(f"/api/v1/conversations/{cid}/messages")
    assert messages.status_code == 200
    msgs = messages.json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["tool_calls"] == []  # no tool on the user turn
    asst_tools = msgs[1]["tool_calls"]
    assert [c["name"] for c in asst_tools] == ["read_file", "list_dir"]
    assert asst_tools[0]["result"] == "dosya içeriği"


def test_messages_without_tool_calls_returns_empty_list(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tool-less turn (and old turns from before the tool_calls column) → ``[]``."""
    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage",
        _mock_complete_chat,
    )
    created = client.post("/api/v1/conversations", json={"title": "Araçsız"})
    cid = created.json()["id"]
    client.post("/api/v1/chat", json={"text": "selam", "conversation_id": cid})

    msgs = client.get(f"/api/v1/conversations/{cid}/messages").json()["messages"]
    assert all(m["tool_calls"] == [] for m in msgs)


def test_append_turn_tool_calls_roundtrip(tmp_path) -> None:
    """Service level: append_turn(tool_calls=...) → list_messages returns them."""
    svc = ConversationService.for_data_dir(tmp_path)
    cid = svc.create().id
    calls = [{"id": "c1", "name": "search", "phase": "end", "result": "ok"}]
    svc._episodic.append_turn(
        turn_id="01ASST00000000000000000001",
        conversation_id=cid,
        role="assistant",
        text="sonuç",
        tool_calls=calls,
    )
    messages = svc.list_messages(cid)
    assert len(messages) == 1
    assert messages[0].tool_calls == calls
    # Persistence: a new service instance (a fresh connection) reads the same data too.
    svc2 = ConversationService.for_data_dir(tmp_path)
    assert svc2.list_messages(cid)[0].tool_calls == calls


def test_append_turn_ask_user_roundtrip(tmp_path) -> None:
    """Service level (U3): append_turn(ask_user=...) → list_messages returns the payload.

    A question turn must persist its STRUCTURED AskUser payload so the interactive
    card can be re-rendered after a chat switch / reload — not just the summary text.
    """
    svc = ConversationService.for_data_dir(tmp_path)
    cid = svc.create().id
    ask = {
        "id": "ask-1",
        "questions": [
            {
                "question": "Çay mı kahve mi?",
                "header": "İçecek",
                "multiSelect": False,
                "options": [{"label": "Çay"}, {"label": "Kahve"}],
            }
        ],
    }
    svc._episodic.append_turn(
        turn_id="01ASST00000000000000000010",
        conversation_id=cid,
        role="assistant",
        text="Çay mı kahve mi?",
        ask_user=ask,
    )
    messages = svc.list_messages(cid)
    assert len(messages) == 1
    assert messages[0].ask_user == ask
    # A fresh service instance (fresh connection) reads the same durable payload.
    svc2 = ConversationService.for_data_dir(tmp_path)
    assert svc2.list_messages(cid)[0].ask_user == ask
    # A normal (non-question) turn stays None.
    svc._episodic.append_turn(
        turn_id="01ASST00000000000000000011",
        conversation_id=cid,
        role="assistant",
        text="merhaba",
    )
    assert svc.list_messages(cid)[1].ask_user is None


def test_messages_route_includes_ask_user(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Route level (U3): GET /messages emits the ask_user dict on a question turn only."""
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8766")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    app = create_app()
    with TestClient(app) as client:
        cid = client.post("/api/v1/conversations", json={"title": "Soru"}).json()["id"]
        svc = ConversationService.for_data_dir(tmp_path)
        ask = {"id": "ask-2", "questions": [{"question": "Hangisi?", "options": [{"label": "A"}]}]}
        svc._episodic.append_turn(
            turn_id="01ASST00000000000000000020",
            conversation_id=cid,
            role="assistant",
            text="Hangisi?",
            ask_user=ask,
        )
        svc._episodic.append_turn(
            turn_id="01ASST00000000000000000021",
            conversation_id=cid,
            role="assistant",
            text="normal cevap",
        )
        msgs = client.get(f"/api/v1/conversations/{cid}/messages").json()["messages"]
        by_text = {m["content"]: m for m in msgs}
        assert by_text["Hangisi?"]["ask_user"] == ask
        # A normal turn omits the field entirely (no null noise on non-question turns).
        assert "ask_user" not in by_text["normal cevap"]


def test_ask_user_migrates_on_old_schema_db(tmp_path) -> None:
    """U3: an old memory.db without the ask_user column migrates via idempotent ALTER.

    Mirrors the additive-JSON-column pattern (tool_calls/usage): opening the store on a
    legacy DB adds the column; old rows read None and new rows round-trip.
    """
    import sqlite3

    db_dir = tmp_path / "db"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "memory.db"
    # Hand-craft a legacy turns table WITHOUT ask_user (has tool_calls/file_ids/usage).
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE turns (
            id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, ts TEXT NOT NULL,
            role TEXT NOT NULL, text TEXT NOT NULL, lang TEXT, importance REAL,
            tool_call_id TEXT, duration_ms INTEGER, tool_calls TEXT, file_ids TEXT, usage TEXT
        );
        INSERT INTO turns (id, conversation_id, ts, role, text)
        VALUES ('old1', 'c-old', '2020-01-01T00:00:00.000Z', 'assistant', 'eski cevap');
        """
    )
    conn.commit()
    conn.close()

    svc = ConversationService.for_data_dir(tmp_path)  # opening runs the migration
    # Old row reads ask_user=None (no crash).
    old = svc.list_messages("c-old")
    assert len(old) == 1
    assert old[0].ask_user is None
    # A new question turn on the migrated DB round-trips the payload.
    ask = {"id": "ask-3", "questions": [{"question": "Q?", "options": [{"label": "X"}]}]}
    svc._episodic.append_turn(
        turn_id="01ASST00000000000000000030",
        conversation_id="c-old",
        role="assistant",
        text="Q?",
        ask_user=ask,
    )
    assert svc.list_messages("c-old")[-1].ask_user == ask


def test_blocking_llm_error_does_not_persist_orphan_turn(
    client: TestClient, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5: LLM error on a blocking turn → there must be NO orphan/dangling user turn.

    The user turn used to be persisted BEFORE the LLM call; when LLMCallError was
    raised, an orphan turn with no assistant + a meta counter off by 1 remained. Now
    persistence happens AFTER success (the same contract as post_voice 5c2ddd4) → on
    the error path nothing is written.
    """
    from akana_server.conversation_service import ConversationService
    from akana_server.orchestrator.llm_dispatch import LLMCallError

    async def boom(settings, user_text, **kwargs):
        raise LLMCallError("upstream blew up", status_code=502)

    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage", boom
    )
    cid = client.post("/api/v1/conversations", json={"title": "Hata turu"}).json()["id"]
    r = client.post("/api/v1/chat", json={"text": "patla", "conversation_id": cid})
    assert r.status_code == 502

    # the v2 store is read directly (the same get_memory_core singleton as the app):
    svc = ConversationService(tmp_path)
    assert svc.list_messages(cid) == []  # neither a user nor an assistant turn was written
    meta = svc.get(cid)
    assert meta is not None and meta.message_count == 0  # the counter did not drift


def test_chat_to_soft_deleted_conversation_does_not_resurrect(
    client: TestClient, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#8: a turn arriving at a soft-deleted conversation is NOT written despite a successful LLM.

    The persist path _conversation_chat_usable → looks at svc.get(); the v2 adapter
    returns None for a deleted (json_metadata.deleted) conversation → the turn is not
    persisted → the deleted history is not written back (no resurrection).
    """
    from akana_server.conversation_service import ConversationService

    async def ok(settings, user_text, **kwargs):
        return "cevap", {"prompt_tokens": 1, "completion_tokens": 1, "tool_calls": []}

    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage", ok
    )
    cid = client.post("/api/v1/conversations", json={"title": "Silinecek"}).json()["id"]
    assert client.delete(f"/api/v1/conversations/{cid}").status_code == 204

    client.post("/api/v1/chat", json={"text": "merhaba", "conversation_id": cid})

    svc = ConversationService(tmp_path)
    assert svc.get(cid) is None  # still deleted (not resurrected)
    assert svc.list_messages(cid) == []  # no turn was written


def test_empty_conversation_messages_and_chat_restore(client: TestClient) -> None:
    created = client.post("/api/v1/conversations", json={"title": "Boş sohbet"})
    assert created.status_code == 200
    cid = created.json()["id"]

    messages = client.get(f"/api/v1/conversations/{cid}/messages")
    assert messages.status_code == 200
    assert messages.json()["messages"] == []

    restore = client.get(f"/api/v1/chat/conversations/{cid}")
    assert restore.status_code == 200
    body = restore.json()
    assert body["conversation_id"] == cid
    assert body["turns"] == []
    assert body["dropped_turns"] == 0


def test_delete_conversation_clears_messages_and_meta(
    client: TestClient,
    tmp_path,
) -> None:
    created = client.post("/api/v1/conversations", json={"title": "Silinecek REST"})
    cid = created.json()["id"]
    r = client.delete(f"/api/v1/conversations/{cid}")
    assert r.status_code == 204
    assert client.get(f"/api/v1/conversations/{cid}").status_code == 404
    assert client.get(f"/api/v1/conversations/{cid}/messages").status_code == 404
    svc = ConversationService.for_data_dir(tmp_path)
    assert svc.get(cid) is None
    assert svc.list_messages(cid) == []


def test_delete_conversation_purges_fts_rows_set_based(tmp_path) -> None:
    """PERF + correctness: ``delete_conversation`` deletes turns + FTS rows.

    The delete path used to rely on the ``turns_fts_ad`` trigger and run a separate
    FTS DELETE for each row (in a long conversation ~0.34 ms/row → seconds). Now the
    FTS rows are deleted in a SINGLE statement via ``conversation_id``.
    This test both indirectly protects the speed (the set-based path) and verifies
    that no ORPHAN FTS row remains (search must not return the deleted conversation).
    """
    svc = ConversationService.for_data_dir(tmp_path)
    cid = svc.create(title="Silinecek FTS").id
    for j in range(50):
        svc._episodic.append_turn(
            turn_id=f"{cid}-t{j}",
            conversation_id=cid,
            role="user",
            text=f"benzersizkelime{j} mesaj icerigi",
        )
    # Before deletion, search finds it
    assert svc.search("benzersizkelime0")
    deleted = svc._episodic.delete_conversation(cid)
    assert deleted == 50  # a single set-based delete removed all the turns

    # No orphan FTS row must remain → search now returns nothing
    # (search_keyword runs over FTS → returning empty proves there is no orphan row)
    assert svc._episodic.search_keyword("benzersizkelime0") == []


def test_delete_conversation_is_idempotent_after_purge(tmp_path) -> None:
    """The second delete is a no-op (0 rows) — the route no longer double-deletes but let it stay safe."""
    svc = ConversationService.for_data_dir(tmp_path)
    cid = svc.create(title="İki kez").id
    svc._episodic.append_turn(
        turn_id=f"{cid}-t0", conversation_id=cid, role="user", text="tek mesaj"
    )
    assert svc._episodic.delete_conversation(cid) == 1
    assert svc._episodic.delete_conversation(cid) == 0  # already gone — safely a no-op


def test_messages_unknown_conversation_does_not_create_row(
    client: TestClient,
    tmp_path,
) -> None:
    missing = "01UNKNOWN000000000000000000"
    r = client.get(f"/api/v1/conversations/{missing}/messages")
    assert r.status_code == 404

    svc = ConversationService.for_data_dir(tmp_path)
    assert svc.get(missing) is None
