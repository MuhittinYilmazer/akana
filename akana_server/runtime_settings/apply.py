"""runtime_settings.apply — Settings overlay (restart-required honest application).

Called at server startup via :func:`apply_runtime_overrides`: ``restart_required``
settings (Telegram) are ACTUALLY applied to Settings on restart.

Also :func:`rebuild_app_settings` applies **non-restart** settings (such as
wake_threshold) with ``settings_attr`` to ``app.state.settings`` immediately on
PUT/reset: a fresh ``load_settings()`` base is taken, current store overrides are
applied on top, and the live snapshot is replaced. Since all consumers read
``request.app.state.settings`` FRESH on every request (no long-lived capture),
the next request sees the new value.
"""

from __future__ import annotations

import logging
from typing import Any

# Absolute submodule import: `from . import store` was creating a cycle back to
# the package __init__ (caught by arch test); _store module-alias is preserved
# (for get_store access).
import akana_server.runtime_settings.store as _store
from .resolve import _coerce_runtime
from .schema import _SPECS

log = logging.getLogger(__name__)


def apply_runtime_overrides(settings: Any) -> Any:
    """Apply runtime values to Settings (only keys with ``settings_attr``).

    Called at server startup so that ``restart_required`` settings (Telegram)
    are ACTUALLY applied on restart; also keeps startup paths that read settings
    directly consistent with runtime values. On error, settings is returned as-is.
    """
    import dataclasses

    data_dir = getattr(settings, "data_dir", None)
    if data_dir is None or not dataclasses.is_dataclass(settings):
        return settings
    try:
        stored = _store.get_store(data_dir).load()
    except Exception:
        return settings
    patch: dict[str, Any] = {}
    for spec in _SPECS:
        if spec.settings_attr is None or spec.key not in stored:
            continue
        value = _coerce_runtime(spec, stored[spec.key])
        # None only ever means corruption — a valid value (incl. "" for a str
        # spec) never coerces to None. Skip ALL corrupt values so the field
        # falls back to env/default; the old ``and spec.type != "str"`` carve-out
        # stamped None onto str fields, violating their non-Optional contract and
        # dropping the env-baked value.
        if value is None:
            continue
        patch[spec.settings_attr] = value
    if not patch:
        return settings
    try:
        return dataclasses.replace(settings, **patch)
    except (TypeError, ValueError):
        log.warning("Failed to apply runtime overrides to Settings", exc_info=True)
        return settings


def rebuild_app_settings(app: Any) -> Any:
    """Apply non-restart settings with ``settings_attr`` to live ``app.state.settings``.

    Called after PUT/reset. Instead of patching the existing snapshot piece by piece,
    a FRESH ``load_settings()`` base is taken and current store overrides are applied
    on top — so **reset** also works correctly (a key deleted from the store is no longer
    in the patch → the value reverts to env/default; patching the old override-stamped
    snapshot could not do this). The result is the same composition as at startup
    (config + override).

    Since Settings is frozen, constructing a new instance and ASSIGNING it to
    ``app.state.settings`` is safe: all consumers read this attribute fresh on every
    request (no long-lived object capture). Failure never breaks PUT — the current
    snapshot is preserved and returned.
    """
    from akana_server.config import load_settings

    current = getattr(getattr(app, "state", None), "settings", None)
    try:
        fresh = load_settings()
        rebuilt = apply_runtime_overrides(fresh)
    except Exception:  # pragma: no cover - snapshot refresh must not break PUT
        log.warning("Failed to rebuild app.state.settings", exc_info=True)
        return current
    try:
        app.state.settings = rebuilt
    except Exception:  # pragma: no cover - keep old snapshot if state is not writable
        log.warning("Failed to assign app.state.settings", exc_info=True)
        return current
    return rebuilt
