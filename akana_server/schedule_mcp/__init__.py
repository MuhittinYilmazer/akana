"""ScheduleEngine MCP server package — serves the schedule tools over stdio.

Mirrors :mod:`akana_server.vault_mcp`: a stdio JSON-RPC server (``mcp.py``)
exposing the schedule tools to the claude/cursor providers. The tool logic +
schemas are single-sourced from :mod:`akana_server.schedule.tools`; this package
only owns the protocol loop and the ``data_dir``-scoped launcher.
"""

from __future__ import annotations

__all__: list[str] = []
