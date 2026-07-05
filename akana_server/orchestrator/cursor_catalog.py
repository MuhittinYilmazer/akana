"""Cursor model catalog and API health via Node bridge (``Cursor.models.list``)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from akana_server.config import Settings
from akana_server.llm_settings import cursor_model_options
from akana_server.orchestrator.catalog_core import (
    CatalogCache,
    build_models_response,
    key_fingerprint,
)
from akana_server.orchestrator.cursor_provider import bridge_env, runtime_cursor_key

log = logging.getLogger(__name__)

_PROBE_TIMEOUT = 15.0


_cache = CatalogCache()


def _key_fingerprint(settings: Settings) -> str:
    return key_fingerprint(runtime_cursor_key(settings))


def invalidate_cursor_catalog_cache() -> None:
    """Reset the cache when the key changes or on a forced refresh."""
    _cache.invalidate()


async def _run_list_models_bridge(settings: Settings) -> dict[str, Any]:
    script = settings.bridge_dir / "list_models.mjs"
    if not script.is_file():
        return {"ok": False, "error": f"bridge script missing: {script}"}

    proc = await asyncio.create_subprocess_exec(
        "node",
        str(script),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=bridge_env(settings),
        # Own process group → on timeout terminate_process_group takes down ALL
        # of the node bridge's child processes (the Cursor SDK grandchildren); a
        # bare proc.kill() would only kill the direct node child and leave the
        # grandchildren orphaned.
        start_new_session=True,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=_PROBE_TIMEOUT
        )
    except TimeoutError:
        from akana_server.orchestrator.llm_process import terminate_process_group

        await terminate_process_group(proc.pid)
        return {"ok": False, "error": "Cursor API probe timed out"}
    except asyncio.CancelledError:
        # The inflight refresh task this probe runs in was cancelled (key
        # rotation via invalidate_cursor_catalog_cache(), or a force_refresh —
        # see _fetch_bridge_cached) mid-``communicate()``. No pid file was
        # registered for this probe subprocess, so the boot-time orphan reaper
        # cannot clean it up either — the process group (and its Cursor SDK
        # children) must be terminated here before the cancellation propagates,
        # same as the TimeoutError branch above.
        from akana_server.orchestrator.llm_process import terminate_process_group

        await asyncio.shield(terminate_process_group(proc.pid))
        raise

    raw_out = (stdout_b or b"").decode("utf-8", errors="replace").strip()
    raw_err = (stderr_b or b"").decode("utf-8", errors="replace").strip()
    line = raw_out.splitlines()[-1].strip() if raw_out else ""
    if not line:
        return {"ok": False, "error": raw_err or "empty bridge output"}
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return {"ok": False, "error": f"invalid bridge output: {raw_out[:200]}"}
    if proc.returncode != 0 and parsed.get("ok") is not True:
        return {
            "ok": False,
            "error": str(parsed.get("error") or raw_err or "list_models failed"),
        }
    return parsed


async def _fetch_bridge_cached(
    settings: Settings, *, force_refresh: bool = False
) -> dict[str, Any]:
    """Single-flight + TTL cache (shared :mod:`catalog_core` discipline).

    The core carries the follower-recovery fix for "a concurrent force_refresh
    cancelled the shared inflight task" — previously only :mod:`.claude_catalog`
    had it.
    """
    fp = _key_fingerprint(settings)
    if not fp:
        return {"ok": False, "error": "CURSOR_API_KEY is not set"}

    # `_run_list_models_bridge` is resolved at call time so tests can monkeypatch it.
    return await _cache.get(
        fp, lambda: _run_list_models_bridge(settings), force_refresh=force_refresh
    )


def _options_from_sdk(models: list[dict[str, Any]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in models:
        mid = str(raw.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        label = str(raw.get("displayName") or mid).strip() or mid
        desc = raw.get("description")
        if isinstance(desc, str) and desc.strip():
            label = f"{label} — {desc.strip()}"[:120]
        out.append({"value": mid, "label": label})
    out.sort(key=lambda x: x["label"].casefold())
    return out


def _friendly_bridge_error(raw: str) -> str:
    """Turn a raw Node bridge failure into an actionable one-liner.

    A missing bridge (``node_modules/@cursor/sdk`` absent — e.g. the user picked cursor
    in the first-run onboarding without ``python akana.py add cursor``) surfaces as a raw
    ``ERR_MODULE_NOT_FOUND`` stack. The onboarding/settings banner shows this verbatim, so
    map the known signatures to a hint instead of a cryptic Node trace."""
    low = raw.lower()
    if "@cursor/sdk" in low or "err_module_not_found" in low or "cannot find package" in low:
        return "Cursor bridge not installed — run: python akana.py add cursor"
    return raw


def _bridge_error_code(raw: str) -> str:
    """Stable, language-neutral tag for a bridge failure (``bridge_missing`` |
    ``auth_rejected`` | ``unreachable``). The onboarding wizard maps it to a localized
    message so a TR banner isn't the verbatim English ``error`` string; surfaces without
    a dictionary (the settings panel) keep showing ``error`` as the English default.

    ``auth_rejected`` is the AUTH-CERTAIN class: the bridge got a definitive
    "this key is bad" answer (401 / unauthorized / invalid api key). The wizard treats
    it as a hard rejection (revert a just-saved key to the prior provider), whereas the
    catch-all ``unreachable`` (network blip, bridge warming up) keeps the keyed provider
    selected with an amber warning — a transient blip must not un-select a valid key."""
    low = raw.lower()
    if "@cursor/sdk" in low or "err_module_not_found" in low or "cannot find package" in low:
        return "bridge_missing"
    if (
        "401" in low
        or "unauthorized" in low
        or "invalid api key" in low
        or "invalid user api key" in low
        or "invalid_api_key" in low
    ):
        return "auth_rejected"
    return "unreachable"


def _probe_from_bridge(parsed: dict[str, Any]) -> dict[str, Any]:
    if not parsed.get("ok"):
        raw = str(parsed.get("error") or "Cursor API unreachable")
        return {
            "key_set": True,
            "reachable": False,
            "error": _friendly_bridge_error(raw),
            "error_code": _bridge_error_code(raw),
            "model_count": 0,
        }
    models = parsed.get("models") if isinstance(parsed.get("models"), list) else []
    return {
        "key_set": True,
        "reachable": True,
        "error": None,
        "error_code": None,
        "model_count": len(models),
    }


async def probe_cursor_api(
    settings: Settings, *, force_refresh: bool = False
) -> dict[str, Any]:
    """Live Cursor API health — auth + catalog reachability (cached).

    ``error_code`` is a stable, language-neutral tag (``no_key`` | ``bridge_missing`` |
    ``auth_rejected`` | ``unreachable``) the onboarding wizard maps to a localized message;
    ``error`` stays as the English default for surfaces without a dictionary (the settings
    panel). ``auth_rejected`` is auth-certain (a bad key); ``unreachable`` is transient."""
    key = runtime_cursor_key(settings)
    if not key:
        return {
            "key_set": False,
            "reachable": False,
            "error": "CURSOR_API_KEY is not set",
            "error_code": "no_key",
            "model_count": 0,
        }
    parsed = await _fetch_bridge_cached(settings, force_refresh=force_refresh)
    return _probe_from_bridge(parsed)


async def fetch_cursor_models(
    settings: Settings, *, force_refresh: bool = False
) -> dict[str, Any]:
    """Live model catalog for UI (mirrors Ollama ``/system/ollama/models`` shape).

    No key → static fallback; the shared :func:`catalog_core.build_models_response`
    assembles the rest."""
    from akana_server.llm_settings import load_llm_settings, resolve_cursor_model_tag

    llm = load_llm_settings(settings.data_dir, settings)
    active = resolve_cursor_model_tag(settings, llm)
    fallback = cursor_model_options()

    return await build_models_response(
        _cache,
        fp=_key_fingerprint(settings),
        fetch=lambda fr: _fetch_bridge_cached(settings, force_refresh=fr),
        options_from=_options_from_sdk,
        fallback=fallback,
        active=active,
        preconditions=[
            (bool(runtime_cursor_key(settings)), "CURSOR_API_KEY is not set"),
        ],
        unreachable_error="Cursor API unreachable",
        force_refresh=force_refresh,
    )


def probe_cursor_api_sync(settings: Settings) -> dict[str, Any]:
    """Sync probe for ``akana doctor`` (no running event loop)."""
    return asyncio.run(probe_cursor_api(settings))
