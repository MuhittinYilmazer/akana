"""Start Akana server."""

from __future__ import annotations

import os

from akana_cli import i18n, io
from akana_cli.env_util import load_repo_dotenv, server_host_port
from akana_cli.paths import REPO_ROOT, default_data_dir, venv_exists, venv_python
from akana_cli.runner import run
from akana_cli.stop_cmd import find_pids_on_port


def _valid_provider(prov: str) -> str:
    """Return ``prov`` only if it is a recognized provider, else "" (unconfigured).

    Mirrors the server's ``resolve_provider`` sanitize step: a corrupt/legacy value
    like ``foo`` in llm_settings.json / LLM_PROVIDER is mapped to "" server-side and
    chat refuses. Without this the CLI would treat ``foo`` as truthy and suppress both
    the 'no provider configured' warning and the key hint while chat is actually dead.
    Guarded so a missing server package degrades to accepting the raw value."""
    if not prov:
        return ""
    try:
        from akana_server.llm_settings import _VALID_PROVIDERS

        return prov if prov in _VALID_PROVIDERS else ""
    except Exception:  # noqa: BLE001 — never let a server-package gap block `start`
        return prov


def _resolved_provider() -> str:
    """Active provider the SERVER will use: the persisted store (llm_settings.json) wins
    over .env — the app records provider switches there, so .env's LLM_PROVIDER goes stale.
    Guarded so a broken/absent server package never blocks `start`; falls back to env."""
    try:
        import json

        store = default_data_dir() / "llm_settings.json"
        if store.is_file():
            raw = json.loads(store.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                prov = _valid_provider(str(raw.get("provider") or "").strip().lower())
                if prov:
                    return prov
    except Exception:  # noqa: BLE001 — never let a store read stop the server from starting
        pass
    return _valid_provider(os.environ.get("LLM_PROVIDER", "").strip().lower())


def _key_present(key_env: str) -> bool:
    """True if a real (non-placeholder) key exists for `key_env`. Checks the runtime secret
    store FIRST (where the UI writes keys — the documented happy path keeps them out of .env),
    then env/.env. Guarded so a missing server package degrades to the env check."""
    try:
        from akana_server.secret_store import get_secret, is_real_secret

        if is_real_secret(get_secret(default_data_dir(), key_env)):
            return True
    except Exception:  # noqa: BLE001
        pass
    return bool(os.environ.get(key_env, "").strip())


def run_start() -> int:
    if not venv_exists():
        io.fail(i18n.t("doctor.venv_missing"))
        return 1
    load_repo_dotenv()
    # No provider is privileged as a default — start the server regardless and let the
    # user pick one in Settings. Hint if unconfigured, else a provider-aware credential hint.
    # Resolve against the SAME sources the server does (persisted store + secret store), not
    # just .env, so the documented happy path (setup → paste key in the UI) doesn't trigger a
    # false "not configured" warning.
    _provider = _resolved_provider()
    if not _provider:
        io.warn(i18n.t("start.no_provider"))
    else:
        # Provider-aware credential hint (key-based providers only; claude/ollama have none).
        from akana_cli.components import provider_key_envs

        _key_env = provider_key_envs().get(_provider)
        if _key_env and not _key_present(_key_env):
            io.warn(i18n.t("start.key_missing", key=_key_env, provider=_provider))

    py = venv_python()
    env = os.environ.copy()
    repo = str(REPO_ROOT)
    env["PYTHONPATH"] = repo + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

    host, port = server_host_port()
    if find_pids_on_port(port, host):
        io.fail(i18n.t("start.port_in_use", host=host, port=port))
        print("  " + i18n.t("start.stop_hint"))
        return 1

    io.step(i18n.t("start.starting", host=host, port=port))
    print("  " + i18n.t("start.ctrl_c"))
    print()
    import sys

    cmd = [str(py), "-m", "akana_server.main"]
    if sys.platform == "win32":
        # os.execve does NOT replace the current process on Windows — the launching
        # shell would return immediately while a detached child runs, so Ctrl+C and the
        # "press Ctrl+C to stop" hint become misleading. Run it as a child and wait.
        cp = run(cmd, cwd=REPO_ROOT, env=env, check=False)
        rc = cp.returncode or 0
        # Windows Ctrl+C delivers STATUS_CONTROL_C_EXIT (0xC000013A) to the child — that is
        # a clean stop, not a failure (mirrors the POSIX KeyboardInterrupt path).
        if rc in (0, 0xC000013A) or (rc & 0xFFFFFFFF) == 0xC000013A:
            return 0
        return rc
    try:
        os.execve(str(py), cmd, env)
    except OSError:
        # exec failed — fallback subprocess (should not happen on POSIX)
        run(cmd, cwd=REPO_ROOT, env=env)
    return 0
