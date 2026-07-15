"""Persisted LLM settings.

Only two knobs survive: the active provider/model selection and how many chat
turns to keep in conversation history.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from akana_server.config import Settings
from akana_server.json_store import lock_for, write_json_atomic

log = logging.getLogger(__name__)

_SETTINGS_FILE = "llm_settings.json"

_CURSOR_MODEL_OPTIONS: list[dict[str, str]] = [
    {"value": "composer-2", "label": "Composer 2 (default)"},
    {"value": "default", "label": "Default (Cursor decides)"},
    {"value": "claude-haiku-4-5", "label": "Claude Haiku 4.5 (cheap)"},
    {"value": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6"},
    {"value": "claude-opus-4-7", "label": "Claude Opus 4.7 (powerful)"},
    {"value": "gpt-5.4-nano", "label": "GPT-5.4 Nano"},
    {"value": "gpt-5.4-mini", "label": "GPT-5.4 Mini"},
    {"value": "gemini-3-flash", "label": "Gemini 3 Flash"},
    {"value": "kimi-k2.5", "label": "Kimi K2.5"},
]


# Bare aliases: the claude CLI always resolves these to the LATEST version of the
# corresponding class; they do not go stale and do not 404. (Mirrors
# claude_provider._KNOWN_MODEL_ALIASES; defined separately here to avoid an
# import cycle.)
_CLAUDE_MODEL_ALIASES = frozenset({"opus", "sonnet", "haiku"})


_CLAUDE_MODEL_OPTIONS: list[dict[str, str]] = [
    # Always the latest — leaves the version choice to the claude CLI.
    {"value": "opus", "label": "Opus (latest — most powerful)"},
    {"value": "sonnet", "label": "Sonnet (latest — balanced)"},
    {"value": "haiku", "label": "Haiku (latest — fastest)"},
    # Pinned versions — to lock onto a specific version.
    {"value": "claude-opus-4-7", "label": "Claude Opus 4.7"},
    {"value": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6 (default)"},
    {"value": "claude-haiku-4-5", "label": "Claude Haiku 4.5"},
]


# Gemini (DIRECT Google API — the user's own key, NOT Cursor's key).
# If the optional google-genai is not installed, the provider can be selected but
# the call gives a clear "unavailable" error (gemini_provider make_client returns
# None). These are NATIVE Google model names; do not confuse them with the
# switcher's "gemini-3-flash" (Cursor-routed) — that is cursor_model, this is
# gemini_model.
_GEMINI_MODEL_OPTIONS: list[dict[str, str]] = [
    {"value": "gemini-2.5-flash", "label": "Gemini 2.5 Flash (fast)"},
    {"value": "gemini-2.5-pro", "label": "Gemini 2.5 Pro (powerful)"},
    {"value": "gemini-2.0-flash", "label": "Gemini 2.0 Flash"},
]


# OpenAI (DIRECT OpenAI API — the user's own key, NOT Cursor's key).
# These are NATIVE OpenAI model names; do not confuse them with the switcher's
# "gpt-5.4-*" (Cursor-routed) — that is cursor_model, this is openai_model. The
# first option is the default (resolve_openai_model_tag falls back to it when the
# setting is empty + there is no env; symmetric with _GEMINI).
_OPENAI_MODEL_OPTIONS: list[dict[str, str]] = [
    {"value": "gpt-5.4", "label": "GPT-5.4 (powerful)"},
    {"value": "gpt-5.4-mini", "label": "GPT-5.4 Mini (fast)"},
    {"value": "o5-mini", "label": "o5 Mini (reasoning)"},
]


# Codex (OpenAI Codex CLI — ChatGPT subscription auth via `codex login`, NOT an API
# key). These are the Codex-family model tags passed to the CLI via ``-m``; they are
# a CURATED STATIC list (the Codex CLI has no key-authorized ``/v1/models`` catalog
# endpoint like OpenAI's platform API — it authenticates through the ChatGPT OAuth
# session in ``~/.codex/auth.json``). SOURCE: OpenAI Codex model docs
# (developers.openai.com/codex/models), captured 2026-07. The first option is the
# default (``resolve_codex_model_tag`` falls back to it). The value is passed
# verbatim to ``codex exec -m <value>``; a plan that lacks a given model returns a
# clear CLI error, so the user can pick another from this list.
_CODEX_MODEL_OPTIONS: list[dict[str, str]] = [
    {"value": "gpt-5-codex", "label": "GPT-5 Codex (default)"},
    {"value": "gpt-5-codex-mini", "label": "GPT-5 Codex Mini (fast/cheap)"},
    {"value": "gpt-5.2-codex", "label": "GPT-5.2 Codex"},
    {"value": "gpt-5.4-codex", "label": "GPT-5.4 Codex (powerful)"},
    {"value": "gpt-5.3-codex-spark", "label": "GPT-5.3 Codex Spark (real-time)"},
]


# Stable defaults: cursor + claude (no badge). gemini/ollama/openai are not yet
# mature → the "badge" field is drawn in the UI as an "in development" marker (the
# single source of truth is here; the frontend produces the badge from this field).
_PROVIDER_OPTIONS: list[dict[str, str]] = [
    {"value": "cursor", "label": "Cursor (API)"},
    {"value": "claude", "label": "Claude CLI (subscription)"},
    {"value": "ollama", "label": "Ollama (local model)", "badge": "in development"},
    {"value": "gemini", "label": "Gemini (direct API)", "badge": "in development"},
    {"value": "openai", "label": "OpenAI (direct API)", "badge": "in development"},
    # Codex CLI: ChatGPT-subscription-billed (via `codex login`), NOT the API-key
    # openai provider above. The two are independent — openai uses OPENAI_API_KEY +
    # the platform API; codex bridges the `codex exec` CLI onto the ChatGPT session.
    {"value": "codex", "label": "Codex CLI (subscription)", "badge": "in development"},
]

_VALID_PROVIDERS = {opt["value"] for opt in _PROVIDER_OPTIONS}

# The claude provider runs with FULL PERMISSIONS by default (bypassPermissions:
# every tool including Bash/Edit/Write, without asking for approval). A deliberate
# choice for a single-user local assistant. It can be turned off with
# ``AKANA_CLAUDE_FULL_TOOLS=0``, and the dashboard ``claude_full_tools`` setting
# overrides everything.
_FULL_TOOLS_DEFAULT = True
_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})
_FALSE_TOKENS = frozenset({"0", "false", "no", "off"})


def _env_full_tools_override() -> bool | None:
    """``AKANA_CLAUDE_FULL_TOOLS`` → True/False, or None if unset (no override)."""
    raw = os.environ.get("AKANA_CLAUDE_FULL_TOOLS", "").strip().lower()
    if raw in _TRUE_TOKENS:
        return True
    if raw in _FALSE_TOKENS:
        return False
    return None


def cursor_model_options() -> list[dict[str, str]]:
    return list(_CURSOR_MODEL_OPTIONS)


def claude_model_options() -> list[dict[str, str]]:
    return list(_CLAUDE_MODEL_OPTIONS)


def gemini_model_options() -> list[dict[str, str]]:
    return list(_GEMINI_MODEL_OPTIONS)


def openai_model_options() -> list[dict[str, str]]:
    return list(_OPENAI_MODEL_OPTIONS)


def codex_model_options() -> list[dict[str, str]]:
    return list(_CODEX_MODEL_OPTIONS)


def provider_options() -> list[dict[str, str]]:
    return list(_PROVIDER_OPTIONS)


@dataclass
class LlmSettings:
    cursor_model: str = ""
    chat_max_turns: int = 12
    # Voice preferences (server-side persisted; UI mirrors them).
    tts_lang: str = "auto"  # auto | tr | en
    # NOTE: wake_threshold is no longer here — the single source of truth is the
    # runtime_settings schema (env WAKE_THRESHOLD, settings_attr wake_threshold).
    # "" = follow env (Settings.llm_provider); else "cursor" | "claude".
    provider: str = ""
    # "" = follow env (Settings.claude_model); else a claude-* tag.
    claude_model: str = ""
    # "" = follow env (Settings.ollama_model); else any installed Ollama model tag.
    ollama_model: str = ""
    # "" = follow env / default (resolve_gemini_model_tag → gemini-2.5-flash); else
    # a NATIVE Google model id (gemini-* / models/gemini-*). Independent: cursor_model
    # "gemini-3-flash" (Gemini via Cursor) does NOT mix with this.
    gemini_model: str = ""
    # "" = follow env (OPENAI_MODEL) / default (resolve_openai_model_tag → the first
    # _OPENAI_MODEL_OPTIONS option); else a NATIVE OpenAI model id (gpt-* / o*).
    # Independent: cursor_model "gpt-5.4-mini" (OpenAI via Cursor) does NOT mix with this.
    openai_model: str = ""
    # "" = follow env (CODEX_MODEL) / default (resolve_codex_model_tag → the first
    # _CODEX_MODEL_OPTIONS option); else a Codex-family model tag passed to `codex exec
    # -m`. Independent from openai_model (that is the platform API; this is the CLI).
    codex_model: str = ""
    # claude provider full permissions (bypassPermissions). On by default; if turned
    # off, Bash/Edit/Write are blocked and the permission-mode falls back to "default".
    claude_full_tools: bool = _FULL_TOOLS_DEFAULT

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _settings_path(data_dir: Path) -> Path:
    return data_dir / _SETTINGS_FILE


def _clamp_int(value: Any, *, lo: int, hi: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
    return default


def defaults_from_env(settings: Settings) -> LlmSettings:
    # Single source: the fixed defaults live in the LlmSettings dataclass fields
    # (chat_max_turns/tts_lang/provider/claude_model). Here ONLY the fields derived
    # from env/settings are overridden — writing the literal a second time would
    # create "which value wins" confusion (it would not silently follow the
    # dataclass default).
    env_full = _env_full_tools_override()
    return LlmSettings(
        cursor_model=settings.cursor_model.strip(),
        # ollama_model STAYS "" (like claude_model: "" = follow env/llama3.1 in
        # resolve). Moving settings.ollama_model here would be inconsistent with
        # claude + would look "selected" in the UI; resolve_ollama_model_tag
        # already falls back to env.
        claude_full_tools=_FULL_TOOLS_DEFAULT if env_full is None else env_full,
    )


def _merge(base: LlmSettings, raw: dict[str, Any]) -> LlmSettings:
    cursor_model = str(raw.get("cursor_model") or base.cursor_model).strip()
    if cursor_model == "composer-2-fast":
        cursor_model = "composer-2"
    tts_lang = str(raw.get("tts_lang") or base.tts_lang).strip().lower()
    if tts_lang not in ("auto", "tr", "en"):
        tts_lang = "auto"
    # An out-of-enum provider falls back to base.provider (the same rule every
    # model field below follows). Reset-to-"" would silently WIPE a previously
    # valid selection from one malformed PUT / conversation override, breaking
    # chat with "no provider configured"; resolve_provider still maps a genuinely
    # unset value to "".
    provider = str(raw.get("provider") or base.provider).strip().lower()
    if provider not in _VALID_PROVIDERS:
        provider = base.provider
    claude_model = str(raw.get("claude_model") or base.claude_model).strip()
    if claude_model and not (
        claude_model.startswith("claude-") or claude_model in _CLAUDE_MODEL_ALIASES
    ):
        claude_model = base.claude_model
    # The Ollama model name is free-form (installed models are dynamic; /api/tags
    # lists them) → no shape validation, only a trim. If empty it falls back to
    # env/llama3.1 (resolve).
    ollama_model = str(raw.get("ollama_model") or base.ollama_model).strip()
    # The Gemini model accepts only a NATIVE Google name (gemini-* / models/...);
    # if a foreign tag (composer-2/default/claude-*) leaks in it falls back to base
    # — the same strictness as claude, but here cursor's "gemini-3-flash" alias is
    # not rejected either (it starts with gemini-) because that value is written to
    # the cursor_model field, not here.
    gemini_model = str(raw.get("gemini_model") or base.gemini_model).strip()
    if gemini_model and not (
        gemini_model.startswith("gemini-") or gemini_model.startswith("models/")
    ):
        gemini_model = base.gemini_model
    # The OpenAI model accepts only a NATIVE OpenAI name (gpt-* / o-series
    # reasoning: o1/o3/o5…); if a foreign tag (composer-2/default/claude-*/gemini-*)
    # leaks in it falls back to base — the same strictness as gemini. Cursor's
    # "gpt-5.4-mini" alias is written to the cursor_model field so it does not
    # reach here (no conflict).
    openai_model = str(raw.get("openai_model") or base.openai_model).strip()
    # o-series reasoning tags are o<digit>… (o1/o3/o5-mini); a bare "startswith('o')"
    # also let in foreign tags like "opus" (claude alias) and "ollama-llama3"
    # (neighbouring provider) — anchor to o+digit so only real OpenAI names pass.
    if openai_model and not (
        openai_model.startswith("gpt-") or re.match(r"o\d", openai_model)
    ):
        openai_model = base.openai_model
    # The Codex model accepts only a Codex-family tag (gpt-*-codex / gpt-*codex-* /
    # anything containing "codex"); a foreign tag (composer-2/claude-*/gemini-*) leaking
    # in falls back to base. Codex tags collide syntactically with the openai gpt-*
    # family, so the guard requires the "codex" substring — the native OpenAI gpt-5.4
    # (openai_model) must NOT be accepted here (and vice-versa), keeping the two
    # ChatGPT-family providers independent.
    codex_model = str(raw.get("codex_model") or base.codex_model).strip()
    if codex_model and "codex" not in codex_model.lower():
        codex_model = base.codex_model
    return LlmSettings(
        cursor_model=cursor_model,
        chat_max_turns=_clamp_int(
            raw.get("chat_max_turns"), lo=2, hi=64, default=base.chat_max_turns
        ),
        tts_lang=tts_lang,
        provider=provider,
        claude_model=claude_model,
        ollama_model=ollama_model,
        gemini_model=gemini_model,
        openai_model=openai_model,
        codex_model=codex_model,
        claude_full_tools=_coerce_bool(
            raw.get("claude_full_tools"), default=base.claude_full_tools
        ),
    )


def load_llm_settings(data_dir: Path, settings: Settings) -> LlmSettings:
    path = _settings_path(data_dir)
    base = defaults_from_env(settings)
    if not path.is_file():
        return base
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("could not read llm settings %s: %s", path, e)
        return base
    if not isinstance(raw, dict):
        return base
    return _merge(base, raw)


def save_llm_settings(data_dir: Path, llm: LlmSettings) -> LlmSettings:
    data_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(_settings_path(data_dir), llm.to_dict())
    return llm


def update_llm_settings(
    data_dir: Path, settings: Settings, patch: dict[str, Any]
) -> LlmSettings:
    # load+merge+save under the lock → concurrent PUTs do not overwrite each
    # other's write (lost-update) and the tmp/replace steps do not interleave.
    with lock_for(data_dir):
        return save_llm_settings(
            data_dir, _merge(load_llm_settings(data_dir, settings), patch)
        )


def resolve_cursor_model_tag(settings: Settings, llm: LlmSettings) -> str:
    return (llm.cursor_model or settings.cursor_model or "composer-2").strip()


def resolve_provider(settings: Settings, llm: LlmSettings) -> str:
    """Active LLM provider: persisted setting wins, else env. Empty if unconfigured.

    No provider is privileged as a default — an unset/invalid value resolves to ""
    ("unconfigured"), and chat refuses with a clear message until the user picks one.
    """
    provider = (llm.provider or settings.llm_provider or "").strip().lower()
    return provider if provider in _VALID_PROVIDERS else ""


def resolve_claude_model_tag(settings: Settings, llm: LlmSettings) -> str:
    """Active claude model: persisted setting wins, else env, else sonnet."""
    return (
        llm.claude_model
        or getattr(settings, "claude_model", "")
        or "claude-sonnet-4-6"
    ).strip()


def resolve_ollama_model_tag(settings: Settings, llm: LlmSettings) -> str:
    """Active ollama model: persisted setting wins, else env, else llama3.1."""
    return (
        llm.ollama_model
        or getattr(settings, "ollama_model", "")
        or "llama3.1"
    ).strip()


def resolve_gemini_model_tag(settings: Settings, llm: LlmSettings) -> str:
    """Active gemini model: persisted setting wins, else env, else gemini-2.5-flash.

    A NATIVE Google model name (gemini_provider gives it directly to Google). The
    dispatch ``model`` argument (the provider-agnostic cursor tag) does NOT reach
    here — gemini always uses this resolution (a stricter form of ollama's
    foreign-tag guard)."""
    return (
        llm.gemini_model
        or getattr(settings, "gemini_model", "")
        or "gemini-2.5-flash"
    ).strip()


#: ``resolve_openai_model_tag`` default = the first catalog option (single source;
#: changing the head of the list also changes the default). The env fallback
#: OPENAI_MODEL is the OpenAI SDK's own canonical name (NOT AKANA_*; the same logic
#: as gemini's GEMINI_API_KEY env fallback — the canonical source is still the
#: dashboard setting).
_OPENAI_MODEL_DEFAULT = _OPENAI_MODEL_OPTIONS[0]["value"]


def resolve_openai_model_tag(settings: Settings, llm: LlmSettings) -> str:
    """Active openai model: persisted setting wins, else env (OPENAI_MODEL), else default.

    A NATIVE OpenAI model name (openai_provider gives it directly to OpenAI). The
    dispatch ``model`` argument (the provider-agnostic cursor tag) does NOT reach
    here — openai always uses this resolution (the same pattern as gemini/ollama's
    foreign-tag guard). There is NO ``openai_model`` field in ``Settings`` → the
    env OPENAI_MODEL is read directly (a settings.openai_model test-double is still
    preferred via getattr)."""
    return (
        llm.openai_model
        or getattr(settings, "openai_model", "")
        or os.environ.get("OPENAI_MODEL", "").strip()
        or _OPENAI_MODEL_DEFAULT
    ).strip()


#: ``resolve_codex_model_tag`` default = the first catalog option (single source;
#: changing the head of the list also changes the default). The env fallback
#: CODEX_MODEL is the Codex CLI's own canonical env name (NOT AKANA_*; the same
#: logic as openai's OPENAI_MODEL env fallback — the canonical source is still the
#: dashboard setting).
_CODEX_MODEL_DEFAULT = _CODEX_MODEL_OPTIONS[0]["value"]


def resolve_codex_model_tag(settings: Settings, llm: LlmSettings) -> str:
    """Active codex model: persisted setting wins, else env (CODEX_MODEL), else default.

    A Codex-family model tag handed to ``codex exec -m``. The dispatch ``model``
    argument (the provider-agnostic cursor tag) does NOT reach here — codex always
    uses this resolution (the same foreign-tag guard as gemini/openai). There is NO
    ``codex_model`` field in ``Settings`` → the env CODEX_MODEL is read directly (a
    settings.codex_model test-double is still preferred via getattr)."""
    return (
        llm.codex_model
        or getattr(settings, "codex_model", "")
        or os.environ.get("CODEX_MODEL", "").strip()
        or _CODEX_MODEL_DEFAULT
    ).strip()


def resolve_claude_full_tools(settings: Settings, llm: LlmSettings) -> bool:
    """Is the claude provider at full permissions? The persisted setting is the
    single source; if there is no settings file, ``defaults_from_env`` has already
    applied the env override."""
    return bool(llm.claude_full_tools)


def public_llm_payload(
    llm: LlmSettings, *, settings: Settings, active_tag: str
) -> dict[str, Any]:
    return {
        "settings": llm.to_dict(),
        "active_cursor_model_tag": active_tag,
        "active_claude_model_tag": resolve_claude_model_tag(settings, llm),
        "active_ollama_model_tag": resolve_ollama_model_tag(settings, llm),
        "active_gemini_model_tag": resolve_gemini_model_tag(settings, llm),
        "active_openai_model_tag": resolve_openai_model_tag(settings, llm),
        "active_codex_model_tag": resolve_codex_model_tag(settings, llm),
        "active_claude_full_tools": resolve_claude_full_tools(settings, llm),
        "active_provider": resolve_provider(settings, llm),
        "cursor_models": cursor_model_options(),
        "claude_models": claude_model_options(),
        "gemini_models": gemini_model_options(),
        "openai_models": openai_model_options(),
        "codex_models": codex_model_options(),
        "providers": provider_options(),
        "defaults": defaults_from_env(settings).to_dict(),
    }


# -- Per-conversation LLM (json_metadata) -----------------------------------------

_CONV_LLM_FIELDS = (
    ("provider", "llm_provider"),
    ("cursor_model", "llm_cursor_model"),
    ("claude_model", "llm_claude_model"),
    ("ollama_model", "llm_ollama_model"),
    ("gemini_model", "llm_gemini_model"),
    ("openai_model", "llm_openai_model"),
    ("codex_model", "llm_codex_model"),
)


def conversation_llm_override_from_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Only explicit ``llm_*`` fields — turn execution / leak-guard."""
    patch: dict[str, Any] = {}
    for field, meta_key in _CONV_LLM_FIELDS:
        raw = meta.get(meta_key)
        if isinstance(raw, str) and raw.strip():
            patch[field] = raw.strip()
    return patch


def conversation_llm_patch_from_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Restore/UI: ``llm_*`` + the ``agent_provider`` hint (legacy ``cursor_agent_provider`` fallback)."""
    patch = conversation_llm_override_from_meta(meta)
    if "provider" not in patch:
        legacy = meta.get("agent_provider") or meta.get("cursor_agent_provider")
        if isinstance(legacy, str) and legacy.strip():
            prov = legacy.strip().lower()
            if prov in _VALID_PROVIDERS:
                patch["provider"] = prov
    return patch


def merge_conversation_llm(base: LlmSettings, meta: dict[str, Any]) -> LlmSettings:
    """Merge the global/base settings with the conversation override (explicit llm_* only)."""
    patch = conversation_llm_override_from_meta(meta)
    if not patch:
        return base
    return _merge(base, patch)


def merge_conversation_llm_for_restore(base: LlmSettings, meta: dict[str, Any]) -> LlmSettings:
    """Conversation-switch restore — including the legacy provider hint."""
    patch = conversation_llm_patch_from_meta(meta)
    if not patch:
        return base
    return _merge(base, patch)


def conversation_llm_to_meta(patch: dict[str, Any]) -> dict[str, Any]:
    """Convert an LLM patch to conversation metadata keys."""
    out: dict[str, Any] = {}
    for field, meta_key in _CONV_LLM_FIELDS:
        if field not in patch:
            continue
        raw = patch[field]
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            out[meta_key] = None
        else:
            out[meta_key] = str(raw).strip()
    return out


def llm_settings_to_conversation_patch(
    llm: LlmSettings, *, settings: Settings
) -> dict[str, Any]:
    """Produce the fields to write to the conversation metadata from a full LlmSettings."""
    return conversation_llm_to_meta(
        {
            "provider": resolve_provider(settings, llm),
            "cursor_model": llm.cursor_model,
            "claude_model": llm.claude_model,
            "ollama_model": llm.ollama_model,
            "gemini_model": llm.gemini_model,
            "openai_model": llm.openai_model,
            "codex_model": llm.codex_model,
        }
    )
