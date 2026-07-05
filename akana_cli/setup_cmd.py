"""Interactive setup (Windows + Linux)."""

from __future__ import annotations

import os
import re
import shutil
import sys

from akana_cli import i18n, io
from akana_cli.paths import (
    BRIDGE_DIR,
    ENV_EXAMPLE,
    ENV_FILE,
    REPO_ROOT,
    VENV_DIR,
    find_system_python,
    venv_exists,
    venv_python,
)
from akana_cli.runner import npm_base, run, run_progress, run_quiet


def _write_env_key(key: str, value: str) -> None:
    from akana_cli.env_util import _read_env_text

    lines: list[str] = []
    if ENV_FILE.is_file():
        # utf-8-sig so a BOM'd file (some Windows editors / PS5.1 `Out-File -Encoding utf8`)
        # doesn't hide its first key behind U+FEFF — otherwise the key-match regex misses it
        # and we'd append a duplicate. Written back below as plain utf-8 (BOM dropped).
        lines = _read_env_text().splitlines()
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=")
    out: list[str] = []
    replaced = False
    for line in lines:
        if pat.match(line):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")


def _ensure_env_file() -> bool:
    """Create .env from .env.example if missing — called BEFORE pip install so the
    data-dir bootstrap in _pip_install_core reads the user's AKANA_DATA_DIR if set."""
    if ENV_FILE.is_file():
        return True
    if ENV_EXAMPLE.is_file():
        shutil.copy(ENV_EXAMPLE, ENV_FILE)
        io.ok(i18n.t("setup.env_created"))
        return True
    io.warn(i18n.t("setup.env_example_missing"))
    return False


def _warn_provider_unconfigured() -> None:
    """If the chosen provider relies on an API key and it's empty, say so clearly."""
    from akana_cli.components import provider_key_envs
    from akana_cli.env_util import read_env_key

    provider = (read_env_key("LLM_PROVIDER") or "").strip().lower()
    if not provider:
        return  # unconfigured — the provider step already prompts the user to pick one
    key_env = provider_key_envs().get(provider)
    if not key_env:
        return  # claude (CLI) / ollama (local) have no .env key to check
    val = (read_env_key(key_env) or "").strip()
    if not val or val.startswith("#"):
        print()
        io.warn(i18n.t("setup.provider_unconfigured", key=key_env, provider=provider))


def _python_missing_hint() -> None:
    """No 3.11+ on PATH — say so AND show how to get it for this OS (the launcher
    can't proceed without it, so a bare error would just dead-end the user)."""
    io.fail(i18n.t("setup.py_required"))
    print()
    if sys.platform == "darwin":
        print("  macOS (Homebrew):  brew install python@3.12")
        print(f"  {i18n.t('setup.py_hint_download')}")
    elif sys.platform.startswith("linux"):
        print("  Debian/Ubuntu:  sudo apt install -y python3 python3-venv python3-pip")
        print("  Fedora/RHEL:    sudo dnf install -y python3 python3-pip")
        print("  Arch:           sudo pacman -S --needed python python-pip")
    elif sys.platform == "win32":
        print("  winget:  winget install Python.Python.3.12")
        print(f"  {i18n.t('setup.py_hint_download_win')}")
    else:
        print("  Download Python 3.11+ from https://www.python.org/downloads/")
    print()


def _create_venv(python_exe: str) -> None:
    io.step(i18n.t("setup.venv_creating", py=python_exe))
    cp = run([python_exe, "-m", "venv", str(VENV_DIR)], check=False)
    if cp.returncode != 0:
        # `python -m venv` exits non-zero on Debian/Ubuntu without the separately-packaged
        # python3-venv — give the actionable hint instead of a raw CalledProcessError.
        io.fail(i18n.t("setup.venv_failed"))
        raise SystemExit(1)


def _repair_venv() -> None:
    """`--repair`: delete the venv so it is rebuilt from scratch. For the case where
    the venv is corrupt (half-built, wrong Python, missing pip) and re-running plain
    setup can't fix it. Only the regenerable venv/ dir is removed — never user data."""
    if not VENV_DIR.exists():
        return
    io.step(i18n.t("setup.venv_repair"))
    try:
        shutil.rmtree(VENV_DIR)
    except OSError as exc:
        io.warn(i18n.t("setup.venv_repair_failed", path=VENV_DIR, err=exc))


def _pip_cmd() -> list[str]:
    """`python -m pip` — NOT the venv's pip SCRIPT. The bin/pip wrapper can be absent
    (a venv built --without-pip, a system that strips it, or a pip upgrade that drops
    the old shim) even when the pip MODULE is importable; `python -m pip` works in
    every case.

    ``--isolated`` makes pip ignore environment variables AND user/site config files
    (pip.conf / pip.ini), so a stray ``PIP_INDEX_URL``, ``PIP_TARGET``, or a corporate
    pip.ini on the host can't redirect the install into the wrong index or directory.
    It's a general option, so it must precede the subcommand (``pip --isolated install``)."""
    return [str(venv_python()), "-m", "pip", "--isolated"]


def _pip_env() -> dict[str, str]:
    """Environment for pip subprocesses with every ``PIP_*`` variable stripped.

    ``_pip_cmd()`` already passes ``--isolated`` (which tells pip to ignore env vars),
    so this is belt-and-suspenders: it makes the intent explicit and guards against a
    future pip that honours some env var even under ``--isolated``. A full copy of the
    environment is returned (minus ``PIP_*``) so the child still inherits PATH/HOME/etc."""
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("PIP_")}


def _ensure_pip() -> None:
    """Bootstrap pip into the venv when `python -m pip` is unavailable (ensurepip)."""
    if run_quiet([*_pip_cmd(), "--version"], env=_pip_env()):
        return
    io.step(i18n.t("setup.pip_bootstrap"))
    try:
        run([str(venv_python()), "-m", "ensurepip", "--upgrade"])
    except Exception:
        io.warn(i18n.t("setup.pip_bootstrap_failed"))


def _read_requirement_names(req_file: str) -> list[str]:
    """Top-level package names from a requirements file (for the 'installing …' plan).

    Skips comments, blank lines, and ``-r`` includes; strips version specifiers/extras
    so the plan reads as clean package names (fastapi, uvicorn, …)."""
    path = REPO_ROOT / req_file
    if not path.is_file():
        return []
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("-"):
            continue
        name = re.split(r"[=<>!~;\[ ]", s, maxsplit=1)[0].strip()
        if name:
            names.append(name)
    return names


def _pip_substatus(line: str) -> str | None:
    """Parse ONE pip output line into a short live phrase (or None to ignore it).

    Turns pip's raw "Collecting/Downloading/Building/Installing" chatter into a calm
    "downloading numpy" / "installing …" the spinner can show — so the user watches
    real progress instead of a blind timer. Noise ("Requirement already satisfied",
    hashes, "Using cached") is intentionally dropped."""
    s = line.strip()
    if s.startswith("Collecting "):
        raw = s.split(None, 1)[1]
        name = re.split(r"[=<>!~;\[ ]", raw, maxsplit=1)[0]
        return i18n.t("setup.pkg_downloading", pkg=name)
    if s.startswith("Downloading "):
        fn = s.split(None, 2)[1]  # e.g. fastapi-0.111.0-py3-none-any.whl
        return i18n.t("setup.pkg_downloading", pkg=fn.split("-")[0])
    if s.startswith("Building wheel for "):
        return i18n.t("setup.pkg_installing", pkg=s[len("Building wheel for "):].split(" ")[0])
    if s.startswith("Installing collected packages"):
        rest = s.split(":", 1)[1].strip() if ":" in s else ""
        return i18n.t("setup.pkg_installing", pkg=rest.split(",")[0].strip() or "…")
    return None


def _show_pkg_plan(req_file: str) -> None:
    """Tell the user WHAT is about to be installed (count + the first several names)."""
    names = _read_requirement_names(req_file)
    if not names:
        return
    shown = ", ".join(names[:8])
    if len(names) > 8:
        shown += " " + i18n.t("setup.pkg_plan_more", n=len(names) - 8)
    io.step(i18n.t("setup.pkg_plan", n=len(names), names=shown))


def _pip_install_core() -> bool:
    """Core deps + data dirs. Providers and optional extras (voice, embeddings) are
    installed interactively afterwards via the component registry — see
    _offer_components()."""
    import time

    py = str(venv_python())
    _ensure_pip()
    pip_env = _pip_env()
    # pip upgrade is quick + noisy → keep it quiet (-q); the spinner shows it working.
    run_progress(
        [*_pip_cmd(), "install", "-q", "--upgrade", "pip"], i18n.t("setup.pip_upgrade"), env=pip_env
    )
    # Akana is clone-and-run (not pip-installable); deps live in requirements-*.txt.
    # requirements-dev.txt pulls in requirements.txt (core) plus the test tools.
    # NB: no -q here — we WANT pip's per-package chatter so _pip_substatus can surface a
    # live "downloading X" next to the spinner (full output is still captured for errors).
    _show_pkg_plan("requirements.txt")
    start = time.time()
    ok, out = run_progress(
        [*_pip_cmd(), "install", "-r", "requirements-dev.txt"],
        i18n.t("setup.core_installing"),
        cwd=REPO_ROOT,
        env=pip_env,
        substatus=_pip_substatus,
    )
    if not ok:
        ok, out = run_progress(
            [*_pip_cmd(), "install", "-r", "requirements.txt"],
            i18n.t("setup.core_installing"),
            cwd=REPO_ROOT,
            env=pip_env,
            substatus=_pip_substatus,
        )
    if not ok:
        io.fail(i18n.t("setup.core_failed"))
        if out:
            print(out.rstrip())
        return False  # core is required; doctor will flag the rest — don't crash setup
    io.ok(i18n.t("setup.done_in", label=i18n.t("setup.core_label"), secs=int(time.time() - start)))
    io.step(i18n.t("setup.data_dirs"))
    # Non-fatal: the server also calls ensure_data_dirs() on startup, so a transient
    # failure here must NOT abort setup with a traceback (only KeyboardInterrupt is
    # caught upstream). Warn and move on; doctor/start will create the dirs.
    made = run_quiet(
        [
            py,
            "-c",
            "from akana_server.config import load_settings, ensure_data_dirs; "
            "s=load_settings(); ensure_data_dirs(s.data_dir)",
        ],
        cwd=REPO_ROOT,
    )
    if not made:
        io.warn(i18n.t("setup.data_dirs_skipped"))
    return True


def _pip_install_extra(req_file: str, label: str) -> None:
    """Install one optional requirements-*.txt (a provider or voice extra)."""
    _show_pkg_plan(req_file)
    ok, out = run_progress(
        [*_pip_cmd(), "install", "-r", req_file],
        i18n.t("setup.extra_installing", label=label),
        cwd=REPO_ROOT,
        env=_pip_env(),
        substatus=_pip_substatus,
    )
    if ok:
        io.ok(i18n.t("setup.extra_ready", label=label))
    else:
        io.warn(i18n.t("setup.extra_failed", label=label))
        if out:
            print(out.rstrip())


def _npm_bridge() -> None:
    if not shutil.which("node") or not shutil.which("npm"):
        io.warn(i18n.t("setup.bridge_no_node"))
        return
    # `npm ci` installs exactly what package-lock.json pins (fails loudly on drift),
    # so a fresh clone gets the same @cursor/sdk build the repo was tested against
    # instead of whatever `install` happens to resolve from the ^ range. Requires the
    # committed package-lock.json — which cursor_bridge ships. --no-fund/--no-audit
    # trim funding + audit noise; errors still surface.
    ok, out = run_progress(
        [*npm_base(), "ci", "--omit=dev", "--no-fund", "--no-audit"],
        i18n.t("setup.bridge_installing"),
        cwd=BRIDGE_DIR,
    )
    if ok:
        io.ok(i18n.t("setup.bridge_ready"))
    else:
        io.warn(i18n.t("setup.bridge_failed"))
        if out:
            print(out.rstrip())


def _select_and_install() -> list[str]:
    """Step 1 — pick providers + add-ons from ONE checklist (nothing installed yet).
    Step 2 — install the chosen ones as a batch with visible per-item progress.

    Returns the chosen ids so the caller can pick the active provider afterwards (API
    keys are entered in the Akana UI, not in this wizard). Re-running setup later is how
    a user adds a new provider's requirements (the checklist shows what's installed)."""
    from akana_cli.add_cmd import install_component
    from akana_cli.components import REGISTRY, deps_installed, extras, providers

    items = providers() + extras()
    choices = {
        c.id: i18n.t("comp." + c.id, default=c.label)
        + (f"   [{i18n.t('setup.installed_tag')}]" if deps_installed(c) else "")
        for c in items
    }
    picks = io.ask_checklist(
        i18n.t("setup.choose_prompt"),
        choices,
        preselected=set(),
    )
    pending = [REGISTRY[c] for c in picks if not deps_installed(REGISTRY[c])]
    if not pending:
        io.ok(i18n.t("setup.all_selected_present"))
        return picks
    total = len(pending)
    print()
    io.step(i18n.t("setup.installing_n", n=total))
    failed: set[str] = set()
    for idx, comp in enumerate(pending, 1):
        print()
        print(f"  [{idx}/{total}] {i18n.t('comp.' + comp.id, default=comp.label)}")
        # Voice extras prompt for WHICH Piper voices + Whisper size, so first-run setup
        # customizes exactly like `add` does (we are already in the interactive branch).
        # Providers/embeddings stay non-interactive: their keys are configured after the
        # batch (in the UI) and their models download on first use — nothing to pick here.
        voice_interactive = comp.id in ("voice-piper", "voice-full")
        if not install_component(comp, interactive=voice_interactive):
            failed.add(comp.id)
    if failed:
        # A failed provider must NOT become the active default (its first chat would
        # fail silently). Drop failures from the returned picks so the caller only ever
        # selects a provider that actually installed; tell the user how to retry.
        print()
        for cid in failed:
            io.warn(i18n.t("setup.install_errored", id=cid))
        picks = [c for c in picks if c not in failed]
    return picks


def _configure_after_install(picks: list[str]) -> None:
    """After the batch install: pick the default active provider.

    Provider API keys are NOT collected here — you enter them in the Akana UI after
    launch (Settings → Identity). Keeping secrets out of the terminal wizard makes setup
    non-interactive and robust, and the app's credential store stays the single source of
    truth. This step only records which provider is active (LLM_PROVIDER) and points you
    at where to paste each key."""
    from akana_cli.components import REGISTRY

    picked_providers = [REGISTRY[c] for c in picks if REGISTRY[c].kind == "provider"]
    prov_ids = [c.id for c in picked_providers]
    if not prov_ids:
        return  # no provider selected → leave LLM_PROVIDER as-is (unconfigured if never set)
    if len(prov_ids) == 1:
        _write_env_key("LLM_PROVIDER", prov_ids[0])
        io.ok(i18n.t("setup.active_provider", id=prov_ids[0]))
    else:
        print()
        pick = io.ask_choice(
            i18n.t("setup.which_default"),
            {pid: i18n.t("comp." + pid, default=REGISTRY[pid].label) for pid in prov_ids},
            default=prov_ids[0],
        )
        _write_env_key("LLM_PROVIDER", pick)
    keyed = [c for c in picked_providers if c.key_env]
    if keyed:
        print()
        io.step(i18n.t("setup.enter_keys"))
        for c in keyed:
            print(f"      {c.id}: set {c.key_env}  ({c.key_url})")


def _maybe_set_remote_token() -> None:
    """AKANA_TOKEN gates REMOTE/proxied access only; local (loopback) use needs none.

    On opt-in we AUTO-GENERATE a strong key (secrets.token_urlsafe) so the user never
    has to invent one — a manual paste path stays open via a no-echo prompt (empty =
    keep the generated key). The key is printed ONCE for the user to save."""
    import secrets

    print()
    if not io.ask_yes_no(i18n.t("setup.token_prompt"), default=False):
        return
    generated = secrets.token_urlsafe(32)
    token = io.ask_secret(i18n.t("setup.token_ask"), default=generated).strip() or generated
    _write_env_key("AKANA_TOKEN", token)
    print()
    io.ok(i18n.t("setup.token_saved", token=token))


def _install_voice_noninteractive(voice_mode: str) -> None:
    """Honor an explicit `--voice` flag (CI / power users) without prompts."""
    from akana_cli.add_cmd import install_component
    from akana_cli.components import REGISTRY

    comp = REGISTRY.get({"piper": "voice-piper", "full": "voice-full"}.get(voice_mode, voice_mode))
    if comp is not None:
        install_component(comp, interactive=False)


def _ensure_active_provider_noninteractive() -> None:
    """Non-interactive (CI/unattended): make sure the active provider's requirements
    are present — e.g. the Cursor Node bridge when cursor is the active provider.
    No provider is privileged: if none is configured, there is nothing to ensure."""
    from akana_cli.add_cmd import install_component
    from akana_cli.components import REGISTRY, deps_installed
    from akana_cli.env_util import read_env_key

    prov = (read_env_key("LLM_PROVIDER") or "").strip().lower()
    if not prov:
        return
    comp = REGISTRY.get(prov)
    if comp is not None and not deps_installed(comp):
        install_component(comp, interactive=False)


def _node_version() -> tuple[int, int] | None:
    """(major, minor) of the Node on PATH, or None if absent/unparseable."""
    import subprocess

    node = shutil.which("node")
    if not node:
        return None
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"v?(\d+)\.(\d+)", (out.stdout or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def _print_node_install_hint() -> None:
    print(f"      {i18n.t('tool.node_install_hint')}")
    if sys.platform == "darwin":
        print(f"        brew install node        {i18n.t('tool.node_hint_or')}")
    elif sys.platform.startswith("linux"):
        print(f"        {i18n.t('tool.node_hint_distro')}")
    elif sys.platform == "win32":
        print(f"        winget install OpenJS.NodeJS.LTS   {i18n.t('tool.node_hint_or')}")
    else:
        print("        https://nodejs.org/")


def _print_claude_install_hint() -> None:
    print(f"      {i18n.t('tool.claude_install_hint')}")
    print("        npm i -g @anthropic-ai/claude-code")
    print(f"      {i18n.t('tool.claude_login_hint')}")


def _toolchain_preflight() -> None:
    """Show the OPTIONAL provider toolchains (Node.js for the Cursor bridge, the Claude
    CLI for Claude) so the user chooses providers KNOWING what's ready — and gets a clear,
    copy-pasteable hint for anything missing. Informational only; installs happen when a
    provider is picked in the checklist (Cursor → bridge, Claude → global CLI)."""
    io.step(i18n.t("tool.checking"))
    nv = _node_version()
    if nv is None:
        io.warn(i18n.t("tool.node_missing"))
        _print_node_install_hint()
    elif nv >= (18, 0):
        # 18+ is the real floor: cursor_bridge ships a Symbol.dispose polyfill that
        # lets @cursor/sdk run on Node 18/19 (proven in the field), so don't scare
        # users on a working 18.x with a "too old" alarm.
        if shutil.which("npm"):
            io.ok(i18n.t("tool.node_ok", ver=f"{nv[0]}.{nv[1]}"))
        else:
            io.warn(i18n.t("tool.npm_missing"))
    else:
        io.warn(i18n.t("tool.node_old", ver=f"{nv[0]}.{nv[1]}"))
        _print_node_install_hint()
    if shutil.which("claude"):
        io.ok(i18n.t("tool.claude_ok"))
    else:
        io.warn(i18n.t("tool.claude_missing"))
        _print_claude_install_hint()


def _select_language(non_interactive: bool, lang: str | None) -> None:
    """Choose the CLI + default app language BEFORE anything else, so the whole wizard
    (and Akana afterwards) speaks it. Priority: --lang > interactive picker > env/.env."""
    if lang:
        i18n.set_lang(lang)
    elif not non_interactive:
        i18n.set_lang(io.ask_language(default=i18n.get_lang()))
    # non-interactive without --lang keeps the env/.env language (default en).


def run_setup(
    *,
    non_interactive: bool = False,
    voice_mode: str | None = None,
    repair: bool = False,
    lang: str | None = None,
) -> int:
    _select_language(non_interactive, lang)

    io.banner(i18n.t("setup.title"))
    print(f"{i18n.t('setup.repo')}: {REPO_ROOT}")
    print(f"{i18n.t('setup.platform')}: {sys.platform}")
    print()
    print(i18n.t("setup.intro"))
    print()

    if voice_mode is not None:
        voice_mode = voice_mode.strip().lower()

    py = find_system_python()
    if not py:
        _python_missing_hint()
        return 1

    # .env must exist BEFORE _pip_install_core: its data-dir bootstrap reads
    # AKANA_DATA_DIR, so a user who set a custom data dir in .env doesn't get dirs
    # created at the default.
    _ensure_env_file()
    # Record the chosen language as Akana's DEFAULT: the server resolves language as
    # store > AKANA_LANGUAGE env > en, and the UI reconciles to it on boot — so Akana
    # starts in the language picked here (en → English, tr → Türkçe).
    _write_env_key("AKANA_LANGUAGE", i18n.get_lang())
    if non_interactive:
        ci_key = os.environ.get("CURSOR_API_KEY", "").strip()
        if ci_key:
            _write_env_key("CURSOR_API_KEY", ci_key)

    if repair:
        _repair_venv()
    if not venv_exists():
        _create_venv(py)
    else:
        io.ok(i18n.t("setup.venv_present"))

    def _section(num: int, title: str) -> None:
        # Lightweight progress signposts (interactive only — CI doesn't need them).
        if not non_interactive:
            print()
            print(f"  ── [{num}/4] {title} ──")

    _section(1, i18n.t("setup.sec_python"))
    core_ok = _pip_install_core()
    # An explicit --voice (CI / power users) is honored without prompts in either mode.
    if voice_mode and voice_mode != "none":
        _install_voice_noninteractive(voice_mode)
    if non_interactive:
        # CI / unattended: install the active provider's requirements (cursor bridge).
        _ensure_active_provider_noninteractive()
    else:
        # Show the optional toolchains (Node.js / Claude CLI) so provider choice is informed.
        print()
        _toolchain_preflight()
        # Interactive: choose everything up front (checklist), install as a batch with
        # progress, then configure keys + the default provider.
        _section(2, i18n.t("setup.sec_choose"))
        picks = _select_and_install()
        _section(3, i18n.t("setup.sec_configure"))
        _configure_after_install(picks)
        _maybe_set_remote_token()
    _warn_provider_unconfigured()

    print()
    if not non_interactive:
        print(f"  ── [4/4] {i18n.t('setup.sec_health')} ──")
    io.banner(i18n.t("setup.complete"))
    from akana_cli.doctor import run_doctor

    # Offline-safe: skip the live Cursor API probe during setup — it can hang on an
    # air-gapped install and print a scary "unreachable" warning even when all is fine.
    # Belt-and-suspenders: preflight must NEVER crash setup itself. A broken
    # system-python cryptography binding (real observed case: PanicException from
    # pyo3 escaping vault_crypto's guard) or any other unexpected import failure
    # tracebacks setup and hides the useful "python akana.py start" hint below.
    # A doctor crash means we can't tell the user everything is fine — but the
    # venv install itself already succeeded, so we degrade to a one-liner and
    # let the user run doctor themselves against the freshly-built venv.
    try:
        run_doctor(verbose=True, probe_network=False)
    except BaseException as _doctor_exc:  # noqa: BLE001 - preflight is best-effort
        io.warn(
            i18n.t(
                "setup.doctor_skipped",
                exc=f"{type(_doctor_exc).__name__}: {_doctor_exc}",
            )
        )

    from akana_cli.env_util import read_env_key

    _prov = (read_env_key("LLM_PROVIDER") or "").strip().lower()
    _tok = (read_env_key("AKANA_TOKEN") or "").strip()
    from akana_cli.components import REGISTRY, deps_installed

    # `c.requirements` filters out cursor/claude/openai/ollama (empty tuples),
    # so a fresh user who installed the Cursor bridge saw "Installed: none" right
    # after installing it. Gate on `c.installer != "none"` instead so real
    # payloads (pip + npm_bridge + npm_global + external) count when actually
    # present, while pure keyless slots (openai) don't fake a positive.
    _installed = [c.id for c in REGISTRY.values() if c.installer != "none" and deps_installed(c)]
    _prov_str = _prov or i18n.t("setup.sum_none_provider")
    _inst_str = ", ".join(_installed) or i18n.t("setup.sum_none")
    _tok_str = (
        i18n.t("setup.sum_token_set")
        if (_tok and not _tok.startswith("#"))
        else i18n.t("setup.sum_token_local")
    )
    print()
    print(f"  {i18n.t('setup.your_setup')}")
    print(
        f"    {i18n.t('setup.sum_provider')}: {_prov_str}   "
        f"{i18n.t('setup.sum_installed')}: {_inst_str}   "
        f"{i18n.t('setup.sum_token')}: {_tok_str}"
    )
    if not _prov:
        # No provider = the first chat fails silently. Make it impossible to miss;
        # not a re-prompt — the web onboarding / `add` command can still fix it.
        print()
        io.warn(i18n.t("setup.no_provider_callout"))
    # Read the configured host/port from .env (like doctor.py) so the hint points at
    # the real bind address, not a hardcoded default the user may have changed.
    _host = (read_env_key("AKANA_HOST") or "").strip() or "127.0.0.1"
    _port = (read_env_key("AKANA_PORT") or "").strip() or "8766"
    print()
    print(f"{i18n.t('setup.start_hint')}   python akana.py start")
    print(f"{i18n.t('setup.stop_hint')}   python akana.py stop")
    print(f"{i18n.t('setup.browser_hint')}   http://{_host}:{_port}")
    print(f"{i18n.t('setup.add_hint')}   python akana.py add <name>   {i18n.t('setup.add_hint_tail')}")
    print()
    return 0 if core_ok else 1
