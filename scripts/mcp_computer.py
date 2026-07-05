"""Standalone launcher for the akana computer-control MCP stdio server.

Run as a FILE — ``python <abs>/scripts/mcp_computer.py`` — NOT ``python -m
akana_server.computer_mcp``. Computes ``<repo>`` from its OWN absolute ``__file__``
and puts it first on ``sys.path`` so ``akana_server`` imports from ANY cwd with NO
environment. Symmetric with scripts/mcp_vault.py; removes the PYTHONPATH/cwd
dependency from the spawn.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from akana_server.computer_mcp import main  # noqa: E402 - follows the sys.path bootstrap

if __name__ == "__main__":
    raise SystemExit(main())
