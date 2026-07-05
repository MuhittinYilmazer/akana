"""Faithful CLIENT test: register the real akana_memory/akana_vault config with the
claude CLI and run `claude mcp list` — the ACTUAL MCP client the app uses with the
Claude provider. Confirms the servers CONNECT under the real client on THIS OS, not
just under a direct subprocess spawn (scripts/diagnose_mcp.py covers that).

Graceful: if the claude CLI is absent or can't run a health check here (e.g. no auth
in CI), it SKIPS (exit 0) rather than failing. It only returns non-zero when claude
gives a definitive "not connected" for a built-in server.

Run:  python scripts/diagnose_claude_mcp.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

_BUILTIN = ("akana_memory", "akana_vault")


def _reconfigure_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass


def _claude_argv(extra: list[str]) -> list[str]:
    # On Windows `claude` is a .cmd shim that shell-less CreateProcess can't launch and
    # Python 3.12+ refuses to run without a shell → route it through `cmd /c`.
    exe = shutil.which("claude") or "claude"
    if sys.platform == "win32" and exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe, *extra]
    return [exe, *extra]


def _run(extra: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _claude_argv(extra),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def main() -> int:
    _reconfigure_utf8()
    print(f"Platform: {sys.platform}")
    if shutil.which("claude") is None:
        print("claude CLI not found — skipping client handshake test (not a failure).")
        return 0

    with tempfile.TemporaryDirectory(prefix="akana-claude-") as td:
        os.environ["AKANA_DATA_DIR"] = td
        from akana_server.config import load_settings
        from akana_server.orchestrator.memory_tools import mcp_servers_payload

        payload = {
            k: v
            for k, v in (mcp_servers_payload(load_settings()) or {}).items()
            if k in _BUILTIN
        }
    print("registering with claude (local scope):", list(payload))
    for name, cfg in payload.items():
        try:
            r = _run(["mcp", "add-json", name, json.dumps(cfg), "--scope", "local"], timeout=60)
            print(f"  add {name}: rc={r.returncode} {(r.stdout or '').strip()[:100]} {(r.stderr or '').strip()[:150]}")
        except Exception as exc:  # noqa: BLE001
            print(f"  add {name}: error {exc} — skipping client test")
            return 0

    try:
        r = _run(["mcp", "list"], timeout=120)
    except Exception as exc:  # noqa: BLE001
        print(f"`claude mcp list` could not run ({exc}) — skipping client gate (not a failure).")
        return 0
    print("=== claude mcp list ===")
    print(r.stdout)
    if r.stderr.strip():
        print("[stderr]", r.stderr.strip()[:500])

    lines = (r.stdout or "").splitlines()
    status = {
        name: next((ln for ln in lines if ln.strip().startswith(name + ":")), "")
        for name in payload
    }
    if r.returncode != 0 or not any(status.values()):
        print("Could not get a parseable connection status (auth/CI?) — skipping gate (not a failure).")
        return 0

    failed = 0
    for name, line in status.items():
        if "Connected" in line or "✔" in line:
            print(f"  RESULT {name}: CONNECTED")
        else:
            failed += 1
            print(f"  RESULT {name}: NOT CONNECTED -> {line.strip() or '(no status line)'}")
    print(f"\n{failed} built-in server(s) failed the claude-client handshake.")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
