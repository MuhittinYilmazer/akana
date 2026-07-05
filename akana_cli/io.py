"""Terminal UI helpers for interactive setup."""

from __future__ import annotations

from akana_cli import i18n


def _input(prompt: str) -> str:
    """input() that treats EOF (closed/exhausted stdin) as a cancellation.

    Bare input() raises EOFError, which no caller here catches — it would
    otherwise propagate as a raw traceback instead of the clean one-liner
    main.py already prints for KeyboardInterrupt. Re-raising as
    KeyboardInterrupt reuses that existing top-level handler.
    """
    try:
        return input(prompt)
    except EOFError as exc:
        raise KeyboardInterrupt from exc


def banner(title: str = "Akana") -> None:
    print()
    print("═" * 44)
    print(f" {title}")
    print("═" * 44)
    print()


def step(msg: str) -> None:
    print(f"▸ {msg}")


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def warn(msg: str) -> None:
    print(f"  ⚠ {msg}")


def fail(msg: str) -> None:
    print(f"  ✗ {msg}")


def ask_secret(prompt: str, *, default: str = "") -> str:
    """No-echo prompt for a secret (e.g. an access token): the typed value is not

    shown on screen. Empty input returns ``default`` (used for the auto-generated
    token, so pressing Enter keeps it). Falls back to a visible prompt only if the
    terminal cannot do no-echo input (e.g. a redirected/piped stdin).
    """
    import getpass

    suffix = f" [{i18n.t('io.secret_default_hint')}]" if default else ""
    try:
        raw = getpass.getpass(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise
    except Exception:
        # No-echo unavailable (unusual TTY) — fall back to a visible prompt.
        raw = _input(f"{prompt}{suffix}: ").strip()
    return raw or default


def ask_language(default: str = "en") -> str:
    """Bilingual language picker — shown BEFORE a language is set, so both read clearly.

    Accepts the number (1/2), the code (en/tr), or the name; empty = default.
    """
    print()
    print(i18n.t("lang.prompt"))
    order = ["en", "tr"]
    labels = {"en": i18n.t("lang.english"), "tr": i18n.t("lang.turkish")}
    for n, code in enumerate(order, 1):
        mark = "●" if code == default else "○"
        print(f"  {mark} {n}) {labels[code]}")
    while True:
        raw = _input(f"{i18n.t('lang.choice')} [{default}]: ").strip().lower()
        if not raw:
            return default
        if raw in ("1", "en", "english", "ingilizce", "i̇ngilizce"):
            return "en"
        if raw in ("2", "tr", "turkish", "türkçe", "turkce"):
            return "tr"
        print(f"  {i18n.t('io.invalid_pick', opts='en, tr')}")


def ask_yes_no(prompt: str, *, default: bool = True) -> bool:
    hint = i18n.t("io.yn_default_yes") if default else i18n.t("io.yn_default_no")
    raw = _input(f"{prompt} ({hint}): ").strip().lower()
    if not raw:
        return default
    # The CLI is fully bilingual (EN/TR) — Turkish-language prompts (e.g.
    # tool.npm_confirm) invite a Turkish answer, so an explicit "evet"/"e" must
    # count as yes, not silently fall through to No.
    return raw in ("y", "yes", "e", "evet")


def ask_choice(prompt: str, choices: dict[str, str], *, default: str) -> str:
    print(prompt)
    for key, label in choices.items():
        mark = "●" if key == default else "○"
        print(f"  {mark} {key}) {label}")
    while True:
        raw = _input(f"{i18n.t('io.choice')} [{default}]: ").strip().lower() or default
        if raw in choices:
            return raw
        print(f"  {i18n.t('io.invalid_pick', opts=', '.join(choices))}")


def ask_checklist(
    prompt: str, choices: dict[str, str], *, preselected: set[str] | None = None
) -> list[str]:
    """Multi-select checklist: toggle items on/off, then confirm to install as a batch.

    Returns the chosen ids in `choices` order. Empty/Enter confirms the current
    selection; 'a' selects all, 'n' clears, 'q' skips (returns []). Numbers can be
    space/comma-separated to toggle several at once (e.g. ``1 3 5``).
    """
    ids = list(choices)
    selected = {c for c in (preselected or set()) if c in choices}
    print()
    print(prompt)
    while True:
        for n, cid in enumerate(ids, 1):
            mark = "x" if cid in selected else " "
            print(f"  {n:>2}. [{mark}] {choices[cid]}")
        print(f"  {i18n.t('io.checklist_controls')}")
        raw = _input(f"{i18n.t('io.toggle_num')}: ").strip().lower()
        if raw in ("", "i", "install", "ok", "done"):
            return [c for c in ids if c in selected]
        if raw in ("q", "quit", "skip"):
            return []
        if raw in ("a", "all"):
            selected = set(ids)
        elif raw in ("n", "none"):
            selected = set()
        else:
            hit = False
            for tok in raw.replace(",", " ").split():
                if tok.isdigit() and 1 <= int(tok) <= len(ids):
                    selected.symmetric_difference_update({ids[int(tok) - 1]})
                    hit = True
            if not hit:
                print(f"  {i18n.t('io.checklist_hint')}")
        print()
