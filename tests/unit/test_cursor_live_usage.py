"""Cursor provider LIVE usage (usage_live) + done cost parities.

The Claude provider streams live token/cost during production while Cursor only
reported at the end. CUR-1/CUR-2 close that gap:

  - CUR-1: the bridge emits ``{ev:"usage", usage:{...}}`` NDJSON lines; llm_dispatch
    converts them to Claude's ``{"usage_live": {...}}`` shape and yields them (the
    producer relays this to the SSE ``usage`` event provider-agnostically).
  - CUR-2: the terminal ``done.usage`` CARRIES cost (cursor does not give cost → it is
    estimated from the active model tag, sonnet default).

The fake bridge stdout is modeled on the REAL NDJSON shapes fed by
``test_cursor_client_readline``; ``AKANA_BRIDGE_DAEMON=0`` forces the direct
(daemon-less) ``stream_user_chat`` path.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from akana_server.orchestrator import cursor_provider, llm_dispatch


# Fake bridge: consumes the stdin payload, writes REAL NDJSON events.
# meta → delta → LIVE usage (estimate) → delta → REAL usage (turn-ended) → done.
# ``done.usage`` is the cursor shape (inputTokens/outputTokens) and CARRIES no cost.
_FAKE_BRIDGE = r"""
import sys, json
sys.stdin.buffer.read()  # consume the payload (the bridge reads stdin)
def emit(o): sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
emit({"ev": "meta", "run_id": None, "agent_id": "agent-xyz", "model": "composer-2"})
emit({"ev": "delta", "text": "Mer"})
emit({"ev": "usage", "usage": {"outputTokens": 8}})           # live estimate
emit({"ev": "delta", "text": "haba"})
emit({"ev": "usage", "usage": {"inputTokens": 123, "outputTokens": 45}})  # turn-ended real
emit({"ev": "done", "ok": True, "text": "Merhaba", "status": "finished",
      "usage": {"inputTokens": 123, "outputTokens": 45}, "agent_id": "agent-xyz"})
"""


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        data_dir=tmp_path,
        bridge_dir=tmp_path,
        workspace=tmp_path,
        cursor_model="composer-2",
        cursor_api_key="k-test",
        # idle ceiling = min(bridge_timeout, llm_idle_timeout); keep it generous.
        bridge_timeout=30.0,
        llm_idle_timeout=0,
        # Circuit breaker OFF (threshold 0) → the direct stream skips the breaker block.
        network_breaker_threshold=0,
        network_max_retries=1,
    )


async def _collect(settings: SimpleNamespace) -> list[dict]:
    out: list[dict] = []
    async for ev in llm_dispatch.stream_user_chat(settings, "selam"):
        out.append(ev)
    return out


def test_cursor_stream_yields_usage_live_and_done_cost(tmp_path, monkeypatch) -> None:
    # Direct path: daemon OFF + provider cursor + fake bridge argv.
    monkeypatch.setenv("AKANA_BRIDGE_DAEMON", "0")
    monkeypatch.setattr(llm_dispatch, "_active_provider", lambda _s: "cursor")

    fake = tmp_path / "fake_bridge.py"
    fake.write_text(_FAKE_BRIDGE, encoding="utf-8")
    monkeypatch.setattr(
        cursor_provider, "bridge_args", lambda _s: [sys.executable, str(fake)]
    )

    from akana_server.network.guard import reset_global_registry

    reset_global_registry()  # test isolation

    events = asyncio.run(_collect(_settings(tmp_path)))

    # --- CUR-1: at least one usage_live, with int prompt/completion ---
    live = [e["usage_live"] for e in events if "usage_live" in e]
    assert live, f"no usage_live at all; events: {events}"
    for block in live:
        assert isinstance(block["prompt"], int)
        assert isinstance(block["completion"], int)
    # The real turn-ended usage was also reflected live (prompt=123, completion=45).
    assert any(b["prompt"] == 123 and b["completion"] == 45 for b in live), live

    # --- CUR-2: terminal done.usage CARRIES cost (cost_usd) ---
    done = next((e for e in events if e.get("done")), None)
    assert done is not None, f"no done event: {events}"
    usage = done["usage"]
    assert usage["prompt_tokens"] == 123
    assert usage["completion_tokens"] == 45
    assert "cost_usd" in usage, f"done.usage carries no cost: {usage}"
    assert isinstance(usage["cost_usd"], float) and usage["cost_usd"] > 0
    assert done["text"] == "Merhaba"


def test_usage_to_tokens_estimates_cost_without_explicit_value() -> None:
    """``_usage_to_tokens`` estimates cost token-by-token from cursor usage
    (cursor's done usage carries no cost field). Cost is OPT-IN: when ``estimate_cost``
    (or ``model``) is passed it gains cost like the stream_user_chat done block."""
    tokens = cursor_provider.usage_to_tokens(
        {"inputTokens": 1000, "outputTokens": 500}, estimate_cost=True
    )
    assert tokens["prompt_tokens"] == 1000
    assert tokens["completion_tokens"] == 500
    assert tokens.get("cost_usd", 0) > 0  # estimated from the sonnet default price


def test_usage_to_tokens_bare_call_has_no_cost() -> None:
    """A single-argument (opt-out) call ADDS no cost field — backward-compat:
    the bridge_pool daemon path and the token-coercion tests rely on the bare block."""
    tokens = cursor_provider.usage_to_tokens(
        {"inputTokens": 1000, "outputTokens": 500}
    )
    assert tokens["prompt_tokens"] == 1000
    assert tokens["completion_tokens"] == 500
    assert "cost_usd" not in tokens


def test_usage_to_tokens_prefers_explicit_cost() -> None:
    """If an explicit ``cost_usd`` is given it is used instead of the estimate (Claude parity)."""
    tokens = cursor_provider.usage_to_tokens(
        {"inputTokens": 10, "outputTokens": 5}, cost_usd=0.4242
    )
    assert tokens["cost_usd"] == pytest.approx(0.4242)


def test_friendly_provider_error_classifies_subtypes() -> None:
    """CUR-3: auth/rate-limit/timeout/resume sub-types are clarified in English."""
    f = llm_dispatch.friendly_provider_error
    assert "authentication failed" in f("boom", status=401)
    assert "authentication failed" in f("Invalid API key provided")
    assert "rate limit" in f("429 Too Many Requests")
    assert "rate limit" in f("x", error_code="rate_limit_exceeded")
    assert "timed out" in f("request timed out")
    assert "previous session" in f("no conversation found with session id abc")
