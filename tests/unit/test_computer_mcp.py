"""Computer-control MCP + pack — non-GUI assertions (CI-safe without pyautogui/mss).

The GUI backends are lazy-imported inside the handlers, so the MODULE imports, the
manifest parses, the pack discovers/validates, and the tool registry is inspectable on
any machine. The two tests that actually touch the screen (screen_info / screenshot)
are guarded with importorskip so CI without the backends still passes green.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from akana_server.computer_mcp import build_server, main
from akana_server.skills.skill_resolve import needed_servers, server_for_tool

_REPO = Path(__file__).resolve().parents[2]
_PACK = _REPO / "packs" / "computer-control"

_EXPECTED_TOOLS = {
    "screen_info",
    "screenshot",
    # Perception (a11y tree + refs) — the structured, provider-agnostic alternative to
    # screenshot+pixel-eyeballing (Phase 1 desktop intelligence).
    "read_screen",
    "find_element",
    "click_ref",
    "double_click_ref",
    "right_click_ref",
    "type_into_ref",
    "left_click",
    "double_click",
    "right_click",
    "middle_click",
    "triple_click",
    "mouse_move",
    "mouse_down",
    "mouse_up",
    "drag",
    "scroll",
    "hscroll",
    "cursor_position",
    "type_text",
    "paste_text",
    "key",
    "hotkey",
    "hold_key",
    "read_clipboard",
    "write_clipboard",
    "open_application",
    "list_windows",
    "focus_window",
    "maximize_window",
    "minimize_window",
    "move_window",
    "resize_window",
    "close_window",
}


def _backends_present() -> bool:
    return all(importlib.util.find_spec(m) is not None for m in ("pyautogui", "mss"))


def _tool_names(server) -> set[str]:
    return {t.name for t in asyncio.run(server.list_tools())}


# -- module + tool registry (no GUI) ---------------------------------------------


def test_module_imports_without_backends():
    # Importing the package and building the server must NOT require pyautogui/mss —
    # the backends are lazy-imported per handler.
    assert callable(build_server)
    assert callable(main)
    build_server()


def test_tool_registry_has_all_tools():
    names = _tool_names(build_server())
    assert names == _EXPECTED_TOOLS, sorted(names)


def test_server_name_is_computer():
    assert build_server().name == "computer"


# -- pack manifest + skill (no GUI) ----------------------------------------------


def test_pack_manifest_parses_and_validates():
    from packs.contract.manifest import (
        autodiscover_contents,
        load_manifest,
        validate_pack_dir,
    )

    manifest = load_manifest(_PACK / "pack.yaml")
    autodiscover_contents(manifest, _PACK)
    assert manifest.id == "akana/computer-control"
    assert "computer" in manifest.contains.skills
    ext = manifest.dependencies.external_tools
    assert len(ext) == 1
    assert ext[0].name == "computer"
    assert ext[0].kind == "mcp_server"
    assert ext[0].required is True

    result = validate_pack_dir(_PACK)
    assert result.ok, result.errors


def test_pack_mcp_config_targets_the_launcher_file():
    from packs.contract.manifest import load_manifest

    manifest = load_manifest(_PACK / "pack.yaml")
    mcp_cfg = manifest.dependencies.external_tools[0].model_dump().get("mcp")
    assert isinstance(mcp_cfg, dict)
    assert mcp_cfg.get("type") == "stdio"
    # The spawn must invoke the cwd-immune launcher FILE, not `-m akana_server.computer_mcp`:
    # akana_server is not pip-installed, so `-m` only resolves when the child inherits
    # cwd=repo-root, which the claude CLI / in-process bridge do not (dies with
    # ModuleNotFoundError off the repo root). `<AKANA_REPO>` is the mount-time marker
    # ToolsAdapter.consent rewrites to the absolute repo root.
    args = mcp_cfg.get("args")
    assert args == ["<AKANA_REPO>/scripts/mcp_computer.py"], args
    assert "-m" not in args
    assert "AKANA_DATA_DIR" in (mcp_cfg.get("env") or {})


def test_skill_manifest_is_high_risk_and_lists_every_tool():
    import yaml

    data = yaml.safe_load((_PACK / "skills" / "computer" / "manifest.yaml").read_text("utf-8"))
    assert data["id"] == "computer"
    assert data["risk"] == "high"
    assert data["requires_approval"] is True
    allowed = {t.split(".", 1)[1] for t in data["tools_allowed"] if t.startswith("computer.")}
    assert allowed == _EXPECTED_TOOLS, sorted(allowed)
    # bilingual triggers (both a TR and an EN phrase present)
    triggers = data["triggers"]
    assert "take a screenshot" in triggers
    assert "ekran görüntüsü" in triggers


def test_skill_resolve_maps_computer_tools_to_the_server():
    assert server_for_tool("computer.screenshot") == "computer"
    assert needed_servers(["computer.screenshot"]) == ["computer"]
    allow = [f"computer.{t}" for t in _EXPECTED_TOOLS]
    assert needed_servers(allow) == ["computer"]


# -- GUI-guarded (skipped when backends are absent) ------------------------------


@pytest.mark.skipif(not _backends_present(), reason="pyautogui/mss not installed")
def test_screen_info_returns_geometry():
    server = build_server()
    result = asyncio.run(server.call_tool("screen_info", {}))
    # FastMCP returns (content_blocks, structured) in this SDK; find the JSON text.
    payload = _first_json(result)
    assert "monitors" in payload or "primary" in payload


@pytest.mark.skipif(not _backends_present(), reason="pyautogui/mss not installed")
def test_screenshot_writes_valid_png(tmp_path, monkeypatch):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    server = build_server()
    payload = _first_json(asyncio.run(server.call_tool("screenshot", {"monitor": 0})))
    png = Path(payload["path"])
    assert png.is_file()
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert payload["width"] > 0 and payload["height"] > 0


def _first_json(call_result) -> dict:
    """Pull the first JSON object out of a FastMCP call_tool result (SDK-shape tolerant)."""
    # call_tool may return content blocks, or a (content, structured) tuple.
    candidates = call_result
    if isinstance(call_result, tuple):
        for part in call_result:
            if isinstance(part, dict):
                return part
        candidates = call_result[0]
    for block in candidates or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue
    raise AssertionError(f"no JSON payload in call result: {call_result!r}")
