"""runtime_settings.payload — GET /settings/runtime response body (schema is the single source).

The UI form is generated from :func:`runtime_payload` output: categories + a schema
row for each setting + the resolved value + the source layer (runtime/env/default).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .resolve import get_runtime, resolve_source
from .schema import CATEGORIES, _SPECS
from .spec import RuntimeSettingSpec

def _payload_value(spec: RuntimeSettingSpec, value: Any) -> Any:
    """JSON-clean value (Path → str, tuple → list)."""
    if isinstance(value, tuple):
        return [str(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if value is None:
        return ""
    return value


def runtime_payload(settings: Any, *, include_hidden: bool = False) -> dict[str, Any]:
    """GET /settings/runtime body: categories + schema + value + source.

    ``hidden`` specs are skipped by default (they have a dedicated UI control, e.g.
    ``wake_threshold`` → the voice-panel slider). The read-only ``/settings/effective``
    debug view passes ``include_hidden=True`` to still show the full resolved config.
    """
    items: list[dict[str, Any]] = []
    for spec in _SPECS:
        if spec.hidden and not include_hidden:
            continue
        items.append(
            {
                "key": spec.key,
                "label": spec.label,
                "description": spec.description,
                "category": spec.category,
                "type": spec.type,
                "env_var": spec.env_var,
                "min": spec.min,
                "max": spec.max,
                "unit": spec.unit,
                "restart_required": spec.restart_required,
                "hidden": spec.hidden,
                "options": list(spec.options) if spec.options else None,
                "default": _payload_value(spec, spec.default),
                "value": _payload_value(spec, get_runtime(spec.key, settings)),
                "source": resolve_source(spec.key, settings),
            }
        )
    return {"categories": [dict(c) for c in CATEGORIES], "settings": items}
