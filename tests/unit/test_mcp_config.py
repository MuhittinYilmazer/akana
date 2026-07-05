"""External MCP config (mcp_servers.yaml) + merged ``mcp_servers`` payload."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from akana_server.orchestrator.mcp_config import load_external_mcp_servers
from akana_server.orchestrator.memory_tools import (
    mcp_servers_payload,
    memory_mcp_servers,
)

LOGGER = "akana_server.orchestrator.mcp_config"

VALID_YAML = """
servers:
  filesystem:
    type: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/notes"]
    env:
      LOG_LEVEL: warn
      PORT: 8080
    cwd: /tmp
    bilinmeyen_alan: at-beni
  github:
    type: http
    url: https://api.example.com/mcp/
    headers:
      Authorization: Bearer abc
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "mcp_servers.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def settings(tmp_path):
    return SimpleNamespace(data_dir=tmp_path)


@pytest.fixture(autouse=True)
def _memory_tools_on(monkeypatch):
    monkeypatch.delenv("AKANA_MEMORY_TOOLS", raising=False)
    # This file tests the memory + external yaml MERGE behavior; the built-in
    # akana_vault server is orthogonal (its own test file) → disabled here so the
    # payload sets stay clean.
    monkeypatch.setenv("AKANA_VAULT_TOOLS", "0")


# -- load_external_mcp_servers ------------------------------------------------------


def test_missing_yaml_returns_empty(tmp_path):
    assert load_external_mcp_servers(tmp_path) == {}


def test_valid_stdio_and_http_parsed(tmp_path):
    """Valid entries collapse into the Cursor McpServerConfig shape; unknown fields
    and int env values are normalized."""
    _write(tmp_path, VALID_YAML)
    servers = load_external_mcp_servers(tmp_path)
    assert set(servers) == {"filesystem", "github"}
    assert servers["filesystem"] == {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/notes"],
        "env": {"LOG_LEVEL": "warn", "PORT": "8080"},  # int → str
        "cwd": "/tmp",
    }  # bilinmeyen_alan was dropped
    assert servers["github"] == {
        "type": "http",
        "url": "https://api.example.com/mcp/",
        "headers": {"Authorization": "Bearer abc"},
    }


def test_type_inferred_when_missing(tmp_path):
    """When type is omitted, infer command→stdio, url→http."""
    _write(
        tmp_path,
        "servers:\n"
        "  fetch:\n"
        "    command: uvx\n"
        '    args: ["mcp-server-fetch"]\n'
        "  uzak:\n"
        "    url: https://uzak.example/mcp\n",
    )
    servers = load_external_mcp_servers(tmp_path)
    assert servers["fetch"]["type"] == "stdio"
    assert servers["uzak"] == {"type": "http", "url": "https://uzak.example/mcp"}


def test_sse_ok_and_sse_without_url_skipped(tmp_path, caplog):
    _write(
        tmp_path,
        "servers:\n"
        "  canli:\n"
        "    type: sse\n"
        "    url: https://canli.example/sse\n"
        "  bozuk-sse:\n"
        "    type: sse\n",
    )
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        servers = load_external_mcp_servers(tmp_path)
    assert set(servers) == {"canli"}
    assert servers["canli"] == {"type": "sse", "url": "https://canli.example/sse"}
    assert "'url' is required" in caplog.text


def test_enabled_false_skipped(tmp_path):
    _write(
        tmp_path,
        "servers:\n"
        "  kapali:\n"
        "    type: stdio\n"
        "    command: npx\n"
        "    enabled: false\n"
        "  acik:\n"
        "    type: stdio\n"
        "    command: uvx\n"
        "    enabled: true\n",
    )
    assert set(load_external_mcp_servers(tmp_path)) == {"acik"}


def test_invalid_name_and_missing_command_skipped(tmp_path, caplog):
    _write(
        tmp_path,
        "servers:\n"
        '  "kötü ad!":\n'
        "    type: stdio\n"
        "    command: npx\n"
        "  komutsuz:\n"
        "    type: stdio\n"
        '    args: ["x"]\n'
        "  gecerli:\n"
        "    type: stdio\n"
        "    command: npx\n",
    )
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        servers = load_external_mcp_servers(tmp_path)
    assert set(servers) == {"gecerli"}
    assert "invalid MCP server name" in caplog.text  # name regex
    assert "'command' is required" in caplog.text


def test_unknown_type_and_non_mapping_entry_skipped(tmp_path, caplog):
    _write(
        tmp_path,
        "servers:\n"
        "  garip:\n"
        "    type: websocket\n"
        "    url: wss://x\n"
        "  duz_metin: sadece-string\n",
    )
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert load_external_mcp_servers(tmp_path) == {}
    assert "unknown type" in caplog.text
    assert "not a mapping" in caplog.text


def test_reserved_akana_memory_skipped(tmp_path, caplog):
    _write(
        tmp_path,
        "servers:\n"
        "  akana_memory:\n"
        "    type: stdio\n"
        "    command: /usr/bin/kotu\n",
    )
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert load_external_mcp_servers(tmp_path) == {}
    assert "reserved" in caplog.text


def test_broken_yaml_warns_and_returns_empty(tmp_path, caplog):
    _write(tmp_path, "servers: [bozuk\n  :::")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert load_external_mcp_servers(tmp_path) == {}
    assert "could not read" in caplog.text


def test_non_mapping_root_and_servers(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _write(tmp_path, "- a\n- b\n")
        assert load_external_mcp_servers(tmp_path) == {}
        _write(tmp_path, "servers: [a, b]\n")
        assert load_external_mcp_servers(tmp_path) == {}
        _write(tmp_path, "")  # an empty file is fine
        assert load_external_mcp_servers(tmp_path) == {}
    assert caplog.text.count("not a mapping") >= 2


def test_env_not_inherited_from_server_process(tmp_path, monkeypatch):
    """SECURITY: os.environ does not leak into the external server — only the yaml env is passed."""
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "sizdirma-beni")
    _write(
        tmp_path,
        "servers:\n"
        "  fs:\n"
        "    type: stdio\n"
        "    command: npx\n"
        "    env:\n"
        "      SADECE: yaml\n",
    )
    entry = load_external_mcp_servers(tmp_path)["fs"]
    assert entry["env"] == {"SADECE": "yaml"}
    assert "SUPER_SECRET_TOKEN" not in entry["env"]


# -- mcp_servers_payload / memory_mcp_servers (merge) -------------------------------


def test_payload_without_yaml_only_akana_memory(settings):
    payload = mcp_servers_payload(settings)
    assert payload is not None and set(payload) == {"akana_memory"}
    assert payload["akana_memory"]["command"] == sys.executable
    # the backward-compatible name yields an identical result (chat.py/voice.py call path)
    assert memory_mcp_servers(settings) == payload


def test_payload_merges_external_with_akana_memory(settings, tmp_path):
    _write(tmp_path, VALID_YAML)
    payload = memory_mcp_servers(settings)
    assert set(payload) == {"akana_memory", "filesystem", "github"}
    assert payload["akana_memory"]["args"][0].endswith("mcp_memory.py")
    assert payload["filesystem"]["command"] == "npx"
    assert payload["github"]["type"] == "http"


def test_payload_external_only_when_memory_disabled(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("AKANA_MEMORY_TOOLS", "0")
    _write(tmp_path, VALID_YAML)
    payload = memory_mcp_servers(settings)
    assert set(payload) == {"filesystem", "github"}


def test_payload_none_when_both_sources_empty(settings, monkeypatch):
    monkeypatch.setenv("AKANA_MEMORY_TOOLS", "0")
    assert mcp_servers_payload(settings) is None
    assert memory_mcp_servers(settings) is None


def test_payload_user_akana_memory_collision_loses(settings, tmp_path, caplog):
    """Even if the user defines akana_memory in yaml, the built-in wins."""
    _write(
        tmp_path,
        "servers:\n"
        "  akana_memory:\n"
        "    type: stdio\n"
        "    command: /usr/bin/kotu\n"
        "  fetch:\n"
        "    type: stdio\n"
        "    command: uvx\n",
    )
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        payload = memory_mcp_servers(settings)
    assert set(payload) == {"akana_memory", "fetch"}
    assert payload["akana_memory"]["command"] == sys.executable  # ours
    assert "reserved" in caplog.text


def test_payload_broken_yaml_keeps_akana_memory(settings, tmp_path, caplog):
    _write(tmp_path, "{{{bozuk")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        payload = memory_mcp_servers(settings)
    assert set(payload) == {"akana_memory"}
    assert "could not read" in caplog.text
