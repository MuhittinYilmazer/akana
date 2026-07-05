"""SecureVault MCP server — read/write/delete tools + JSON-RPC dispatch + payload wiring.

Scope (in-process; NO real stdio/subprocess):

* vault tools — read (list/get/get_credential) AND write/delete (set/set_credential/
  delete/delete_credential) — over the REAL ``secure_vault`` (the autouse
  ``_isolated_vault_key`` fixture gives each test a throwaway master key, so
  writes/reads round-trip with real crypto),
* MCP protocol: initialize/ping/tools.list/tools.call + error paths,
* ``mcp_servers_payload`` includes the vault entry by default; ``AKANA_VAULT_TOOLS=0``
  disables it; the master-key env is forwarded into the child env; vault is file/crypto
  (NO loopback REST / conversation_id in the entry).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from akana_server import secure_vault
from akana_server.vault_mcp.mcp import McpServer, mcp_tool_list
from akana_server.vault_mcp.tools import VaultTools


def _call(srv: McpServer, name: str, arguments: dict) -> dict:
    resp = srv.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    result = resp["result"]
    payload = json.loads(result["content"][0]["text"])
    return {"isError": result["isError"], "payload": payload}


@pytest.fixture
def tools(tmp_path) -> VaultTools:
    return VaultTools(tmp_path)


@pytest.fixture
def srv(tools: VaultTools) -> McpServer:
    return McpServer(tools)


# -- tools/list -----------------------------------------------------------------------


def test_tool_list_exposes_vault_tools() -> None:
    names = {t["name"] for t in mcp_tool_list()}
    assert names == {
        "vault_list",
        "vault_get",
        "vault_get_credential",
        "vault_set",
        "vault_set_credential",
        "vault_delete",
        "vault_delete_credential",
    }
    # Every tool has a description + JSON Schema input schema (for model selection).
    for t in mcp_tool_list():
        assert t["description"]
        assert t["inputSchema"]["type"] == "object"


# -- MCP protocol paths ---------------------------------------------------------------


def test_initialize_returns_server_info(srv: McpServer) -> None:
    resp = srv.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    assert resp["result"]["serverInfo"]["name"] == "akana-vault"
    assert resp["result"]["capabilities"]["tools"]["listChanged"] is False


def test_ping_and_notifications(srv: McpServer) -> None:
    assert srv.handle({"jsonrpc": "2.0", "id": 1, "method": "ping"})["result"] == {}
    # notification (no id) → no response
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method_is_method_not_found(srv: McpServer) -> None:
    resp = srv.handle({"jsonrpc": "2.0", "id": 9, "method": "frobnicate"})
    assert resp["error"]["code"] == -32601


def test_batch_rejected(srv: McpServer) -> None:
    resp = srv.handle([{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert resp["error"]["code"] == -32600


def test_non_object_request_is_invalid(srv: McpServer) -> None:
    resp = srv.handle("not-a-dict")
    assert resp["error"]["code"] == -32600


def test_tools_call_requires_name(srv: McpServer) -> None:
    resp = srv.handle(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {}}
    )
    assert resp["error"]["code"] == -32602


def test_unknown_tool_is_error_result(srv: McpServer) -> None:
    out = _call(srv, "yok_boyle_arac", {})
    assert out["isError"] is True
    assert "unknown tool" in out["payload"]["error"].lower()


# -- vault read tools (REAL secure_vault) ---------------------------------------------


def test_vault_list_reports_scalars_and_credentials(srv: McpServer, tmp_path) -> None:
    secure_vault.set_scalar(tmp_path, "github_token", "ghp_xxx")
    secure_vault.set_fields(tmp_path, "reddit", {"username": "u", "password": "p"})
    out = _call(srv, "vault_list", {})
    assert out["isError"] is False
    assert out["payload"]["scalars"] == ["github_token"]
    creds = out["payload"]["credentials"]
    assert creds == [
        {"namespace": "reddit", "profile": "default", "fields": ["password", "username"]}
    ]


def test_vault_get_returns_value(srv: McpServer, tmp_path) -> None:
    secure_vault.set_scalar(tmp_path, "github_token", "ghp_secret")
    out = _call(srv, "vault_get", {"key": "github_token"})
    assert out["isError"] is False
    assert out["payload"] == {"key": "github_token", "value": "ghp_secret"}


def test_vault_get_missing_is_error(srv: McpServer) -> None:
    out = _call(srv, "vault_get", {"key": "nope"})
    assert out["isError"] is True
    assert "no secret" in out["payload"]["error"].lower()


def test_vault_get_empty_key_is_error(srv: McpServer) -> None:
    out = _call(srv, "vault_get", {"key": "  "})
    assert out["isError"] is True
    assert "empty" in out["payload"]["error"].lower()


def test_vault_get_credential_whole_bundle(srv: McpServer, tmp_path) -> None:
    secure_vault.set_fields(tmp_path, "reddit", {"username": "u", "password": "p"})
    out = _call(srv, "vault_get_credential", {"namespace": "reddit"})
    assert out["isError"] is False
    assert out["payload"]["namespace"] == "reddit"
    assert out["payload"]["profile"] == "default"
    assert out["payload"]["fields"] == {"username": "u", "password": "p"}


def test_vault_get_credential_single_field(srv: McpServer, tmp_path) -> None:
    secure_vault.set_fields(tmp_path, "reddit", {"username": "u", "password": "p"})
    out = _call(srv, "vault_get_credential", {"namespace": "reddit", "field": "password"})
    assert out["isError"] is False
    assert out["payload"] == {
        "namespace": "reddit",
        "profile": "default",
        "field": "password",
        "value": "p",
    }


def test_vault_get_credential_unknown_field_is_error(srv: McpServer, tmp_path) -> None:
    secure_vault.set_fields(tmp_path, "reddit", {"username": "u"})
    out = _call(srv, "vault_get_credential", {"namespace": "reddit", "field": "token"})
    assert out["isError"] is True
    assert "not found" in out["payload"]["error"].lower()


def test_vault_get_credential_missing_namespace_is_error(srv: McpServer) -> None:
    out = _call(srv, "vault_get_credential", {"namespace": "ghost"})
    assert out["isError"] is True
    assert "no credential" in out["payload"]["error"].lower()


def test_vault_get_credential_invalid_namespace_is_clean_error(srv: McpServer) -> None:
    # Uppercase fails the ^[a-z]... charset → ValueError inside secure_vault → clean text.
    out = _call(srv, "vault_get_credential", {"namespace": "BadNS"})
    assert out["isError"] is True
    assert "invalid request" in out["payload"]["error"].lower()


# -- vault write/delete tools (REAL secure_vault) -------------------------------------


def test_vault_set_then_get_roundtrip(srv: McpServer, tmp_path) -> None:
    out = _call(srv, "vault_set", {"key": "github_token", "value": "ghp_new"})
    assert out["isError"] is False
    assert out["payload"] == {"key": "github_token", "status": "saved"}
    # the value is really persisted and readable back
    assert secure_vault.get_scalar(tmp_path, "github_token") == "ghp_new"
    got = _call(srv, "vault_get", {"key": "github_token"})
    assert got["payload"]["value"] == "ghp_new"


def test_vault_set_empty_value_is_error(srv: McpServer) -> None:
    out = _call(srv, "vault_set", {"key": "k", "value": "  "})
    assert out["isError"] is True
    assert "empty" in out["payload"]["error"].lower()


def test_vault_set_credential_merges_fields(srv: McpServer, tmp_path) -> None:
    a = _call(srv, "vault_set_credential", {"namespace": "reddit", "field": "username", "value": "u"})
    assert a["isError"] is False
    assert a["payload"]["status"] == "saved"
    _call(srv, "vault_set_credential", {"namespace": "reddit", "field": "password", "value": "p"})
    # both fields coexist (set_fields merges, not replaces)
    assert secure_vault.load_fields(tmp_path, "reddit", "default") == {
        "username": "u",
        "password": "p",
    }


def test_vault_delete_removes_scalar(srv: McpServer, tmp_path) -> None:
    secure_vault.set_scalar(tmp_path, "github_token", "ghp_xxx")
    out = _call(srv, "vault_delete", {"key": "github_token"})
    assert out["isError"] is False
    assert out["payload"] == {"key": "github_token", "removed": True, "status": "deleted"}
    assert secure_vault.get_scalar(tmp_path, "github_token") is None


def test_vault_delete_absent_scalar_reports_absent(srv: McpServer) -> None:
    # Honest signal: deleting something that was never there is not "deleted".
    out = _call(srv, "vault_delete", {"key": "never_stored"})
    assert out["isError"] is False
    assert out["payload"] == {"key": "never_stored", "removed": False, "status": "absent"}


def test_vault_delete_credential_field(srv: McpServer, tmp_path) -> None:
    secure_vault.set_fields(tmp_path, "reddit", {"username": "u", "password": "p"})
    out = _call(srv, "vault_delete_credential", {"namespace": "reddit", "field": "password"})
    assert out["isError"] is False
    assert out["payload"]["status"] == "deleted"
    assert out["payload"]["removed"] is True
    assert secure_vault.load_fields(tmp_path, "reddit", "default") == {"username": "u"}


def test_vault_delete_absent_field_reports_absent(srv: McpServer, tmp_path) -> None:
    secure_vault.set_fields(tmp_path, "reddit", {"username": "u"})
    out = _call(srv, "vault_delete_credential", {"namespace": "reddit", "field": "password"})
    assert out["isError"] is False
    assert out["payload"]["removed"] is False
    assert out["payload"]["status"] == "absent"
    # the existing field is untouched
    assert secure_vault.load_fields(tmp_path, "reddit", "default") == {"username": "u"}


def test_vault_delete_absent_profile_reports_absent(srv: McpServer) -> None:
    # Finding 2 fix: the MCP surface no longer claims status "deleted" with removed False.
    out = _call(srv, "vault_delete_credential", {"namespace": "ghost"})
    assert out["isError"] is False
    assert out["payload"]["removed"] is False
    assert out["payload"]["status"] == "absent"


def test_vault_delete_credential_whole_profile(srv: McpServer, tmp_path) -> None:
    secure_vault.set_fields(tmp_path, "reddit", {"username": "u", "password": "p"})
    out = _call(srv, "vault_delete_credential", {"namespace": "reddit"})
    assert out["isError"] is False
    assert out["payload"]["status"] == "deleted"
    assert out["payload"]["removed"] is True
    # the whole profile is gone (rmtree), not just emptied
    assert secure_vault.load_fields(tmp_path, "reddit", "default") == {}


# -- payload wiring -------------------------------------------------------------------


def test_payload_includes_vault_by_default(tmp_path, monkeypatch) -> None:
    """DEFAULT ON: the secure-vault MCP server is added to the claude turn."""
    monkeypatch.delenv("AKANA_VAULT_TOOLS", raising=False)
    monkeypatch.setenv("AKANA_MEMORY_TOOLS", "0")  # isolate: look at vault only
    from akana_server.orchestrator.memory_tools import mcp_servers_payload

    settings = SimpleNamespace(data_dir=tmp_path)
    payload = mcp_servers_payload(settings, conversation_id="c")
    assert payload is not None
    assert "akana_vault" in payload
    entry = payload["akana_vault"]
    assert entry["type"] == "stdio"
    # Standalone launcher FILE (cwd/PYTHONPATH-immune), not `-m akana_server.vault_mcp.mcp`.
    assert len(entry["args"]) == 1
    assert entry["args"][0].endswith("mcp_vault.py")
    assert entry["env"]["AKANA_DATA_DIR"] == str(tmp_path)
    # vault is file/crypto: NO loopback REST base and NO conversation_id in the entry.
    assert "AKANA_BASE_URL" not in entry["env"]
    assert "AKANA_CONV_ID" not in entry["env"]


def test_payload_forwards_master_key_env(tmp_path, monkeypatch) -> None:
    """The master-key source env is forwarded so the child decrypts with the same key.

    The autouse ``_isolated_vault_key`` fixture sets ``AKANA_VAULT_KEYFILE`` in the
    process env; the vault entry must carry it through to the child."""
    monkeypatch.delenv("AKANA_VAULT_TOOLS", raising=False)
    from akana_server.orchestrator.memory_tools import mcp_servers_payload

    settings = SimpleNamespace(data_dir=tmp_path)
    payload = mcp_servers_payload(settings) or {}
    entry = payload["akana_vault"]
    import os

    assert entry["env"]["AKANA_VAULT_KEYFILE"] == os.environ["AKANA_VAULT_KEYFILE"]


def test_payload_excludes_vault_when_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AKANA_VAULT_TOOLS", "0")
    monkeypatch.setenv("AKANA_MEMORY_TOOLS", "0")
    from akana_server.orchestrator.memory_tools import mcp_servers_payload

    settings = SimpleNamespace(data_dir=tmp_path)
    payload = mcp_servers_payload(settings, conversation_id="c")
    # both memory and vault are off → None unless there are external ones.
    assert payload is None or "akana_vault" not in payload


def test_vault_tools_enabled_flag(monkeypatch) -> None:
    from akana_server.orchestrator.memory_tools import vault_tools_enabled

    monkeypatch.delenv("AKANA_VAULT_TOOLS", raising=False)
    assert vault_tools_enabled() is True  # default ON
    monkeypatch.setenv("AKANA_VAULT_TOOLS", "0")
    assert vault_tools_enabled() is False
    monkeypatch.setenv("AKANA_VAULT_TOOLS", "off")
    assert vault_tools_enabled() is False
    monkeypatch.setenv("AKANA_VAULT_TOOLS", "1")
    assert vault_tools_enabled() is True
