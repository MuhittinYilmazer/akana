"""Computer-control MCP surface — screenshot + mouse + keyboard as NATIVE tools.

A separate stdio MCP process (like the ``akana_memory`` / ``akana_vault`` children)
that lets the model SEE and DRIVE the owner's live desktop: capture the screen, move
and click the mouse, type text, press hotkeys, and manage windows.

Built on :class:`mcp.server.fastmcp.FastMCP`. The GUI backends (``pyautogui`` /
``mss`` / ``pygetwindow``) are LAZY-imported inside each handler so the module
imports on any machine (CI without a display, tests) and only the operations that
actually touch the screen require the extra dependencies — see
``requirements-computer.txt``.

Run::

    AKANA_DATA_DIR=~/.akana python -m akana_server.computer_mcp
"""

from __future__ import annotations

from akana_server.computer_mcp.__main__ import build_server, main

__all__ = ["build_server", "main"]
