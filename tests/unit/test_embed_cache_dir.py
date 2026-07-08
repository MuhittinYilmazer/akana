"""fastembed model cache must live under a reboot-persistent path.

Regression for the Linux boot loop: fastembed's default cache is
``<tempdir>/fastembed_cache`` (i.e. ``/tmp`` on Linux, cleared on reboot), so the
~220MB model re-downloaded on every boot. LocalEmbedder now passes an explicit
XDG-anchored ``cache_dir`` (overridable via ``FASTEMBED_CACHE_PATH``).

Hermetic: a fake ``fastembed`` module is injected so nothing is downloaded and the
test runs even where fastembed is not installed (e.g. CI).
"""

from __future__ import annotations

import sys
import types

from akana.memory import embed
from akana.memory.embed import LocalEmbedder


def _install_fake_fastembed(monkeypatch) -> dict:
    captured: dict = {}

    class FakeTextEmbedding:
        def __init__(self, *, model_name, cache_dir, **_kw):
            captured["model_name"] = model_name
            captured["cache_dir"] = cache_dir

        def embed(self, texts):
            return [[0.0, 1.0] for _ in texts]

    fake = types.ModuleType("fastembed")
    fake.TextEmbedding = FakeTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", fake)
    return captured


def test_default_cache_dir_is_persistent_not_tmp(monkeypatch, tmp_path):
    captured = _install_fake_fastembed(monkeypatch)
    monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

    LocalEmbedder().embed(["hi"])

    assert captured["cache_dir"] == str(tmp_path / "xdg" / "fastembed")
    # the reboot-volatile default must never be used
    assert "fastembed_cache" not in captured["cache_dir"]


def test_home_cache_fallback_when_no_xdg(monkeypatch, tmp_path):
    captured = _install_fake_fastembed(monkeypatch)
    monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(embed.Path, "home", classmethod(lambda cls: tmp_path))

    LocalEmbedder().embed(["hi"])

    assert captured["cache_dir"] == str(tmp_path / ".cache" / "fastembed")


def test_fastembed_cache_path_env_overrides(monkeypatch, tmp_path):
    captured = _install_fake_fastembed(monkeypatch)
    custom = str(tmp_path / "custom-cache")
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", custom)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "ignored"))

    LocalEmbedder().embed(["hi"])

    assert captured["cache_dir"] == custom
