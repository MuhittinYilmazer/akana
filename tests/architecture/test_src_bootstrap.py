"""Architecture guard for the src-layout bootstrap (the akana.py shadow fix).

WHY: the repo-root launcher ``akana.py`` shadows the ``akana`` package that lives
in ``src/akana``. The project's rule is that there is exactly ONE mechanism that
puts ``src/`` on ``sys.path`` — ``_akana_src_bootstrap.ensure_src_on_path()`` —
and every entry point routes through it. Historically THREE modules each carried
their own hand-rolled "PERMANENT" ``sys.path.insert(0, .../src)`` preamble, and
``akana_cli/reset_memory_cmd.py`` had none at all (so it died with a swallowed
ModuleNotFoundError). These tests lock the fix in:

  1. ``import akana`` resolves to the ``src/akana`` PACKAGE, never the launcher.
  2. NO module except the central bootstrap does its own ``src``-directed
     ``sys.path`` surgery (a new scattered bridge fails the build).
  3. Every sanctioned entry point actually wires the central bootstrap.
  4. The central function is idempotent and lands ``src`` first.

METHOD: pure ``ast`` + stdlib, no new pip dependency (same discipline as
``test_repo_boundaries.py``).
"""

from __future__ import annotations

import ast
from pathlib import Path

import akana

# tests/architecture/<this file> → repo root is two levels up.
REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"

#: The ONE module allowed to perform ``src``-directed ``sys.path`` surgery.
CENTRAL_BOOTSTRAP = REPO / "_akana_src_bootstrap.py"

#: Directories never scanned (third-party / generated code / nested checkouts).
#: ``.claude`` holds sibling-agent git worktrees (nested copies of this repo);
#: scanning them would report the OTHER checkout's files, not this tree's.
_SKIP_DIRS = {
    "venv",
    ".venv",
    "node_modules",
    "__pycache__",
    ".git",
    ".claude",
    "cursor_bridge",
}

#: Entry points that MUST route through the central bootstrap. Each is checked
#: for a call to ``ensure_src_on_path`` (directly or via an alias import).
_ENTRY_POINTS = (
    REPO / "akana.py",
    REPO / "akana_server" / "__init__.py",
    REPO / "scripts" / "mcp_memory.py",
    REPO / "tests" / "conftest.py",
)


def _iter_repo_py_files() -> list[Path]:
    """Every ``.py`` under the repo, skipping vendored/generated dirs (deterministic)."""
    out: list[Path] = []
    for path in sorted(REPO.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.relative_to(REPO).parts):
            continue
        out.append(path)
    return out


def _mentions_src(node: ast.AST) -> bool:
    """True if an AST subtree references the string 'src' or a ``.../src`` path.

    Catches ``sys.path.insert(0, str(... / "src"))`` and ``_SRC = ".../src"``
    regardless of how the src path is spelled (literal, ``Path(...) / "src"``,
    an ``_SRC`` name, etc.).
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            tail = sub.value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            if tail == "src":
                return True
        if isinstance(sub, ast.Name) and sub.id in {"_SRC", "SRC_DIR", "SRC"}:
            return True
    return False


def _is_sys_path_mutation(node: ast.AST) -> bool:
    """True for ``sys.path.insert(...)`` / ``sys.path.append(...)`` calls."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in {"insert", "append", "extend"}:
        return False
    # func.value must be the ``sys.path`` attribute.
    inner = func.value
    return (
        isinstance(inner, ast.Attribute)
        and inner.attr == "path"
        and isinstance(inner.value, ast.Name)
        and inner.value.id == "sys"
    )


def test_import_akana_is_the_src_package_not_the_launcher() -> None:
    """The whole point: ``import akana`` must be the package, not the root script."""
    resolved = Path(akana.__file__).resolve()
    expected = (SRC / "akana" / "__init__.py").resolve()
    assert resolved == expected, (
        f"`import akana` resolved to {resolved}, expected the src package at "
        f"{expected}. The root akana.py launcher is shadowing the package — the "
        f"central bootstrap did not run for this entry point."
    )
    # ``akana`` must be a real package (has a path), not a top-level module.
    assert hasattr(akana, "__path__"), "`akana` is not a package (launcher shadow won)."


def test_only_the_central_bootstrap_does_src_sys_path_surgery() -> None:
    """No scattered ``src``-bridge preambles may reappear anywhere in the repo.

    A ``sys.path.insert/append`` whose argument references a ``.../src`` path is
    exactly the old "PERMANENT" bridge. Only ``_akana_src_bootstrap.py`` is
    permitted to do it; any other module that reintroduces one fails here.
    """
    offenders: list[str] = []
    for path in _iter_repo_py_files():
        if path.resolve() == CENTRAL_BOOTSTRAP.resolve():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if _is_sys_path_mutation(node) and _mentions_src(node):
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert not offenders, (
        "src-directed sys.path surgery outside the central bootstrap "
        "(_akana_src_bootstrap.py). Route these through ensure_src_on_path() "
        "instead:\n  " + "\n  ".join(offenders)
    )


def test_every_entry_point_wires_the_central_bootstrap() -> None:
    """Each sanctioned entry point must call ``ensure_src_on_path`` (or an alias)."""
    missing: list[str] = []
    for entry in _ENTRY_POINTS:
        assert entry.exists(), f"entry point vanished: {entry.relative_to(REPO)}"
        tree = ast.parse(entry.read_text(encoding="utf-8"))
        # Accept the import under any alias (``ensure_src_on_path as _ensure...``).
        aliases = {"ensure_src_on_path"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "_akana_src_bootstrap":
                for alias in node.names:
                    if alias.name == "ensure_src_on_path":
                        aliases.add(alias.asname or alias.name)
        called = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in aliases
            for node in ast.walk(tree)
        )
        if not called:
            missing.append(str(entry.relative_to(REPO)))
    assert not missing, (
        "these entry points do not call ensure_src_on_path() — the central "
        f"src bootstrap is not wired there: {missing}"
    )


def test_ensure_src_on_path_is_idempotent_and_src_first() -> None:
    """The central function lands ``src`` at sys.path[0] and is safe to re-call."""
    import sys

    import _akana_src_bootstrap

    src = str(_akana_src_bootstrap.SRC_DIR)
    _akana_src_bootstrap.ensure_src_on_path()
    assert sys.path[0] == src, "ensure_src_on_path did not put src first."
    before = list(sys.path)
    _akana_src_bootstrap.ensure_src_on_path()  # second call
    assert sys.path == before, "ensure_src_on_path is not idempotent (path changed)."
    # Exactly one occurrence — no duplicate accumulation across calls.
    assert sys.path.count(src) == 1, "src appears more than once on sys.path."
