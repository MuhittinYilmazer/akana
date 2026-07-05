"""Cross-provider error vocabulary + the neutral LLM result type.

These three names — :class:`LLMCallError`, :func:`friendly_provider_error` and
:class:`LLMResult` — are shared by every provider path (cursor/claude/gemini/
openai/ollama) and by the dispatch hub. They historically lived in
``llm_dispatch`` (the module that also carried the whole Cursor provider), which
forced every other provider to import them *from the hub that dispatches to
them* — an inverted, only-lazily-avoided import cycle. Moving them to this leaf
module (imported by everyone, importing nobody in the orchestrator) breaks that
cycle. ``llm_dispatch`` re-exports all three under their historical names for
backward compatibility.

Behaviour is identical — this is a move, not a rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class LLMCallError(Exception):
    def __init__(self, message: str, *, status_code: int = 503) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def friendly_provider_error(
    raw: str,
    *,
    provider: str = "cursor",
    error_code: str | None = None,
    status: int | None = None,
) -> str:
    """Map raw provider/bridge error text to a clear, actionable message.

    No silent fallback (owner's decision): instead of quietly switching to another
    model, a comprehensible error is returned. Unrecognised errors surface only
    the first meaningful line + provider name, never a cryptic Node stack trace.

    CUR-3: the bridge (``normalizeError``) can now carry ``error_code``
    (CursorAgentError.code/type) and HTTP ``status`` in addition to the raw message.
    These structural hints narrow the sub-type (auth/rate-limit/timeout) even when
    the raw text is ambiguous. Text-based scanning (backward-compatible) is kept
    as a fallback when no structural match is found.
    """
    text = (raw or "").strip()
    low = text.lower()
    code = (error_code or "").strip().lower()
    prov = provider.capitalize()
    # --- Structural hints (classify correctly even when raw text is ambiguous) ---
    if status in (401, 403) or any(k in code for k in ("unauthorized", "forbidden", "auth", "api_key", "apikey")):
        return f"{prov} authentication failed — check your API key in Settings."
    if (
        status == 429
        or ("rate" in code and "limit" in code)
        or "too_many" in code
        or "rate_limit" in code
    ):
        return f"{prov} rate limit reached (too many requests). Wait a moment and try again."
    if status in (408, 504) or "timeout" in code or "timed_out" in code:
        return f"{prov} response timed out. Please try again."
    # --- Text-based scanning (backward-compatible; used when bridge sends no error code) ---
    # Cursor SDK Node bridge-specific advice (npm/Node/session-resume) only for
    # provider=="cursor"; other providers use direct HTTP/CLI, not a bridge.
    if provider == "cursor":
        if "module_not_found" in low or "cannot find module" in low:
            return (
                "Cursor bridge dependencies are not installed. In the terminal, run "
                "`cd cursor_bridge && npm install` and try again."
            )
        if "node: not found" in low or "command not found" in low or ("enoent" in low and "node" in low):
            return "Node.js not found — the Cursor bridge requires Node.js (install it and add it to PATH)."
    if any(k in low for k in ("econnrefused", "connection refused", "not reachable", "ulaşılam")):
        if provider == "ollama":
            return (
                "Cannot reach Ollama (default http://localhost:11434). "
                "Is `ollama serve` running?"
            )
        return f"Could not reach the {prov} service (connection refused). Check the service/network status."
    if any(k in low for k in ("rate limit", "rate_limit", "too many requests", "429", "quota")):
        return f"{prov} rate limit reached (too many requests). Wait a moment and try again."
    if any(k in low for k in ("unauthorized", "invalid api key", "forbidden", "401", "403", "api key")):
        return f"{prov} authentication failed — check your API key in Settings."
    if "already has active run" in low or "active run" in low:
        return "A response is already in progress for this conversation; wait for it to finish and try again."
    if provider == "cursor" and any(
        k in low for k in ("no conversation", "session not found", "resume", "could not load history")
    ):
        return (
            "Cursor could not resume the previous session (provider switch/stale session) — "
            "send the message again; if the problem persists, start a new chat."
        )
    if "timed out" in low or "timeout" in low:
        return f"{prov} response timed out. Please try again."
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    # "bridge" (Node bridge) terminology only for cursor; others use direct HTTP/CLI.
    if provider == "cursor":
        return f"The Cursor bridge returned an error: {first[:200]}" if first else "The Cursor bridge returned an unexpected error."
    return f"{prov} returned an error: {first[:200]}" if first else f"{prov} returned an unexpected error."


@dataclass(frozen=True, slots=True)
class LLMResult:
    text: str
    status: str
    raw: dict[str, Any]


__all__ = [
    "LLMCallError",
    "LLMResult",
    "friendly_provider_error",
]
