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


def _resolved_provider() -> str:
    """Active provider the SERVER will use: the persisted store (llm_settings.json) wins
    over .env — the app records provider switches there, so .env's LLM_PROVIDER goes stale.
    Mirrors start_cmd._resolved_provider and the server's resolve_provider so every
    provider-conditional check below keys off the SAME provider the server would run.
    Guarded so a broken/absent server package degrades to the .env/env value."""
    from akana_cli.start_cmd import _valid_provider

    try:
        import json

        store = default_data_dir() / "llm_settings.json"
        if store.is_file():
            raw = json.loads(store.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                prov = _valid_provider(str(raw.get("provider") or "").strip().lower())
                if prov:
                    return prov
    except Exception:  # noqa: BLE001 — never let a store read break doctor
        pass
    # .env / shell env fallback (shell wins, same precedence as the port check below).
    env_val = os.environ.get("LLM_PROVIDER") or read_env_key("LLM_PROVIDER") or ""
    return _valid_provider(env_val.strip().lower())


def _stored_key(key_env: str) -> str | None:
    """Resolved real key for `key_env`, or None. Checks the runtime secret store FIRST
    (the documented happy path pastes keys in the UI, which writes them there, NOT .env),
    then .env/env. The store is keyed by the lowercase ALLOWED_KEYS name, so normalise
    the uppercase env-var name before the lookup. A shipped placeholder counts as unset.
    The .env/env leg gates on looks_like_placeholder (no length floor) — the same rule
    the server runtime applies to env keys — so doctor and server agree on what "set"
    means; the write-path length floor (is_real_secret) applies to the store leg only."""
    try:
        from akana_server.secret_store import get_secret, is_real_secret

        stored = get_secret(default_data_dir(), key_env.lower())
        if is_real_secret(stored):
            return stored
    except Exception:  # noqa: BLE001 — a missing server package degrades to .env/env
        pass
    for raw in (read_env_key(key_env), os.environ.get(key_env)):
        val = (raw or "").strip()
        if val and not _is_placeholder(val):
            return val
    return None


def _is_placeholder(value: str) -> bool:
    """Server's looks_like_placeholder when importable; a marker-scan fallback so an
    unimportable server package still lets doctor resolve .env/env keys (degrade path)."""
    try:
        from akana_server.secret_store import looks_like_placeholder

        return looks_like_placeholder(value)
    except Exception:  # noqa: BLE001 — degrade: mirror _PLACEHOLDER_MARKERS' core set
        low = value.lower()
        return any(m in low for m in ("your-", "-here", "changeme", "change-me", "replace-me", "replace_me"))


def run_doctor(*, verbose: bool = True, probe_network: bool = True, mcp: bool = False) -> int:
    issues = 0
    if verbose:
        io.banner(i18n.t("doctor.title"))

    provider = _resolved_provider()

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
            # Resolve the key the SAME way the server does: the runtime secret store
            # (where the UI writes keys — the documented happy path keeps them out of
            # .env) wins, then .env/env. A shipped ACTIVE placeholder (e.g. the old
            # .env.example CURSOR_API_KEY=your-…-here) still counts as unset via the
            # is_real_secret gate inside _stored_key. Reading .env ALONE here declared a
            # UI-configured key "empty" and skipped the store-aware probe below.
            _key_ok = _stored_key(key_env) is not None
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
