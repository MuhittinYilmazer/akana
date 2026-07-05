"""Regression: the claude_provider mirrors of the cursor env/breaker hardening.

The cursor path applies an ``llm_dispatch._bridge_env`` denylist plus a
network/guard breaker ``GeneratorExit`` exclusion; the same two guarantees must
hold for the claude provider:

* the spawned ``claude`` CLI + its MCP subprocesses must NOT inherit foreign-provider
  API keys (OPENAI/GEMINI/GOOGLE) from the environment, and
* a consumer disconnect (``GeneratorExit``) must NOT be counted as a circuit-breaker
  failure (only real provider faults trip the breaker).

The breaker MECHANISM's GeneratorExit exclusion is covered behaviorally in
``tests/unit/test_bh_env_breaker.py`` (network/guard stream_breaker); here we assert
the claude stream's inline breaker except wires GeneratorExit into the exclusion.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from akana_server.orchestrator import claude_provider


def test_claude_env_strips_foreign_provider_keys(monkeypatch) -> None:
    for k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(k, "sk-should-not-leak")
    monkeypatch.setenv("PATH", "/usr/bin")  # a benign var must survive

    env = claude_provider._claude_env(SimpleNamespace(data_dir=None))

    for k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        assert k not in env, f"{k} leaked into the spawned claude CLI environment"
    assert env.get("PATH") == "/usr/bin", "non-secret env must be preserved"


def test_claude_breaker_excludes_generatorexit_alongside_cancelled() -> None:
    src = Path(claude_provider.__file__).read_text(encoding="utf-8")
    assert re.search(
        r"isinstance\(\s*_net_exc,\s*\(\s*asyncio\.CancelledError,\s*GeneratorExit\s*\)",
        src,
    ), "claude_provider stream breaker must exclude GeneratorExit alongside CancelledError"
