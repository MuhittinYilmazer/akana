"""The ``llm_chat_titles`` runtime toggle — schema spec + titler honoring.

The user-facing on/off for LLM chat titles is a runtime setting (renders in the
runtime settings form, category «genel»). This locks the spec's shape and that the
titler's ``_titles_enabled`` gate honors the resolved value (and defaults ON on any
resolution failure, so titling never breaks on a config hiccup).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from akana_server.orchestrator import chat_titler
from akana_server.runtime_settings.schema import SCHEMA


def test_llm_chat_titles_spec_registered() -> None:
    spec = SCHEMA.get("llm_chat_titles")
    assert spec is not None, "llm_chat_titles must be a runtime setting (settings toggle)"
    assert spec.type == "bool"
    assert spec.default is True  # on by default
    assert spec.category == "genel"
    assert spec.hidden is False, "the toggle must be visible in the runtime settings form"
    assert spec.settings_attr == "llm_chat_titles"
    assert spec.env_var == "AKANA_LLM_CHAT_TITLES"


@pytest.mark.parametrize(
    "resolved,expected",
    [(True, True), (False, False)],
)
def test_titles_enabled_honors_resolved_flag(
    monkeypatch: pytest.MonkeyPatch, resolved: bool, expected: bool
) -> None:
    import akana_server.runtime_settings as rs

    monkeypatch.setattr(
        rs, "get_runtime", lambda k, s: resolved if k == "llm_chat_titles" else None
    )
    assert chat_titler._titles_enabled(SimpleNamespace(data_dir=None)) is expected


def test_titles_enabled_defaults_on_when_resolution_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import akana_server.runtime_settings as rs

    def _boom(_k, _s):  # noqa: ANN001, ANN202
        raise KeyError("llm_chat_titles")

    monkeypatch.setattr(rs, "get_runtime", _boom)
    assert chat_titler._titles_enabled(SimpleNamespace(data_dir=None)) is True
