"""Bootstrap scripts (install.sh / install.ps1) — bilingual + encoding contract.

The two-line bootstrap runs BEFORE the Python wizard, so its own messages must read
in the user's language. Guards: args are forwarded to ``akana.py setup`` (so ``--lang``
reaches the wizard), a ``--lang`` detector + bilingual ``say``/``Say`` helper exist,
Turkish strings are present, and — critically — the FILE ENCODING is right:

* ``install.ps1`` needs a UTF-8 **BOM**: Windows PowerShell 5.1 reads a no-BOM .ps1 as
  the ANSI code page and turns ç/ş/ı into mojibake.
* ``install.sh`` must NOT have a BOM: it would corrupt the ``#!`` shebang line.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SH = REPO_ROOT / "install.sh"
PS1 = REPO_ROOT / "install.ps1"

_BOM = b"\xef\xbb\xbf"


def test_scripts_exist() -> None:
    assert SH.is_file(), "install.sh missing"
    assert PS1.is_file(), "install.ps1 missing"


def test_install_sh_no_bom_and_shebang() -> None:
    data = SH.read_bytes()
    assert not data.startswith(_BOM), "install.sh must NOT have a UTF-8 BOM (breaks the shebang)"
    assert data.startswith(b"#!/usr/bin/env bash"), "install.sh must start with the bash shebang"


def test_install_ps1_has_utf8_bom() -> None:
    assert PS1.read_bytes().startswith(_BOM), (
        "install.ps1 must be saved UTF-8 WITH BOM — otherwise Windows PowerShell 5.1 "
        "reads it as ANSI and its Turkish messages render as mojibake"
    )


def test_both_forward_args_to_setup() -> None:
    sh = SH.read_text(encoding="utf-8")
    ps1 = PS1.read_text(encoding="utf-8-sig")
    assert "akana.py setup" in sh and '"$@"' in sh, "install.sh must forward args to setup"
    assert '"akana.py" "setup"' in ps1 and "@args" in ps1, "install.ps1 must forward args to setup"


def test_both_have_lang_detector_and_say_helper() -> None:
    sh = SH.read_text(encoding="utf-8")
    ps1 = PS1.read_text(encoding="utf-8-sig")
    assert "LANG_SEL" in sh and "say()" in sh and "--lang" in sh
    assert "LangSel" in ps1 and "function Say" in ps1 and "--lang" in ps1


def test_both_carry_turkish_strings() -> None:
    sh = SH.read_text(encoding="utf-8")
    ps1 = PS1.read_text(encoding="utf-8-sig")
    for needle in ("Python kullanılıyor", "tekrar çalıştır"):
        assert needle in sh, f"install.sh missing Turkish string: {needle}"
        assert needle in ps1, f"install.ps1 missing Turkish string: {needle}"
