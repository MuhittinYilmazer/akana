"""Computer-control per-action APPROVAL gate (Phase 2) — headless-safe.

The gate lives inside the computer MCP child (the only provider-neutral point) and reads
its mode LIVE from <data_dir>/runtime_settings.json. These tests inject the approval
decision via approval.set_prompter (NEVER the real native dialog) and a fake pyautogui, so
they run anywhere and never pop a window.

Locked contract:
  • mode "off" (default) → nothing is gated; every action runs (prompter never consulted).
  • mode "destructive" → open_application/close_window/drag ask; clicks/typing do NOT.
  • mode "all" → every actuation asks; read-only perception (screenshot/read_screen) never.
  • approved → the action runs; DENIED → {ok:False, denied:True} and the action never ran.
  • fail-safe: a broken/absent prompter DENIES an action that requires approval.
  • mode resolves runtime_settings.json > env > "off"; unknown value → "off".
"""

from __future__ import annotations

import asyncio
import json

import pytest

from akana_server.computer_mcp import __main__ as cm
from akana_server.computer_mcp import approval, perception


class _FailSafe(Exception):
    pass


class _FakePyautogui:
    FailSafeException = _FailSafe

    def __init__(self):
        self.calls = []

    def click(self, x=None, y=None, button="left"):
        self.calls.append(("click", x, y, button))

    def doubleClick(self, x=None, y=None):
        self.calls.append(("doubleClick", x, y))

    def moveTo(self, x=None, y=None):
        self.calls.append(("moveTo", x, y))

    def dragTo(self, x=None, y=None, button="left"):
        self.calls.append(("dragTo", x, y))


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Server with AKANA_DATA_DIR=tmp, a fake pyautogui, and a recording prompter."""
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AKANA_COMPUTER_APPROVAL", raising=False)
    fake_pg = _FakePyautogui()
    monkeypatch.setattr(cm, "_pyautogui", lambda: fake_pg)
    prompts = []
    decision = {"allow": True}

    def _prompter(title, summary):
        prompts.append((title, summary))
        return decision["allow"]

    approval.set_prompter(_prompter)
    server = cm.build_server()

    def set_mode(mode):  # write the live setting the child reads
        (tmp_path / "runtime_settings.json").write_text(
            json.dumps({"computer_control_approval": mode}), encoding="utf-8"
        )

    yield server, fake_pg, prompts, decision, set_mode
    approval.set_prompter(None)


def _call(server, name, args=None):
    res = asyncio.run(server.call_tool(name, args or {}))
    payload = res[1] if isinstance(res, tuple) else res
    if isinstance(payload, dict):
        return payload
    for block in payload if isinstance(payload, (list, tuple)) else [payload]:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except Exception:
                continue
    raise AssertionError(f"could not parse tool result for {name}: {res!r}")


# ── mode off (default): nothing gated ───────────────────────────────────────────
def test_off_mode_runs_without_prompting(rig):
    server, pg, prompts, _dec, set_mode = rig
    # no runtime_settings.json written → default off
    out = _call(server, "left_click", {"x": 10, "y": 20})
    assert out["ok"] is True
    assert pg.calls == [("click", 10, 20, "left")]
    assert prompts == []  # the owner is never asked in off mode


# ── mode destructive: only destructive tools ask ────────────────────────────────
def test_destructive_mode_gates_destructive_not_click(rig, monkeypatch):
    server, pg, prompts, decision, set_mode = rig
    set_mode("destructive")
    # a medium action (left_click) is NOT gated in destructive mode
    _call(server, "left_click", {"x": 1, "y": 2})
    assert prompts == [] and pg.calls == [("click", 1, 2, "left")]
    # a destructive action (drag) IS gated — deny it
    decision["allow"] = False
    out = _call(server, "drag", {"x1": 0, "y1": 0, "x2": 5, "y2": 5})
    assert out["ok"] is False and out["denied"] is True
    assert len(prompts) == 1 and "Drag" in prompts[0][1]
    assert not any(c[0] == "dragTo" for c in pg.calls)  # the drag never ran


def test_destructive_mode_gates_open_application(rig, monkeypatch):
    server, _pg, prompts, decision, set_mode = rig
    set_mode("destructive")
    decision["allow"] = False
    # stub the launcher so a denied open never actually starts anything
    import akana_server.computer_mcp.__main__ as m
    started = []
    monkeypatch.setattr(m.os, "startfile", lambda p: started.append(p), raising=False)
    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: started.append(a))
    out = _call(server, "open_application", {"name": "notepad"})
    assert out["ok"] is False and out["denied"] is True
    assert "Open application: notepad" in prompts[-1][1]
    assert started == []  # the app was NOT launched


def test_destructive_mode_gates_close_equivalents(rig):
    """The low-level window-close/drag vectors (hotkey/middle_click/mouse hold) are gated
    in destructive mode too, so it can't be trivially bypassed."""
    server, pg, prompts, decision, set_mode = rig
    set_mode("destructive")
    decision["allow"] = False
    for tool, args in (("hotkey", {"keys": ["alt", "f4"]}), ("middle_click", {"x": 1, "y": 1}),
                       ("mouse_down", {"x": 1, "y": 1})):
        out = _call(server, tool, args)
        assert out.get("denied") is True, f"{tool} must be gated in destructive mode"


# ── mode all: every actuation asks; perception never ────────────────────────────
def test_all_mode_gates_click_but_not_read_screen(rig, monkeypatch):
    server, pg, prompts, decision, set_mode = rig
    set_mode("all")
    # read-only perception is never gated (safe) — override the a11y backend so it works
    perception.set_backend_override(lambda *a, **k: perception.A11yNode(role="desktop"))
    try:
        _call(server, "read_screen", {})
    finally:
        perception.set_backend_override(None)
    assert prompts == []  # screenshot/read_screen never prompt
    # a click asks; approve → runs
    decision["allow"] = True
    out = _call(server, "left_click", {"x": 7, "y": 8})
    assert out["ok"] is True and len(prompts) == 1
    assert ("click", 7, 8, "left") in pg.calls


def test_all_mode_denied_click_does_not_run(rig):
    server, pg, prompts, decision, set_mode = rig
    set_mode("all")
    decision["allow"] = False
    out = _call(server, "left_click", {"x": 3, "y": 4})
    assert out["ok"] is False and out["denied"] is True
    assert "not approved" in out["error"]
    assert pg.calls == []  # denied → the click never happened


# ── fail-safe: a broken/absent prompter denies ──────────────────────────────────
def test_broken_prompter_denies(rig):
    server, pg, prompts, _dec, set_mode = rig
    set_mode("all")

    def _boom(title, summary):
        raise RuntimeError("no display")

    approval.set_prompter(_boom)
    out = _call(server, "left_click", {"x": 1, "y": 1})
    assert out["ok"] is False and out["denied"] is True
    assert pg.calls == []  # a click that can't be confirmed must not run


# ── mode resolution: json > env > default; unknown → off ────────────────────────
def test_resolve_mode_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv("AKANA_COMPUTER_APPROVAL", raising=False)
    assert approval.resolve_mode(tmp_path) == "off"  # nothing set
    monkeypatch.setenv("AKANA_COMPUTER_APPROVAL", "all")
    assert approval.resolve_mode(tmp_path) == "all"  # env
    (tmp_path / "runtime_settings.json").write_text(
        json.dumps({"computer_control_approval": "destructive"}), encoding="utf-8"
    )
    assert approval.resolve_mode(tmp_path) == "destructive"  # json wins over env
    (tmp_path / "runtime_settings.json").write_text(
        json.dumps({"computer_control_approval": "bogus"}), encoding="utf-8"
    )
    # invalid json value → fall through to env
    assert approval.resolve_mode(tmp_path) == "all"


def test_summarize_is_owner_readable():
    assert cm._summarize("open_application", {"name": "notepad"}) == "Open application: notepad"
    assert "the Save button" in cm._summarize("click_ref", {"ref": "w1e1", "element": "the Save button"})
    assert "gizli" in cm._summarize("type_into_ref", {"ref": "w1e2", "text": "gizli"})
