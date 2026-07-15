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
  • ``destructive``  — ask before destructive actions only (open app / close window / drag).
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
from typing import Callable

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
_DESTRUCTIVE: frozenset[str] = frozenset({
    "open_application", "close_window", "drag",
    "hotkey", "middle_click", "mouse_down", "mouse_up",
})
# Everything else that actuates (clicks, typing, scroll, window moves, ref actions) is
# "medium": gated only in ``all`` mode.


def risk_of(tool: str) -> str:
    """``safe`` | ``destructive`` | ``medium`` for a bare tool name (no ``computer_`` prefix)."""
    name = str(tool or "").split(".")[-1]
    if name.startswith("computer_"):
        name = name[len("computer_"):]
    if name in _SAFE:
        return "safe"
    if name in _DESTRUCTIVE:
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


def needs_approval(tool: str, mode: str) -> bool:
    """Does ``tool`` require the owner's OK under ``mode``?

    ``mode`` is always one of :data:`MODES` in practice (``resolve_mode`` clamps it); the
    unknown-mode → ``False`` here is the fail-OPEN direction, so a future caller must keep
    passing a clamped value (never a raw, unvalidated setting).
    """
    if mode not in MODES or mode == "off":
        return False
    r = risk_of(tool)
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


def gate(tool: str, data_dir: Path, summary: str) -> str | None:
    """Return a denial reason (str) if the action must NOT proceed, else ``None`` (allow).

    ``None`` also for the common ``off`` / safe-tool / approved cases — the caller executes
    only when this returns ``None``.
    """
    # Fast path: a read-only perception/introspection tool is never gated, so don't even
    # read runtime_settings.json on the hot screenshot/read_screen loop.
    if risk_of(tool) == "safe":
        return None
    mode = resolve_mode(data_dir)
    if not needs_approval(tool, mode):
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
