"""friendly_provider_error: raw provider/bridge error → CLEAR + actionable English
message. No fallback (owner's decision) — a cryptic Node stack trace never leaks to the user."""

from __future__ import annotations

from akana_server.orchestrator.llm_dispatch import friendly_provider_error


def test_module_not_found_suggests_npm_install() -> None:
    raw = "Error: Cannot find module 'x'\n  at loader.js:818\n  code: 'MODULE_NOT_FOUND'"
    msg = friendly_provider_error(raw, provider="cursor")
    assert "npm install" in msg
    assert "MODULE_NOT_FOUND" not in msg and "loader.js" not in msg  # raw trace does not leak


def test_connection_refused_cursor_vs_ollama() -> None:
    assert "could not reach" in friendly_provider_error("ECONNREFUSED", provider="cursor").lower()
    o = friendly_provider_error("ollama not reachable at http://localhost:11434", provider="ollama")
    assert "ollama serve" in o.lower()


def test_active_run_is_friendly() -> None:
    msg = friendly_provider_error("Agent abc already has active run", provider="cursor")
    assert "a response is already in progress" in msg.lower()


def test_auth_failure_points_to_settings() -> None:
    msg = friendly_provider_error("401 Unauthorized: invalid api key", provider="cursor")
    assert "api key" in msg.lower()


def test_unknown_error_trims_to_first_line_not_full_trace() -> None:
    raw = "boom happened\n  at internal/foo.js:1\n  at bar.js:2"
    msg = friendly_provider_error(raw, provider="cursor")
    assert "boom happened" in msg
    assert "internal/foo.js" not in msg  # stack lines do not leak


def test_empty_raw_has_generic_nonempty_message() -> None:
    assert friendly_provider_error("", provider="cursor").strip()


def test_non_cursor_provider_gets_no_bridge_or_npm_advice() -> None:
    # The Node bridge is cursor-SPECIFIC: openai/gemini/claude/ollama use direct HTTP/CLI.
    # Text like "cannot find module" gives no npm/Node advice for non-cursor providers.
    msg = friendly_provider_error("Error: Cannot find module 'x'", provider="openai")
    assert "npm install" not in msg
    assert "node.js" not in msg.lower()
    assert "bridge" not in msg.lower()
    assert "openai" in msg.lower()  # the generic fallback carries the provider name


def test_non_cursor_unknown_error_has_no_bridge_wording() -> None:
    msg = friendly_provider_error("weird failure", provider="gemini")
    assert "bridge" not in msg.lower()  # "bridge returned an error" never for non-cursor
    assert "weird failure" in msg


def test_non_cursor_session_resume_text_is_not_cursor_specific() -> None:
    # "session not found" returns the cursor session-resume message ONLY for cursor.
    msg = friendly_provider_error("session not found", provider="gemini")
    assert "Cursor could not resume" not in msg
