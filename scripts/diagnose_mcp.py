"""Cross-platform MCP + Cursor-bridge spawn diagnostic.

Reproduces EXACTLY how the app launches its stdio children — the real
``mcp_servers_payload`` command/args/env/cwd for ``akana_memory`` / ``akana_vault``
and the Cursor bridge daemon — runs the JSON-RPC handshake against each, and prints
the real stdout / stderr / exit code. The point is to surface a Windows-only failure
(the child never starts, or the handshake blocks) WITHOUT running the whole app/UI.

The unit suite mocks every subprocess, so it can be green on Windows while the real
spawn is broken; this script closes that gap. It is what CI runs on windows-latest,
and it doubles as a one-shot local probe: ``python scripts/diagnose_mcp.py``.

Exit code: number of REQUIRED children that failed (0 = all good).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Handshake the MCP servers must answer. tools/list comes AFTER initialized; both
# must arrive before stdin EOF so the child replies then exits cleanly.
_MCP_REQUESTS = [
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "diagnose", "version": "1"},
        },
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
]

_HANDSHAKE_TIMEOUT = 30.0  # generous: a cold Windows import + DB open is still seconds


def _hr(title: str) -> None:
    print(f"\n{'=' * 4} {title} {'=' * (72 - len(title))}")


def _run_child(
    name: str,
    command: str,
    args: list[str],
    env_overrides: dict[str, str],
    cwd: str | None,
    stdin_text: str,
    timeout: float,
) -> dict[str, object]:
    """Spawn one stdio child exactly as the app does; feed stdin, capture everything.

    env is the FULL parent environment plus the payload overrides (a child needs PATH
    etc. to even find python/node — the app's child inherits the server env and adds
    PYTHONPATH/AKANA_DATA_DIR on top).
    """
    argv = [command, *args]
    env = {**os.environ, **env_overrides}
    print(f"  command : {command}")
    print(f"  args    : {args}")
    print(f"  cwd     : {cwd}")
    print(f"  env+    : {env_overrides}")
    # Pre-flight: do the paths the app hands the child actually exist on this OS?
    if not Path(command).exists() and not _on_path(command):
        print(f"  !! command not found on disk or PATH: {command}")
    if cwd and not Path(cwd).is_dir():
        print(f"  !! cwd does not exist: {cwd}")
    started = time.perf_counter()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        print(f"  !! spawn failed: {type(exc).__name__}: {exc}")
        return {"name": name, "ok": False, "reason": f"spawn failed: {exc}"}
    try:
        out, err = proc.communicate(stdin_text, timeout=timeout)
        elapsed = time.perf_counter() - started
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        elapsed = time.perf_counter() - started
        print(f"  !! HANDSHAKE TIMED OUT after {timeout:.0f}s — child never replied")
        print(f"  stderr:\n{_indent(err)}")
        return {"name": name, "ok": False, "reason": "handshake timeout", "stderr": err}
    print(f"  exit    : {proc.returncode}   elapsed: {elapsed * 1000:.0f}ms")
    if err.strip():
        print(f"  stderr  :\n{_indent(err)}")
    return {
        "name": name,
        "ok": proc.returncode == 0 or bool(out.strip()),
        "exit": proc.returncode,
        "stdout": out,
        "stderr": err,
    }


def _on_path(name: str) -> bool:
    import shutil

    return shutil.which(name) is not None


def _indent(text: str, prefix: str = "    | ") -> str:
    return "\n".join(prefix + ln for ln in text.strip().splitlines()) or "    | (empty)"


def _check_mcp_handshake(name: str, report: dict[str, object]) -> bool:
    """A real MCP server returns the initialize result + the tools/list result."""
    out = str(report.get("stdout") or "")
    init_ok = False
    tools_ok = False
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            print(f"  !! NON-PROTOCOL line on stdout (breaks framing): {line[:100]!r}")
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            print(f"  !! invalid JSON on stdout: {line[:100]!r}")
            continue
        if obj.get("id") == 1 and "result" in obj:
            init_ok = True
        if obj.get("id") == 2 and "result" in obj:
            tools = obj["result"].get("tools", [])
            tools_ok = True
            print(f"  tools   : {[t.get('name') for t in tools]}")
    verdict = init_ok and tools_ok
    print(f"  RESULT  : {'PASS' if verdict else 'FAIL'} "
          f"(initialize={'ok' if init_ok else 'MISSING'}, tools/list={'ok' if tools_ok else 'MISSING'})")
    return verdict


def diagnose_mcp_servers(data_dir: Path) -> list[tuple[str, bool, bool]]:
    """Spawn every server in the real payload and handshake it. Returns
    (name, required, passed) per server."""
    os.environ["AKANA_DATA_DIR"] = str(data_dir)
    from akana_server.config import load_settings
    from akana_server.orchestrator.memory_tools import mcp_servers_payload

    settings = load_settings()
    payload = mcp_servers_payload(settings) or {}
    if not payload:
        print("  !! mcp_servers_payload returned nothing (both servers disabled?)")
        return []
    stdin_text = "".join(json.dumps(r) + "\n" for r in _MCP_REQUESTS)
    results: list[tuple[str, bool, bool]] = []
    for name, cfg in payload.items():
        _hr(f"MCP server: {name}")
        report = _run_child(
            name,
            command=str(cfg["command"]),
            args=[str(a) for a in cfg.get("args", [])],
            env_overrides={k: str(v) for k, v in (cfg.get("env") or {}).items()},
            cwd=str(cfg["cwd"]) if cfg.get("cwd") else None,
            stdin_text=stdin_text,
            timeout=_HANDSHAKE_TIMEOUT,
        )
        passed = bool(report.get("ok")) and _check_mcp_handshake(name, report)
        results.append((name, True, passed))
    return results


def diagnose_cursor_bridge() -> tuple[str, bool, bool] | None:
    """Spawn the bridge daemon with a bogus key; the auth error MUST come back tagged
    with the REAL request id (regression guard for the id:'?' bug)."""
    _hr("Cursor bridge daemon (bogus key → real-id error)")
    bridge_dir = _REPO_ROOT / "cursor_bridge"
    daemon = bridge_dir / "bridge_daemon.mjs"
    if not daemon.is_file():
        print("  !! bridge_daemon.mjs missing — skipping")
        return None
    if not (bridge_dir / "node_modules" / "@cursor" / "sdk").is_dir():
        print("  -- @cursor/sdk not installed — skipping bridge auth probe")
        return None
    req = json.dumps(
        {"id": "r1", "op": "run", "stream": True, "prompt": "hi", "model": "composer-2"}
    ) + "\n"
    report = _run_child(
        "cursor_bridge",
        command="node",
        args=[str(daemon)],
        env_overrides={"CURSOR_API_KEY": "diagnose-bogus-key-not-real"},
        cwd=str(bridge_dir),
        stdin_text=req,
        timeout=_HANDSHAKE_TIMEOUT,
    )
    real_id_error = False
    for line in str(report.get("stdout") or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("ev") == "error":
            tagged = obj.get("id")
            print(f"  error event id={tagged!r} error={obj.get('error')!r} status={obj.get('status')}")
            if tagged == "r1":
                real_id_error = True
            elif tagged == "?":
                print("  !! error tagged id:'?' — the BUG A regression (never reaches consumer)")
    print(f"  RESULT  : {'PASS' if real_id_error else 'FAIL'} (auth error carries real id)")
    return ("cursor_bridge", True, real_id_error)


def diagnose_bridge_dispose_symbol() -> tuple[str, bool, bool] | None:
    """Verify the bridge's Node runtime exposes Symbol.dispose AFTER loading lib.mjs.

    @cursor/sdk's local runtime uses `using` (Symbol.dispose), absent on Node <20.4.
    ``cursor_bridge/dispose-polyfill.mjs`` backfills it and is imported before the SDK
    in lib.mjs. If that regresses on Node 18 a real Cursor run returns an EMPTY
    response — but the bogus-key probe above CANNOT catch it (it 401s at auth, before
    the dispose path). This imports the REAL lib.mjs and asserts Symbol.dispose is
    defined, so a Node-18 CI leg turns a missing/broken polyfill into a red build."""
    _hr("Cursor bridge Node runtime (Symbol.dispose availability)")
    bridge_dir = _REPO_ROOT / "cursor_bridge"
    lib = bridge_dir / "lib.mjs"
    if not lib.is_file():
        print("  !! lib.mjs missing — skipping")
        return None
    if not (bridge_dir / "node_modules" / "@cursor" / "sdk").is_dir():
        print("  -- @cursor/sdk not installed — skipping dispose-symbol probe")
        return None
    lib_url = lib.resolve().as_uri()
    code = (
        f"import({json.dumps(lib_url)}).then(()=>{{"
        "process.stdout.write(JSON.stringify({"
        "node:process.version,dispose:typeof Symbol.dispose,"
        "asyncDispose:typeof Symbol.asyncDispose})+'\\n');process.exit(0);})"
        ".catch((e)=>{process.stderr.write(String(e&&e.stack||e)+'\\n');process.exit(2);})"
    )
    print(f"  lib     : {lib}")
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", code],
            cwd=str(bridge_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_HANDSHAKE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  !! node spawn failed: {exc}")
        return ("bridge_dispose_symbol", True, False)
    elapsed = (time.perf_counter() - started) * 1000
    if proc.stderr.strip():
        print(f"  stderr  :\n{_indent(proc.stderr)}")
    dispose_ok = False
    for line in str(proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        dispose_ok = obj.get("dispose") == "symbol" and obj.get("asyncDispose") == "symbol"
        print(
            f"  node    : {obj.get('node')}  Symbol.dispose={obj.get('dispose')}  "
            f"Symbol.asyncDispose={obj.get('asyncDispose')}"
        )
    print(f"  exit    : {proc.returncode}   elapsed: {elapsed:.0f}ms")
    if not dispose_ok:
        print(
            "  !! Symbol.dispose missing after loading lib.mjs — on Node <20.4 the SDK "
            "needs cursor_bridge/dispose-polyfill.mjs imported BEFORE @cursor/sdk, "
            "or a real Cursor run returns an EMPTY response."
        )
    print(f"  RESULT  : {'PASS' if dispose_ok else 'FAIL'} (Symbol.dispose defined for the bridge)")
    return ("bridge_dispose_symbol", True, dispose_ok)


def main() -> int:
    # This diagnostic prints captured child stderr (which contains Turkish text) and a
    # few arrows; the Windows CI console is cp1252, so force UTF-8 or our OWN prints
    # would raise UnicodeEncodeError before we ever report the child's failure.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass
    print(f"Platform: {sys.platform}   Python: {sys.version.split()[0]}   exe: {sys.executable}")
    with tempfile.TemporaryDirectory(prefix="akana-diag-") as td:
        data_dir = Path(td)
        results = diagnose_mcp_servers(data_dir)
        bridge = diagnose_cursor_bridge()
        if bridge is not None:
            results.append(bridge)
        dispose = diagnose_bridge_dispose_symbol()
        if dispose is not None:
            results.append(dispose)
    _hr("SUMMARY")
    failures = 0
    for name, required, passed in results:
        flag = "PASS" if passed else "FAIL"
        print(f"  [{flag}] {name}{'' if required else ' (optional)'}")
        if required and not passed:
            failures += 1
    print(f"\n{failures} required child(ren) failed.")
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
