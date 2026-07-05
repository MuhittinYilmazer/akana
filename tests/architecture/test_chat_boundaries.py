"""Static tests that lock the architectural boundaries of the ``api/routes/chat/`` package.

WHY: the 4073-line ``api/routes/chat.py`` god-file was deliberately split into
a PACKAGE — ``__init__.py`` (endpoints + thin wiring), ``gates.py``,
``streaming.py``, ``models.py``, ``_base.py``. The split
relies on **function-level (deferred)** imports to break potential CIRCULAR
imports: submodules import the symbols they read from the package
(``__init__``) at call time, INSIDE a ``def``/``async def``.

This test prevents two regressions:
  1. MODULE-LEVEL cycle — if a submodule imports the package (``__init__``) or
     a sibling in a higher layer at module scope, the import-time cycle
     reappears and the god-file coupling re-forms.
  2. The god-file re-bloating — if a single ``.py`` grows too large.

METHOD: pure ``ast`` + stdlib (NO new pip dependency). Each module's AST is
walked; ONLY module-scope ``import``/``from`` statements (NOT INSIDE a
``def``/``async def``) are collected. In-function imports are a deliberate
escape hatch → ALLOWED.

LAYERING (intended DAG — edges flow only downward; see the LAYER map for the full
order after the OSS chat-core seam split):
    _base, models   (L0 — leaf, NO intra-package back-dependency)
        ↑
    gates                                       (L2 — reads only leaves)
        ↑
    chat_state                                  (L3 — registry/buffer/predicates)
        ↑
    chat_bridge, chat_commands_sse, turn_gate   (L4)
        ↑
    persist, tts_pipeline, turn_core            (L5 — persist now BELOW the producer)
        ↑
    chat_producer                               (L6 — live SSE producer)
        ↑
    chat_detached                               (L7 — turn machine)
        ↑
    streaming                                   (L8 — thin re-export facade)
        ↑
    routes                                      (L9 — the 8 HTTP route handlers)
        ↑
    __init__                                    (L10 — top; wires everything together)

A module-level import TARGET must always be in a LOWER layer than itself. The
most critical rule: no submodule may import the package itself
(``akana_server.api.routes.chat`` or ``from ...chat import X``) at module
scope — that is the reverse edge that brings the cycle back. The genuine test/voice
patch surface (``stream_user_chat`` / ``complete_chat_with_usage`` / persist writers /
``plan_skill_turn`` / ``_run_turn_gates`` / …) is still read via an IN-FUNCTION
``_chatpkg`` import — the allowed escape hatch.
"""

from __future__ import annotations

import ast
from pathlib import Path

# tests/architecture/<this file> → repo root is two levels up.
REPO = Path(__file__).resolve().parents[2]
PKG_DIR = REPO / "akana_server" / "api" / "routes" / "chat"
PKG = "akana_server.api.routes.chat"

#: Submodules within the package (excluding ``__init__``) and the intended layer
#: order. Lower number = deeper leaf. A module-level import may only target a
#: LOWER layer (DAG; reverse/equal edge = cycle risk).
LAYER = {
    "_base": 0,
    "models": 0,
    # L1 (``commands``) was removed with the pre-LLM command short-circuit feature.
    "gates": 2,
    # streaming.py was split BY RESPONSIBILITY into cohesion modules (formerly a single
    # ~1619-line god-file). The chat-core seam split (OSS cleanup) then INVERTED the
    # persist↔producer edge: the SSE/context/persist helpers are now MODULE-LEVEL imports
    # (the old call-time reach-up into the package is gone), so ``persist`` sits BELOW
    # ``chat_producer`` (chat_producer down-imports persist, not the reverse). Downward
    # DAG (each import only to a LOWER layer):
    #   chat_state (registry/buffer/predicate + _turn_wrote_memory)
    #     → chat_bridge + chat_commands_sse + turn_gate
    #     → persist + tts_pipeline + turn_core
    #     → chat_producer (live producer)
    #     → chat_detached (turn machine)
    #     → streaming (thin facade)
    #     → routes (the 8 HTTP route handlers, split out of __init__)
    #     → __init__
    "chat_state": 3,
    "chat_bridge": 4,
    "chat_commands_sse": 4,
    "turn_gate": 4,  # public register/release/busy seam over chat_state (for connectors)
    "persist": 5,  # persist/capture: reads chat_state leaf; BELOW chat_producer now
    "tts_pipeline": 5,  # streaming-TTS side-pipeline (delta→WAV→SSE); reads chat_state
    "turn_core": 5,  # shared non-streaming turn core (blocking /chat; voice rebases later)
    "chat_producer": 6,
    "chat_detached": 7,
    "streaming": 8,  # thin facade: down-imports submodules, preserves the old surface
    "routes": 9,  # the 8 HTTP route handlers (ARCH-03 split out of __init__)
    "__init__": 10,  # top layer: package interface, wires everything
}

#: Tested leaf/middle submodules (``__init__`` is the top, handled separately).
SUBMODULES = [
    "_base",
    "models",
    "gates",
    "chat_state",
    "chat_bridge",
    "chat_commands_sse",
    "turn_gate",
    "persist",
    "tts_pipeline",
    "turn_core",
    "chat_producer",
    "chat_detached",
    "streaming",
    "routes",
]

#: God-file guard: no ``.py`` under ``chat/`` should exceed this.
#: After streaming.py (~1619 lines) was split by responsibility, the largest
#: module in the package is chat_producer (~1200 lines; the ARCH-03 split then moved
#: the 8 route handlers into routes.py, thinning __init__ to ~165 lines). The ceiling
#: is 1500 so normal growth doesn't alarm but a real god-file return is caught
#: (aligned with the repo-wide test_repo_boundaries; the old 1650 was a stopgap
#: for the previous god-file).
MAX_LINES = 1500


def _module_level_imports(tree: ast.Module) -> list[ast.stmt]:
    """Return module-SCOPE ``import``/``ImportFrom`` statements.

    Does NOT descend into function/lambda bodies — imports there are a
    deliberate (cycle-breaking) escape hatch and outside this check. By
    contrast, module-scope ``if``/``try``/``with`` blocks (e.g.
    ``if TYPE_CHECKING:``) are still module level, so we descend into them so a
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
            # FunctionDef / AsyncFunctionDef / ClassDef → DON'T DESCEND:
            # in-function imports are an allowed escape hatch; no import is
            # expected in a class body.

    walk(tree.body)
    return out


def _intra_pkg_targets(stmts: list[ast.stmt]) -> list[tuple[int, str]]:
    """Extract import targets starting with ``PKG`` from the given statements.

    Each returned item is ``(line, full_module_path)``. Both ``from PKG.x
    import y`` and ``import PKG.x`` forms are covered.
    """
    targets: list[tuple[int, str]] = []
    for node in stmts:
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == PKG or mod.startswith(PKG + "."):
                targets.append((node.lineno, mod))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == PKG or alias.name.startswith(PKG + "."):
                    targets.append((node.lineno, alias.name))
    return targets


def _leaf_name(full_module: str) -> str:
    """Reduce a ``PKG``-rooted full path to its intra-package leaf name.

    ``akana_server.api.routes.chat.gates`` → ``gates``.
    The package ITSELF (``...routes.chat``) → ``__init__`` (top-layer representative).
    Multi-part (``...chat.foo.bar``) → first part (``foo``).
    """
    if full_module == PKG:
        return "__init__"
    rest = full_module[len(PKG) + 1 :]  # drop the leading '.'
    return rest.split(".", 1)[0] if rest else "__init__"


def _parse(name: str) -> ast.Module:
    path = PKG_DIR / f"{name}.py"
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# --------------------------------------------------------------------------- #
# 1) Package EXISTS and the expected modules are in place (so the test doesn't
#    degrade into a dead no-op).
# --------------------------------------------------------------------------- #
def test_chat_paketi_ve_modulleri_mevcut():
    assert PKG_DIR.is_dir(), f"chat package not found: {PKG_DIR}"
    for name in [*SUBMODULES, "__init__"]:
        assert (PKG_DIR / f"{name}.py").is_file(), (
            f"expected chat module missing: {name}.py — the refactor shape may "
            f"have changed, update this architecture test."
        )


# --------------------------------------------------------------------------- #
# 2) CORE ANTI-CYCLE: no submodule imports the package (__init__) at module
#    scope. That is the reverse edge that brings back the god-file coupling.
# --------------------------------------------------------------------------- #
def test_alt_moduller_init_i_modul_seviyesinde_import_etmez():
    ihlaller: list[str] = []
    for name in SUBMODULES:
        mod_imports = _module_level_imports(_parse(name))
        for lineno, target in _intra_pkg_targets(mod_imports):
            if _leaf_name(target) == "__init__":
                ihlaller.append(
                    f"{name}.py:{lineno} imports the package/__init__ at module "
                    f"level ({target!r}) → import-time cycle. "
                    f"Use an IN-FUNCTION (inside def/async def) "
                    f"`from {PKG} import ...` instead; that breaks the cycle."
                )
    assert not ihlaller, "module-level back-import in the chat package (cycle):\n" + "\n".join(
        ihlaller
    )


# --------------------------------------------------------------------------- #
# 3) LAYERING (DAG): every module-level import target must be in a LOWER layer
#    than itself. Equal/reverse edge = cycle risk.
# --------------------------------------------------------------------------- #
def test_modul_seviyesi_importlar_katmanlamayi_korur():
    ihlaller: list[str] = []
    for name in SUBMODULES:
        kaynak_kat = LAYER[name]
        mod_imports = _module_level_imports(_parse(name))
        for lineno, target in _intra_pkg_targets(mod_imports):
            hedef = _leaf_name(target)
            if hedef not in LAYER:
                # An unrecognized intra-package module is imported at module
                # level — the layering map doesn't know it; a deliberate
                # decision is needed.
                ihlaller.append(
                    f"{name}.py:{lineno} imports an unknown intra-package module "
                    f"at module level ({target!r}). Add it to the LAYER map "
                    f"and set its layer deliberately."
                )
                continue
            hedef_kat = LAYER[hedef]
            if hedef_kat >= kaynak_kat:
                ihlaller.append(
                    f"{name}.py:{lineno}  {name}(L{kaynak_kat}) → "
                    f"{hedef}(L{hedef_kat}) binds to a higher/equal layer at "
                    f"module level → DAG violation (cycle risk). Only a LOWER "
                    f"layer may be imported at module level; higher-layer "
                    f"symbols are taken via an IN-FUNCTION import."
                )
    assert not ihlaller, "chat package layering (DAG) violation:\n" + "\n".join(ihlaller)


# --------------------------------------------------------------------------- #
# 4) Leaf modules (``_base``/``models``) carry NO intra-package module-level
#    import (so they stay true leaves; no back-dependency is born).
# --------------------------------------------------------------------------- #
def test_yaprak_moduller_paket_ici_modul_seviyesi_import_tasimaz():
    ihlaller: list[str] = []
    for name in ("_base", "models"):
        mod_imports = _module_level_imports(_parse(name))
        for lineno, target in _intra_pkg_targets(mod_imports):
            ihlaller.append(
                f"{name}.py:{lineno} leaf module carries an intra-package import "
                f"({target!r}). {name} is an L0 leaf; it must not depend on "
                f"chat internals (this is the cornerstone of the split)."
            )
    assert not ihlaller, "leaf module acquired an intra-package dependency:\n" + "\n".join(ihlaller)


# --------------------------------------------------------------------------- #
# 5) GOD-FILE GUARD: no ``.py`` under ``chat/`` exceeds MAX_LINES.
# --------------------------------------------------------------------------- #
def test_hicbir_chat_modulu_god_file_olmaz():
    asanlar: list[str] = []
    for path in sorted(PKG_DIR.glob("*.py")):
        n = path.read_text(encoding="utf-8").count("\n") + 1
        if n > MAX_LINES:
            asanlar.append(f"{path.name}: {n} lines (> {MAX_LINES})")
    assert not asanlar, (
        "god-file return in the chat package — the following module(s) exceeded "
        "the ceiling, split by responsibility:\n" + "\n".join(asanlar)
    )
