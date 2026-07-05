"""Akana Cursor — FastAPI server (Cursor API, no OpenClaw).

Importing this package is one of the sanctioned entry points for the src-layout
bootstrap: it routes through the SINGLE mechanism (``_akana_src_bootstrap``) so
that ``import akana`` resolves to ``src/akana`` and not the root ``akana.py``
launcher shadow. This runs before ANY ``akana_server.*`` submodule (including
``api/routes/memory.py`` and ``memory_core.py``, which used to carry their own
hand-rolled "PERMANENT" sys.path preambles). See ``_akana_src_bootstrap`` for
the full rationale.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# Repo root = this file's parent's parent (``<repo>/akana_server/__init__.py``).
# Ensure it is importable so the central bootstrap module (which lives at the
# repo root) can be imported no matter the process cwd or launcher.
_repo_root = str(_Path(__file__).resolve().parents[1])
if _repo_root not in _sys.path:
    _sys.path.insert(0, _repo_root)

from _akana_src_bootstrap import ensure_src_on_path as _ensure_src_on_path  # noqa: E402

_ensure_src_on_path()

del _sys, _Path, _repo_root, _ensure_src_on_path
