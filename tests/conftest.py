"""Suite-wide test isolation.

Why this exists: ``akana_server.config`` loads the repo ``.env`` with
``load_dotenv(override=False)`` — which injects ``.env`` values only for env vars
NOT already set in the process. That is correct for production, but it means a
developer's real ``.env`` (their provider choice, keys, …) leaks into the app
under test. Most fixtures already pin the env they assert on (``CURSOR_API_KEY=""``,
``CURSOR_MODEL=…``, ``AKANA_TOKEN=""``) "so assertions don't depend on the repo
.env" — but ``LLM_PROVIDER`` was missed. A contributor whose ``.env`` sets
``LLM_PROVIDER=claude`` (or gemini/openai/ollama) would then see every
provider-default test fail (active_provider/cursor-key/timeout assertions).

Pinning ``LLM_PROVIDER`` to ``"cursor"`` here makes the whole suite hermetic:
``load_dotenv(override=False)`` won't override an already-set var, so a contributor's
``.env`` can't leak in. ``"cursor"`` is the suite's chosen test provider — the
product itself no longer privileges any provider as a default (an unset value
resolves to ``""`` = "unconfigured", and chat refuses until one is picked). Tests
that exercise a specific provider still set it explicitly; their
``monkeypatch.setenv`` runs after this autouse fixture and wins. Tests that assert
the unconfigured behavior set ``LLM_PROVIDER=""`` themselves.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# Route pytest through the SINGLE src-layout bootstrap (one of its four sanctioned
# entry points), so `import akana` resolves to src/akana exactly as it does under
# `python akana.py` and the server. pytest.ini's `pythonpath = src .` already puts
# src first; this is belt-and-suspenders and makes the mechanism uniform across
# every entry point (asserted by tests/architecture/test_src_bootstrap.py).
_repo_root = str(_Path(__file__).resolve().parents[1])
if _repo_root not in _sys.path:
    _sys.path.insert(0, _repo_root)
from _akana_src_bootstrap import ensure_src_on_path as _ensure_src_on_path  # noqa: E402

_ensure_src_on_path()

import pytest  # noqa: E402

from akana_server.network.guard import reset_global_registry  # noqa: E402


@pytest.fixture(autouse=True)
def _hermetic_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Block the repo .env's LLM_PROVIDER from leaking into tests (see module docstring).
    # Provider-specific tests override this with their own setenv (runs later → wins).
    monkeypatch.setenv("LLM_PROVIDER", "cursor")


@pytest.fixture(autouse=True)
def _reset_network_registry():
    """Reset the process-global circuit-breaker registry around EVERY test.

    NetworkEngine F0 keeps a process-global breaker registry (``global_registry``).
    Any test that wraps a provider/bridge call and produces consecutive errors can
    trip a breaker OPEN; that state then persists across tests because the registry
    is a module singleton. A later test whose own capture/LLM path is gated by the
    same breaker then fails only in a full-suite run (never in isolation) — a classic
    order-dependent flake.

    This used to live in ``tests/unit/conftest.py`` and so only protected unit tests;
    integration tests (which have no conftest of their own) inherited nothing and were
    vulnerable — e.g. ``test_llm_capture_lands_in_v2_inbox`` dropped its staged capture
    when an earlier integration/e2e test left a breaker open. Hoisting the reset to the
    suite-wide conftest guarantees every test — arch, e2e, integration, unit — starts
    and ends with a clean registry.
    """
    reset_global_registry()
    yield
    reset_global_registry()
