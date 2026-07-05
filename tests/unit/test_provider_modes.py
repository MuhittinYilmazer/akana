"""Drift guard for the canonical thinking-mode names (providers:smell:1).

The Akana canonical mode names (hizli/normal/derin/yogun/azami/ultra) used to be
duplicated across each provider's mapping table; they now live once in
``orchestrator.modes`` and every three-level provider DERIVES its table from
``modes.tier_map``. These tests assert that (a) every canonical name is covered by
every provider table, and (b) the derived provider values still match the intended
tiers — so adding a mode in one place cannot silently degrade a provider."""

from __future__ import annotations

from akana_server.orchestrator import (
    gemini_provider,
    modes,
    ollama_provider,
    openai_provider,
)


def test_canonical_mode_names_are_the_expected_set() -> None:
    assert modes.THINKING_MODES == (
        "hizli",
        "normal",
        "derin",
        "yogun",
        "azami",
        "ultra",
    )


def test_tier_map_covers_every_canonical_mode() -> None:
    m = modes.tier_map(low="L", medium="M", high="H")
    assert set(m) == set(modes.THINKING_MODES)
    assert m == {
        "hizli": "L",
        "normal": "M",
        "derin": "H",
        "yogun": "H",
        "azami": "H",
        "ultra": "H",
    }


def test_gemini_table_covers_every_canonical_mode() -> None:
    table = gemini_provider._THINKING_LEVELS
    for name in modes.THINKING_MODES:
        assert name in table, name
    assert table["hizli"] == "LOW"
    assert table["normal"] == "MEDIUM"
    for name in ("derin", "yogun", "azami", "ultra"):
        assert table[name] == "HIGH", name


def test_openai_table_covers_every_canonical_mode() -> None:
    table = openai_provider._REASONING_EFFORTS
    for name in modes.THINKING_MODES:
        assert name in table, name
    assert table["hizli"] == "low"
    assert table["normal"] == "medium"
    for name in ("derin", "yogun", "azami", "ultra"):
        assert table[name] == "high", name


def test_ollama_think_flag_true_for_every_canonical_mode() -> None:
    # Ollama has no graduated tiers: any canonical mode enables the boolean think flag.
    for name in modes.THINKING_MODES:
        assert ollama_provider._think_flag(name) is True, name
    assert ollama_provider._think_flag(None) is False
    assert ollama_provider._think_flag("  ") is False
