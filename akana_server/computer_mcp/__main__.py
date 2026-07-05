"""Computer-control MCP stdio server — the ``computer.*`` tools (screenshot + input).

This is a FastMCP server named ``computer``. It exposes the owner's live desktop to
the model as native MCP tools: capture the screen, read pixel coordinates, then move /
click the mouse, type text, press hotkeys, and manage windows.

The GUI backends (``pyautogui`` / ``mss`` / ``pygetwindow``) are lazy-imported inside
each handler. On a machine without them the tool call returns a clear install hint
(see ``requirements-computer.txt``) instead of the module failing to import — so the
handshake, ``tools/list`` and the non-GUI verification stay green everywhere.

Coordinate system: EVERY x/y is a PHYSICAL PIXEL of the captured screenshot (the same
pixels ``screenshot`` reports as ``width``/``height``). The operating loop is always
``screenshot -> Read the PNG -> act -> screenshot again``.

Run::

    AKANA_DATA_DIR=~/.akana python -m akana_server.computer_mcp
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

log = logging.getLogger(__name__)

__all__ = ["build_server", "main"]

#: Shown whenever a GUI backend is missing — points at the pinned requirements file.
_INSTALL_HINT = (
    "computer-control backend is not installed. Install it into Akana's environment: "
    "pip install -r requirements-computer.txt "
    "(pyautogui + mss + pygetwindow). See requirements-computer.txt for the exact pins."
)

#: Returned when pyautogui's FAILSAFE trips (cursor slammed to a screen corner). This is
#: the OWNER'S deliberate emergency stop — a distinct message so the model does not
#: mistake it for a random crash and retry through the abort.
_FAILSAFE_MSG = "aborted by fail-safe (cursor at a screen corner — the owner's emergency stop)"


class _BackendMissing(RuntimeError):
    """A GUI backend import failed — surfaced to the model as a tool error."""


def _data_dir() -> Path:
    return Path(os.environ.get("AKANA_DATA_DIR") or Path.home() / ".akana").expanduser()


def _shots_dir() -> Path:
    """``<AKANA_DATA_DIR>/run/computer`` — created on demand."""
    d = _data_dir() / "run" / "computer"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _shot_name() -> str:
    """A sortable, collision-free basename for a screenshot PNG."""
    try:
        import ulid

        return str(ulid.new())
    except Exception:  # ulid is optional — fall back to a timestamp
        return time.strftime("%Y%m%d-%H%M%S-") + f"{int(time.time() * 1000) % 1000:03d}"


def _pyautogui() -> Any:
    """Lazy-import pyautogui with FAILSAFE on; raise a hint if it is missing."""
    try:
        import pyautogui
    except Exception as exc:  # ImportError, or platform errors (no display)
        raise _BackendMissing(f"{_INSTALL_HINT} (pyautogui: {exc})") from exc
    pyautogui.FAILSAFE = True
    return pyautogui


def _mss() -> Any:
    try:
        import mss
    except Exception as exc:
        raise _BackendMissing(f"{_INSTALL_HINT} (mss: {exc})") from exc
    return mss


def _pygetwindow() -> Any:
    try:
        import pygetwindow
    except Exception as exc:
        raise _BackendMissing(f"{_INSTALL_HINT} (pygetwindow: {exc})") from exc
    return pygetwindow


def _pyperclip() -> Any:
    """Lazy-import pyperclip (clipboard read/write); raise a hint if it is missing."""
    try:
        import pyperclip
    except Exception as exc:
        raise _BackendMissing(f"{_INSTALL_HINT} (pyperclip: {exc})") from exc
    return pyperclip


def build_server() -> FastMCP:
    """Construct the ``computer`` FastMCP server with all tools registered."""
    mcp = FastMCP("computer")

    def _resolve_window(title_contains: str):
        """Find the first window whose title CONTAINS ``title_contains`` (case-insensitive).

        Returns ``(window, None)`` on a match, or ``(None, error_dict)`` on a miss (the
        error dict lists the open titles). Shared by ``focus_window`` and the
        window-management tools so they match identically.
        """
        gw = _pygetwindow()
        needle = str(title_contains or "").strip().lower()
        if not needle:
            return None, {"ok": False, "error": "title_contains must be non-empty"}
        matches = [
            w
            for w in gw.getAllWindows()
            if needle in (getattr(w, "title", "") or "").lower()
        ]
        if not matches:
            titles = sorted(
                {
                    (getattr(w, "title", "") or "").strip()
                    for w in gw.getAllWindows()
                    if (getattr(w, "title", "") or "").strip()
                }
            )
            return None, {"ok": False, "error": "no window matched", "open_titles": titles}
        return matches[0], None

    @mcp.tool()
    def screen_info() -> dict[str, Any]:
        """Report the desktop geometry BEFORE any action.

        Returns the primary screen size and every monitor's bounds (in physical
        pixels). Use it to sanity-check that a coordinate you intend to click is on
        an actual screen, and to pick a ``monitor`` for ``screenshot`` on multi-monitor
        setups. No mouse or keyboard is touched.
        """
        info: dict[str, Any] = {}
        try:
            pg = _pyautogui()
            w, h = pg.size()
            info["primary"] = {"width": int(w), "height": int(h)}
        except _BackendMissing as exc:
            info["primary_error"] = str(exc)
        monitors: list[dict[str, int]] = []
        try:
            mss = _mss()
            with mss.MSS() as sct:
                # sct.monitors[0] is the virtual "all monitors" bounding box; 1.. are real.
                for idx, mon in enumerate(sct.monitors):
                    monitors.append(
                        {
                            "index": idx,
                            "left": int(mon["left"]),
                            "top": int(mon["top"]),
                            "width": int(mon["width"]),
                            "height": int(mon["height"]),
                        }
                    )
            info["monitors"] = monitors
            info["note"] = (
                "monitor 0 is the full virtual desktop (all screens); 1, 2, ... are "
                "individual monitors. Coordinates in screenshots/clicks are physical pixels."
            )
        except _BackendMissing as exc:
            info["monitors_error"] = str(exc)
        return info

    @mcp.tool()
    def screenshot(monitor: int = 0) -> dict[str, Any]:
        """Capture the screen to a PNG and return its path — ALWAYS do this before acting.

        ``monitor``: 0 captures the FULL virtual desktop (all screens stitched); 1, 2, ...
        capture a single monitor (see ``screen_info`` for the indices). The image is
        saved under ``<AKANA_DATA_DIR>/run/computer/<id>.png``.

        Returns ``{path, width, height}`` where ``path`` is ABSOLUTE. You MUST then Read
        that path to actually see the screen — this tool only saves the file, it does not
        return the pixels. The returned ``width``/``height`` are the coordinate space for
        every subsequent click/move (top-left is 0,0; x grows right, y grows down).
        """
        mss = _mss()
        try:
            from PIL import Image
        except Exception as exc:  # Pillow ships with the pack, but be defensive
            raise _BackendMissing(f"{_INSTALL_HINT} (Pillow: {exc})") from exc
        out = _shots_dir() / f"{_shot_name()}.png"
        with mss.MSS() as sct:
            mons = sct.monitors
            idx = monitor if 0 <= monitor < len(mons) else 0
            raw = sct.grab(mons[idx])
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        img.save(str(out), format="PNG")
        return {
            "path": str(out.resolve()),
            "width": int(raw.width),
            "height": int(raw.height),
            "instructions": (
                "Read this path now to see the screen, then use physical-pixel "
                "coordinates for any click/move/drag."
            ),
        }

    @mcp.tool()
    def left_click(x: int, y: int) -> dict[str, Any]:
        """Left-click at physical pixel (x, y). Screenshot + Read first to locate the target."""
        pg = _pyautogui()
        try:
            pg.click(x=int(x), y=int(y), button="left")
        except pg.FailSafeException:
            return {"ok": False, "action": "left_click", "error": _FAILSAFE_MSG}
        except Exception as exc:
            return {"ok": False, "action": "left_click", "error": str(exc)}
        return {"ok": True, "action": "left_click", "x": int(x), "y": int(y)}

    @mcp.tool()
    def double_click(x: int, y: int) -> dict[str, Any]:
        """Double-click at physical pixel (x, y) — e.g. to open an item."""
        pg = _pyautogui()
        try:
            pg.doubleClick(x=int(x), y=int(y))
        except pg.FailSafeException:
            return {"ok": False, "action": "double_click", "error": _FAILSAFE_MSG}
        except Exception as exc:
            return {"ok": False, "action": "double_click", "error": str(exc)}
        return {"ok": True, "action": "double_click", "x": int(x), "y": int(y)}

    @mcp.tool()
    def right_click(x: int, y: int) -> dict[str, Any]:
        """Right-click at physical pixel (x, y) — opens the context menu there."""
        pg = _pyautogui()
        try:
            pg.click(x=int(x), y=int(y), button="right")
        except pg.FailSafeException:
            return {"ok": False, "action": "right_click", "error": _FAILSAFE_MSG}
        except Exception as exc:
            return {"ok": False, "action": "right_click", "error": str(exc)}
        return {"ok": True, "action": "right_click", "x": int(x), "y": int(y)}

    @mcp.tool()
    def mouse_move(x: int, y: int) -> dict[str, Any]:
        """Move the cursor to physical pixel (x, y) WITHOUT clicking (e.g. to hover)."""
        pg = _pyautogui()
        try:
            pg.moveTo(int(x), int(y))
        except pg.FailSafeException:
            return {"ok": False, "action": "mouse_move", "error": _FAILSAFE_MSG}
        except Exception as exc:
            return {"ok": False, "action": "mouse_move", "error": str(exc)}
        return {"ok": True, "action": "mouse_move", "x": int(x), "y": int(y)}

    @mcp.tool()
    def drag(x1: int, y1: int, x2: int, y2: int) -> dict[str, Any]:
        """Press at (x1, y1), drag to (x2, y2), release — e.g. select text or move an item.

        All four values are physical pixels. Confirm with the owner before dragging
        something that moves or deletes data irreversibly.
        """
        pg = _pyautogui()
        try:
            pg.moveTo(int(x1), int(y1))
            pg.dragTo(int(x2), int(y2), button="left")
        except pg.FailSafeException:
            return {"ok": False, "action": "drag", "error": _FAILSAFE_MSG}
        except Exception as exc:
            return {"ok": False, "action": "drag", "error": str(exc)}
        return {
            "ok": True,
            "action": "drag",
            "from": [int(x1), int(y1)],
            "to": [int(x2), int(y2)],
        }

    @mcp.tool()
    def scroll(amount: int, x: int | None = None, y: int | None = None) -> dict[str, Any]:
        """Scroll the wheel by ``amount`` clicks: positive = UP, negative = DOWN.

        If (x, y) are given, the cursor moves there first so the scroll targets that
        region (physical pixels). Omit them to scroll wherever the cursor already is.
        """
        pg = _pyautogui()
        try:
            if x is not None and y is not None:
                pg.moveTo(int(x), int(y))
            pg.scroll(int(amount))
        except pg.FailSafeException:
            return {"ok": False, "action": "scroll", "error": _FAILSAFE_MSG}
        except Exception as exc:
            return {"ok": False, "action": "scroll", "error": str(exc)}
        return {"ok": True, "action": "scroll", "amount": int(amount), "x": x, "y": y}

    @mcp.tool()
    def type_text(text: str) -> dict[str, Any]:
        """Type ``text`` into whatever currently has keyboard focus (as if typed by hand).

        Click the target field FIRST. NEVER type passwords, card numbers, OTPs or other
        credentials — stop and ask the owner to enter those by hand.
        """
        pg = _pyautogui()
        try:
            pg.typewrite(str(text), interval=0.01)
        except pg.FailSafeException:
            return {"ok": False, "action": "type_text", "error": _FAILSAFE_MSG}
        except Exception as exc:
            return {"ok": False, "action": "type_text", "error": str(exc)}
        return {"ok": True, "action": "type_text", "chars": len(str(text))}

    @mcp.tool()
    def hotkey(keys: list[str]) -> dict[str, Any]:
        """Press a key combination together, e.g. ``["ctrl", "c"]`` or ``["alt", "tab"]``.

        Each item is a pyautogui key name (letters, digits, ``ctrl``/``alt``/``shift``/
        ``win``/``enter``/``tab``/``esc``/``f1``..., arrows as ``left``/``right``/``up``/
        ``down``). The keys are pressed in order and released in reverse.
        """
        pg = _pyautogui()
        ks = [str(k) for k in (keys or [])]
        if not ks:
            return {"ok": False, "error": "keys must be a non-empty list"}
        try:
            pg.hotkey(*ks)
        except pg.FailSafeException:
            return {"ok": False, "action": "hotkey", "error": _FAILSAFE_MSG}
        except Exception as exc:
            return {"ok": False, "action": "hotkey", "error": str(exc)}
        return {"ok": True, "action": "hotkey", "keys": ks}

    @mcp.tool()
    def list_windows() -> dict[str, Any]:
        """List the titles of open top-level windows — use to find a target for ``focus_window``.

        Returns each window's title plus its bounds (left/top/width/height in physical
        pixels) so you can decide where it is on screen. Untitled windows are skipped.
        """
        gw = _pygetwindow()
        windows: list[dict[str, Any]] = []
        for w in gw.getAllWindows():
            title = getattr(w, "title", "") or ""
            if not title.strip():
                continue
            windows.append(
                {
                    "title": title,
                    "left": int(getattr(w, "left", 0)),
                    "top": int(getattr(w, "top", 0)),
                    "width": int(getattr(w, "width", 0)),
                    "height": int(getattr(w, "height", 0)),
                }
            )
        return {"windows": windows, "count": len(windows)}

    @mcp.tool()
    def focus_window(title_contains: str) -> dict[str, Any]:
        """Bring the first window whose title CONTAINS ``title_contains`` to the front.

        Case-insensitive substring match. Restores the window if minimized, then
        activates it. After focusing, take a fresh ``screenshot`` before acting — the
        layout changed. Returns which title matched, or an error listing candidates.
        """
        target, err = _resolve_window(title_contains)
        if err:
            return err
        try:
            if getattr(target, "isMinimized", False):
                target.restore()
            target.activate()
        except Exception as exc:  # some window managers reject activate()
            return {"ok": False, "error": f"could not focus window: {exc}"}
        return {"ok": True, "action": "focus_window", "title": getattr(target, "title", "")}

    # -- click / mouse primitives (reference-set parity) -------------------------

    @mcp.tool()
    def triple_click(x: int, y: int) -> dict[str, Any]:
        """Triple-click at physical pixel (x, y) — selects the whole line/paragraph.

        The standard way to select existing text before replacing it (triple-click,
        then ``type_text``/``paste_text`` the new value).
        """
        pg = _pyautogui()
        try:
            pg.click(x=int(x), y=int(y), clicks=3, interval=0.05)
        except pg.FailSafeException:
            return {"ok": False, "action": "triple_click", "error": _FAILSAFE_MSG}
        except Exception as exc:
            return {"ok": False, "action": "triple_click", "error": str(exc)}
        return {"ok": True, "action": "triple_click", "x": int(x), "y": int(y)}

    @mcp.tool()
    def middle_click(x: int, y: int) -> dict[str, Any]:
        """Middle-click at (x, y) — e.g. open a link in a new tab, or close a browser tab."""
        pg = _pyautogui()
        try:
            pg.click(x=int(x), y=int(y), button="middle")
        except pg.FailSafeException:
            return {"ok": False, "action": "middle_click", "error": _FAILSAFE_MSG}
        except Exception as exc:
            return {"ok": False, "action": "middle_click", "error": str(exc)}
        return {"ok": True, "action": "middle_click", "x": int(x), "y": int(y)}

    @mcp.tool()
    def mouse_down(x: int, y: int, button: str = "left") -> dict[str, Any]:
        """Press and HOLD a mouse button at (x, y) WITHOUT releasing — pair with ``mouse_up``.

        ``button`` is "left" | "right" | "middle". Use for gestures ``drag`` cannot express:
        hold a modifier (via ``hotkey``/``hold_key``) across the press, or draw a freehand
        path with several ``mouse_move`` steps between ``mouse_down`` and ``mouse_up``.
        """
        pg = _pyautogui()
        try:
            pg.mouseDown(x=int(x), y=int(y), button=str(button))
        except pg.FailSafeException:
            return {"ok": False, "action": "mouse_down", "error": _FAILSAFE_MSG}
        except Exception as exc:
            return {"ok": False, "action": "mouse_down", "error": str(exc)}
        return {"ok": True, "action": "mouse_down", "x": int(x), "y": int(y), "button": str(button)}

    @mcp.tool()
    def mouse_up(x: int, y: int, button: str = "left") -> dict[str, Any]:
        """Release a held mouse button at (x, y) — the other half of ``mouse_down``."""
        pg = _pyautogui()
        try:
            pg.mouseUp(x=int(x), y=int(y), button=str(button))
        except pg.FailSafeException:
            return {"ok": False, "action": "mouse_up", "error": _FAILSAFE_MSG}
        except Exception as exc:
            return {"ok": False, "action": "mouse_up", "error": str(exc)}
        return {"ok": True, "action": "mouse_up", "x": int(x), "y": int(y), "button": str(button)}

    @mcp.tool()
    def hscroll(amount: int, x: int | None = None, y: int | None = None) -> dict[str, Any]:
        """Scroll HORIZONTALLY by ``amount`` clicks: positive = RIGHT, negative = LEFT.

        If (x, y) are given the cursor moves there first (physical pixels). Use for wide
        tables, timelines, or horizontally-scrolling galleries. Vertical scroll is ``scroll``.
        """
        pg = _pyautogui()
        try:
            if x is not None and y is not None:
                pg.moveTo(int(x), int(y))
            pg.hscroll(int(amount))
        except pg.FailSafeException:
            return {"ok": False, "action": "hscroll", "error": _FAILSAFE_MSG}
        except Exception as exc:
            return {"ok": False, "action": "hscroll", "error": str(exc)}
        return {"ok": True, "action": "hscroll", "amount": int(amount), "x": x, "y": y}

    @mcp.tool()
    def cursor_position() -> dict[str, Any]:
        """Return the current mouse cursor position as ``{x, y}`` (physical pixels).

        Lets you confirm a ``mouse_move``/``drag`` landed where intended without spending a
        screenshot. No mouse or keyboard is touched.
        """
        pg = _pyautogui()
        try:
            pos = pg.position()
        except Exception as exc:
            return {"ok": False, "action": "cursor_position", "error": str(exc)}
        return {"ok": True, "x": int(pos[0]), "y": int(pos[1])}

    # -- keyboard primitives -----------------------------------------------------

    @mcp.tool()
    def key(name: str, presses: int = 1) -> dict[str, Any]:
        """Press a SINGLE named key, optionally repeated ``presses`` times.

        e.g. ``name="enter"``, ``"tab"``, ``"esc"``, ``"backspace"``, ``"delete"``,
        ``"f5"``, ``"up"``/``"down"``/``"left"``/``"right"``, ``"pageup"``. For a chord
        held together (Ctrl+C, Alt+Tab) use ``hotkey`` instead.
        """
        pg = _pyautogui()
        k = str(name or "").strip()
        if not k:
            return {"ok": False, "error": "name must be non-empty"}
        n = max(1, int(presses))
        try:
            pg.press(k, presses=n)
        except pg.FailSafeException:
            return {"ok": False, "action": "key", "error": _FAILSAFE_MSG}
        except Exception as exc:
            return {"ok": False, "action": "key", "error": str(exc)}
        return {"ok": True, "action": "key", "name": k, "presses": n}

    @mcp.tool()
    def hold_key(keys: list[str], duration: float = 1.0) -> dict[str, Any]:
        """Hold key(s) DOWN for ``duration`` seconds, then release them (in reverse order).

        e.g. ``keys=["shift"]`` to extend a selection while another action runs, or
        ``keys=["w"]`` to hold a movement key in a game. ``duration`` is clamped to 10s.
        Keys are pyautogui names (letters, ``ctrl``/``alt``/``shift``/arrows/...).
        """
        pg = _pyautogui()
        ks = [str(k) for k in (keys or [])]
        if not ks:
            return {"ok": False, "error": "keys must be a non-empty list"}
        secs = max(0.0, min(float(duration), 10.0))
        try:
            for k in ks:
                pg.keyDown(k)
            time.sleep(secs)
            for k in reversed(ks):
                pg.keyUp(k)
        except Exception as exc:
            for k in reversed(ks):  # best-effort release so no key is left stuck down
                try:
                    pg.keyUp(k)
                except Exception:
                    pass
            return {"ok": False, "action": "hold_key", "error": str(exc)}
        return {"ok": True, "action": "hold_key", "keys": ks, "duration": secs}

    # -- clipboard ---------------------------------------------------------------

    @mcp.tool()
    def read_clipboard() -> dict[str, Any]:
        """Return the current TEXT contents of the system clipboard as ``{text}``.

        Use to read data the owner copied for you, or to verify a copy succeeded.
        """
        pc = _pyperclip()
        try:
            return {"ok": True, "text": str(pc.paste())}
        except Exception as exc:
            return {"ok": False, "action": "read_clipboard", "error": str(exc)}

    @mcp.tool()
    def write_clipboard(text: str) -> dict[str, Any]:
        """Set the system clipboard to ``text`` (e.g. to paste it later with ``hotkey(["ctrl","v"])``)."""
        pc = _pyperclip()
        try:
            pc.copy(str(text))
        except Exception as exc:
            return {"ok": False, "action": "write_clipboard", "error": str(exc)}
        return {"ok": True, "action": "write_clipboard", "chars": len(str(text))}

    @mcp.tool()
    def paste_text(text: str) -> dict[str, Any]:
        """Type ``text`` into the focused field via the CLIPBOARD (copy + Ctrl/Cmd+V).

        PREFER THIS over ``type_text`` for anything with non-ASCII — Turkish (ç ğ ı İ ö ş
        ü), accents, emoji, CJK. ``type_text`` sends per-key scan codes and SILENTLY DROPS
        characters that are not on the US keyboard layout, whereas paste inserts the exact
        text. Click the target field FIRST. Overwrites the clipboard. NEVER paste
        passwords, OTPs, or card numbers — ask the owner to enter those by hand.
        """
        pc = _pyperclip()
        pg = _pyautogui()
        try:
            pc.copy(str(text))
            mod = "command" if sys.platform == "darwin" else "ctrl"
            pg.hotkey(mod, "v")
        except pg.FailSafeException:
            return {"ok": False, "action": "paste_text", "error": _FAILSAFE_MSG}
        except Exception as exc:
            return {"ok": False, "action": "paste_text", "error": str(exc)}
        return {"ok": True, "action": "paste_text", "chars": len(str(text))}

    # -- application launch (HIGH RISK) ------------------------------------------

    @mcp.tool()
    def open_application(name: str) -> dict[str, Any]:
        """Launch an application (or open a document) by ``name``. HIGH RISK — starts a
        program on the owner's machine; if it is unexpected, confirm with the owner first.

        Windows: ``name`` may be an app on PATH (``"notepad"``, ``"chrome"``) or a file
        path. macOS: an application name (``"Safari"``, ``"Notes"``). Linux: an executable
        or an xdg-openable target. The new window takes time to appear — ``wait`` a beat
        (or just re-``screenshot`` a couple of times) before acting on it.
        """
        app = str(name or "").strip()
        if not app:
            return {"ok": False, "error": "name must be non-empty"}
        try:
            if sys.platform == "win32":
                # `start "" <name>` resolves PATH apps AND documents; list form (no
                # shell=True) avoids shell-metacharacter injection from the name.
                subprocess.Popen(["cmd", "/c", "start", "", app])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-a", app])
            else:
                subprocess.Popen([app])
        except Exception as exc:
            return {"ok": False, "action": "open_application", "error": str(exc)}
        return {"ok": True, "action": "open_application", "launched": app}

    # -- window management (min / max / move / resize / close) -------------------

    @mcp.tool()
    def maximize_window(title_contains: str) -> dict[str, Any]:
        """Maximize the first window whose title CONTAINS ``title_contains`` (case-insensitive)."""
        target, err = _resolve_window(title_contains)
        if err:
            return err
        try:
            target.maximize()
        except Exception as exc:
            return {"ok": False, "action": "maximize_window", "error": str(exc)}
        return {"ok": True, "action": "maximize_window", "title": getattr(target, "title", "")}

    @mcp.tool()
    def minimize_window(title_contains: str) -> dict[str, Any]:
        """Minimize the first window whose title CONTAINS ``title_contains`` (case-insensitive)."""
        target, err = _resolve_window(title_contains)
        if err:
            return err
        try:
            target.minimize()
        except Exception as exc:
            return {"ok": False, "action": "minimize_window", "error": str(exc)}
        return {"ok": True, "action": "minimize_window", "title": getattr(target, "title", "")}

    @mcp.tool()
    def move_window(title_contains: str, x: int, y: int) -> dict[str, Any]:
        """Move the matched window so its TOP-LEFT is at (x, y) in physical pixels.

        Handy to park an app at a known position for stable clicks. Re-``screenshot``
        after moving — everything shifted.
        """
        target, err = _resolve_window(title_contains)
        if err:
            return err
        try:
            target.moveTo(int(x), int(y))
        except Exception as exc:
            return {"ok": False, "action": "move_window", "error": str(exc)}
        return {"ok": True, "action": "move_window", "title": getattr(target, "title", ""), "x": int(x), "y": int(y)}

    @mcp.tool()
    def resize_window(title_contains: str, width: int, height: int) -> dict[str, Any]:
        """Resize the matched window to ``width`` x ``height`` physical pixels."""
        target, err = _resolve_window(title_contains)
        if err:
            return err
        try:
            target.resizeTo(int(width), int(height))
        except Exception as exc:
            return {"ok": False, "action": "resize_window", "error": str(exc)}
        return {
            "ok": True,
            "action": "resize_window",
            "title": getattr(target, "title", ""),
            "width": int(width),
            "height": int(height),
        }

    @mcp.tool()
    def close_window(title_contains: str) -> dict[str, Any]:
        """Close the first window whose title CONTAINS ``title_contains``. DESTRUCTIVE —
        can discard unsaved work. State which window you will close and confirm with the
        owner BEFORE calling this, per the skill's safety rules.
        """
        target, err = _resolve_window(title_contains)
        if err:
            return err
        title = getattr(target, "title", "")
        try:
            target.close()
        except Exception as exc:
            return {"ok": False, "action": "close_window", "error": str(exc)}
        return {"ok": True, "action": "close_window", "title": title}

    return mcp


def main() -> int:
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:  # pragma: no cover - best effort (parity with the other MCP children)
            pass
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    log.info("akana-computer MCP serving on stdio (data_dir=%s)", _data_dir())
    build_server().run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
