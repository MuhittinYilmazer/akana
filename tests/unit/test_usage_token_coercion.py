"""Usage→token normalization boundary-value lock (QUALITY pass).

Bridge/CLI stdout is external JSON; the token count may arrive as a float-string
("12.5"), a garbage string ("x"), a list, or None. These values used to throw
``ValueError``/``TypeError`` under ``int(...)``, swallowing the ``done`` event and
crashing the ENTIRE turn. Now anything that does not convert to a number/numeric
string counts as 0 and the hot path is never broken.
"""

from __future__ import annotations

import pytest

from akana_server.orchestrator.base import coerce_cost_usd
from akana_server.orchestrator.base import coerce_token_count as _coerce_token_count
from akana_server.orchestrator.claude_provider import (
    _usage_to_tokens as claude_usage,
)
from akana_server.orchestrator.cursor_provider import (
    usage_to_tokens as cursor_usage,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, 0),
        (0, 0),
        (42, 42),
        (5.9, 5),  # float is rounded down (int truncation)
        (True, 1),  # bool behaves like int
        (False, 0),
        ("17", 17),
        ("12.5", 12),  # float-string — the old path crashed here with ValueError
        ("  8  ", 8),  # numeric string with whitespace
        ("x", 0),  # garbage string → 0 (NO crash)
        ("", 0),
        ([1, 2], 0),  # list → 0 (NO crash)
        ({"a": 1}, 0),
    ],
)
def test_coerce_token_count_never_crashes(value: object, expected: int) -> None:
    assert _coerce_token_count(value) == expected


def test_cursor_usage_float_string_does_not_crash() -> None:
    out = cursor_usage({"inputTokens": "12.5", "outputTokens": "9.9"})
    assert out["prompt_tokens"] == 12
    assert out["completion_tokens"] == 9


def test_cursor_usage_garbage_cache_field_zeroed() -> None:
    out = cursor_usage({"cacheReadTokens": "x", "cacheWriteTokens": [1]})
    assert out["cache_read_tokens"] == 0
    assert out["cache_write_tokens"] == 0


def test_cursor_usage_list_token_does_not_crash() -> None:
    out = cursor_usage({"inputTokens": [1]})
    assert out["prompt_tokens"] == 0


def test_claude_usage_bad_strings_do_not_crash() -> None:
    out = claude_usage(
        {
            "input_tokens": "9.9",
            "output_tokens": "bad",
            "cache_read_input_tokens": [2],
            "cache_creation_input_tokens": None,
        }
    )
    assert out["prompt_tokens"] == 9
    assert out["completion_tokens"] == 0
    assert out["cache_read_tokens"] == 0
    assert out["cache_write_tokens"] == 0


def test_usage_none_returns_zeros() -> None:
    assert cursor_usage(None)["prompt_tokens"] == 0
    assert claude_usage(None)["completion_tokens"] == 0


def test_valid_integer_usage_unchanged() -> None:
    """Behavior-locking: valid integer inputs pass through unchanged."""
    out = cursor_usage(
        {
            "inputTokens": 3,
            "outputTokens": 5,
            "cacheReadTokens": 7,
            "cacheWriteTokens": 11,
        }
    )
    assert out == {
        "prompt_tokens": 3,
        "completion_tokens": 5,
        "tool_calls": [],
        "cache_read_tokens": 7,
        "cache_write_tokens": 11,
    }


# --- Cost (total_cost_usd) hardening + attaching to done -----------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, 0.0),
        (0, 0.0),
        (0.0123, 0.0123),
        (1, 1.0),
        ("0.0099", 0.0099),
        ("  0.5  ", 0.5),
        (True, 0.0),  # bool → not a cost
        (False, 0.0),
        (-0.5, 0.0),  # negative cost is meaningless → 0
        ("x", 0.0),  # garbage string → 0 (NO crash)
        ("", 0.0),
        ([1], 0.0),  # list → 0
        (float("nan"), 0.0),  # NaN → 0
        (float("inf"), 0.0),  # inf → 0
    ],
)
def test_coerce_cost_usd_never_crashes(value: object, expected: float) -> None:
    assert coerce_cost_usd(value) == expected


def test_claude_usage_appends_cost_when_positive() -> None:
    out = claude_usage({"input_tokens": 3, "output_tokens": 5}, 0.0123)
    assert out["cost_usd"] == 0.0123
    assert out["prompt_tokens"] == 3
    assert out["completion_tokens"] == 5


def test_claude_usage_omits_cost_when_zero_or_missing() -> None:
    assert "cost_usd" not in claude_usage({"input_tokens": 1})
    assert "cost_usd" not in claude_usage({"input_tokens": 1}, 0)
    assert "cost_usd" not in claude_usage({"input_tokens": 1}, None)
    assert "cost_usd" not in claude_usage({"input_tokens": 1}, -1.0)


def test_claude_usage_cost_with_none_usage_still_attaches() -> None:
    """Even when usage is None, if a cost arrived it must be carried onto done (card parity)."""
    out = claude_usage(None, 0.05)
    assert out["cost_usd"] == 0.05
    assert out["prompt_tokens"] == 0


def test_done_tokens_block_surfaces_cost() -> None:
    """Producer done block: cost_usd from usage is carried into the SSE, otherwise it never appears."""
    from akana_server.api.routes.chat.chat_producer import _done_tokens_block

    with_cost = _done_tokens_block(
        {"prompt_tokens": 10, "completion_tokens": 20, "cost_usd": 0.0042}
    )
    assert with_cost == {"prompt": 10, "completion": 20, "cost_usd": 0.0042}

    no_cost = _done_tokens_block({"prompt_tokens": 10, "completion_tokens": 20})
    assert no_cost == {"prompt": 10, "completion": 20}
    assert "cost_usd" not in no_cost

    # Broken/None usage → no crash, zero block
    assert _done_tokens_block(None) == {"prompt": 0, "completion": 0}
