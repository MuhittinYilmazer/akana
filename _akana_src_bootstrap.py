"""THE single mechanism that makes ``import akana`` resolve to ``src/akana``.

Why this file exists
--------------------
The repo-root launcher ``akana.py`` shares its name with the ``akana`` package
that lives in ``src/akana``. Under ``python akana.py …`` the repo root is
``sys.path[0]``, so a bare ``import akana`` would find the *launcher script*
instead of the package ("'akana' is not a package"). Historically three modules
(``akana_server/memory_core.py``, ``akana_server/api/routes/memory.py``,
``scripts/mcp_memory.py``) each carried their OWN hand-rolled, "PERMANENT"
``sys.path.insert(0, src)`` preamble, and every other consumer imported
``akana.*`` at module scope and only worked by import-order luck.

This module replaces all of that with ONE function. It is imported exactly once
at each *entry point* (see "Wired at" below), before any ``import akana`` runs,
so ``src/`` is on ``sys.path`` ahead of the launcher shadow from EVERY entry
point: the ``akana.py`` launcher (all CLI subcommands), the uvicorn server
(``akana_server`` package import), the standalone MCP script, and pytest.

Akana is clone-and-run: dependencies come from ``requirements*.txt`` and NO
editable install is required. An optional editable install (``pip install -e .``
via ``pyproject.toml``) is supported for IDE/mypy convenience and, when present,
makes this bootstrap a harmless no-op (``src/`` is already importable).

Wired at (the ONLY call sites — do not add scattered sys.path surgery elsewhere;
``tests/architecture/test_src_bootstrap.py`` enforces this):
  * ``akana.py``                      — launcher / all CLI subcommands
  * ``akana_server/__init__.py``      — uvicorn server + anything importing the app
  * ``scripts/mcp_memory.py``         — standalone MCP stdio server
  * ``tests/conftest.py``             — pytest (belt-and-suspenders with pytest.ini)
"""

from __future__ import annotations

import sys
from pathlib import Path

#: Absolute path to ``<repo>/src`` derived from THIS file's location, so it is
#: correct regardless of the process cwd or how the entry point was launched.
SRC_DIR = Path(__file__).resolve().parent / "src"


def ensure_src_on_path() -> None:
    """Put ``<repo>/src`` first on ``sys.path`` so ``import akana`` finds the package.

    Idempotent: safe to call from every entry point and multiple times. If ``src``
    is already at the front nothing changes; otherwise any stale later entry is
    removed and it is inserted at index 0 so the ``src/akana`` package wins over
    the root ``akana.py`` launcher shadow.
    """
    src = str(SRC_DIR)
    if sys.path and sys.path[0] == src:
        return
    # Drop any later duplicate so the freshly inserted entry is unambiguously first.
    try:
        sys.path.remove(src)
    except ValueError:
        pass
    sys.path.insert(0, src)
