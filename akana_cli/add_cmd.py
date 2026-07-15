"""`akana.py add <component>` — install an optional add-on after first setup.

`setup` is the full first run; `add` is the à-la-carte "I skipped embeddings/voice
/ a provider, now I want it" path — it installs ONE component without re-running the
whole wizard. Both pull from `akana_cli.components`, so the catalogue never drifts.
"""

from __future__ import annotations

from akana_cli import i18n, io
from akana_cli.components import (
    REGISTRY,
    Component,
    deps_installed,
    is_ready,
    key_configured,
)
from akana_cli.paths import venv_exists

#: Whisper STT size ids offered when adding full voice (mirrors `setup`); each row's
#: label is localized per-call via add.whisper_<size> so the picker isn't half-English.
_WHISPER_SIZES = ("tiny", "base", "small", "medium")


def _install_requirements(comp: Component) -> bool:
    """pip-install the component's requirements-*.txt files (in order).

    Special-case openwakeword (voice extras): its metadata declares an
    unconditional `tflite-runtime>=2.8,<3` on Linux, and tflite-runtime has no
    wheels for Python 3.12+ — so a plain resolve fails on modern distros
    (Ubuntu 24.04+, Fedora 39+). We drive the ONNX framework, not TFLite, so
    install openwakeword itself WITHOUT its declared deps and let the real
    transitive runtime deps come from requirements-voice.txt (see the file's
    header note). Non-voice components take the plain path.

    Returns False when the openwakeword preinstall failed and the install was
    skipped (its own specific message was already printed) — the caller then
    suppresses the generic verify message so the user doesn't see a contradictory
    'install reported success but…' right after being told it failed and was skipped.
    """
    from akana_cli.paths import REPO_ROOT
    from akana_cli.runner import run_progress
    from akana_cli.setup_cmd import _pip_cmd, _pip_env, _pip_install_extra

    if "requirements-voice.txt" in comp.requirements:
        ok, out = run_progress(
            [*_pip_cmd(), "install", "--no-deps", "openwakeword>=0.6"],
            i18n.t("setup.extra_installing", label="openwakeword (no-deps)"),
            cwd=REPO_ROOT,
            env=_pip_env(),
        )
        # If this preinstall fails, the requirements-voice.txt resolve below would pull
        # openwakeword WITH its declared tflite-runtime pin (no cp312/cp313 Linux wheels)
        # and the whole install would die with the exact resolver error this two-step
        # dance exists to avoid — with no hint that the guard step failed. Surface it and
        # skip the doomed resolve. We signal the failure up so install_component reports
        # the incomplete state without ALSO printing the generic 'reported success but not
        # importable' verify line, which would contradict this specific message.
        if not ok:
            io.fail(i18n.t("add.oww_preinstall_failed"))
            if out:
                print(out.rstrip())
            return False
    for req in comp.requirements:
        _pip_install_extra(req, comp.id)
    return True


def _prompt_piper_voices() -> list[str] | None:
    """Let the user pick WHICH Piper voices to download (interactive only).

    Returns the chosen voice names, or None to use the shipped defaults (TR + EN).
    The checklist is preselected to the defaults, so pressing Enter installs exactly
    what the fixed path used to — no behaviour change for a user who just confirms.
    """
    from akana_cli.voice_assets import PIPER_CATALOG, default_voice_names

    choices = {v.name: i18n.t("voice.desc." + v.name, default=v.desc) for v in PIPER_CATALOG}
    picks = io.ask_checklist(
        i18n.t("add.piper_choose"),
        choices,
        preselected=set(default_voice_names()),
    )
    # 'q'/skip returns [] → fall back to the defaults rather than downloading nothing.
    return picks or None


def _download_piper_voices(selection: list[str] | None = None) -> None:
    io.step(i18n.t("add.piper_voices"))
    try:
        from akana_cli.voice_assets import install_piper_voices

        install_piper_voices(selection=selection)
    except Exception:
        io.warn(i18n.t("add.piper_failed"))


def _prompt_whisper_size() -> None:
    size = io.ask_choice(
        i18n.t("add.whisper_prompt"),
        {s: i18n.t("add.whisper." + s) for s in _WHISPER_SIZES},
        default="small",
    )
    from akana_cli.setup_cmd import _write_env_key

    _write_env_key("WHISPER_MODEL", size)


def _note_wake_bundled() -> None:
    """Note that the bundled 'Hey Akana' wake word is active — no download, no choice.

    The openWakeWord model SHIPS in-repo (akana_server/voice/wake_models/hey_akana.onnx)
    and is the default WAKE_MODEL in server config, so it is ALWAYS bundled and on. Setup
    never presents it as a selectable/downloadable item — this is a one-line reassurance.
    Turning wake on/off is a runtime choice made later in the Akana UI (Settings → Voice),
    not an install-time one.
    """
    io.ok(i18n.t("add.wake_active"))


def _post_install(comp: Component, *, interactive: bool) -> None:
    """Component-specific finishing steps: key hint, asset download, model size.

    Interactive prompts (key hint, voice selection, Whisper size) are skipped when
    `interactive` is False (e.g. `setup --yes --voice full` in CI) — defaults apply
    (shipped TR+EN voices, small Whisper) and keys come from env. The 'Hey Akana' wake
    word is bundled + always on, so it is never prompted (just noted).
    """
    # Provider API keys are entered in the Akana UI (Settings → Identity), not in the
    # terminal — so `add` only points you at where to paste it.
    if comp.key_env and interactive and not key_configured(comp):
        io.step(i18n.t("add.key_hint", key=comp.key_env, url=comp.key_url))
    if comp.id in ("voice-piper", "voice-full"):
        selection = _prompt_piper_voices() if interactive else None
        _download_piper_voices(selection)
    if comp.id == "voice-full":
        if interactive:
            _prompt_whisper_size()
        # The 'Hey Akana' wake model is bundled + default-wired: no download, no choice.
        _note_wake_bundled()


def _install_npm_global(comp: Component, *, interactive: bool) -> None:
    """Install a global npm CLI (e.g. claude-code). Best-effort: a missing npm or a
    failed install degrades to a manual-install hint rather than aborting setup."""
    import shutil

    if not shutil.which("npm"):
        io.warn(i18n.t("tool.npm_missing_for", pkg=comp.npm_package, bin=comp.probe_bin))
        return
    do_it = (not interactive) or io.ask_yes_no(
        i18n.t("tool.npm_confirm", pkg=comp.npm_package), default=True
    )
    if not do_it:
        io.warn(i18n.t("tool.npm_install_later", pkg=comp.npm_package))
        return
    from akana_cli.runner import npm_base, run_progress

    ok, out = run_progress(
        [*npm_base(), "install", "-g", comp.npm_package],
        i18n.t("tool.claude_installing", pkg=comp.npm_package),
    )
    if ok:
        io.ok(i18n.t("tool.claude_installed"))
        # Guide the one remaining manual step so "install everything easily" holds true.
        # Per-component login hint (claude → `claude login`, codex → `codex login`);
        # with comp.id="claude" this resolves to the historical tool.claude_login_hint
        # key unchanged, so existing behaviour is preserved.
        print(f"      {i18n.t('tool.' + comp.id + '_login_hint', default=i18n.t('tool.claude_login_hint'))}")
    else:
        io.warn(i18n.t("tool.claude_install_failed", pkg=comp.npm_package))
        if out:
            print(out.rstrip())


def _do_install(comp: Component, *, interactive: bool) -> bool:
    """Download the component's requirements per its installer kind (idempotent).

    Returns False when the install reported a SPECIFIC failure and already printed
    its own message (currently only the openwakeword preinstall skip) — the caller
    then suppresses the generic verify line so the two don't contradict each other.
    """
    if deps_installed(comp):
        io.ok(i18n.t("add.already_present", id=comp.id))
        return True
    if comp.installer == "pip":
        return _install_requirements(comp)
    elif comp.installer == "npm_bridge":
        from akana_cli.setup_cmd import _npm_bridge

        _npm_bridge()
    elif comp.installer == "npm_global":
        _install_npm_global(comp, interactive=interactive)
    elif comp.installer == "external":
        io.warn(
            i18n.t(
                "comp." + comp.id + ".hint",
                default=comp.install_hint or i18n.t("add.external_hint", id=comp.id),
            )
        )
    # "none" → nothing to download
    return True


def install_component(comp: Component, *, interactive: bool = True) -> bool:
    """Install one component's requirements (idempotent) + run its post-steps.

    The single install path shared by `setup` (first run) and `add` (later) — so
    the two never drift. Caller handles banners / the restart hint. Returns False
    when the install is verified to have failed (so the caller doesn't report success).
    """
    install_ok = _do_install(comp, interactive=interactive)
    # Verify what was supposed to be downloaded actually lives where the SERVER
    # looks. Two failure modes we must not silently paper over:
    #   • pip extras — a bare-pip or wrong-interpreter install can report success
    #     yet be invisible to the venv (e.g. user-site on a PIP_USER machine),
    #     surfacing later as silent capability loss (vector recall → keyword
    #     fallback).
    #   • npm_bridge (cursor) — a Node-less machine hits _npm_bridge's warn-and-
    #     return path (setup_cmd.py). Without this guard install_component returns
    #     True, _select_and_install keeps `cursor` in picks, and
    #     _configure_after_install writes LLM_PROVIDER=cursor with no working
    #     bridge — the exact "misled into a broken state" trap that finish-line
    #     setups should never leave.
    # Both cases fail loudly with the concrete retry command.
    ok = True
    verifiable = (
        (comp.installer == "pip" and bool(comp.modules))
        or comp.installer == "npm_bridge"
        # npm_global (claude-code CLI): a missing npm, a declined confirm, or a failed
        # `npm i -g` all warn-and-return from _install_npm_global, yet the install still
        # reported success — so without this check a broken claude becomes the active
        # provider (LLM_PROVIDER=claude) and the first chat 503s. deps_installed probes
        # the CLI on PATH via shutil.which(probe_bin).
        or (comp.installer == "npm_global" and bool(comp.probe_bin))
    )
    if not install_ok:
        # _do_install already printed a SPECIFIC failure (e.g. the openwakeword
        # preinstall skip) and the install was deliberately aborted — don't ALSO
        # print the generic 'reported success but not importable' verify line, which
        # would contradict the message the user just saw. The install still failed.
        ok = False
    elif verifiable and not deps_installed(comp):
        key = {
            "npm_bridge": "add.verify_failed_bridge",
            "npm_global": "add.verify_failed_npm",
        }.get(comp.installer, "add.verify_failed_pip")
        io.fail(i18n.t(key, id=comp.id))
        ok = False
    _post_install(comp, interactive=interactive)
    if comp.note:
        io.warn(i18n.t("comp." + comp.id + ".note", default=comp.note))
    return ok


def _menu() -> Component | None:
    """Interactive picker (no component argument). Shows install state per row."""
    choices = {
        cid: i18n.t("comp." + cid, default=comp.label)
        + f"  [{i18n.t('add.installed') if deps_installed(comp) else i18n.t('add.not_installed')}]"
        for cid, comp in REGISTRY.items()
    }
    cid = io.ask_choice(i18n.t("add.which"), choices, default=next(iter(REGISTRY)))
    return REGISTRY[cid]


def _resolve(component: str | None) -> Component | None:
    if not component:
        print()
        print(i18n.t("add.intro"))
        return _menu()
    comp = REGISTRY.get(component)
    if comp is None:
        io.fail(i18n.t("add.unknown", id=component))
        print("  " + i18n.t("add.available", ids=", ".join(REGISTRY)))
    return comp


def run_add(component: str | None = None) -> int:
    if not venv_exists():
        io.fail(i18n.t("doctor.venv_missing"))
        return 1

    comp = _resolve(component)
    if comp is None:
        return 1

    io.banner(i18n.t("add.banner", id=comp.id))

    # Idempotent: if deps are already present AND any provider key is set, there is
    # nothing to do unless the user wants to re-run (e.g. re-enter a key).
    if is_ready(comp):
        io.ok(i18n.t("add.already_ready", id=comp.id))
        if not io.ask_yes_no(i18n.t("add.rerun"), default=False):
            return 0

    ok = install_component(comp, interactive=True)

    print()
    if not ok:
        io.fail(i18n.t("add.incomplete", id=comp.id))
        return 1
    io.ok(i18n.t("add.ready", id=comp.id))
    print("  " + i18n.t("add.restart"))
    return 0
