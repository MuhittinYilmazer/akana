"""Tool gateway — stream ``tool_call`` events → audit + ledger (T0 / PR-T1).

FULL AUTONOMY (user decision): the per-tool-call risk/approval gate was removed
entirely. This module is now ONLY observability:

* :func:`record_tool_call` — writes the call to ``audit.jsonl``, the ledger
  ``tool.run`` row, and the in-memory recent list. It makes no decisions, blocks
  no calls, and never raises.
* :func:`list_recent_tool_calls` — the most recent tool calls (``/tools/recent``).

The old PolicyEngine surface (risk scoring, deny/require_approval, pending approval
record, ``PolicyDeniedError``, ``task_policy_gate``) has been removed.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from pathlib import Path
from typing import Any

import ulid

from akana_server.audit import write_event as audit_write
from akana_server.timeutil import iso_now

log = logging.getLogger(__name__)

_RECENT_MAX = 100
_recent: deque[dict[str, Any]] = deque(maxlen=_RECENT_MAX)
_recent_lock = threading.Lock()


def _tool_name(call: dict[str, Any]) -> str:
    for key in ("name", "toolName", "tool_name", "tool"):
        val = call.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    fn = call.get("function")
    if isinstance(fn, dict):
        n = fn.get("name")
        if isinstance(n, str) and n.strip():
            return n.strip()
    tc = call.get("toolCall") or call.get("tool_call")
    if isinstance(tc, dict):
        provider = tc.get("providerIdentifier") or tc.get("provider_identifier")
        tool = tc.get("toolName") or tc.get("tool_name")
        if (
            isinstance(provider, str)
            and provider.strip()
            and isinstance(tool, str)
            and tool.strip()
        ):
            return f"{provider.strip()}/{tool.strip()}"
        for key in ("name", "toolName", "tool_name"):
            val = tc.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return "?"


def _audit_payload(call: dict[str, Any], *, mode: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "tool": {
            "id": call.get("id") or call.get("call_id"),
            "name": _tool_name(call),
            "phase": call.get("phase"),
            "status": call.get("status"),
        },
    }


def record_tool_call(
    data_dir: Path,
    call: dict[str, Any],
    *,
    turn_id: str | None = None,
    conv_id: str | None = None,
    task_id: str | None = None,
    client_ip: str | None = None,
    mode: str = "stream",
) -> dict[str, Any] | None:
    """Append tool invocation to audit.jsonl + in-memory recent list.

    Pure observability: the call is recorded and never blocked. The return value
    is the entry written to the record (legacy callers may ignore the return value).
    """
    if not isinstance(call, dict) or not call:
        return None

    ts = iso_now()
    entry: dict[str, Any] = {
        "id": str(ulid.new()),
        "ts": ts,
        "turn_id": turn_id,
        "conv_id": conv_id,
        "task_id": task_id,
        "mode": mode,
        "call": dict(call),
    }
    with _recent_lock:
        _recent.append(entry)

    audit_write(
        data_dir,
        "tool_call",
        turn_id=turn_id,
        conv_id=conv_id,
        client_ip=client_ip,
        data=_audit_payload(call, mode=mode),
    )

    return entry


def list_recent_tool_calls(*, limit: int = 20) -> list[dict[str, Any]]:
    """Return the most recent tool call records (newest last)."""
    cap = max(1, min(limit, _RECENT_MAX))
    with _recent_lock:
        items = list(_recent)
    if len(items) <= cap:
        return items
    return items[-cap:]


def reset_recent_for_tests() -> None:
    """Clear in-memory recent buffer (tests only)."""
    with _recent_lock:
        _recent.clear()


__all__ = [
    "list_recent_tool_calls",
    "record_tool_call",
    "reset_recent_for_tests",
]
