"""Setup/add must NOT prompt for provider API keys — keys are entered in the Akana UI.

Terminal key entry was brittle and redundant with Settings → Identity, so it was removed.
These guard that the wizard only records the active provider (LLM_PROVIDER) and never
calls an input prompt for a secret.
"""

from __future__ import annotations


from akana_cli import add_cmd, io, setup_cmd


def _no_input(*_a, **_k):
    raise AssertionError("setup/add must not prompt for input (keys go in the UI)")


def test_prompt_key_helper_removed() -> None:
    # The whole terminal key-prompt helper is gone (regression guard).
    assert not hasattr(setup_cmd, "_prompt_key")


def test_configure_single_provider_sets_active_without_prompting(monkeypatch) -> None:
    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(setup_cmd, "_write_env_key", lambda k, v: writes.append((k, v)))
    monkeypatch.setattr(io, "ask_yes_no", _no_input)
    monkeypatch.setattr(io, "ask_choice", _no_input)  # single provider → no choice prompt

    setup_cmd._configure_after_install(["gemini"])  # gemini requires a key

    assert ("LLM_PROVIDER", "gemini") in writes
    # the ONLY env write is the provider — no key was written
    assert all(k == "LLM_PROVIDER" for k, _ in writes)


def test_configure_multi_provider_picks_default_no_key_prompt(monkeypatch) -> None:
    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(setup_cmd, "_write_env_key", lambda k, v: writes.append((k, v)))
    monkeypatch.setattr(io, "ask_choice", lambda *_a, **_k: "openai")  # user picks default

    setup_cmd._configure_after_install(["gemini", "openai"])

    assert ("LLM_PROVIDER", "openai") in writes


def test_configure_no_provider_is_noop(monkeypatch) -> None:
    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(setup_cmd, "_write_env_key", lambda k, v: writes.append((k, v)))
    monkeypatch.setattr(io, "ask_choice", _no_input)

    setup_cmd._configure_after_install(["embeddings"])  # extra only, no provider

    assert writes == []


def test_add_post_install_does_not_prompt_for_key(monkeypatch) -> None:
    """`add gemini` points at the UI instead of prompting (io must not prompt)."""
    monkeypatch.setattr(io, "ask_yes_no", _no_input)
    from akana_cli.components import REGISTRY

    # gemini needs a key and is not configured in the test env → still no prompt.
    add_cmd._post_install(REGISTRY["gemini"], interactive=True)
