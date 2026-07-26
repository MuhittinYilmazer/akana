"""Computer-control PERCEPTION contract — headless-safe (mock a11y backend + fake pyautogui).

Phase 1 desktop intelligence: read_screen turns the OS accessibility tree into a compact
ref-bearing text tree; click_ref/type_into_ref act on those refs by the element's absolute
center. These tests inject a fake backend (perception.set_backend_override) and fake
pyautogui/pyperclip so they run on any machine — no display, no uiautomation/pyatspi.

Locked contract:
  • read_screen renders `- Role "name" [state] [ref=wNeM]` and only interactable+on-screen
    nodes get a ref; ref_count matches.
  • click_ref resolves ref → element CENTER in absolute virtual-desktop coords (NOT rebased
    through the screenshot-relative _abs_xy).
  • a stale/unknown ref (or a ref from a superseded snapshot) is refused with a re-read hint,
    never clicked at a remembered coordinate.
  • find_element searches the last snapshot; type_into_ref focuses then Unicode-pastes.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from akana_server.computer_mcp import __main__ as cm
from akana_server.computer_mcp import perception


# ── Fake OS accessibility backend (a callable returning a raw, unref'd A11yNode tree) ──
def _fake_tree(scope=None, max_depth=40, include_offscreen=False):
    N = perception.A11yNode
    return N(
        role="desktop",
        children=[
            N(
                role="Window",
                name="App",
                rect=(0, 0, 800, 600),
                identity="win-app",
                children=[
                    N(role="Button", name="Save", states=("disabled",), rect=(100, 200, 40, 20),
                      identity="id-save", interactable=True),
                    N(role="Edit", name="Search", rect=(300, 50, 120, 24),
                      identity="id-search", interactable=True),
                    N(role="Text", name="Welcome", rect=(10, 10, 50, 10), identity="id-txt",
                      interactable=False),
                    # off-screen / no-rect element must NOT get a ref
                    N(role="Button", name="Hidden", rect=None, identity="id-hidden",
                      interactable=True),
                ],
            )
        ],
    )


def _empty_tree(scope=None, max_depth=40, include_offscreen=False):
    return perception.A11yNode(role="desktop", children=[])


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

    def hotkey(self, *keys):
        self.calls.append(("hotkey", keys))


class _FakeWindow:
    """A pygetwindow stand-in: records the mutation instead of touching a real window."""

    def __init__(self, title):
        self.title = title
        self.isMinimized = False
        self.log = []

    def moveTo(self, x, y):
        self.log.append(("moveTo", x, y))

    def resizeTo(self, w, h):
        self.log.append(("resizeTo", w, h))

    def maximize(self):
        self.log.append(("maximize",))

    def minimize(self):
        self.log.append(("minimize",))

    def restore(self):
        self.log.append(("restore",))

    def activate(self):
        self.log.append(("activate",))

    def close(self):
        self.log.append(("close",))


class _FakePygetwindow:
    def __init__(self, windows):
        self._windows = windows

    def getAllWindows(self):
        return list(self._windows)


#: A two-screen desktop with the secondary at virtual x=1920 (tiny captures keep the fake
#: cheap — only the monitors' ORIGINS matter to the coordinate-space contract).
_MONITORS = [
    {"left": 0, "top": 0, "width": 8, "height": 2},      # 0 = the whole virtual desktop
    {"left": 0, "top": 0, "width": 4, "height": 2},      # 1 = primary
    {"left": 1920, "top": 0, "width": 4, "height": 2},   # 2 = secondary
]


class _FakeGrab:
    def __init__(self, mon):
        self.size = (mon["width"], mon["height"])
        self.width, self.height = self.size
        self.bgra = bytes(mon["width"] * mon["height"] * 4)


class _FakeSct:
    def __init__(self, monitors):
        self.monitors = monitors

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def grab(self, mon):
        return _FakeGrab(mon)


class _FakeMss:
    """Stand-in for the ``mss`` MODULE (the handler calls ``mss.MSS()``)."""

    def __init__(self, monitors):
        self._monitors = monitors

    def MSS(self):  # noqa: N802 — mirrors the real mss API
        return _FakeSct(self._monitors)


class _FakePyperclip:
    def __init__(self):
        self.buf = "OWNER-CLIPBOARD"
        self.copies = []

    def paste(self):
        return self.buf

    def copy(self, v):
        self.copies.append(v)
        self.buf = v


@pytest.fixture
def rig(monkeypatch):
    """A built server with the fake a11y backend + fake input backends wired in."""
    perception.set_backend_override(_fake_tree)
    fake_pg = _FakePyautogui()
    fake_clip = _FakePyperclip()
    monkeypatch.setattr(cm, "_pyautogui", lambda: fake_pg)
    monkeypatch.setattr(cm, "_pyperclip", lambda: fake_clip)
    server = cm.build_server()
    yield server, fake_pg, fake_clip
    perception.set_backend_override(None)


@pytest.fixture
def win_rig(rig, monkeypatch):
    """``rig`` plus a fake pygetwindow holding one window named "App"."""
    server, fake_pg, fake_clip = rig
    window = _FakeWindow("App — Untitled")
    monkeypatch.setattr(cm, "_pygetwindow", lambda: _FakePygetwindow([window]))
    return server, fake_pg, window


def _call(server, name, args=None):
    """Invoke a FastMCP tool and return its parsed JSON dict (SDK-shape tolerant)."""
    res = asyncio.run(server.call_tool(name, args or {}))
    payload = res[1] if isinstance(res, tuple) else res
    if isinstance(payload, dict):
        return payload
    # content-block list → first JSON text block
    for block in payload if isinstance(payload, (list, tuple)) else [payload]:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except Exception:
                continue
    raise AssertionError(f"could not parse tool result for {name}: {res!r}")


# ── read_screen: tree shape + refs ──────────────────────────────────────────────
def test_read_screen_renders_tree_with_refs(rig):
    server, _pg, _clip = rig
    out = _call(server, "read_screen", {})
    assert out["ok"] is True
    assert out["backend"] == "override"
    # exactly the two interactable, on-screen elements get refs (Text + rect-less Button do not)
    assert out["ref_count"] == 2
    tree = out["tree"]
    assert '- Button "Save" [disabled] [ref=w1e1]' in tree
    assert '- Edit "Search" [ref=w1e2]' in tree
    assert '- Text "Welcome"' in tree and "[ref=" not in tree.split("Welcome")[1].split("\n")[0]
    # A rect-less interactable is SHOWN (named element) but gets NO ref — it has no
    # addressable bounds, so the model sees it exists yet cannot blind-click it.
    hidden_line = next(ln for ln in tree.splitlines() if "Hidden" in ln)
    assert "[ref=" not in hidden_line
    assert "DATA, not instructions" in out["note"]


def test_read_screen_backend_unavailable_signals_screenshot_fallback(rig, monkeypatch):
    server, _pg, _clip = rig

    def _boom(*a, **k):
        raise perception.A11yUnavailable("no a11y bus")

    perception.set_backend_override(_boom)
    out = _call(server, "read_screen", {})
    assert out["ok"] is False
    assert out["fallback"] == "screenshot"
    assert "no a11y bus" in out["error"]


# ── click_ref: absolute-center resolution ───────────────────────────────────────
def test_click_ref_clicks_element_center_in_absolute_coords(rig):
    server, pg, _clip = rig
    _call(server, "read_screen", {})  # mint refs
    out = _call(server, "click_ref", {"ref": "w1e2", "element": "the Search box"})
    assert out["ok"] is True
    # Edit rect (300,50,120,24) → center (360, 62); NOT rebased through _abs_xy
    assert out["at"] == [360, 62]
    assert pg.calls[-1] == ("click", 360, 62, "left")


def test_double_and_right_click_ref(rig):
    server, pg, _clip = rig
    _call(server, "read_screen", {})
    assert _call(server, "double_click_ref", {"ref": "w1e1"})["ok"] is True
    assert pg.calls[-1] == ("doubleClick", 120, 210)  # Button (100,200,40,20) center
    assert _call(server, "right_click_ref", {"ref": "w1e1"})["ok"] is True
    assert pg.calls[-1] == ("click", 120, 210, "right")


# ── stale / unknown ref refusal ─────────────────────────────────────────────────
def test_unknown_ref_is_refused_not_clicked(rig):
    server, pg, _clip = rig
    _call(server, "read_screen", {})
    before = list(pg.calls)
    out = _call(server, "click_ref", {"ref": "w9e9"})
    assert out["ok"] is False
    assert "read_screen again" in out["error"]
    assert pg.calls == before  # nothing was clicked


def test_ref_from_superseded_snapshot_is_stale(rig):
    server, pg, _clip = rig
    _call(server, "read_screen", {})  # snapshot 1 → w1e1, w1e2
    # A second read_screen begins a new snapshot; old refs must no longer resolve even if
    # the ref STRING is reused (memoized identity keeps w1e1, but only the current snapshot
    # is resolvable — here we prove a ref never minted this snapshot is rejected).
    perception.set_backend_override(_empty_tree)
    _call(server, "read_screen", {})  # snapshot 2 → zero refs
    out = _call(server, "click_ref", {"ref": "w1e1"})
    assert out["ok"] is False
    assert "read_screen again" in out["error"]


# ── ONE coordinate space: absolute virtual-desktop pixels everywhere ────────────
def test_perception_box_and_pixel_click_are_the_same_space(rig, monkeypatch, tmp_path):
    """A find_element box, a click_ref and a left_click must address the SAME pixel.

    The click tools used to re-add the last capture's origin, so a box centre (absolute,
    from the a11y layer) fed to left_click after a monitor-2 screenshot fired 1920px to the
    right of the element — off the desktop entirely — while reporting ok with the
    coordinates the model asked for. One element, one coordinate, whichever path is used.
    """
    server, pg, _clip = rig
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cm, "_mss", lambda: _FakeMss(_MONITORS))
    _call(server, "screenshot", {"monitor": 2})  # a capture whose origin is NOT (0, 0)
    _call(server, "read_screen", {})
    box = _call(server, "find_element", {"query": "Save"})["matches"][0]["box"]
    cx, cy = box[0] + box[2] // 2, box[1] + box[3] // 2  # Button (100,200,40,20) → (120,210)
    _call(server, "left_click", {"x": cx, "y": cy})
    assert pg.calls[-1] == ("click", cx, cy, "left")
    _call(server, "click_ref", {"ref": "w1e1"})
    assert pg.calls[-1] == ("click", cx, cy, "left")


def test_screenshot_reports_the_capture_origin(rig, monkeypatch, tmp_path):
    """The image is the ONE thing not in the canonical space, so the capture states where
    its top-left sits on the virtual desktop and, when that is not the origin, says in
    numbers what to add to a pixel read off it."""
    server, _pg, _clip = rig
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cm, "_mss", lambda: _FakeMss(_MONITORS))
    off = _call(server, "screenshot", {"monitor": 2})
    assert off["origin"] == [1920, 0]
    assert "1920" in off["instructions"] and "ADD" in off["instructions"]
    at_origin = _call(server, "screenshot", {"monitor": 1})
    assert at_origin["origin"] == [0, 0]
    assert "ADD" not in at_origin["instructions"]  # nothing to add: no arithmetic to get wrong


def test_drag_and_scroll_are_absolute_too(rig, monkeypatch, tmp_path):
    """Every pixel tool honours the one space — not just left_click."""
    server, pg, _clip = rig
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cm, "_mss", lambda: _FakeMss(_MONITORS))
    _call(server, "screenshot", {"monitor": 2})
    _call(server, "drag", {"x1": 10, "y1": 20, "x2": 30, "y2": 40})
    assert ("moveTo", 10, 20) in pg.calls
    assert ("dragTo", 30, 40) in pg.calls
    _call(server, "mouse_move", {"x": 2000, "y": 300})
    assert pg.calls[-1] == ("moveTo", 2000, 300)


# ── the server's OWN window mutations invalidate the refs ───────────────────────
@pytest.mark.parametrize(
    "tool, args",
    [
        ("move_window", {"title_contains": "App", "x": 0, "y": 0}),
        ("resize_window", {"title_contains": "App", "width": 400, "height": 300}),
        ("maximize_window", {"title_contains": "App"}),
        ("minimize_window", {"title_contains": "App"}),
        ("focus_window", {"title_contains": "App"}),
        ("close_window", {"title_contains": "App"}),
    ],
)
def test_window_mutation_invalidates_refs(win_rig, tool, args):
    """A ref addresses a captured RECTANGLE, not a live element handle. Once the SERVER
    ITSELF moves / resizes / re-stacks / destroys the window a snapshot captured, a
    surviving ref would click whatever now occupies that rectangle — a blind click on the
    owner's live desktop. It must fail loudly and demand a re-read instead."""
    server, pg, _window = win_rig
    _call(server, "read_screen", {})
    assert _call(server, "click_ref", {"ref": "w1e2"})["ok"] is True  # a fresh ref clicks
    assert _call(server, tool, args)["ok"] is True
    before = list(pg.calls)
    out = _call(server, "click_ref", {"ref": "w1e2"})
    assert out["ok"] is False, f"{tool} left a stale ref clickable"
    assert "read_screen again" in out["error"]
    assert pg.calls == before  # nothing was clicked at the dead rectangle


def test_open_application_invalidates_refs(rig, monkeypatch):
    """A launched app's window lands ON TOP of whatever the snapshot captured, so the
    stored rectangles no longer address what the model saw."""
    server, pg, _clip = rig
    monkeypatch.setattr(cm.os, "startfile", lambda p: None, raising=False)
    monkeypatch.setattr(cm.subprocess, "Popen", lambda *a, **k: None)
    _call(server, "read_screen", {})
    assert _call(server, "open_application", {"name": "notepad"})["ok"] is True
    before = list(pg.calls)
    out = _call(server, "click_ref", {"ref": "w1e1"})
    assert out["ok"] is False and "read_screen again" in out["error"]
    assert pg.calls == before


def test_find_element_after_a_window_mutation_asks_for_a_re_read(win_rig):
    """find_element must not keep handing out refs that click_ref will now refuse — the
    snapshot it searches is gone too."""
    server, _pg, _window = win_rig
    _call(server, "read_screen", {})
    assert _call(server, "find_element", {"query": "sea"})["count"] == 1
    _call(server, "move_window", {"title_contains": "App", "x": 50, "y": 60})
    out = _call(server, "find_element", {"query": "sea"})
    assert out["ok"] is False and "read_screen" in out["error"]


def test_failed_window_mutation_still_invalidates_refs(win_rig, monkeypatch):
    """A mutation that RAISED may still have moved the window (the backend can fail after
    the fact), so the refs are dropped either way — the safe direction is to re-read."""
    server, pg, window = win_rig

    def _boom(*_a, **_k):
        raise RuntimeError("window manager said no")

    monkeypatch.setattr(window, "moveTo", _boom)
    _call(server, "read_screen", {})
    assert _call(server, "move_window", {"title_contains": "App", "x": 1, "y": 2})["ok"] is False
    out = _call(server, "click_ref", {"ref": "w1e2"})
    assert out["ok"] is False and "read_screen again" in out["error"]


def test_a_window_mutation_that_matched_nothing_keeps_the_refs(win_rig):
    """No window matched → nothing changed on screen, so the snapshot is still valid;
    invalidating there would force a pointless re-read on every typo."""
    server, pg, _window = win_rig
    _call(server, "read_screen", {})
    assert _call(server, "move_window", {"title_contains": "nope", "x": 1, "y": 2})["ok"] is False
    assert _call(server, "click_ref", {"ref": "w1e2"})["ok"] is True


# ── find_element + type_into_ref ────────────────────────────────────────────────
def test_find_element_searches_last_snapshot(rig):
    server, _pg, _clip = rig
    _call(server, "read_screen", {})
    out = _call(server, "find_element", {"query": "sea"})
    assert out["ok"] is True and out["count"] == 1
    assert out["matches"][0]["ref"] == "w1e2" and out["matches"][0]["role"] == "Edit"


def test_find_element_requires_a_snapshot(rig):
    server, _pg, _clip = rig
    out = _call(server, "find_element", {"query": "x"})
    assert out["ok"] is False and "read_screen first" in out["error"]


def test_type_into_ref_focuses_then_unicode_pastes(rig, monkeypatch):
    server, pg, clip = rig
    monkeypatch.setattr(cm.time, "sleep", lambda *_: None)  # skip the 0.3s restore delay
    _call(server, "read_screen", {})
    out = _call(server, "type_into_ref", {"ref": "w1e2", "text": "Merhaba ç"})
    assert out["ok"] is True and out["chars"] == len("Merhaba ç")
    # focus click at the Edit center, then the pasted string, then a ctrl/cmd+v hotkey
    assert ("click", 360, 62, "left") in pg.calls
    assert "Merhaba ç" in clip.copies
    assert any(c[0] == "hotkey" for c in pg.calls)
    assert clip.buf == "OWNER-CLIPBOARD"  # the owner's clipboard is restored (after the delay)


def test_type_into_ref_does_not_wipe_a_nontext_clipboard(rig, monkeypatch):
    server, pg, clip = rig
    monkeypatch.setattr(cm.time, "sleep", lambda *_: None)
    clip.buf = ""  # pyperclip returns "" for image/file clipboard content
    _call(server, "read_screen", {})
    _call(server, "type_into_ref", {"ref": "w1e2", "text": "hi"})
    # the empty prior clipboard is NOT restored over the typed text (would wipe an image)
    assert clip.copies[-1] == "hi"
    assert "" not in clip.copies[1:]  # no restore-to-empty after the paste


# ── review fixes: duplicate identity, budget/cap, memo eviction, window-not-found ──
def test_duplicate_identity_mints_distinct_refs():
    reg = perception.RefRegistry()
    reg.begin_snapshot()
    N = perception.A11yNode
    a = N(role="Button", name="OK", rect=(0, 0, 10, 10), identity="dup", interactable=True)
    b = N(role="Button", name="OK", rect=(50, 0, 10, 10), identity="dup", interactable=True)
    ra = reg.assign(a, 1)
    rb = reg.assign(b, 1)
    assert ra != rb, "two DISTINCT same-identity elements must get different refs"
    # each ref resolves to ITS OWN rect (not both collapsing onto the second)
    assert reg.resolve(ra).rect == (0, 0, 10, 10)
    assert reg.resolve(rb).rect == (50, 0, 10, 10)


def test_memo_is_evicted_after_ttl():
    reg = perception.RefRegistry()
    N = perception.A11yNode
    reg.begin_snapshot()
    r1 = reg.assign(N(role="Button", name="X", rect=(0, 0, 5, 5), identity="keep", interactable=True), 1)
    # advance TTL+1 snapshots WITHOUT re-seeing 'keep' → its memo entry is dropped
    for _ in range(perception._MEMO_TTL_SNAPSHOTS + 1):
        reg.begin_snapshot()
    assert "keep" not in reg._ref_for_identity
    # re-seeing it now mints a FRESH ref (the old memo is gone), proving bounded growth
    r2 = reg.assign(N(role="Button", name="X", rect=(0, 0, 5, 5), identity="keep", interactable=True), 1)
    assert r2 != r1


def test_max_refs_caps_ref_minting():
    reg = perception.RefRegistry()
    N = perception.A11yNode
    kids = [N(role="Button", name=f"b{i}", rect=(i, 0, 5, 5), identity=f"id{i}", interactable=True)
            for i in range(perception._MAX_REFS + 25)]
    root = N(role="desktop", children=[N(role="Window", name="W", rect=(0, 0, 9, 9), identity="w", children=kids)])
    perception._assign_refs(root, reg)
    assert len(perception.flatten_refs(root)) == perception._MAX_REFS


def test_render_tree_truncates_oversized_output():
    N = perception.A11yNode
    big = [N(role="Text", name="x" * 100) for _ in range(1000)]
    root = N(role="desktop", children=[N(role="Window", name="W", children=big)])
    txt = perception.render_tree(root)
    assert len(txt) <= perception._MAX_TREE_CHARS + 200  # cap + the truncation notice line
    assert "tree truncated" in txt


def test_window_not_found_is_distinct_from_backend_missing(rig):
    server, _pg, _clip = rig

    def _no_window(scope=None, max_depth=40, include_offscreen=False):
        raise perception.A11yWindowNotFound("no window matched 'nope'")

    perception.set_backend_override(_no_window)
    out = _call(server, "read_screen", {"window": "nope"})
    assert out["ok"] is False
    assert "no window matched" in out["error"]
    assert "fallback" not in out  # window-not-found must NOT suggest the screenshot fallback


def test_degenerate_rect_ref_is_not_clickable(rig):
    server, pg, _clip = rig

    def _thin(scope=None, max_depth=40, include_offscreen=False):
        N = perception.A11yNode
        return N(role="desktop", children=[N(role="Window", name="W", rect=(0, 0, 100, 100), identity="w",
                 children=[N(role="Button", name="Thin", rect=(10, 10, 0, 20), identity="thin", interactable=True)])])

    perception.set_backend_override(_thin)
    _call(server, "read_screen", {})
    before = list(pg.calls)
    out = _call(server, "click_ref", {"ref": "w1e1"})
    assert out["ok"] is False and "clickable bounds" in out["error"]
    assert pg.calls == before  # a 0-width element is never clicked
