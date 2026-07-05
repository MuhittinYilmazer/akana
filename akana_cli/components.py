"""Installable components — the single source of truth for what `setup`/`add` offer.

Both providers (cursor, claude, gemini, openai, ollama) and optional extras (voice,
embeddings) live here, so the interactive menus in `setup` and the standalone `add`
command never drift, and a user who switches providers later can install the new
provider's requirements the same way they installed the first.

The module stays dependency-light: it imports nothing from the rest of the CLI at
load time, so `main.py` can build its argument parser from REGISTRY cheaply.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Component:
    """One installable capability.

    installer picks how it is installed and how "installed?" is probed:
      • "pip"        — `requirements` files; presence proven by importing `modules`.
      • "npm_bridge" — the in-repo Node Cursor bridge (`npm install` in cursor_bridge);
                       present when node_modules/@cursor/sdk exists.
      • "npm_global" — a global npm CLI (`npm_package`); present when `probe_bin` is on PATH.
      • "external"   — installed by the user out-of-band (e.g. the Ollama app); present
                       when `probe_bin` is on PATH. We only print `install_hint`.
      • "none"       — nothing to download (e.g. OpenAI uses core httpx); always present.

    key_env/key_url: the provider API key written to .env, if any.
    note: a one-line reality check printed after install (download sizes, etc.).
    """

    id: str
    label: str
    kind: str = "extra"  # "provider" | "extra"
    installer: str = "pip"
    requirements: tuple[str, ...] = ()
    modules: tuple[str, ...] = ()
    probe_bin: str = ""
    npm_package: str = ""
    key_env: str = ""
    key_url: str = ""
    install_hint: str = ""
    note: str = ""


#: id → Component. Order is the menu order: providers first, then extras.
REGISTRY: dict[str, Component] = {
    # ── providers ────────────────────────────────────────────────────────────
    "cursor": Component(
        id="cursor",
        label="Cursor — wide model catalog (Node bridge + API key)",
        kind="provider",
        installer="npm_bridge",
        key_env="CURSOR_API_KEY",
        key_url="https://cursor.com/dashboard/integrations",
        install_hint="Cursor needs Node.js + npm for the @cursor/sdk bridge.",
    ),
    "claude": Component(
        id="claude",
        label="Claude — claude-code CLI + subscription",
        kind="provider",
        installer="npm_global",
        npm_package="@anthropic-ai/claude-code",
        probe_bin="claude",
        install_hint="After install, run `claude` once to log in.",
    ),
    "gemini": Component(
        id="gemini",
        label="Gemini — Google API key",
        kind="provider",
        installer="pip",
        requirements=("requirements-gemini.txt",),
        modules=("google.genai",),
        key_env="GEMINI_API_KEY",
        key_url="https://aistudio.google.com/apikey",
    ),
    "openai": Component(
        id="openai",
        label="OpenAI — API key (no extra package)",
        kind="provider",
        installer="none",
        key_env="OPENAI_API_KEY",
        key_url="https://platform.openai.com/api-keys",
    ),
    "ollama": Component(
        id="ollama",
        label="Ollama — local models, no key",
        kind="provider",
        installer="external",
        probe_bin="ollama",
        install_hint="Install from https://ollama.com, run `ollama serve`, then `ollama pull <model>`.",
    ),
    # ── optional extras ──────────────────────────────────────────────────────
    "embeddings": Component(
        id="embeddings",
        label="Semantic memory recall (fastembed ONNX, ~220 MB, no GPU)",
        installer="pip",
        requirements=("requirements-vector.txt",),
        modules=("fastembed",),
        note="The embedding model downloads on first recall (~220 MB).",
    ),
    "voice-piper": Component(
        id="voice-piper",
        label="Piper TTS — offline speech output",
        installer="pip",
        requirements=("requirements-piper.txt",),
        modules=("piper",),
    ),
    "voice-full": Component(
        id="voice-full",
        label="Full voice — Piper TTS + Whisper STT + 'Hey Akana' wake word",
        installer="pip",
        requirements=("requirements-voice.txt", "requirements-piper.txt"),
        modules=("piper", "faster_whisper", "openwakeword"),
    ),
    "xtts": Component(
        id="xtts",
        label="XTTS-v2 — high-quality local TTS (TR + voice cloning), heavy",
        installer="pip",
        requirements=("requirements-xtts.txt",),
        modules=("TTS", "torch"),
        note=(
            "XTTS needs PyTorch (the CUDA wheel installs separately); the model "
            "downloads on first synthesis (~2 GB)."
        ),
    ),
}


def providers() -> list[Component]:
    return [c for c in REGISTRY.values() if c.kind == "provider"]


def extras() -> list[Component]:
    return [c for c in REGISTRY.values() if c.kind == "extra"]


def _module_present(name: str) -> bool:
    """True if `name` is importable in the CURRENT interpreter."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _modules_present_in_venv(modules: tuple[str, ...]) -> bool:
    """True when ALL `modules` import in the VENV interpreter — the one the server runs.

    ``deps_installed`` must reflect what the RUNNING SERVER can import, and the server
    is ALWAYS the venv python (``akana.py start`` → venv). The interpreter running the
    CLI is not: on FIRST setup the venv does not exist yet at ``akana.py`` launch, so
    the launcher runs under SYSTEM python. System/user-site often has packages the venv
    cannot see (e.g. a ``pip install`` that landed in user-site on a PIP_USER machine),
    so an in-process ``find_spec`` would report "installed" when the venv — and thus the
    server — sees nothing; setup then SKIPS the venv install and recall silently
    degrades. Probing the venv python keeps setup's view consistent with runtime.

    Fast path: when we ARE the venv python (the normal case — ``akana.py`` re-execs into
    the venv once it exists), the in-process check is correct and no subprocess is spawned.
    """
    if not modules:
        return True
    from akana_cli.paths import venv_exists, venv_python

    if not venv_exists():
        return all(_module_present(m) for m in modules)
    vpy = str(venv_python())
    try:
        same = os.path.samefile(vpy, sys.executable)
    except OSError:
        same = os.path.normcase(os.path.realpath(vpy)) == os.path.normcase(
            os.path.realpath(sys.executable)
        )
    if same:
        return all(_module_present(m) for m in modules)
    code = (
        "import importlib.util as u, sys\n"
        "mods = %r\n"
        "res = []\n"
        "for m in mods:\n"
        "    try: res.append(u.find_spec(m) is not None)\n"
        "    except Exception: res.append(False)\n"
        "sys.exit(0 if all(res) else 1)\n"
    ) % (list(modules),)
    try:
        return (
            subprocess.run([vpy, "-c", code], capture_output=True, timeout=30).returncode == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def deps_installed(comp: Component) -> bool:
    """True when the component's downloadable requirements are present.

    The check is installer-specific (Python modules in the VENV, the Cursor bridge dir,
    or a CLI on PATH). "none" installers (nothing to download) are always True.
    """
    if comp.installer == "pip":
        return _modules_present_in_venv(comp.modules)
    if comp.installer == "npm_bridge":
        from akana_cli.paths import BRIDGE_DIR

        return (BRIDGE_DIR / "node_modules" / "@cursor" / "sdk").is_dir()
    if comp.installer in ("npm_global", "external"):
        return bool(comp.probe_bin) and shutil.which(comp.probe_bin) is not None
    return True  # "none"


def key_configured(comp: Component) -> bool:
    """True when the component needs no key, or its key is present (uncommented) in
    .env. Imported lazily to keep this module dependency-light at load time."""
    if not comp.key_env:
        return True
    from akana_cli.env_util import read_env_key

    val = (read_env_key(comp.key_env) or "").strip()
    if not val or val.startswith("#"):
        return False
    # Mirror the server's is_real_secret() gate: .env.example ships an ACTIVE
    # placeholder (e.g. CURSOR_API_KEY=your-cursor-api-key-here) that must not
    # report as configured here while the server treats it as unset.
    from akana_server.secret_store import looks_like_placeholder

    return not looks_like_placeholder(val)


def is_ready(comp: Component) -> bool:
    """Fully usable: requirements present AND (if a provider) its key is configured."""
    return deps_installed(comp) and key_configured(comp)


def component_for_module(module: str) -> Component | None:
    """Reverse lookup: which component provides `module`? (doctor "add" hints)."""
    for comp in REGISTRY.values():
        if module in comp.modules:
            return comp
    return None


def provider_key_envs() -> dict[str, str]:
    """provider id -> its .env API-key variable, for providers that need one.

    Single source of truth (REGISTRY.key_env) instead of a copy-pasted dict —
    keyless providers (claude, ollama) are simply absent from the result.
    """
    return {c.id: c.key_env for c in providers() if c.key_env}
