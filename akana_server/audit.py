"""JSONL audit log under ``~/.akana/audit/YYYY-MM-DD.jsonl``.

One event = one line of JSON. We never block the request on the write; if the
file is unwritable we just drop the event (with a single warning log so the
operator notices).

Event shapes (small, future-extensible):

    {
      "ts":         "2026-05-16T13:42:08.123Z",     # ISO 8601 UTC
      "kind":       "chat" | "voice" | "policy_block" | "model_profile" | ...
      "turn_id":    "01HZA…",        # optional
      "conv_id":    "01HZB…",        # optional
      "client_ip":  "127.0.0.1",     # optional
      "data":       { … }            # kind-specific payload
    }

The writer is intentionally synchronous + tiny so it can run from anywhere
(routes, lifespan, background tasks). It's not async because file appends are
fast (kernel buffer) and we don't want to fight asyncio task ordering for a
log line. If audit ever needs to scale, swap this for a queue + worker.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from akana_server.timeutil import iso_now

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_DEFAULT_REL = Path("audit")


def _audit_root(data_dir: Path) -> Path:
    return (data_dir / _DEFAULT_REL).resolve()


def _today_path(data_dir: Path) -> Path:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return _audit_root(data_dir) / f"{today}.jsonl"


def _iso_now() -> str:
    return iso_now()


def write_event(
    data_dir: Path,
    kind: str,
    *,
    turn_id: str | None = None,
    conv_id: str | None = None,
    client_ip: str | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    """Append one event to today's audit file. Never raises."""
    record: dict[str, Any] = {"ts": _iso_now(), "kind": kind}
    if turn_id:
        record["turn_id"] = turn_id
    if conv_id:
        record["conv_id"] = conv_id
    if client_ip:
        record["client_ip"] = client_ip
    if data is not None:
        record["data"] = data

    try:
        # `default=str` coerces non-JSON-native values (datetime/bytes/Path/set/
        # arbitrary objects) instead of raising — an audit event must survive with
        # readable values, not be lost. The serialization stays INSIDE the guard
        # (with TypeError/ValueError caught) so a pathological payload (e.g. a
        # circular ref) drops one event rather than crashing the caller: this
        # writer is "fire and forget" from request/voice/connector paths and is
        # documented to never raise.
        line = json.dumps(
            record, ensure_ascii=False, separators=(",", ":"), default=str
        ) + "\n"
        path = _today_path(data_dir)
        # 0o700: audit JSONL files contain client_ip/conv_id/query → on a shared
        # host other users must not be able to read them. mkdir with exist_ok does
        # NOT change the mode → tighten the existing directory too.
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        # `O_APPEND` makes the write atomic for sub-PIPE_BUF lines on POSIX, so
        # we don't need a per-process lock for correctness — but we still take
        # one to avoid interleaving with concurrent log rotation.
        with _LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except (OSError, TypeError, ValueError) as e:
        log.warning("audit write failed (%s): %s", kind, e)


def read_tail(data_dir: Path, *, limit: int = 100) -> list[dict[str, Any]]:
    """Return the last `limit` audit events from today. Best-effort."""
    path = _today_path(data_dir)
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    if limit > 0 and len(out) > limit:
        return out[-limit:]
    return out


def files_in(data_dir: Path) -> list[Path]:
    """Audit files sorted by date (oldest first)."""
    root = _audit_root(data_dir)
    if not root.is_dir():
        return []
    try:
        return sorted(p for p in root.iterdir() if p.is_file() and p.suffix == ".jsonl")
    except OSError:
        return []


def purge_older_than(data_dir: Path, *, keep_days: int) -> list[Path]:
    """Delete audit files older than `keep_days`. Returns the list deleted."""
    if keep_days <= 0:
        return []
    from datetime import timedelta

    cutoff = datetime.now(UTC).date() - timedelta(days=keep_days)
    deleted: list[Path] = []
    for p in files_in(data_dir):
        stem = p.stem
        try:
            d = datetime.strptime(stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < cutoff:
            try:
                os.unlink(p)
                deleted.append(p)
            except OSError:
                continue
    return deleted


__all__ = [
    "files_in",
    "purge_older_than",
    "read_tail",
    "write_event",
]
