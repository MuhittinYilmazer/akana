"""Drift guard: are the ``AKANA_*`` envs read from the code documented in .env.example?

How it works:

* The runtime code (``akana_server/`` + ``src/akana/``) is scanned via AST;
  full-literal ``AKANA_<NAME>`` string constants are collected. Env names always
  appear in code as a full string literal (``os.environ.get("AKANA_X")``,
  ``_secret("AKANA_X")``, ``HISTORY_BUDGET_ENV = "AKANA_X"`` etc.) — so the
  full-literal match catches both direct and indirect reads, while it does not
  catch mentions inside docstrings (no false positives).
* The documented names in .env.example are extracted from ``[# ]NAME=`` lines
  (variables documented as a comment line count too).
* Any name in the code but NOT in .env.example fails the test — deliberate
  exclusions live in ``ALLOWLIST`` with a rationale.

When you add a new env variable: either add it to .env.example with a one-line
description, or (if it really won't be exposed to the user) add it to ALLOWLIST with a rationale.
"""

from __future__ import annotations

import ast
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
