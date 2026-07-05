"""Canonical thinking-mode names — the single source for the per-provider tables.

``chat_producer`` sends ``body.thinking_mode`` with Akana's canonical level names
(``hizli``/``normal``/``derin``/``yogun``/``azami``/``ultra``); each provider maps
those onto its own native reasoning knob (gemini ``thinking_level``, openai
``reasoning_effort``, ollama's boolean ``think``, claude's effort tiers). Before this
module the canonical names were magic strings duplicated across four separate mapping
tables, and the gemini table even carried a comment recording a past drift bug
("previously NONE matched, so they all fell to MEDIUM"). Adding a new mode meant
editing 3-4 places or it silently degraded.

Here the canonical names live once (:data:`THINKING_MODES`) and each provider's map is
DERIVED from a small per-provider tier assignment, so a new mode is a single edit and
:func:`tier_map` guarantees every canonical name is covered. ``ultra`` is a
claude/fable-only prompt keyword ("ultracode"); on gemini/openai it simply tops out at
the provider's highest tier (there is no higher tier to map to).

claude keeps its own ``_EFFORT_LEVELS`` table for now (it migrates in a later wave);
gemini/openai/ollama are migrated onto this module. The drift guard
(``tests/unit/test_provider_modes.py``) asserts the derived tables stay in sync with
each provider's public mapping.
"""

from __future__ import annotations

#: Akana's canonical thinking-mode names, ordered from lightest to heaviest. This is
#: the authoritative list ``chat_producer`` chooses from; every provider mapping must
#: cover all of these (``ultra`` maps to the top tier where the provider has no higher
#: level).
THINKING_MODES: tuple[str, ...] = (
    "hizli",
    "normal",
    "derin",
    "yogun",
    "azami",
    "ultra",
)

#: Canonical name → coarse tier (``low``/``medium``/``high``). Each provider translates
#: these tiers into its own native vocabulary (see :func:`tier_map`). ``derin`` and up
#: all sit at ``high`` because gemini/openai expose only three graduated levels; the
#: finer Akana names above ``derin`` are preserved for claude (migrating later) and for
#: forward compatibility, but currently collapse to ``high`` on the three-level
#: providers.
_CANONICAL_TIER: dict[str, str] = {
    "hizli": "low",
    "normal": "medium",
    "derin": "high",
    "yogun": "high",
    "azami": "high",
    "ultra": "high",
}


def tier_map(low: str, medium: str, high: str) -> dict[str, str]:
    """Derive a ``{canonical_name: provider_value}`` table from three tier values.

    ``low``/``medium``/``high`` are the provider's native names for those tiers (e.g.
    gemini ``"LOW"``/``"MEDIUM"``/``"HIGH"``; openai ``"low"``/``"medium"``/``"high"``).
    Every canonical name in :data:`THINKING_MODES` is included, so a mapping built with
    this helper can never silently miss a mode. Callers may still add extra
    provider-native aliases (e.g. gemini's ``"minimal"``) on top of the returned dict."""
    by_tier = {"low": low, "medium": medium, "high": high}
    return {name: by_tier[_CANONICAL_TIER[name]] for name in THINKING_MODES}


__all__ = ["THINKING_MODES", "tier_map"]
