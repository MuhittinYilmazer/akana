"""Per-action approval gate for computer-control (Phase 2 — opt-in, default OFF).

The gate lives INSIDE the computer MCP child because that is the only PROVIDER-NEUTRAL
point: every provider (the claude/cursor/codex CLIs AND the native gemini/openai/ollama
in-process bridge) executes ``computer.*`` tools here, out of the Akana server's reach —
a server-side check could never gate the CLI providers, which run tools in their own
subprocess. Putting the gate in the tool handlers covers all of them with one mechanism.

Mode is read LIVE from ``<data_dir>/runtime_settings.json`` (key
``computer_control_approval``) with an ``AKANA_COMPUTER_APPROVAL`` env fallback, so
toggling it in Settings applies to the NEXT tool call with no restart:
  • ``off``          — no approval; current full-autonomy behavior (the DEFAULT).
  • ``destructive``  — ask before destructive actions only (open app / close window / drag,
    and any KEY CHORD that reproduces one, e.g. alt+F4 — whatever tool presses it).
  • ``all``          — ask before every actuation (clicks, typing, window ops); read-only
    perception (screenshot / read_screen / find_element / clipboard read) is never gated.

Channel: a NATIVE confirmation dialog on the controlled desktop — the owner is physically
there when Akana drives their machine, so a modal is the most direct, self-contained,
zero-plumbing approval surface (no server round-trip, no auth, no frontend card). If no
dialog backend is usable AND approval is required, the action is DENIED — fail-safe: an
action the owner cannot confirm must never proceed silently. The prompter is pluggable via
``set_prompter`` (tests inject a decision; a future in-chat approval card can replace it).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

MODES = ("off", "destructive", "all")
_DEFAULT_MODE = "off"

#: Read-only perception + introspection — NEVER gated (no side effect on the desktop).
_SAFE: frozenset[str] = frozenset({
    "screen_info", "screenshot", "read_screen", "find_element",
    "cursor_position", "read_clipboard", "list_windows",
})

#: Hard-to-undo / launches or closes things — gated in BOTH ``destructive`` and ``all``.
#: Includes the low-level primitives that REPRODUCE a destructive action, so ``destructive``
#: mode cannot be trivially bypassed: ``hotkey`` (alt+f4 / ctrl+w close a window/tab),
#: ``middle_click`` (closes a browser tab), and ``mouse_down``/``mouse_up`` (a press-drag,
#: i.e. ``drag`` by hand). ``key`` stays "medium" (a lone keystroke is not window-destroying,
#: and gating every Enter/Tab would make ``destructive`` mode unusably noisy).
#: This name set is a FLOOR, never loosened — but a name set ALONE cannot hold the line:
#: ``hold_key`` pressed the identical alt+F4 down/up sequence under a different name and so
#: ran unapproved. See :func:`chord_risk` for the second, name-independent layer.
_DESTRUCTIVE: frozenset[str] = frozenset({
    "open_application", "close_window", "drag",
    "hotkey", "middle_click", "mouse_down", "mouse_up",
})
# Everything else that actuates (clicks, typing, scroll, window moves, ref actions) is
# "medium": gated only in ``all`` mode.

#: Canonical modifier names, and every spelling that maps onto one. ``command``/``cmd`` stays
#: distinct from ``win``: on macOS ⌘ is the app-command modifier (⌘W/⌘Q close like ctrl+W /
#: ctrl+Q) while the launcher chord is ⌘Space, not ⌘R — folding it into either alone would
#: both miss real chords and flag ⌘R (reload) as destructive.
_MODIFIERS: frozenset[str] = frozenset({"ctrl", "alt", "shift", "win", "cmd"})
_KEY_ALIASES: dict[str, str] = {
    "control": "ctrl", "ctrlleft": "ctrl", "ctrlright": "ctrl",
    "altleft": "alt", "altright": "alt",
    "option": "alt", "optionleft": "alt", "optionright": "alt",
    "shiftleft": "shift", "shiftright": "shift",
    "winleft": "win", "winright": "win", "super": "win",
    "command": "cmd", "cmdleft": "cmd", "cmdright": "cmd",
    "del": "delete",
}

#: ``(any of these modifiers) + (any of these keys)`` reproduces a DESTRUCTIVE tool. Kept
#: deliberately narrow: every entry closes/quits/deletes/launches, so ``destructive`` mode
#: stays usable (ctrl+c, ctrl+v, alt+tab and a held shift must NOT prompt).
_DESTRUCTIVE_CHORDS: tuple[tuple[frozenset[str], frozenset[str]], ...] = (
    (frozenset({"alt"}), frozenset({"f4"})),                    # close the focused window
    (frozenset({"ctrl", "cmd"}), frozenset({"w", "q", "f4"})),  # close tab / quit app
    (frozenset({"win"}), frozenset({"r"})),                     # Run box → open_application's reach
    (frozenset({"cmd"}), frozenset({"space"})),                 # Spotlight → the same reach
    (frozenset({"shift"}), frozenset({"delete"})),              # permanent delete (skips the bin)
)


def chord_risk(keys: Any) -> str:
    """``destructive`` if pressing ``keys`` TOGETHER reproduces a destructive action.

    Classification by the key set is what closes the synonym hole: the tool that presses a
    chord may be called anything (``hotkey``, ``hold_key``, tomorrow's ``press_combo``), but
    the chord it presses is the same window-closing action either way. A lone key with no
    modifier is never destructive here — that is the same call the ``key`` note above makes.
    """
    ks = {_KEY_ALIASES.get(k, k) for k in (str(x or "").strip().lower() for x in keys or ())}
    mods = ks & _MODIFIERS
    if not mods:
        return "medium"
    rest = ks - _MODIFIERS
    for need, targets in _DESTRUCTIVE_CHORDS:
        if (mods & need) and (rest & targets):
            return "destructive"
    return "medium"


def _presses_a_destructive_chord(args: Any) -> bool:
    """True if ANY argument is a key list whose chord is destructive.

    Argument-name-independent on purpose: a list of key names is the only way to express a
    chord over MCP, so the SHAPE of the argument — not the tool's or the parameter's name —
    is what gets classified.
    """
    if not isinstance(args, dict):
        return False
    for val in args.values():
        if isinstance(val, (list, tuple)) and val and all(isinstance(k, str) for k in val):
            if chord_risk(val) == "destructive":
                return True
    return False


def risk_of(tool: str, args: Any = None) -> str:
    """``safe`` | ``destructive`` | ``medium`` for a bare tool name (no ``computer_`` prefix).

    ``args`` (the call's keyword arguments) only ever ESCALATES medium → destructive: the
    name sets decide first, so this can never open a gate, only close one the tool name
    alone would have missed.
    """
    name = str(tool or "").split(".")[-1]
    if name.startswith("computer_"):
        name = name[len("computer_"):]
    if name in _SAFE:
        return "safe"
    if name in _DESTRUCTIVE:
        return "destructive"
    if _presses_a_destructive_chord(args):
        return "destructive"
    return "medium"


def resolve_mode(data_dir: Path) -> str:
    """Current approval mode, read LIVE: runtime_settings.json > env > default (``off``)."""
    try:
        raw = json.loads((Path(data_dir) / "runtime_settings.json").read_text(encoding="utf-8"))
        val = str(raw.get("computer_control_approval", "")).strip().lower()
        if val in MODES:
            return val
    except (OSError, ValueError, TypeError):
        pass
    env = os.environ.get("AKANA_COMPUTER_APPROVAL", "").strip().lower()
    return env if env in MODES else _DEFAULT_MODE


def needs_approval(tool: str, mode: str, args: Any = None) -> bool:
    """Does ``tool`` require the owner's OK under ``mode``?

    ``mode`` is always one of :data:`MODES` in practice (``resolve_mode`` clamps it); the
    unknown-mode → ``False`` here is the fail-OPEN direction, so a future caller must keep
    passing a clamped value (never a raw, unvalidated setting).
    """
    if mode not in MODES or mode == "off":
        return False
    r = risk_of(tool, args)
    if r == "safe":
        return False
    if mode == "destructive":
        return r == "destructive"
    return True  # mode == "all": every non-safe actuation


#: Prompter: ``(title, summary) -> bool`` (True = approved). Overridable for tests and a
#: future in-chat card. Default = a native desktop dialog; fail-safe DENY if unavailable.
_prompter: Callable[[str, str], bool] | None = None


def set_prompter(fn: Callable[[str, str], bool] | None) -> None:
    global _prompter
    _prompter = fn


#: The native approval dialog auto-denies after this long — an owner who walked away must
#: not hang the (inline-on-event-loop) MCP child forever. Only "Allow" approves, so a
#: timeout (or any other reply) denies.
_DIALOG_TIMEOUT_MS = 120_000


def _native_dialog(title: str, summary: str) -> bool:
    """A blocking Allow/Deny dialog on the controlled desktop. Any failure (no display,
    tkinter missing on Linux, timeout, window closed) → False (deny) so an un-confirmable
    action stops."""
    try:
        import pymsgbox  # ships with pyautogui (mouseinfo → pymsgbox)
    except Exception:
        return False
    try:
        # confirm() returns the button text, None if the window closed, or "Timeout" when
        # the bounded timeout elapses — only an explicit "Allow" approves.
        try:
            choice = pymsgbox.confirm(
                text=summary, title=title, buttons=["Allow", "Deny"], timeout=_DIALOG_TIMEOUT_MS
            )
        except TypeError:
            # Older pymsgbox without a timeout kwarg — still correct, just unbounded.
            choice = pymsgbox.confirm(text=summary, title=title, buttons=["Allow", "Deny"])
        return choice == "Allow"
    except Exception:
        return False


def ask(title: str, summary: str) -> bool:
    fn = _prompter or _native_dialog
    try:
        return bool(fn(title, summary))
    except Exception:
        return False  # fail-safe: a broken prompter denies


def gate(tool: str, data_dir: Path, summary: str, args: Any = None) -> str | None:
    """Return a denial reason (str) if the action must NOT proceed, else ``None`` (allow).

    ``None`` also for the common ``off`` / safe-tool / approved cases — the caller executes
    only when this returns ``None``. ``args`` are the call's keyword arguments; pass them so
    a chord-pressing tool is classified by the keys it presses, not by its name.
    """
    # Fast path: a read-only perception/introspection tool is never gated, so don't even
    # read runtime_settings.json on the hot screenshot/read_screen loop.
    if risk_of(tool, args) == "safe":
        return None
    mode = resolve_mode(data_dir)
    if not needs_approval(tool, mode, args):
        return None
    title = "Akana — computer control"
    prompt = (
        "Akana wants to perform this action on your computer:\n\n"
        f"    {summary}\n\nAllow it?"
    )
    if ask(title, prompt):
        return None
    return (
        f"denied by the owner (computer_control_approval={mode!r}; the {tool} action was "
        "not approved). Do not retry the same action; ask the owner what to do instead."
    )
