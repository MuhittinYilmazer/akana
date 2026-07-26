"""Vault secrets must never reach ``memory.db`` through a turn's ``tool_calls``.

The vault's whole design is "ciphertext at rest, master key OUTSIDE ~/.akana", and
``akana backup`` tells the user the archive holds no plaintext secret. A turn's
``tool_calls`` column is written into that same archive (``db/memory.db``), so a single
``vault_get`` used to convert an encrypted secret into a permanent plaintext copy inside
the very directory the master key was deliberately kept out of.

The turn row must still show WHICH vault tool ran and whether it succeeded — the tool card
is the user's only trace of the access — so only the secret-carrying fields are dropped.

NOTE: the sync ``def`` + ``asyncio.run(...)`` idiom is intentional — the fast path
(``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1``) cannot load pytest-asyncio.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from akana_server.api.routes.chat.persist import _persist_assistant_turn_end
from akana_server.conversation_service import ConversationService
from akana_server.memory_core import get_memory_core

CID = "01CONVVAULTLEAKPROBE0000000"
SECRET = "ghp_SUPERSECRET_TOKEN_0123456789abcdef"


def _fake_request(tmp_path: Path):
    state = SimpleNamespace(
        conversation_service=ConversationService(tmp_path),
        settings=SimpleNamespace(data_dir=tmp_path),
        chat_cleanup_tombstones=set(),
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _persist(tmp_path: Path, tool_calls: list[dict], *, turn: str) -> None:
    request = _fake_request(tmp_path)
    request.app.state.conversation_service.ensure(CID)

    async def run() -> list[dict[str, str]]:
        return await _persist_assistant_turn_end(
            request,
            conversation_id=CID,
            user_text="read my github token",
            assistant_text="Done.",
            user_turn_id="01USERVAULT00000000000000000",
            assistant_turn_id=turn,
            lang="en",
            latency_ms=3,
            intent="chat",
            tool_calls=tool_calls,
            stage_captures=False,
        )

    asyncio.run(run())


def _db_bytes(tmp_path: Path) -> bytes:
    """Every on-disk byte of memory.db INCLUDING the -wal sidecar.

    Reading only ``memory.db`` would pass while the row still sits in the
    write-ahead log — a green test that proves nothing.
    """
    blob = b""
    for p in sorted((tmp_path / "db").glob("memory.db*")):
        blob += p.read_bytes()
    return blob


def test_vault_get_result_is_not_stored_in_memory_db(tmp_path: Path) -> None:
    """``vault_get``'s result is the decrypted secret itself — it must not land on disk."""
    tid = "01ASSTVAULTGET00000000000000"
    _persist(
        tmp_path,
        [
            {
                "id": "toolu_1",
                "name": "mcp__akana_vault__vault_get",
                "phase": "end",
                "args": {"key": "github_token"},
                "result": [
                    {
                        "type": "text",
                        "text": '{"key": "github_token", "value": "%s"}' % SECRET,
                    }
                ],
                "status": "ok",
            }
        ],
        turn=tid,
    )
    stored = get_memory_core(tmp_path).episodic.get_turn(tid)
    assert stored is not None
    assert SECRET.encode() not in _db_bytes(tmp_path), (
        "the vault secret was archived in PLAINTEXT in memory.db"
    )
    # The access itself stays visible: which tool ran, and that it succeeded.
    call = stored.tool_calls[0]
    assert call["name"] == "mcp__akana_vault__vault_get"
    assert call["status"] == "ok"
    assert call["args"]["key"] == "github_token"  # the NAME is not the secret


def test_vault_set_value_argument_is_not_stored(tmp_path: Path) -> None:
    """``vault_set`` carries the secret in its ARGS (phase=start), not its result."""
    tid = "01ASSTVAULTSET00000000000000"
    _persist(
        tmp_path,
        [
            {
                "id": "toolu_2",
                "name": "vault_set",  # gemini/openai native name (no mcp__ prefix)
                "phase": "end",
                "args": {"key": "github_token", "value": SECRET},
                "result": "Saved secret 'github_token' to the vault.",
                "status": "ok",
            }
        ],
        turn=tid,
    )
    stored = get_memory_core(tmp_path).episodic.get_turn(tid)
    assert stored is not None
    assert SECRET.encode() not in _db_bytes(tmp_path)
    assert stored.tool_calls[0]["args"]["key"] == "github_token"
    assert stored.tool_calls[0]["status"] == "ok"


def test_vault_get_credential_fields_are_not_stored(tmp_path: Path) -> None:
    """A whole credential profile (every field value) rides one result."""
    tid = "01ASSTVAULTCRED0000000000000"
    _persist(
        tmp_path,
        [
            {
                "id": "toolu_3",
                "name": "akana_vault/vault_get_credential",
                "phase": "end",
                "args": {"namespace": "reddit", "profile": "default"},
                "result": {"fields": {"username": "alice", "password": SECRET}},
                "status": "ok",
            }
        ],
        turn=tid,
    )
    assert SECRET.encode() not in _db_bytes(tmp_path)
    stored = get_memory_core(tmp_path).episodic.get_turn(tid)
    assert stored is not None
    assert stored.tool_calls[0]["args"]["namespace"] == "reddit"


def test_non_vault_tool_calls_round_trip_unchanged(tmp_path: Path) -> None:
    """Redaction is scoped to the vault: every other tool card must survive intact."""
    tid = "01ASSTREADTOOL000000000000000"[:26]
    _persist(
        tmp_path,
        [
            {
                "id": "toolu_4",
                "name": "Read",
                "phase": "end",
                "args": {"file_path": "/tmp/a.txt", "value": "not-a-secret"},
                "result": "hello world",
                "status": "ok",
            }
        ],
        turn=tid,
    )
    stored = get_memory_core(tmp_path).episodic.get_turn(tid)
    assert stored is not None
    call = stored.tool_calls[0]
    assert call["args"] == {"file_path": "/tmp/a.txt", "value": "not-a-secret"}
    assert call["result"] == "hello world"


def test_redaction_does_not_mutate_the_callers_live_tool_calls(tmp_path: Path) -> None:
    """The caller's list is the SAME object the SSE ``done`` payload and the client tool
    card cache read; redacting it in place would blank the live card the user is watching."""
    live = [
        {
            "id": "toolu_5",
            "name": "vault_get",
            "phase": "end",
            "args": {"key": "github_token"},
            "result": {"key": "github_token", "value": SECRET},
            "status": "ok",
        }
    ]
    _persist(tmp_path, live, turn="01ASSTVAULTNOMUT000000000000")
    assert live[0]["result"] == {"key": "github_token", "value": SECRET}
    assert live[0]["args"] == {"key": "github_token"}


def test_the_voice_persist_path_is_covered_too(tmp_path: Path) -> None:
    """The chat surface is not the only writer — the redaction must sit with the WRITER.

    ``api/routes/voice.py`` does not go through ``_persist_assistant_turn_end``; it calls
    ``turn_writer.persist_assistant_turn`` directly. When the scrub lived in the chat
    module, a ``vault_get`` spoken out loud still archived the plaintext secret. This
    pins the property at the single write path, so any future caller inherits it.
    """
    from akana_server.orchestrator import turn_writer

    (tmp_path / "db").mkdir(parents=True, exist_ok=True)
    turn_writer.persist_user_turn(
        conversation_id="conv-voice",
        user_text="read my github token out loud",
        turn_id="u-voice",
        data_dir=tmp_path,
    )
    turn_writer.persist_assistant_turn(
        conversation_id="conv-voice",
        assistant_text="Here it is.",
        user_turn_id="u-voice",
        assistant_turn_id="a-voice",
        data_dir=tmp_path,
        tool_calls=[
            {
                "id": "t1",
                "name": "mcp__akana_vault__vault_get",
                "status": "ok",
                "result": {"key": "github_token", "value": SECRET},
            }
        ],
    )
    blob = _db_bytes(tmp_path)
    assert SECRET.encode() not in blob, (
        "a vault secret spoken through the VOICE turn was archived in plaintext"
    )
    # The trace of the access must survive — the row is the user's only durable record
    # that a vault read happened at all.
    assert b"vault_get" in blob
