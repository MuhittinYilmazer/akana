"""skill_resolve — skill→MCP server mapping + pack persona resolution.

The persistent resolvers used by in-chat skill injection (turn_injection) and
persona discovery. NO LLM run; a fake mcp_servers.yaml entry (fake MCP) instead
of real Ghidra.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from akana_server.skills.skill_resolve import (
    find_pack_persona,
    needed_servers,
    resolve_skill_servers,
    server_for_tool,
)

_REPO_PACKS = Path(__file__).resolve().parents[2] / "packs"


@pytest.fixture
def settings(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(data_dir=tmp_path)


# -- tool → server mapping --------------------------------------------------------


@pytest.mark.parametrize(
    "tool,expected",
    [
        ("ghidra.decompile_function", "ghidra"),
        ("mcp__ghidra__decompile", "ghidra"),
        ("mcp__filesystem", "filesystem"),
        ("memory_remember", "akana_memory"),
        ("memory_search", "akana_memory"),
        ("plain_tool", None),
        ("", None),
        ("  ", None),
    ],
)
def test_server_for_tool(tool: str, expected: str | None) -> None:
    assert server_for_tool(tool) == expected


def test_needed_servers_dedup_sorted() -> None:
    assert needed_servers(
        ["ghidra.a", "ghidra.b", "memory_remember", "loose", "fs.read"]
    ) == ["akana_memory", "fs", "ghidra"]


def test_resolve_skill_servers_missing_signal(settings) -> None:
    selected, missing = resolve_skill_servers(
        settings, ["ghidra.decompile_function", "memory_remember"]
    )
    assert missing == ["ghidra"]  # not mounted → missing-tool signal
    assert set(selected) <= {"akana_memory"}


def test_resolve_skill_servers_uses_consented_mount(settings, tmp_path) -> None:
    (tmp_path / "mcp_servers.yaml").write_text(
        "servers:\n"
        "  ghidra:\n"
        "    type: http\n"
        "    url: http://127.0.0.1:8089/\n"
        "  fetch:\n"
        "    type: stdio\n"
        "    command: uvx\n",
        encoding="utf-8",
    )
    selected, missing = resolve_skill_servers(settings, ["ghidra.decompile_function"])
    assert missing == []
    assert "ghidra" in selected
    # a server the skill did not declare (fetch) is NOT added to the allowed set
    assert "fetch" not in selected


def test_resolve_skill_servers_folded_alias(settings, tmp_path) -> None:
    (tmp_path / "mcp_servers.yaml").write_text(
        "servers:\n  ghidra-mcp:\n    type: http\n    url: http://127.0.0.1:8089/\n",
        encoding="utf-8",
    )
    selected, missing = resolve_skill_servers(settings, ["ghidra.list_functions"])
    assert missing == []
    assert "ghidra-mcp" in selected


# -- pack persona resolution -------------------------------------------------------


def test_find_pack_persona_resolves_web_operator_from_repo_pack() -> None:
    prompt = find_pack_persona("browse", roots=[_REPO_PACKS])
    assert prompt is not None
    assert "web" in prompt.lower()


def test_find_pack_persona_unknown_skill_is_none() -> None:
    assert find_pack_persona("no_such_skill", roots=[_REPO_PACKS]) is None


def test_find_pack_persona_broken_root_is_none(tmp_path) -> None:
    bad = tmp_path / "bad-pack"
    bad.mkdir()
    (bad / "pack.yaml").write_text(":::: broken yaml", encoding="utf-8")
    assert find_pack_persona("browse", roots=[tmp_path]) is None
