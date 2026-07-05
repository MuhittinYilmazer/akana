"""Standalone launcher for the akana_memory MCP stdio server.

Run as a FILE — ``python <abs>/scripts/mcp_memory.py`` — NOT ``python -m
akana.memory.mcp``. The ``-m`` form is fragile on Windows: it depends on the spawn's
cwd (the repo-root ``akana.py`` shadows the ``akana`` package → "'akana' is not a
package") AND on ``PYTHONPATH`` being set. If the MCP client ignores the config's
``cwd``/``env``, the child dies on import and the server is stuck "connecting".

This launcher routes through the SINGLE src-layout bootstrap (``_akana_src_bootstrap``,
one of its four sanctioned entry points) so the server imports correctly from ANY
working directory with NO environment — eliminating the whole cwd/PYTHONPATH/shadowing
failure class. Because the file is run directly (``sys.path[0]`` = the ``scripts/``
dir, not the repo root), it first puts the repo root on ``sys.path`` so the central
bootstrap module — which lives at the repo root — is importable.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from _akana_src_bootstrap import ensure_src_on_path  # noqa: E402 - after repo-root bootstrap

ensure_src_on_path()

from akana.memory.mcp import main  # noqa: E402 - must follow the src bootstrap

if __name__ == "__main__":
    raise SystemExit(main())
