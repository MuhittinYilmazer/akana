"""Pre-flight checks."""

from __future__ import annotations

import os
import shutil
import socket

from akana_cli import i18n, io
from akana_cli.env_util import read_env_key
from akana_cli.paths import (
    BRIDGE_DIR,
    ENV_FILE,
    default_data_dir,
    find_system_python,
    venv_exists,
    venv_python,
)


def _port_free(host: str, port: int) -> bool:
    # Resolve the host so an IPv6 address/name (::1, a v6-resolving "localhost") uses
    # AF_INET6 — a hardcoded AF_INET would raise OSError on bind and falsely report
    # the port "in use".
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (host, port))]
    for family, socktype, proto, _canon, sockaddr in infos:
        try:
            with socket.socket(family, socktype, proto) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(sockaddr)
            return True
        except OSError:
            continue
    return False


def run_doctor(*, verbose: bool = True, probe_network: bool = True, mcp: bool = False) -> int:
    issues = 0
    if verbose:
        io.banner(i18n.t("doctor.title"))

    provider = (read_env_key("LLM_PROVIDER") or "").strip().lower()

    py_sys = find_system_python()
    if py_sys:
        if verbose:
            io.ok(i18n.t("doctor.py_sys", path=py_sys))
    else:
        io.fail(i18n.t("doctor.py_missing"))
        issues += 1

    if venv_exists():
        if verbose:
            io.ok(i18n.t("doctor.venv", path=venv_python()))
    else:
        io.fail(i18n.t("doctor.venv_missing"))
        issues += 1

    # No provider is privileged as a default — an unset LLM_PROVIDER is "unconfigured".
    # Chat won't work until the user picks one, so flag it (provider-neutral message).
    if not provider:
        # Soft state, NOT a hard failure: a fresh install legitimately has no provider
        # yet — the user picks one in Settings / `python akana.py add`. It's shown as a
        # ⚠ warning, so counting it as a critical issue was inconsistent (⚠ icon but
        # tallied ✗) and made `doctor`/`smoke` exit non-zero on every clean install.
        io.warn(i18n.t("doctor.no_provider"))

    # Node + the Cursor bridge are REQUIRED only for the cursor provider; for the
    # other providers a missing Node is not a problem.
    node_ok = bool(shutil.which("node") and shutil.which("npm"))
    bridge_ok = (BRIDGE_DIR / "node_modules" / "@cursor" / "sdk").is_dir()
    if provider == "cursor":
        if node_ok:
            if verbose:
                io.ok(i18n.t("doctor.node_npm"))
        else:
            # io.fail (✗), not io.warn (⚠): a missing Node genuinely breaks the
            # active cursor provider AND increments issues, so the glyph should
            # match the tally the summary is about to print in red.
            io.fail(i18n.t("doctor.node_missing_cursor"))
            issues += 1
        if bridge_ok:
            if verbose:
                io.ok(i18n.t("doctor.bridge_ok"))
        else:
            io.fail(i18n.t("doctor.bridge_missing"))
            issues += 1
    elif provider and verbose:
        io.ok(
            i18n.t("doctor.node_present")
            if node_ok
            else i18n.t("doctor.node_not_required", provider=provider)
        )

    if not ENV_FILE.is_file():
        io.fail(i18n.t("doctor.env_missing"))
        issues += 1
    else:
        # Provider-aware credential check — only the CHOSEN provider's credential
        # matters (a claude/ollama user has no CURSOR_API_KEY, and that's fine).
        from akana_cli.components import REGISTRY, deps_installed, provider_key_envs

        # For a pip-installer provider (currently gemini; any future pip provider
        # too), the credentials being fine is NOT enough — the package the server
        # will `import` at runtime must also be present in the VENV. Without this
        # probe a fresh user who picks gemini + real key sees green doctor, then
        # every chat raises LLMCallError(503) because google.genai isn't installed.
        # Mirrors the cursor node/bridge check above so the honesty is symmetric.
        _pc = REGISTRY.get(provider) if provider else None
        if _pc is not None and _pc.installer == "pip" and _pc.modules and not deps_installed(_pc):
            io.fail(
                i18n.t("doctor.provider_pkg_missing", provider=provider)
                + i18n.t("doctor.add_hint", id=_pc.id)
            )
            issues += 1

        key_env = provider_key_envs().get(provider)
        if key_env:
            # Also treat a shipped ACTIVE placeholder as "not set": .env.example used
            # to carry an active CURSOR_API_KEY=your-…-here that fooled raw truthiness
            # into "configured", so fresh installs got a silent 401 on the first
            # chat. Mirror the server's is_real_secret gate via looks_like_placeholder.
            _kv = (read_env_key(key_env) or "").strip()
            from akana_server.secret_store import looks_like_placeholder

            _key_ok = bool(_kv) and not looks_like_placeholder(_kv)
            if _key_ok:
                if verbose:
                    io.ok(i18n.t("doctor.key_defined", key=key_env))
                if provider == "cursor" and probe_network:
                    try:
                        from akana_server.config import load_settings
                        from akana_server.orchestrator.cursor_catalog import (
                            probe_cursor_api_sync,
                        )

                        probe = probe_cursor_api_sync(load_settings())
                        if probe.get("reachable"):
                            if verbose:
                                n = probe.get("model_count", 0)
                                io.ok(i18n.t("doctor.cursor_reachable", n=n))
                        else:
                            io.warn(
                                i18n.t(
                                    "doctor.cursor_unreachable",
                                    err=probe.get("error") or "unknown error",
                                )
                            )
                    except Exception as exc:  # pragma: no cover - doctor best-effort
                        io.warn(i18n.t("doctor.cursor_check_skipped", err=exc))
            else:
                io.fail(i18n.t("doctor.key_empty", key=key_env, provider=provider))
                issues += 1
        elif provider == "claude":
            if shutil.which("claude"):
                if verbose:
                    io.ok(i18n.t("doctor.claude_found"))
            else:
                # Genuinely breaks claude chat AND increments issues → ✗, not ⚠.
                io.fail(i18n.t("doctor.claude_missing"))
                issues += 1
        elif provider == "ollama":
            if verbose:
                io.ok(i18n.t("doctor.ollama"))
        elif provider and verbose:
            io.ok(i18n.t("doctor.provider_generic", provider=provider))

    from akana_server.config import LEGACY_ENV_PREFIX

    # Shell env wins over .env — same precedence start_cmd.py uses when it binds
    # the server (via server_host_port). Reading only the .env file made doctor's
    # port check disagree with the address the server actually listens on when
    # a user set AKANA_PORT/HOST in their shell.
    host = (
        os.environ.get("AKANA_HOST")
        or os.environ.get(LEGACY_ENV_PREFIX + "HOST")
        or read_env_key("AKANA_HOST")
        or read_env_key(LEGACY_ENV_PREFIX + "HOST")
        or "127.0.0.1"
    )
    port_s = (
        os.environ.get("AKANA_PORT")
        or os.environ.get(LEGACY_ENV_PREFIX + "PORT")
        or read_env_key("AKANA_PORT")
        or read_env_key(LEGACY_ENV_PREFIX + "PORT")
        or "8766"
    )
    try:
        port = int(port_s)
    except ValueError:
        port = 8766
    if _port_free(host, port):
        if verbose:
            io.ok(i18n.t("doctor.port_ok", host=host, port=port))
    else:
        io.warn(i18n.t("doctor.port_in_use", host=host, port=port))

    data = default_data_dir()
    if data.is_dir():
        if verbose:
            io.ok(i18n.t("doctor.data_dir", path=data))
    else:
        io.warn(i18n.t("doctor.data_dir_missing", path=data))

    if venv_exists():
        vpy = venv_python()
        for label, mod, why in (
            ("fastapi", "fastapi", "API"),
            ("piper", "piper", "voice"),
            ("faster-whisper", "faster_whisper", "voice"),
            ("openwakeword", "openwakeword", "voice"),
            ("fastembed", "fastembed", "semantic vector recall"),
        ):
            code = f"import importlib.util; raise SystemExit(0 if importlib.util.find_spec({mod!r}) else 1)"
            try:
                import subprocess

                r = subprocess.run([str(vpy), "-c", code], capture_output=True, timeout=15)
                if r.returncode == 0 and verbose:
                    io.ok(label)
                elif r.returncode != 0 and verbose:
                    from akana_cli.components import component_for_module

                    comp = component_for_module(mod)
                    hint = i18n.t("doctor.add_hint", id=comp.id) if comp else ""
                    why_label = {
                        "voice": i18n.t("doctor.why_voice"),
                        "semantic vector recall": i18n.t("doctor.why_vector"),
                    }.get(why, why)
                    io.warn(i18n.t("doctor.optional_missing", label=label, why=why_label) + hint)
            except (OSError, subprocess.SubprocessError):
                pass

    # `--mcp` is additive: keep every check above, then ALSO spawn the real MCP /
    # Cursor-bridge stdio children and run their JSON-RPC handshake. Imports stay inside
    # this branch so a normal `doctor` run never pays for them. `scripts/` is not an
    # installed package, so put it on sys.path first.
    if mcp:
        import sys
        import tempfile
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import diagnose_mcp

        print()
        io.banner(i18n.t("doctor.mcp_banner"))
        with tempfile.TemporaryDirectory(prefix="akana-doctor-mcp-") as td:
            results = diagnose_mcp.diagnose_mcp_servers(Path(td))
            bridge = diagnose_mcp.diagnose_cursor_bridge()
            dispose = diagnose_mcp.diagnose_bridge_dispose_symbol()
        if bridge is not None:
            results.append(bridge)
        if dispose is not None:
            results.append(dispose)
        mcp_failures = sum(
            1 for _name, required, passed in results if required and not passed
        )
        if mcp_failures:
            io.fail(i18n.t("doctor.mcp_failed", n=mcp_failures))
        else:
            io.ok(i18n.t("doctor.mcp_ok"))
        issues += mcp_failures

    if verbose:
        print()
        if issues:
            io.fail(i18n.t("doctor.issues", n=issues))
        else:
            io.ok(i18n.t("doctor.ready"))
        print()
    return 1 if issues else 0
