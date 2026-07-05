"""runtime_settings.resolve — validation, coercion, and value resolution.

The priority chain is the same in every resolution::

    runtime_settings.json  >  env (value baked into Settings)  >  default

:func:`get_runtime` uses the path with settings; :func:`runtime_override` uses
the process-wide bound store for env-only modules (planner, context). Neither
ever raises; if the store/data_dir is absent, falls back to env/default.
"""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path
from typing import Any

# Module-alias required (_store._ACTIVE_DATA_DIR must be read LIVE; the value
# cannot be imported). `from . import store` would create a module-level back-edge
# to the package __init__ causing a cycle (caught by arch test); absolute submodule
# import moves the edge to the leaf.
import akana_server.runtime_settings.store as _store
from .schema import SCHEMA
from .spec import RuntimeSettingError, RuntimeSettingSpec

log = logging.getLogger(__name__)

# Bool env parsing sets — consistent with historical semantics in config.py.
_FALSY = {"0", "false", "no", "off"}
_TRUTHY = {"1", "true", "yes", "on"}


def _as_str_list(raw: Any, *, sep: str) -> list[str]:
    if isinstance(raw, str):
        items = [p.strip() for p in raw.split(sep)]
    elif isinstance(raw, (list, tuple)):
        items = [str(p).strip() for p in raw]
    else:
        raise RuntimeSettingError("expected a list or delimited text")
    return [p for p in items if p]


def validate_value(spec: RuntimeSettingSpec, raw: Any) -> Any:
    """Validate a raw value and reduce it to the JSON type for storage.

    Errors are raised as :class:`RuntimeSettingError`; these messages are shown
    directly to the user.
    """
    if spec.type == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lower() in _TRUTHY | _FALSY:
            return raw.strip().lower() in _TRUTHY
        raise RuntimeSettingError(
            f"«{spec.label}» expects a true/false (boolean) value"
        )
    if spec.type in ("int", "float"):
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise RuntimeSettingError(f"«{spec.label}» expects a number")
        try:
            num = (
                int(str(raw).strip()) if spec.type == "int" else float(str(raw).strip())
            )
        except (TypeError, ValueError) as e:
            raise RuntimeSettingError(
                f"«{spec.label}» enter a valid number (got: {raw!r})"
            ) from e
        # Reject NaN/±Inf: they slip past the min/max comparisons below (every
        # comparison with NaN is False) and json.loads accepts the bare ``NaN``/
        # ``Infinity`` tokens by default — an unguarded NaN would be persisted to
        # runtime_settings.json and silently disable any consumer that compares
        # against the value (e.g. wake_threshold: ``score >= NaN`` is always False).
        if isinstance(num, float) and not math.isfinite(num):
            raise RuntimeSettingError(
                f"«{spec.label}» enter a valid number (got: {raw!r})"
            )
        if spec.min is not None and num < spec.min:
            raise RuntimeSettingError(
                f"«{spec.label}» must be at least {spec.min:g}{(' ' + spec.unit) if spec.unit else ''}"
            )
        if spec.max is not None and num > spec.max:
            raise RuntimeSettingError(
                f"«{spec.label}» must be at most {spec.max:g}{(' ' + spec.unit) if spec.unit else ''}"
            )
        return num
    if spec.type == "str":
        if raw is None:
            return ""
        if not isinstance(raw, str):
            raise RuntimeSettingError(f"«{spec.label}» expects text")
        value = raw.strip()
        # Enum enforcement: spec.options previously only drove the UI <select>
        # (payload.py) — PUT /settings/runtime never checked it, so any
        # non-UI client could store an out-of-enum value (e.g. a misspelled
        # voice name) that consumers then silently mishandle. Empty string
        # (unset) is still allowed through so "not configured" is not
        # confused with an invalid choice.
        if spec.options is not None and value and value not in spec.options:
            raise RuntimeSettingError(
                f"«{spec.label}» must be one of: {', '.join(spec.options)}"
            )
        return value
    if spec.type == "csv":
        items = _as_str_list(raw, sep=",")
        if len(items) > 100:
            raise RuntimeSettingError(f"«{spec.label}» can take at most 100 items")
        return items
    if spec.type == "paths":
        items = _as_str_list(raw, sep=os.pathsep)
        for item in items:
            # Cross-platform absoluteness: ``os.path.isabs`` dispatches per-OS, so
            # ``C:\…``/UNC pass on Windows and ``/…`` on POSIX; ``~`` (home) is always
            # accepted. Mirrors the downstream expanduser+resolve in FileService.
            if not (item.startswith("~") or os.path.isabs(os.path.expanduser(item))):
                raise RuntimeSettingError(
                    f"«{spec.label}» requires an absolute path: {item!r} (must start with '/' or '~')"
                )
        return items
    raise RuntimeSettingError(f"unknown setting type: {spec.type}")  # pragma: no cover


def _coerce_runtime(spec: RuntimeSettingSpec, stored: Any) -> Any:
    """Convert a JSON value from the store to the consumer type (None on corruption → fallback)."""
    try:
        validated = validate_value(spec, stored)
    except RuntimeSettingError:
        log.warning(
            "Corrupt runtime setting, falling back to env/default: %s=%r", spec.key, stored
        )
        return None
    if spec.type == "csv":
        return tuple(validated)
    if spec.type == "paths":
        return tuple(Path(os.path.expanduser(p)).resolve() for p in validated)
    return validated


def _env_fallback(spec: RuntimeSettingSpec) -> tuple[bool, Any]:
    """(is set in env, value) — only parses keys without settings_attr."""
    raw = os.environ.get(spec.env_var, "").strip()
    if not raw:
        return False, spec.default
    if spec.env_parse is not None:
        return True, spec.env_parse(raw)
    try:
        if spec.type == "bool":
            # Mirror validate_value / config.py: only KNOWN tokens flip the
            # value; an unrecognized string falls back to the schema default
            # instead of the old "anything not falsy = true" rule, which flipped
            # default-OFF bools ON from any typo (e.g. "disabled") and diverged
            # from the store/UI path (validate_value rejects unknown tokens).
            low = raw.lower()
            if low in _TRUTHY:
                return True, True
            if low in _FALSY:
                return True, False
            log.warning("%s=%r invalid bool; using default", spec.env_var, raw)
            return True, bool(spec.default)
        if spec.type in ("int", "float"):
            # Enforce the SAME schema bounds (and NaN/Inf rejection) that the PUT
            # validator applies, so an env-only numeric setting cannot smuggle in
            # an out-of-range value that /settings/runtime would reject.
            return True, validate_value(spec, raw)
        if spec.type == "str" and spec.options is not None:
            # Enforce spec.options on the env layer too: PUT rejects an out-of-enum
            # str, but a raw env value (e.g. a misspelled voice name) would
            # otherwise reach the consumer unvalidated and fail obscurely at the
            # remote API. Fall back to the default on mismatch, like PUT.
            return True, validate_value(spec, raw)
    except (ValueError, RuntimeSettingError):
        log.warning("%s=%r invalid; using default", spec.env_var, raw)
        return True, spec.default
    return True, raw


def get_runtime(key: str, settings: Any) -> Any:
    """Resolved value: runtime > env (baked into Settings) > default.

    Never raises — if the store/data_dir is absent, falls back to the settings
    attr (env-or-default) or the schema default. ``settings`` is duck-typed
    (test doubles supported).
    """
    spec = SCHEMA.get(key)
    if spec is None:
        raise KeyError(f"unknown runtime setting: {key}")
    data_dir = getattr(settings, "data_dir", None)
    if data_dir is not None:
        try:
            stored = _store.get_store(data_dir).load()
        except Exception:  # store failure must never break setting resolution
            stored = {}
        if spec.key in stored:
            value = _coerce_runtime(spec, stored[spec.key])
            # ``_coerce_runtime`` returns None ONLY on corruption (validation
            # failed) — a valid "str" value, including "", never coerces to
            # None (validate_value's str branch always returns a string on
            # success). The old ``or spec.type == "str"`` clause was dead for
            # valid values and only fired on corruption, wrongly returning
            # None here instead of falling through to env/default.
            if value is not None:
                return value
    if spec.settings_attr is not None:
        value = getattr(settings, spec.settings_attr, spec.default)
        # The env layer bakes AKANA_* into Settings with only a strip() (config.py),
        # so an out-of-enum str (e.g. a misspelled AKANA_GEMINI_LIVE_VOICE) would
        # reach the provider unvalidated — the exact case PUT rejects. Apply the
        # same options check here; on mismatch fall back to the schema default.
        if (
            spec.type == "str"
            and spec.options is not None
            and isinstance(value, str)
            and value
            and value not in spec.options
        ):
            log.warning(
                "%s=%r not in options; using default %r",
                spec.env_var,
                value,
                spec.default,
            )
            return spec.default
        return value
    return _env_fallback(spec)[1]


def resolve_language(settings: Any) -> str:
    """Active language (``en``|``tr``) from the runtime ``language`` setting.

    English-first default: any failure or unrecognized value collapses to ``en``.
    ``settings`` is duck-typed (only ``.data_dir`` is read, via :func:`get_runtime`);
    callers holding a bare ``data_dir`` pass ``SimpleNamespace(data_dir=...)``.
    """
    try:
        lang = str(get_runtime("language", settings) or "en").strip().lower()
        return lang if lang in ("tr", "en") else "en"
    except Exception:
        return "en"


def runtime_override(key: str) -> Any | None:
    """Override from the process-wide bound store (None if absent or unbound).

    For call paths that do not carry Settings (planner decomposer, context
    assembler): if None is returned, the caller continues with its own
    env/default chain.
    """
    spec = SCHEMA.get(key)
    if spec is None or _store._ACTIVE_DATA_DIR is None:
        return None
    try:
        stored = _store.get_store(_store._ACTIVE_DATA_DIR).load()
    except Exception:
        return None
    if spec.key not in stored:
        return None
    return _coerce_runtime(spec, stored[spec.key])


def resolve_source(key: str, settings: Any) -> str:
    """Layer the value comes from: ``runtime`` | ``env`` | ``default``.

    Must mirror the SAME priority as :func:`get_runtime` (#17): if the store
    contains a key but the value is CORRUPT, ``get_runtime`` does NOT use it
    (coerce returns None → falls back to env/default); reporting "runtime" in
    that case would be the wrong layer. Therefore we also coerce the store value
    here and only return "runtime" when ``get_runtime`` would actually use it
    (valid value or ``str`` type).
    """
    spec = SCHEMA[key]
    data_dir = getattr(settings, "data_dir", None)
    if data_dir is not None:
        try:
            stored = _store.get_store(data_dir).load()
        except Exception:
            stored = {}
        if spec.key in stored:
            value = _coerce_runtime(spec, stored[spec.key])
            # Mirror get_runtime's fix (see the comment there): None only
            # ever means corruption, never a valid "str" value.
            if value is not None:
                return "runtime"
    if os.environ.get(spec.env_var, "").strip():
        return "env"
    return "default"
