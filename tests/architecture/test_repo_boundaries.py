"""REPO-WIDE architecture guards covering the entire ``akana_server/`` package.

WHY: ``test_chat_boundaries.py`` only locks the ``api/routes/chat/`` package.
This file extends the same static-analysis discipline to the WHOLE tree: it
sets up regression gates for god-file bloat, MODULE-LEVEL import cycles, and
layer leakage (service → ``api``).

DESIGN PRINCIPLE — "catch TOMORROW's regression, not today's debt":
The repo already carries technical debt (large files, a few module-level
cycles). These tests don't break the debt *today*; they pin existing
violations as a baseline in explicit, named, FROZEN allowlists. Allowlists may
only SHRINK — a line is removed as debt is paid. A NEW violation not in the
list breaks the test. So the gates stop forward motion (new god-file, new
cycle, new layer leak) without punishing the past.

METHOD: pure ``ast`` + stdlib (NO new pip dependency). Each ``.py`` module's
AST is walked; ONLY module-scope ``import``/``from`` statements (NOT INSIDE a
``def``/``async def``) are collected. In-function imports are a deliberate
escape hatch (they break cycles) → ALLOWED; module-scope
``if``/``try``/``with`` blocks (e.g. ``if TYPE_CHECKING:``) are still module
level, so we descend into them so a hidden back-import can't be tucked away.

MEASURED BASELINE (when this file was written, ``wc -l`` + AST graph):
  * Top 8 modules (the god-file ceiling was chosen from this, MAX_LINES=1600):
      1347  akana_server/api/routes/chat/streaming.py
      1142  akana_server/api/routes/chat/__init__.py
      1005  akana_server/api/routes/chat/gates.py
       943  akana_server/runtime_settings.py
       860  akana_server/replay/adapters.py
       799  akana_server/memory_engine/engine.py
       772  akana_server/orchestrator/claude_provider.py
       761  akana_server/api/routes/chat/commands.py
  * Module-level import cycles: NONE (KNOWN_CYCLES empty). When the file was
    written there were 2 groups (memory.recall ↔ recall_format;
    voice ↔ streaming_tts); both were broken — the back-edges were moved to a
    leaf-submodule alias (``import pkg.leaf as x``) or an in-function type import.
  * Service → api module-level leak: NONE (allowlist empty). The only api
    import is ``main.py`` (uvicorn entrypoint) — not a service, out of scope.
"""

from __future__ import annotations

import ast
from pathlib import Path

# tests/architecture/<this file> → repo root is two levels up.
REPO = Path(__file__).resolve().parents[2]
ROOT_PKG = "akana_server"
PKG_DIR = REPO / ROOT_PKG

# --------------------------------------------------------------------------- #
# CONFIG / BASELINE — SHRINK as debt is paid, never widen.
# --------------------------------------------------------------------------- #

#: God-file guard: no ``.py`` under ``akana_server/`` may exceed this.
#: After streaming.py (~1619 lines) was split by responsibility into chat
#: submodules, the largest module repo-wide is __init__.py (~837 lines). The
#: ceiling was 1500; raised to 1600 to accommodate ``claude_provider.py``'s
#: legitimate growth (subagent/todo + tool-input streaming + plan/ask handling,
#: ~1564 lines). TODO: split ``claude_provider.py`` by responsibility (like the
#: chat package) and lower this back toward 1500.
MAX_LINES = 1600

#: FROZEN module-level cycle allowlist. Each item is a ``frozenset`` of the
#: modules in an SCC (strongly connected component). When a cycle is broken
#: (the module-level edge is moved in-function or to an absolute leaf-submodule
#: import) DELETE THE RELEVANT ITEM HERE. A new cycle NOT in the list breaks
#: the test.
#:
#: When this file was written there were 2 cycles (memory.recall, voice); both
#: were broken (leaf-submodule alias + in-function type import) → list empty.
KNOWN_CYCLES: frozenset[frozenset[str]] = frozenset()

#: LAYERING (soft): the service layer (memory*/orchestrator/
#: skills/...) must NOT import the ``akana_server.api`` package AT MODULE
#: LEVEL — the dependency arrow should flow down (api → service), not up
#: (service → api). The top-packages below count as "service"; ``api`` and the
#: root entrypoint (``main``/``__init__`` etc.) are out of scope.
SERVICE_TOP_PACKAGES: frozenset[str] = frozenset(
    {
        "cache",
        "connectors",
        "context",
        "files",
        "memory_core",
        "multimodal",
        "network",
        "orchestrator",
        "packs",
        "persona",
        "skills",
        "tools",
        "voice",
    }
)

#: FROZEN layer-leak allowlist: ``"service_module -> import_target"``.
#: When this file was written there was NO service→api module-level leak → list
#: empty. Unavoidable exceptions are added here by name and removed over time.
KNOWN_LAYER_VIOLATIONS: frozenset[str] = frozenset()

#: Full prefix path of the ``api`` subtree (the "upper" side of the layer rule).
API_PREFIX = f"{ROOT_PKG}.api"


# --------------------------------------------------------------------------- #
# AST helpers — the repo-wide counterpart of the logic in the chat boundary test.
# --------------------------------------------------------------------------- #
def _all_py_files() -> list[Path]:
    """All ``.py`` files under ``akana_server/`` (deterministic)."""
    return sorted(PKG_DIR.rglob("*.py"))


def _module_name(path: Path) -> str:
    """Convert a file path to a dotted module name (``__init__`` drops to the package name)."""
    rel = path.relative_to(REPO).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _module_level_imports(tree: ast.Module) -> list[ast.stmt]:
    """Module-SCOPE ``import``/``ImportFrom`` statements.

    Does NOT descend into function/method bodies — imports there are a
    deliberate (cycle-breaking) escape hatch. Module-scope ``if``/``try``/``with``
    (e.g. ``if TYPE_CHECKING:``) is still module level; we descend into it so a
    hidden back-import can't be tucked away.
    """
    out: list[ast.stmt] = []

    def walk(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                out.append(node)
            elif isinstance(node, ast.If):
                walk(node.body)
                walk(node.orelse)
            elif isinstance(node, ast.Try):
                walk(node.body)
                for handler in node.handlers:
                    walk(handler.body)
                walk(node.orelse)
                walk(node.finalbody)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                walk(node.body)
            # FunctionDef / AsyncFunctionDef / ClassDef → DON'T DESCEND (deliberate escape hatch).

    walk(tree.body)
    return out


def _resolve_relative(node: ast.ImportFrom, current_mod: str, is_package: bool) -> str:
    """Resolve a ``from . / .. import`` relative target to an absolute module path.

    Python rule: in a package's ``__init__``, ``__package__`` is the package
    ITSELF (``from .`` is the same package); in a normal module it's the
    containing package (all parts except the last). ``is_package`` carries this
    distinction. Note: this repo currently uses ABSOLUTE imports everywhere;
    this branch is defensive for forward correctness.
    """
    if node.level == 0:
        return node.module or ""
    # If it's the package's __init__, base = package name; otherwise = containing package.
    pkg_parts = current_mod.split(".") if is_package else current_mod.split(".")[:-1]
    climb = node.level - 1
    if climb > 0:
        pkg_parts = pkg_parts[: len(pkg_parts) - climb] if climb <= len(pkg_parts) else []
    base = ".".join(pkg_parts)
    if node.module:
        return f"{base}.{node.module}" if base else node.module
    return base


def _intra_import_targets(
    stmts: list[ast.stmt], current_mod: str, is_package: bool
) -> list[tuple[int, str]]:
    """Extract ``ROOT_PKG``-rooted import targets as ``(line, full_path)``.

    Covers both absolute (``from akana_server.x import y`` / ``import
    akana_server.x``) and relative (``from . import y``) forms.
    """
    targets: list[tuple[int, str]] = []
    for node in stmts:
        if isinstance(node, ast.ImportFrom):
            mod = _resolve_relative(node, current_mod, is_package)
            if mod == ROOT_PKG or mod.startswith(ROOT_PKG + "."):
                targets.append((node.lineno, mod))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == ROOT_PKG or alias.name.startswith(ROOT_PKG + "."):
                    targets.append((node.lineno, alias.name))
    return targets


def _normalize_to_module(target: str, known: set[str]) -> str | None:
    """Map an import target to a KNOWN module node.

    ``a.b.c`` is either the ``a.b.c`` module or the ``c`` symbol from the
    ``a.b`` module. Find the real module by narrowing back from the longest
    known prefix.
    """
    if target in known:
        return target
    parts = target.split(".")
    for i in range(len(parts) - 1, 0, -1):
        cand = ".".join(parts[:i])
        if cand in known:
            return cand
    return None


def _build_import_graph() -> tuple[dict[str, set[str]], dict[tuple[str, str], tuple[str, int]]]:
    """Build the module-level, intra-package import graph.

    Returns: ``(graph, edge_loc)`` — ``graph[src] = {target_modules}`` and
    ``edge_loc[(src, dst)] = (file_path_str, line)`` (for the error message).
    """
    files = _all_py_files()
    known: set[str] = {_module_name(p) for p in files}

    graph: dict[str, set[str]] = {m: set() for m in known}
    edge_loc: dict[tuple[str, str], tuple[str, int]] = {}

    for path in files:
        src = _module_name(path)
        is_package = path.name == "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, raw_target in _intra_import_targets(
            _module_level_imports(tree), src, is_package
        ):
            dst = _normalize_to_module(raw_target, known)
            if dst is None or dst == src:
                continue  # self-edge/unresolvable target → don't add to the graph
            graph[src].add(dst)
            edge_loc.setdefault((src, dst), (str(path.relative_to(REPO)), lineno))

    return graph, edge_loc


def _find_cycles(graph: dict[str, set[str]]) -> list[frozenset[str]]:
    """Find cycles via Tarjan SCC.

    Every strongly connected component (SCC) with more than one node is a
    cycle. A single-node component counts as a cycle only if it has a self-edge
    (in practice this doesn't happen since self-edges aren't added to the graph).
    """
    index_counter = [0]
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    sccs: list[frozenset[str]] = []

    # Explicit stack instead of recursion (safe for 281 modules + chains).
    def strongconnect(start: str) -> None:
        work: list[tuple[str, int]] = [(start, 0)]
        while work:
            node, pi = work[-1]
            if pi == 0:
                index[node] = index_counter[0]
                lowlink[node] = index_counter[0]
                index_counter[0] += 1
                stack.append(node)
                on_stack[node] = True
            recursed = False
            succ = sorted(graph[node])
            for j in range(pi, len(succ)):
                w = succ[j]
                if w not in index:
                    work[-1] = (node, j + 1)
                    work.append((w, 0))
                    recursed = True
                    break
                if on_stack.get(w):
                    lowlink[node] = min(lowlink[node], index[w])
            if recursed:
                continue
            if lowlink[node] == index[node]:
                comp: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack[w] = False
                    comp.append(w)
                    if w == node:
                        break
                if len(comp) > 1:
                    sccs.append(frozenset(comp))
            work.pop()
            if work:
                parent, _ = work[-1]
                lowlink[parent] = min(lowlink[parent], lowlink[node])

    for v in graph:
        if v not in index:
            strongconnect(v)
    return sccs


def _top_subpackage(module: str) -> str | None:
    """``akana_server.X. ...`` → ``X`` (top subpackage name)."""
    parts = module.split(".")
    if len(parts) >= 2 and parts[0] == ROOT_PKG:
        return parts[1]
    return None


# --------------------------------------------------------------------------- #
# 0) Package EXISTS and contains a reasonable number of modules (so the test
#    doesn't degrade into a dead no-op).
# --------------------------------------------------------------------------- #
def test_paket_mevcut_ve_modul_iceriyor():
    assert PKG_DIR.is_dir(), f"root package not found: {PKG_DIR}"
    files = _all_py_files()
    assert len(files) >= 50, (
        f"fewer modules than expected ({len(files)}) found under {ROOT_PKG} — "
        f"the path may be wrong or the package may have moved; review this architecture test."
    )


# --------------------------------------------------------------------------- #
# 1) GOD-FILE GUARD: no ``.py`` exceeds MAX_LINES.
# --------------------------------------------------------------------------- #
def test_hicbir_modul_god_file_olmaz():
    asanlar: list[str] = []
    for path in _all_py_files():
        n = path.read_text(encoding="utf-8").count("\n") + 1
        if n > MAX_LINES:
            asanlar.append(f"{path.relative_to(REPO)}: {n} lines (> {MAX_LINES})")
    asanlar.sort(key=lambda s: int(s.split(": ")[1].split(" ")[0]), reverse=True)
    assert not asanlar, (
        f"god-file regression — the following module(s) exceeded the {MAX_LINES}-line ceiling. "
        f"Split by responsibility into a PACKAGE (see the api/routes/chat/ example): "
        f"separate out leak modules, move shared symbols to leaf modules, "
        f"break cycle risk with in-function imports.\n" + "\n".join(asanlar)
    )


# --------------------------------------------------------------------------- #
# 2) MODULE-LEVEL CYCLE BAN: no new cycle beyond known (frozen) cycles.
# --------------------------------------------------------------------------- #
def test_modul_seviyesi_import_dongusu_yok():
    graph, edge_loc = _build_import_graph()
    cycles = _find_cycles(graph)

    yeni: list[frozenset[str]] = [c for c in cycles if c not in KNOWN_CYCLES]
    # Resolved cycles: in the allowlist but no longer in the graph → SHRINK the list.
    cozulen: list[frozenset[str]] = [c for c in KNOWN_CYCLES if c not in cycles]

    mesajlar: list[str] = []
    for comp in sorted(yeni, key=lambda c: sorted(c)):
        kenarlar: list[str] = []
        for a in sorted(comp):
            for b in sorted(graph[a]):
                if b in comp:
                    f, ln = edge_loc[(a, b)]
                    kenarlar.append(f"      {a} → {b}  @ {f}:{ln}")
        mesajlar.append(
            "  NEW CYCLE (" + str(len(comp)) + " module(s)):\n" + "\n".join(kenarlar)
        )

    assert not yeni, (
        "a new MODULE-LEVEL import cycle was introduced. Move the edge that closes "
        "the cycle (usually a package ``__init__`` importing a submodule or sibling "
        "module at module scope) INTO a ``def``/``async def`` to turn it into a "
        "deferred import; or, if you accept it deliberately, add it to KNOWN_CYCLES "
        "(but the goal is to SHRINK the list).\n" + "\n".join(mesajlar)
    )
    # Second safeguard to keep the allowlist current as debt is paid:
    assert not cozulen, (
        "congratulations — the following cycle(s) no longer exist; DELETE them from "
        "KNOWN_CYCLES so the gate alarms again if they ever reappear:\n"
        + "\n".join("  " + " ↔ ".join(sorted(c)) for c in cozulen)
    )


# --------------------------------------------------------------------------- #
# 3) LAYERING (soft): the service layer does NOT import ``api`` at module level.
#    No new leak beyond known (frozen) exceptions.
# --------------------------------------------------------------------------- #
def test_servis_katmani_api_yi_modul_seviyesinde_import_etmez():
    _, edge_loc = _build_import_graph()

    bulunan: set[str] = set()
    detay: list[str] = []
    for (src, dst), (f, lineno) in sorted(edge_loc.items()):
        if not (dst == API_PREFIX or dst.startswith(API_PREFIX + ".")):
            continue
        if _top_subpackage(src) not in SERVICE_TOP_PACKAGES:
            continue  # entrypoint/root modules (main etc.) are not services
        anahtar = f"{src} -> {dst}"
        bulunan.add(anahtar)
        if anahtar not in KNOWN_LAYER_VIOLATIONS:
            detay.append(f"    {anahtar}   @ {f}:{lineno}")

    assert not detay, (
        "the service layer imports ``akana_server.api`` at MODULE LEVEL → layer "
        "leak (the dependency arrow flows upward). api is the upper layer; a "
        "service must not depend on it at module scope. Import the needed symbol "
        "IN-function, or move the shared piece to a non-api module. If it's a "
        "deliberate exception, add it to KNOWN_LAYER_VIOLATIONS.\n"
        + "\n".join(detay)
    )

    # Allowlist hygiene: delete an exception that no longer applies.
    bayat = sorted(KNOWN_LAYER_VIOLATIONS - bulunan)
    assert not bayat, (
        "the following layer exceptions no longer apply; DELETE them from KNOWN_LAYER_VIOLATIONS:\n"
        + "\n".join("    " + b for b in bayat)
    )
