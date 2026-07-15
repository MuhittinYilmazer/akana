"""Per-provider usage attribution — the observability panel's token source.

The dispatch hub is the ONE place that knows which provider served a turn, so it
stamps ``usage["provider"]`` on the terminal ``done`` event; ``_done_tokens_block``
persists it with the turn. Without the stamp the panel can only show all-provider
totals. These lock the stamp (present on done, absent elsewhere, never clobbering a
self-reported value) and the persistence passthrough.
"""

from __future__ import annotations

from akana_server.api.routes.chat.chat_producer import _done_tokens_block
from akana_server.orchestrator.llm_dispatch import _stamp_usage_provider


def test_stamp_added_to_done_event_usage():
    ev = {"done": True, "usage": {"prompt_tokens": 3, "completion_tokens": 5}}
    out = _stamp_usage_provider(ev, "codex")
    assert out["usage"]["provider"] == "codex"


def test_stamp_does_not_overwrite_self_reported_provider():
    ev = {"done": True, "usage": {"provider": "already", "prompt_tokens": 1}}
    _stamp_usage_provider(ev, "codex")
    assert ev["usage"]["provider"] == "already"  # setdefault — provider wins


def test_stamp_ignores_non_done_and_usageless_events():
    delta = {"delta": "hi", "done": False}
    assert _stamp_usage_provider(delta, "openai") == delta  # untouched
    done_no_usage = {"done": True}
    assert "usage" not in _stamp_usage_provider(done_no_usage, "openai")


def test_done_tokens_block_persists_provider_stamp():
    block = _done_tokens_block(
        {"prompt_tokens": 10, "completion_tokens": 4, "provider": "gemini"}
    )
    assert block["provider"] == "gemini"
    assert block["prompt"] == 10 and block["completion"] == 4


def test_done_tokens_block_omits_provider_when_absent():
    # Legacy turns / providers that never stamped → the field is simply absent
    # (readers treat it as optional), not an empty string.
    block = _done_tokens_block({"prompt_tokens": 1, "completion_tokens": 1})
    assert "provider" not in block
