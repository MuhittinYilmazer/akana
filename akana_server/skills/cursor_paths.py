"""Cursor Agent skill search paths (SKILLS_AND_TOOLS.md §2.5)."""

from __future__ import annotations

import os
from pathlib import Path


def default_cursor_skill_roots() -> list[Path]:
    """Built-in Cursor skill directories in priority order."""
    home = Path.home()
    roots: list[Path] = []
    for name in ("skills", "skills-cursor"):
        p = (home / ".cursor" / name).resolve()
        if p.is_dir():
            roots.append(p)
    return roots


def extra_skill_paths_from_env() -> list[Path]:
    """Parse ``AKANA_SKILL_PATHS`` (comma-separated directories)."""
    raw = os.environ.get("AKANA_SKILL_PATHS", "").strip()
    if not raw:
        return []
    out: list[Path] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        p = Path(os.path.expanduser(part)).resolve()
        if p.is_dir():
            out.append(p)
    return out


def cursor_skill_roots() -> list[Path]:
    """All Cursor skill roots: default paths then env extras (deduped, order preserved)."""
    seen: set[Path] = set()
    ordered: list[Path] = []
    for p in [*default_cursor_skill_roots(), *extra_skill_paths_from_env()]:
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    return ordered
