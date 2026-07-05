"""RuntimeSettings — no-restart settings layer (single source of truth: schema).

Stored in ``<data_dir>/runtime_settings.json``, these settings can be changed
AT RUNTIME. The priority chain is the same in every resolution::

    runtime_settings.json  >  env (value baked into Settings)  >  default

Design contract:

* **Schema is the single source** — REST (GET /settings/runtime) and the UI form
  are generated from :data:`SCHEMA` in this module; each setting's type, bounds,
  description, and category live here.
* **Honest restart flag** — settings marked ``restart_required=True`` (Telegram)
  are NOT applied to the running process; they are applied to Settings on restart
  via :func:`apply_runtime_overrides`. No fake "applied immediately".
* **Resolution at call time** — consumers read the value via :func:`get_runtime`
  (path with settings) or :func:`runtime_override` (env-only modules: planner,
  context); the frozen ``Settings`` dataclass is never mutated.
* **Defensive** — never raises if the store is unreadable or data_dir is absent;
  falls back to env/default (SimpleNamespace test doubles are supported).
* **Atomic writes** — tmp file + ``os.replace`` (half-written JSON is never visible).
"""

from __future__ import annotations

from .apply import apply_runtime_overrides, rebuild_app_settings
# `_payload_value` / `_coerce_runtime` / `_env_fallback` / `resolve_source` are
# re-exported so they remain accessible from the package namespace (sub-module
# internal API).
from .payload import _payload_value, runtime_payload  # noqa: F401
from .resolve import (  # noqa: F401
    _coerce_runtime,
    _env_fallback,
    get_runtime,
    resolve_language,
    resolve_source,
    runtime_override,
    validate_value,
)
from .schema import CATEGORIES, SCHEMA
from .spec import RuntimeSettingError, RuntimeSettingSpec
from .store import (
    RuntimeStore,
    bind_runtime_data_dir,
    get_store,
    reset_runtime_stores,
)

__all__ = [
    "SCHEMA",
    "CATEGORIES",
    "RuntimeSettingError",
    "RuntimeSettingSpec",
    "RuntimeStore",
    "apply_runtime_overrides",
    "bind_runtime_data_dir",
    "rebuild_app_settings",
    "get_runtime",
    "get_store",
    "reset_runtime_stores",
    "resolve_language",
    "runtime_override",
    "runtime_payload",
    "validate_value",
]
