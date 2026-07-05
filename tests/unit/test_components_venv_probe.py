"""`deps_installed` for pip components must reflect the VENV interpreter, not the CLI's.

Root cause of "I selected fastembed in setup but it isn't installed": on first setup the
launcher runs under SYSTEM python (the venv doesn't exist yet), whose user-site can hold
a copy the venv can't see. An in-process find_spec then reports "installed", setup skips
the venv install, and the server (venv python) silently falls back to keyword recall.
These tests pin the venv-targeted probe.
"""

from __future__ import annotations

import sys

from akana_cli import components
from akana_cli.components import Component, _modules_present_in_venv, deps_installed


def _force_subprocess_probe(monkeypatch, venv_exe: str) -> None:
    """Make the probe spawn ``venv_exe`` instead of using the in-process find_spec."""
    import akana_cli.paths as paths

    monkeypatch.setattr(paths, "venv_exists", lambda: True)
    monkeypatch.setattr(paths, "venv_python", lambda: venv_exe)
    # Force the cross-interpreter (subprocess) branch even though venv_exe IS this python.
    monkeypatch.setattr(components.os.path, "samefile", lambda _a, _b: False)


def test_present_module_true_via_subprocess(monkeypatch) -> None:
    _force_subprocess_probe(monkeypatch, sys.executable)
    assert _modules_present_in_venv(("os",)) is True


def test_absent_module_false_via_subprocess(monkeypatch) -> None:
    _force_subprocess_probe(monkeypatch, sys.executable)
    assert _modules_present_in_venv(("no_such_module_zzz_123",)) is False


def test_in_process_fast_path_when_we_are_the_venv(monkeypatch) -> None:
    """When the CLI IS the venv python, no subprocess — the in-process check is used."""
    import akana_cli.paths as paths

    monkeypatch.setattr(paths, "venv_exists", lambda: True)
    monkeypatch.setattr(paths, "venv_python", lambda: sys.executable)  # samefile → True

    called = {"subprocess": False}
    real_run = components.subprocess.run

    def _tripwire(*a, **k):  # pragma: no cover - must not run
        called["subprocess"] = True
        return real_run(*a, **k)

    monkeypatch.setattr(components.subprocess, "run", _tripwire)
    assert _modules_present_in_venv(("os",)) is True
    assert called["subprocess"] is False


def test_empty_modules_is_trivially_true() -> None:
    assert _modules_present_in_venv(()) is True


def test_deps_installed_uses_venv_probe_for_pip(monkeypatch) -> None:
    """A pip Component routes through the venv probe (present vs absent modules)."""
    _force_subprocess_probe(monkeypatch, sys.executable)
    present = Component(id="x", label="x", installer="pip", modules=("os",))
    absent = Component(id="y", label="y", installer="pip", modules=("no_such_module_zzz_123",))
    assert deps_installed(present) is True
    assert deps_installed(absent) is False
