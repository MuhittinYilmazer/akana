"""CLI i18n + language selection + install-progress helpers.

Guards the bilingual setup experience: the string table is complete and
placeholder-consistent, the `--lang` flag + language picker resolve as specified,
Akana's default language is recorded, and the transparent-progress parsers behave.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from akana_cli import i18n
from akana_cli.main import build_parser

CLI_DIR = Path(i18n.__file__).resolve().parent


@pytest.fixture(autouse=True)
def _restore_lang():
    prev = i18n.get_lang()
    yield
    i18n.set_lang(prev)


# ── i18n core ────────────────────────────────────────────────────────────────
def test_set_lang_normalizes() -> None:
    assert i18n.set_lang("tr") == "tr"
    assert i18n.set_lang("TR") == "tr"
    assert i18n.set_lang("de") == "en"  # unknown → English
    assert i18n.set_lang("") == "en"
    assert i18n.set_lang(None) == "en"


def test_t_interpolates_and_falls_back() -> None:
    i18n.set_lang("en")
    assert i18n.t("setup.pkg_downloading", pkg="numpy") == "downloading numpy"
    assert i18n.t("nope.key", default="FALLBACK") == "FALLBACK"
    assert i18n.t("nope.key") == "nope.key"  # last-resort: the key itself


def test_t_key_placeholder_does_not_collide() -> None:
    """A string may use a {key} placeholder — the first arg is positional-only, so
    ``t("doctor.key_defined", key=...)`` must NOT raise 'multiple values for key'."""
    i18n.set_lang("en")
    assert "CURSOR_API_KEY" in i18n.t("doctor.key_defined", key="CURSOR_API_KEY")
    assert "GEMINI_API_KEY" in i18n.t("add.key_hint", key="GEMINI_API_KEY", url="https://x")


def test_turkish_actually_translates() -> None:
    i18n.set_lang("tr")
    assert i18n.t("setup.core_installing") != "Installing core packages"
    assert "kuruluyor" in i18n.t("setup.core_installing").lower()


def test_all_strings_bilingual_and_nonempty() -> None:
    for key, entry in i18n._STRINGS.items():
        assert set(entry) >= {"en", "tr"}, f"{key} missing a language"
        for lang in ("en", "tr"):
            assert isinstance(entry[lang], str) and entry[lang].strip(), f"{key}.{lang} empty"


def test_placeholders_consistent_across_languages() -> None:
    ph = re.compile(r"\{(\w+)\}")
    for key, entry in i18n._STRINGS.items():
        assert set(ph.findall(entry["en"])) == set(ph.findall(entry["tr"])), (
            f"{key}: placeholder set differs between en/tr"
        )


def test_every_literal_key_used_in_code_is_defined() -> None:
    """Drift guard: every literal t("…")/i18n.t("…") key in akana_cli exists in _STRINGS.

    Dynamic keys (e.g. ``t("comp." + comp.id, default=…)``) are not literals and are
    covered by the completeness test above; this catches a typo'd or undefined literal.
    """
    pat = re.compile(r'(?:i18n\.)?\bt\(\s*["\']([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)["\']')
    missing: dict[str, set[str]] = {}
    for py in CLI_DIR.glob("*.py"):
        if py.name == "i18n.py":
            continue
        for m in pat.finditer(py.read_text(encoding="utf-8")):
            if m.group(1) not in i18n._STRINGS:
                missing.setdefault(py.name, set()).add(m.group(1))
    assert not missing, f"i18n keys used in code but not defined: {missing}"


# ── --lang flag + language selection ─────────────────────────────────────────
def test_build_parser_accepts_lang() -> None:
    p = build_parser()
    assert p.parse_args(["setup", "--lang", "tr"]).lang == "tr"
    assert p.parse_args(["setup"]).lang is None
    with pytest.raises(SystemExit):
        p.parse_args(["setup", "--lang", "de"])  # invalid choice rejected


def test_select_language_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    from akana_cli import setup_cmd

    # 1) explicit --lang wins.
    setup_cmd._select_language(non_interactive=False, lang="tr")
    assert i18n.get_lang() == "tr"

    # 2) non-interactive without --lang keeps the current language (NO prompt).
    i18n.set_lang("en")

    def _no_prompt(default: str = "en") -> str:
        raise AssertionError("setup -y must not prompt for a language")

    monkeypatch.setattr(io_mod := __import__("akana_cli.io", fromlist=["ask_language"]), "ask_language", _no_prompt)
    setup_cmd._select_language(non_interactive=True, lang=None)
    assert i18n.get_lang() == "en"

    # 3) interactive without --lang uses the picker's answer.
    monkeypatch.setattr(io_mod, "ask_language", lambda default="en": "tr")
    setup_cmd._select_language(non_interactive=False, lang=None)
    assert i18n.get_lang() == "tr"


def test_setup_records_default_language(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The chosen language is written to .env as AKANA_LANGUAGE (Akana's default)."""
    from akana_cli import setup_cmd

    env = tmp_path / ".env"
    monkeypatch.setattr(setup_cmd, "ENV_FILE", env)
    i18n.set_lang("tr")
    setup_cmd._write_env_key("AKANA_LANGUAGE", i18n.get_lang())
    assert "AKANA_LANGUAGE=tr" in env.read_text(encoding="utf-8")


# ── transparent progress parsers ─────────────────────────────────────────────
def test_pip_substatus_parses_pip_lines() -> None:
    from akana_cli.setup_cmd import _pip_substatus

    i18n.set_lang("en")
    assert _pip_substatus("Collecting fastapi==0.111.0") == "downloading fastapi"
    assert _pip_substatus("  Downloading fastapi-0.111.0-py3-none-any.whl (92 kB)") == "downloading fastapi"
    assert _pip_substatus("Building wheel for numpy (pyproject.toml)").startswith("installing")
    assert _pip_substatus("Installing collected packages: a, b, c").startswith("installing")
    # Noise is ignored (no live churn from "already satisfied" / blank / cache lines).
    assert _pip_substatus("Requirement already satisfied: certifi") is None
    assert _pip_substatus("  Using cached idna-3.7.whl") is None
    assert _pip_substatus("") is None


def test_read_requirement_names(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from akana_cli import setup_cmd

    (tmp_path / "r.txt").write_text(
        "# comment\nfastapi>=0.1\n-r other.txt\nuvicorn[standard]==1.0\n\nhttpx ; python_version>'3'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(setup_cmd, "REPO_ROOT", tmp_path)
    assert setup_cmd._read_requirement_names("r.txt") == ["fastapi", "uvicorn", "httpx"]
    assert setup_cmd._read_requirement_names("missing.txt") == []


def test_node_version_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    from akana_cli import setup_cmd

    monkeypatch.setattr(setup_cmd.shutil, "which", lambda _n: "/usr/bin/node")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="v20.4.1\n")
    )
    assert setup_cmd._node_version() == (20, 4)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="v18.16.1\n")
    )
    assert setup_cmd._node_version() == (18, 16)
    monkeypatch.setattr(setup_cmd.shutil, "which", lambda _n: None)
    assert setup_cmd._node_version() is None
