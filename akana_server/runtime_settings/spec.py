"""runtime_settings.spec — setting contract types (schema row + error).

This module only defines data types; the schema catalog is in :mod:`.schema`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


class RuntimeSettingError(ValueError):
    """Validation error — message may be Turkish and is suitable for display to the user."""


@dataclass(frozen=True, slots=True)
class RuntimeSettingSpec:
    """Contract for a single runtime setting (schema row)."""

    key: str
    type: str  # "bool" | "int" | "float" | "str" | "csv" | "paths"
    label: str
    description: str
    category: str
    env_var: str
    default: Any
    settings_attr: str | None = None  # Settings field (env-baked fallback)
    min: float | None = None
    max: float | None = None
    restart_required: bool = False
    unit: str = ""
    # Historical parser for env-only booleans (e.g. AKANA_SKILL_INJECT).
    env_parse: Callable[[str], Any] | None = None
    # Fixed option list for enum settings (UI <select>; e.g. gemini_live_voice).
    # Empty/None → free text/number input (current behavior).
    options: tuple[str, ...] | None = None
    # Keep the spec (PUT validation + apply_runtime_overrides read ``_SPECS``) but
    # HIDE it from the generated settings form (``runtime_payload``). Used when a
    # setting has a dedicated UI control elsewhere — e.g. ``wake_threshold`` is edited
    # by the voice-panel slider, so it must not appear twice in the form.
    hidden: bool = False
