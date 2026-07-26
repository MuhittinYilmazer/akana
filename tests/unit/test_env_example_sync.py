"""Drift guard: are the envs read from the code documented in .env.example?

How it works:

* The runtime code (``akana_server/`` + ``src/akana/``) is scanned via AST;
  full-literal ``AKANA_<NAME>`` string constants are collected. Env names always
  appear in code as a full string literal (``os.environ.get("AKANA_X")``,
  ``_secret("AKANA_X")``, ``HISTORY_BUDGET_ENV = "AKANA_X"`` etc.) — so the
  full-literal match catches both direct and indirect reads, while it does not
  catch mentions inside docstrings (no false positives).
* Akana also reads env names that do NOT carry the prefix, because they are the
  provider's own canonical spelling (CLAUDE_MODEL, CODEX_MODEL, WHISPER_PROMPT…).
  A full-literal scan cannot be used for those — plenty of unrelated constants are
  shaped like ``LLM_TIMEOUT`` — so they are collected from ENV ACCESS SITES instead
  (``collect_runtime_env_reads``). Without this leg the guard was structurally blind
  to half the user-facing settings, and .env.example silently drifted (a documented
  ``:``-separated AKANA_FILE_ROOTS that breaks on Windows, a Codex provider the CLI
  offers but the shipped template never mentions).
* The documented names in .env.example are extracted from ``[# ]NAME=`` lines
  (variables documented as a comment line count too).
* Any name in the code but NOT in .env.example fails the test — deliberate
  exclusions live in ``ALLOWLIST`` / ``FOREIGN_ENV_ALLOWLIST`` with a rationale.

When you add a new env variable: either add it to .env.example with a one-line
description, or (if it really won't be exposed to the user) add it to ALLOWLIST with a rationale.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env.example"

#: Runtime roots where env names are searched (tests are deliberately excluded:
#: made-up names in test fixtures must not enter the docs).
RUNTIME_DIRS = ("akana_server", "src/akana")

#: Full-literal env name pattern. Trailing underscore prefixes (e.g. dynamic
#: ``"AKANA_PERSONA_" + channel`` construction) deliberately do not match — the family
#: representative (AKANA_PERSONA_TELEGRAM) is documented in .env.example.
ENV_NAME_RE = re.compile(r"^AKANA_[A-Z0-9]+(?:_[A-Z0-9]+)*$")

#: Any UPPER_SNAKE env name (the non-prefixed leg matches on the ACCESS SITE, so the
#: pattern itself can stay permissive without dragging in unrelated constants).
ANY_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*$")

#: Non-AKANA_ names read by the runtime that are NOT Akana settings → rationale.
#: These belong to the OS or to a third-party CLI; documenting them in our template
#: would invite users to set something we do not own.
FOREIGN_ENV_ALLOWLIST: dict[str, str] = {
    "APPDATA": "Windows-owned: where the vault keyfile lives outside the data dir",
    "USERNAME": "Windows-owned: the account icacls grants the keyfile to",
    "XDG_CONFIG_HOME": "freedesktop-owned: keyfile location on Linux/macOS",
    "XDG_CACHE_HOME": "freedesktop-owned: fastembed model cache location",
    "CLAUDE_CONFIG_DIR": "owned by the claude CLI; we only read where it points",
    "COQUI_TOS_AGREED": "SET (not read) on the XTTS package to skip its interactive license prompt",
}

#: Names deliberately NOT placed in .env.example → rationale.
ALLOWLIST: dict[str, str] = {
    # NOT user-facing config: set PER TURN by the mcp_servers payload builder on
    # the akana_schedule child's env so a schedule_create made mid-chat can
    # default to same-chat delivery. A user setting this in .env would do
    # nothing (the payload builder overwrites it per spawn).
    "AKANA_CONVERSATION_ID": "internal per-turn MCP-child channel, not a setting",
}


def collect_runtime_env_names() -> dict[str, str]:
    """Full-literal ``AKANA_*`` names in the runtime code → the file first seen in."""
    found: dict[str, str] = {}
    for base in RUNTIME_DIRS:
        for path in sorted((REPO_ROOT / base).rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and ENV_NAME_RE.match(node.value)
                ):
                    found.setdefault(node.value, str(path.relative_to(REPO_ROOT)))
    return found


def _env_read_names_in(tree: ast.AST) -> set[str]:
    """Literal env names this module ACCESSES, by syntax rather than by spelling.

    Four shapes cover every read in the codebase:
      • ``os.environ.get("X")`` / ``os.getenv("X")`` / ``environ.pop("X")``
      • ``_int_env("X", …)``, ``_bool_env``, ``_env``, ``_secret("X")`` — config.py's
        own helpers, which is how several settings (WAKE_MIN_FRAMES) are read
      • ``os.environ["X"]``
      • ``_ENV_…_NAME = "X"`` — the module-constant indirection (OPENAI_REALTIME_URL)
    Matching the access site, not the name shape, is what keeps error codes and
    prompt constants (LLM_TIMEOUT, VOICE_DIRECTIVE_TR) out of the result.
    """
    names: set[str] = set()

    def add(value: object) -> None:
        if isinstance(value, str) and ANY_ENV_NAME_RE.match(value):
            names.add(value)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            func = node.func
            label = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            owner = ast.unparse(func.value) if isinstance(func, ast.Attribute) else ""
            if "env" in label.lower() or "environ" in owner or label == "_secret":
                add(node.args[0].value)
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            if "environ" in ast.unparse(node.value):
                add(node.slice.value)
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                tid = getattr(target, "id", "").upper()
                if "ENV" in tid and tid.endswith("NAME"):
                    add(node.value.value)
    return names


def collect_runtime_env_reads() -> dict[str, str]:
    """Non-``AKANA_`` env names read at a real access site → the file first seen in."""
    found: dict[str, str] = {}
    for base in RUNTIME_DIRS:
        for path in sorted((REPO_ROOT / base).rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
                continue
            for name in _env_read_names_in(tree):
                if not name.startswith("AKANA_"):
                    found.setdefault(name, str(path.relative_to(REPO_ROOT)))
    return found


def documented_env_names() -> set[str]:
    """Names documented in ``.env.example`` with ``NAME=`` or ``# NAME=``."""
    names: set[str] = set()
    line_re = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=")
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        m = line_re.match(line)
        if m:
            names.add(m.group(1))
    return names


def test_env_example_exists() -> None:
    assert ENV_EXAMPLE.is_file(), ".env.example must be in the repo root"


def test_all_runtime_akana_envs_are_documented() -> None:
    """Every AKANA_* env read in the code must be in .env.example (or the allowlist)."""
    runtime = collect_runtime_env_names()
    documented = documented_env_names()
    missing = {
        name: where
        for name, where in runtime.items()
        if name not in documented and name not in ALLOWLIST
    }
    assert not missing, (
        "There are env variables read in the code but not documented in .env.example "
        "(add them to .env.example with a one-line description, or add them to "
        f"ALLOWLIST with a rationale): {missing!r}"
    )


def test_allowlist_entries_still_exist_in_code() -> None:
    """Stale allowlist cleanup: if the code no longer reads it, delete the entry."""
    runtime = collect_runtime_env_names()
    stale = [name for name in ALLOWLIST if name not in runtime]
    assert not stale, f"ALLOWLIST has names no longer present in the code, delete them: {stale!r}"


def test_allowlist_entries_not_also_documented() -> None:
    """A name cannot be in both the allowlist and .env.example — single-source principle."""
    overlap = ALLOWLIST.keys() & documented_env_names()
    assert not overlap, (
        f"In both ALLOWLIST and .env.example: {sorted(overlap)!r} — "
        "if it is documented, delete the allowlist entry."
    )


def test_sanity_known_envs_detected() -> None:
    """Notice if the scanner itself breaks: known names must be found."""
    runtime = collect_runtime_env_names()
    for known in ("AKANA_PORT", "AKANA_FAST_PATH_MAX_CHARS", "AKANA_TELEGRAM_ENABLED"):
        assert known in runtime, f"AST scan did not find the name {known} — the scanner may be broken"


def test_all_runtime_non_akana_envs_are_documented() -> None:
    """The provider-canonical settings (no AKANA_ prefix) must be documented too.

    The AKANA_-only guard was cited as the safety net while it could not see
    CODEX_MODEL or WHISPER_PROMPT at all — a user browsing the shipped template
    concluded Codex was unsupported and had no way to pin either setting.
    """
    reads = collect_runtime_env_reads()
    documented = documented_env_names()
    missing = {
        name: where
        for name, where in reads.items()
        if name not in documented and name not in FOREIGN_ENV_ALLOWLIST
    }
    assert not missing, (
        "Env variables read in the code but not documented in .env.example (add them "
        "with a one-line description, or — if the name belongs to the OS or a "
        f"third-party CLI — to FOREIGN_ENV_ALLOWLIST with a rationale): {missing!r}"
    )


def test_foreign_allowlist_entries_still_exist_in_code() -> None:
    reads = collect_runtime_env_reads()
    stale = [name for name in FOREIGN_ENV_ALLOWLIST if name not in reads]
    assert not stale, f"FOREIGN_ENV_ALLOWLIST names no longer read in the code: {stale!r}"


def test_sanity_non_akana_scanner_finds_indirect_reads() -> None:
    """The two shapes a naive scanner misses: a config.py helper call and the
    module-constant indirection. If either disappears from the result, the scanner
    silently narrowed and the guard stops guarding."""
    reads = collect_runtime_env_reads()
    for known in ("WAKE_MIN_FRAMES", "OPENAI_REALTIME_URL", "CODEX_MODEL", "LLM_PROVIDER"):
        assert known in reads, f"env-read scan did not find {known} — the scanner may be broken"
    # …and the shapes it must NOT pick up (error codes / prompt constants).
    for not_env in ("LLM_TIMEOUT", "LLM_UNAVAILABLE", "WAKE_ERROR", "VOICE_DIRECTIVE_TR"):
        assert not_env not in reads, f"{not_env} is a constant, not an env name — scan is too loose"


def test_documented_file_roots_example_is_platform_neutral() -> None:
    """AKANA_FILE_ROOTS is os.pathsep-delimited — ``;`` on Windows, ``:`` on POSIX — so a
    MULTI-root example cannot be correct on both. A Windows user who copied the shipped
    ``a:b`` line got ONE unopenable root, and because the allowlist was then non-empty
    FileEngine suppressed the "AKANA_FILE_ROOTS is empty" message that would explain it.
    """
    from akana_server.config import parse_file_roots

    values = [
        line.split("=", 1)[1].strip()
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if re.match(r"^\s*#?\s*AKANA_FILE_ROOTS\s*=", line)
    ]
    assert values, "AKANA_FILE_ROOTS is no longer documented in .env.example"
    for value in values:
        if not value:
            continue
        roots = parse_file_roots(value)
        assert len(roots) == 1, (
            f"{value!r} is a multi-root example: it silently collapses to one bogus root "
            f"on the platform whose os.pathsep it does not use (got {roots!r} with "
            f"os.pathsep={os.pathsep!r}). Keep the assignment single-root and show the "
            "per-platform forms in prose."
        )
        tail = str(roots[0])[2:] if os.name == "nt" else str(roots[0])  # skip the drive colon
        assert ":" not in tail and ";" not in tail, (
            f"the documented root still contains a separator: {roots[0]!r}"
        )
