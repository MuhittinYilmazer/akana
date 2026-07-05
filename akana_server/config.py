"""Environment configuration for Akana.

All env variables are documented with explanations in ``.env.example`` at the
repo root; kept in sync by ``tests/unit/test_env_example_sync.py`` (add new
env vars there too).
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from akana_server.settings_defaults import DEFAULTS

try:
    from dotenv import load_dotenv
except ImportError:  # BOOTSTRAP: `python akana.py setup` imports this module (via main.py's
    # apply_legacy_env_aliases) BEFORE it installs dependencies — python-dotenv isn't there
    # yet. Degrade to a no-op so setup can run; .env loading resumes once deps are installed
    # (dotenv is a core requirement). Without this, a clean install dead-ends on
    # ModuleNotFoundError before a single package is installed.
    def load_dotenv(*_args: object, **_kwargs: object) -> bool:  # type: ignore[misc]
        return False

log = logging.getLogger(__name__)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


#: Custom-trained "Hey Akana" openWakeWord model shipped with the package; the default
#: WAKE_MODEL when the env var is unset (see load_settings).
_BUNDLED_WAKE_MODEL = Path(__file__).resolve().parent / "voice" / "wake_models" / "hey_akana.onnx"


def _int_env(
    name: str, default: int, *, lo: int | None = None, hi: int | None = None
) -> int:
    """Defensively read a numeric env var — corrupt/out-of-range values must not break boot.

    Follows the ``runtime_settings/resolve._env_fallback`` pattern: on parse error,
    ``log.warning`` + default. Unguarded ``int(os.environ[...])`` raises ``ValueError``
    and crashes the process at startup. If ``lo``/``hi`` are provided, out-of-range
    values also fall back to default (e.g. ``AKANA_PORT=0/99999999`` would cause a
    bind crash).
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s=%r invalid integer; using default: %s", name, raw, default)
        return default
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        log.warning(
            "%s=%r out of range [%s, %s]; using default: %s",
            name, value, lo, hi, default,
        )
        return default
    return value


def _float_env(
    name: str, default: float, *, lo: float | None = None, hi: float | None = None
) -> float:
    """Defensively read a float env var — corrupt/out-of-range value must not crash boot.

    Mirrors :func:`_int_env`: on parse error or (with ``lo``/``hi``) an out-of-range
    value, ``log.warning`` + default. A NEGATIVE duration is the dangerous case — e.g.
    a negative ``CLAUDE_BRIDGE_TIMEOUT`` would feed ``combine_cap`` and silently DISABLE
    the stream idle/hang ceiling — so the timeouts pass their schema floor/ceiling
    (``lo=60``, ``hi=7_200``), matching SCHEMA so env can't bypass the PUT-path bounds.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("%s=%r invalid decimal; using default: %s", name, raw, default)
        return default
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        log.warning(
            "%s=%r out of range [%s, %s]; using default: %s",
            name, value, lo, hi, default,
        )
        return default
    return value


def _str_env(name: str, default: str) -> str:
    """Read a string env var — treat empty/whitespace as absent and fall back to default.

    ``os.environ.get(name, default)`` only returns default when the key is ABSENT;
    an empty string (``AKANA_DATA_DIR=``) gives ``""``. Without this guard that
    leads to surprises like ``Path("").resolve()`` → CWD (all data landing under CWD)
    or ``AKANA_HOST=""`` → 0.0.0.0 bind. Same "empty = unset" contract as
    ``_int_env``/``_float_env``.
    """
    return os.environ.get(name, "").strip() or default


def _load_env() -> None:
    """Load ``.env`` into the process environment.

    python-dotenv reads the file as UTF-8; a UTF-16 (PowerShell 5.1 ``>`` redirection)
    or cp1254 (ANSI editor) ``.env`` makes it raise a raw ``UnicodeDecodeError`` and
    crash the server subprocess with an undiagnosable traceback. The CLI catches this
    on its own reads (``akana_cli/env_util``); a direct server launch has no such
    guard, so translate it into a clear startup error naming the file and the fix.
    """
    env_path = _repo_root() / ".env"
    try:
        load_dotenv(env_path, override=False)
    except UnicodeDecodeError as exc:
        raise RuntimeError(
            f"{env_path} is not UTF-8 ({exc}); re-save it as UTF-8. "
            "A common cause on Windows is PowerShell `>` redirection, which writes UTF-16."
        ) from exc


def clean_secret_value(value: str | None) -> str:
    """Sanitize a pasted API key / OAuth token: drop wrapping quotes + ALL whitespace.

    API keys and OAuth tokens never contain inner whitespace or wrapping quotes;
    both sneak in via copy-paste — line-wrapped terminal output injects a space or
    newline, and ``.env``/GUI paste leaves shell-style ``'…'``/``"…"`` quotes. Either
    poisons the bearer → the upstream API answers ``401 Invalid bearer token`` /
    ``Invalid User API Key`` even though the value "looks" right. Shared by
    :func:`_secret` (``.env`` reads) and ``secret_store`` (runtime-store reads) so
    every credential path sanitizes identically.

    Do NOT split on ``#``: ``python-dotenv`` already handles real inline comments in
    ``.env`` lines; when a value is set as an env variable (export/``docker -e``),
    ``#`` is part of the value, and truncating there would break random tokens.
    """
    if not value:
        return ""
    stripped = value.strip()
    # One layer of surrounding matched quotes (`'…'` or `"…"`).
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in ("'", '"'):
        stripped = stripped[1:-1]
    # Inner + residual outer whitespace (newlines from wrapped paste, stray spaces).
    return re.sub(r"\s+", "", stripped)


def _secret(name: str) -> str | None:
    """Read a secret env var, sanitized via :func:`clean_secret_value`."""
    return clean_secret_value(os.environ.get(name)) or None


@dataclass(frozen=True, slots=True)
class Settings:
    server_host: str
    server_port: int
    api_token: str | None
    data_dir: Path
    workspace: Path
    cursor_api_key: str | None
    cursor_model: str
    bridge_timeout: float
    log_level: str
    # Voice
    voices_dir: Path
    piper_voice_tr: Path
    piper_voice_en: Path
    voice_tts_max_chars: int
    voice_max_record_seconds: float
    primary_lang: str
    wake_model: str
    wake_threshold: float
    wake_min_frames: int
    wake_inference_framework: str
    whisper_model: str
    whisper_compute_type: str
    whisper_device: str
    whisper_prompt: str  # STT initial_prompt: terminology glossary (mixed TR-EN accuracy)
    # NOTE: every default below is read from ``settings_defaults.DEFAULTS`` (the
    # single source of truth) — the same literal used by ``load_settings()``
    # fallbacks and by ``runtime_settings/schema.py``. Change a default THERE, once.
    # TTS engine env override: "" = follow preferences; "auto"|"edge"|"piper" force it.
    tts_engine: str = DEFAULTS["tts_engine"]
    # LLM provider switch: "cursor" | "claude" | "gemini" | "openai" | "ollama".
    # Empty = unconfigured. No provider is privileged as a default — the user picks
    # one (setup / Settings); chat refuses with a clear message until then.
    llm_provider: str = DEFAULTS["llm_provider"]
    claude_bin: str = DEFAULTS["claude_bin"]
    claude_model: str = DEFAULTS["claude_model"]
    claude_bridge_timeout: float = DEFAULTS["claude_bridge_timeout"]
    # LLM chat titles: summarize a new chat's title from the first user message (the
    # user-facing on/off is the runtime `llm_chat_titles` setting; this is its env default).
    llm_chat_titles: bool = DEFAULTS["llm_chat_titles"]
    # SessionCloser cron: idle conversation summaries go to inbox (M3.2). 0 interval = disabled.
    session_closer_enabled: bool = DEFAULTS["session_closer_enabled"]
    session_closer_interval: float = DEFAULTS["session_closer_interval"]
    session_closer_idle_minutes: int = DEFAULTS["session_closer_idle_minutes"]
    # Content-aware early trigger (companion to the runtime turn-threshold): summarize
    # once NEW user/assistant text since the last summary exceeds this many chars. 0 = off.
    session_closer_char_threshold: int = DEFAULTS["session_closer_char_threshold"]
    # Summarization chunk size: the transcript is summarized in chunks of this many
    # chars (one giant message is clipped to it). Wired into SessionCloser(max_chars=).
    session_closer_max_chars: int = DEFAULTS["session_closer_max_chars"]
    # Prior-context recall (B): fold the rolling session summary back into each turn's
    # prompt as a compact «Prior context» block so a long chat resumes with its earlier
    # decisions/open items even after older turns scroll out of the window.
    session_summary_inject_enabled: bool = DEFAULTS["session_summary_inject_enabled"]
    # Hard cap on the injected «Prior context» block (chars) so a long rolling summary
    # can't silently eat the turn budget. 0 = no cap (inject the whole summary).
    session_summary_inject_max_chars: int = DEFAULTS["session_summary_inject_max_chars"]
    # Summary consolidation cron: cluster related session summaries → one staged
    # candidate. 0 interval = disabled (mirrors the session_closer cron shape).
    summary_consolidation_enabled: bool = DEFAULTS["summary_consolidation_enabled"]
    summary_consolidation_interval: float = DEFAULTS["summary_consolidation_interval"]
    # Min shared topical tokens before two session summaries cluster into one topic.
    summary_consolidation_min_overlap: int = DEFAULTS["summary_consolidation_min_overlap"]
    # ConnectorEngine F0-F1 (Telegram MVP): default OFF. Empty allowlist =
    # every chat is rejected (polling does not even start); token resolved via
    # secret_store (`telegram_bot_token`) > env (AKANA_TELEGRAM_BOT_TOKEN).
    telegram_enabled: bool = DEFAULTS["telegram_enabled"]
    telegram_bot_token: str | None = None
    telegram_allowed_chat_ids: tuple[str, ...] = DEFAULTS["telegram_allowed_chat_ids"]
    # Ollama local-model provider (activated with LLM_PROVIDER=ollama).
    ollama_url: str = DEFAULTS["ollama_url"]
    ollama_model: str = DEFAULTS["ollama_model"]
    # FileEngine F0: allowlist roots for Akana's OWN file tools
    # (":"-delimited). Empty = FileService disabled (every operation raises an
    # explicit error): no implicit home-directory access, only explicitly given roots.
    file_roots: tuple[Path, ...] = DEFAULTS["file_roots"]
    # MultimodalEngine F1: multi-type file upload toggle + single-file size limit (MB).
    uploads_enabled: bool = DEFAULTS["uploads_enabled"]
    upload_max_mb: float = DEFAULTS["upload_max_mb"]
    # Gemini Live (full-duplex native-audio voice, Phase 2) — when provider==gemini
    # the voice toggle switches from turn-based to Live WS. Default OFF: audio
    # streams to Google (privacy), opt-in. Model/voice are native-audio names from
    # the Live preview API (user's own gemini_api_key, secret_store > env).
    gemini_live_enabled: bool = DEFAULTS["gemini_live_enabled"]
    gemini_live_model: str = DEFAULTS["gemini_live_model"]
    gemini_live_voice: str = DEFAULTS["gemini_live_voice"]

    # OpenAI Realtime (full-duplex voice, OpenAI twin of Gemini Live) — when
    # provider==openai the voice toggle switches from turn-based to Realtime WS
    # (``/ws/voice/realtime``). Default OFF: audio streams to OpenAI (privacy),
    # opt-in. Uses user's own openai_api_key (secret_store > env). Audio: PCM16@24k
    # (input+output).
    openai_realtime_enabled: bool = DEFAULTS["openai_realtime_enabled"]
    openai_realtime_model: str = DEFAULTS["openai_realtime_model"]
    # ``marin``/``cedar`` are ONLY available on the GA ``gpt-realtime`` model;
    # invalid on the default BETA model → ``alloy`` (accepted by BETA preview)
    # keeps model+voice consistent.
    openai_realtime_voice: str = DEFAULTS["openai_realtime_voice"]

    @property
    def bridge_dir(self) -> Path:
        return _repo_root() / "cursor_bridge"


def _is_readable_voice_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _voices_with_models(directory: Path) -> bool:
    if not directory.is_dir():
        return False
    return any(_is_readable_voice_file(p) for p in directory.glob("*.onnx"))


def _resolve_voices_dir(data_dir: Path) -> Path:
    """Pick a voices directory that contains at least one readable ``.onnx`` file.

    Prefers ``AKANA_VOICES_DIR`` (when set), then ``data_dir/voices``
    (Akana install target). Falls back to ``data_dir/voices`` (created
    if missing) so ``python akana.py setup --voice piper`` has a stable target.
    """
    data_voices = (data_dir / "voices").resolve()
    env = os.environ.get("AKANA_VOICES_DIR", "").strip()

    candidates: list[Path] = [data_voices]
    if env:
        candidates.insert(0, Path(os.path.expanduser(env)).resolve())
    for c in candidates:
        if _voices_with_models(c):
            return c
    data_voices.mkdir(parents=True, exist_ok=True)
    return data_voices


def _voice_path(env_var: str, voices_dir: Path, fallback_name: str) -> Path:
    raw = os.environ.get(env_var, "").strip()
    if raw:
        p = Path(os.path.expanduser(raw))
        candidate = (p if p.is_absolute() else (voices_dir / p)).resolve()
        if _is_readable_voice_file(candidate):
            return candidate
    default = (voices_dir / fallback_name).resolve()
    if _is_readable_voice_file(default):
        return default
    prefix = "tr" if fallback_name.lower().startswith("tr") else "en"
    if voices_dir.is_dir():
        for onnx in sorted(voices_dir.glob("*.onnx")):
            if onnx.name.lower().startswith(prefix) and _is_readable_voice_file(onnx):
                return onnx.resolve()
    return default


def _resolve_primary_lang(data_dir: Path) -> str:
    """Voice primary language (``tr`` | ``en``), STRICTLY following the unified
    ``language`` runtime setting (store > ``AKANA_LANGUAGE`` env > ``en``) so speech
    always matches the UI/persona language picker — selecting English flips voice to
    ``en`` too, with no separate knob to drift out of sync. (The legacy
    ``VOICE_PRIMARY_LANG`` override was dropped: it could silently force a language
    the picker didn't choose.) Defensive: any failure falls back to ``en``.
    """
    try:
        from types import SimpleNamespace

        from akana_server.runtime_settings import get_runtime

        lang = str(
            get_runtime("language", SimpleNamespace(data_dir=data_dir)) or "en"
        ).strip().lower()
        return lang if lang in ("tr", "en") else "en"
    except Exception:
        return "en"


def parse_file_roots(raw: str) -> tuple[Path, ...]:
    """``AKANA_FILE_ROOTS`` → resolved allowlist (``os.pathsep``-delimited:
    ``;`` on Windows, ``:`` on POSIX — Windows paths contain drive-letter
    colons, so ``:`` cannot be the separator there).

    Default is *empty* on purpose: FileEngine only ever touches roots the user
    configured explicitly (expanduser + resolve, no implicit home access).
    """
    roots: list[Path] = []
    for part in (raw or "").split(os.pathsep):
        part = part.strip()
        if part:
            roots.append(Path(os.path.expanduser(part)).resolve())
    return tuple(roots)


def parse_telegram_allowed_chat_ids(raw: str) -> tuple[str, ...]:
    """``AKANA_TELEGRAM_ALLOWED_CHAT_IDS`` → chat id allowlist (comma-delimited).

    Default is empty (nobody can write): messages from chats not explicitly in
    the allowlist are silently ignored.
    """
    return tuple(part.strip() for part in (raw or "").split(",") if part.strip())


def _is_loopback_host(host: str) -> bool:
    """Is the bind address a loopback address? (``127.0.0.0/8``, ``::1``, ``localhost``).

    Non-loopback bind + empty token = auth disabled + accessible from outside
    (e.g. published via Tailscale Serve/reverse-proxy) → triggers a hard warning.
    """
    h = (host or "").strip().lower()
    if h in ("localhost", ""):
        return h == "localhost"
    # Strip an IPv6 bracket form ([::1]) and any %zone id before parsing — ip_address()
    # rejects both, which previously misclassified a genuine loopback bind as non-loopback.
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    h = h.split("%", 1)[0]
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        # any-bind like 0.0.0.0 / :: or a hostname → treat as non-loopback (emit warning).
        return False


def allow_unauthenticated() -> bool:
    """Did the operator explicitly opt into running with no API token?

    Shared by the startup guard (below) and the request-layer guard
    (``require_akana_bearer``) so "open mode" has a single source of truth.
    """
    v = (os.environ.get("AKANA_ALLOW_UNAUTHENTICATED", "") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _warn_if_auth_disabled(settings: Settings) -> None:
    """No token = auth disabled. Two layers guard against accidental exposure.

    * **non-loopback bind** + empty token + no opt-out → REFUSE to start
      (fail-closed): /api/v1/* (vault write, credentials, files, chat) would be
      open to the network.
    * **loopback bind** + empty token → start, but WARN. The bind address can't
      reveal a reverse proxy (e.g. Tailscale Serve) forwarding outside traffic to
      this loopback socket — so loopback is no longer silently trusted. The
      request layer (``require_akana_bearer``) backstops it: a proxied request
      with no token is rejected.

    Deliberate open mode (AKANA_ALLOW_UNAUTHENTICATED=1) downgrades both to a note.
    """
    if settings.api_token:
        return
    opted_out = allow_unauthenticated()
    if not _is_loopback_host(settings.server_host):
        msg = (
            f"SECURITY: AKANA_TOKEN is empty and the server is binding to a NON-loopback "
            f"address (host={settings.server_host}). Auth is DISABLED → /api/v1/* (vault, "
            f"credentials, files, chat) is accessible WITHOUT authentication."
        )
        if opted_out:
            log.warning("%s Continuing with AKANA_ALLOW_UNAUTHENTICATED=1.", msg)
            return
        raise RuntimeError(
            msg + " Set a token (.env: AKANA_TOKEN=...); if you deliberately want open "
            "mode, start with AKANA_ALLOW_UNAUTHENTICATED=1."
        )
    if not opted_out:
        log.warning(
            "AKANA_TOKEN is empty. Direct localhost access works, but requests arriving "
            "through a reverse proxy (e.g. Tailscale Serve) will be REJECTED. Set "
            "AKANA_TOKEN to allow authenticated remote access, or "
            "AKANA_ALLOW_UNAUTHENTICATED=1 to allow unauthenticated proxied access."
        )


#: Legacy ``AKANA_CURSOR_*`` env prefix → new ``AKANA_*`` (Akana rename).
#: ``llm_dispatch``/``claude_provider`` bearer denylist also uses this prefix.
LEGACY_ENV_PREFIX = "AKANA_CURSOR_"
_LEGACY_ENV_SUFFIXES = (
    "TOKEN",
    "DATA_DIR",
    "PORT",
    "HOST",
    "WORKSPACE",
    "VOICES_DIR",
    "SKILL_PATHS",
    "REUSE_AGENT",
)


def apply_legacy_env_aliases(environ: dict[str, str] | None = None) -> None:
    """Bridge legacy ``AKANA_CURSOR_*`` names to their new ``AKANA_*`` equivalents.

    BACKWARD COMPAT: if the user's ``.env`` or shell/systemd/CI environment still
    sets the old ``AKANA_CURSOR_*`` name, copy it to the new ``AKANA_*`` counterpart
    when the new name is ABSENT or EMPTY → all reads see the new name; an
    un-updated environment fails silently in a good way. An explicitly set
    (non-empty) new name always wins. (The prefix is assembled dynamically; the
    .env.example drift guard does not count non-literal names.)

    NOTE (R4-F #2): "bridge empty too" — an empty ``AKANA_TOKEN=`` line in ``.env``
    was set to ``""`` by ``load_dotenv(override=False)``; the old ``setdefault``
    treated this as "present" and skipped bridging the legacy ``AKANA_CURSOR_TOKEN``
    → token was silently disabled. Same "empty = unset" contract as ``_str_env``.
    """
    env = os.environ if environ is None else environ
    for suffix in _LEGACY_ENV_SUFFIXES:
        legacy_val = env.get(LEGACY_ENV_PREFIX + suffix, "")
        if not legacy_val.strip():
            continue  # legacy absent/empty → nothing to bridge
        new_key = "AKANA_" + suffix
        if not env.get(new_key, "").strip():  # new name absent OR empty → bridge
            env[new_key] = legacy_val


def load_settings() -> Settings:
    _load_env()
    apply_legacy_env_aliases()
    data_dir = Path(
        os.path.expanduser(_str_env("AKANA_DATA_DIR", "~/.akana"))
    ).resolve()
    ws_raw = os.environ.get("AKANA_WORKSPACE", "").strip()
    workspace = Path(os.path.expanduser(ws_raw)).resolve() if ws_raw else _repo_root()
    port = _int_env("AKANA_PORT", 8766, lo=1, hi=65535)
    voices_dir = _resolve_voices_dir(data_dir)
    settings = Settings(
        server_host=_str_env("AKANA_HOST", "127.0.0.1"),
        server_port=port,
        api_token=_secret("AKANA_TOKEN"),
        data_dir=data_dir,
        workspace=workspace,
        cursor_api_key=_secret("CURSOR_API_KEY"),
        cursor_model=os.environ.get("CURSOR_MODEL", "composer-2").strip(),
        # lo/hi mirror SCHEMA["bridge_timeout"] (min=60, max=7_200) so the env layer
        # enforces the same floor/ceiling the PUT path does.
        bridge_timeout=_float_env(
            "CURSOR_BRIDGE_TIMEOUT", DEFAULTS["bridge_timeout"], lo=60.0, hi=7_200.0
        ),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        voices_dir=voices_dir,
        piper_voice_tr=_voice_path("PIPER_VOICE_TR", voices_dir, "tr_TR-dfki-medium.onnx"),
        piper_voice_en=_voice_path("PIPER_VOICE_EN", voices_dir, "en_US-amy-medium.onnx"),
        voice_tts_max_chars=_int_env("VOICE_TTS_MAX_CHARS", 5000),
        voice_max_record_seconds=_float_env("VOICE_MAX_RECORD_SECONDS", 60.0),
        primary_lang=_resolve_primary_lang(data_dir),
        # WAKE WORD — a custom-trained "hey_akana" openWakeWord model ships in the repo
        # (akana_server/voice/wake_models/hey_akana.onnx) and is the DEFAULT wake source:
        # local acoustic scoring, tunable threshold, few false wakes. When WAKE_MODEL is
        # unset we fall back to that bundled model if present; set WAKE_MODEL to override,
        # or set it to "" (empty env) to disable server scoring and use the browser
        # SpeechRecognition phrase-match instead. Server scoring also needs openwakeword
        # installed (voice extra); the /voice/wake/config gate reports availability and
        # the browser falls back to SpeechRecognition when it is off.
        wake_model=(
            os.environ.get("WAKE_MODEL", "").strip()
            or (str(_BUNDLED_WAKE_MODEL) if _BUNDLED_WAKE_MODEL.exists() else "")
        ),
        # Bounds mirror SCHEMA["wake_threshold"] (min=0.01, max=1.0): an out-of-range
        # env value must never silently disable wake — e.g. WAKE_THRESHOLD=5.0 makes
        # ``score >= 5.0`` never fire. The PUT/runtime path already enforces the same
        # range, so env parsing must not be the one gap that lets it through.
        wake_threshold=_float_env("WAKE_THRESHOLD", DEFAULTS["wake_threshold"], lo=0.01, hi=1.0),
        wake_min_frames=_int_env("WAKE_MIN_FRAMES", DEFAULTS["wake_min_frames"]),
        wake_inference_framework=os.environ.get("WAKE_INFERENCE_FRAMEWORK", "onnx").strip(),
        whisper_model=os.environ.get("WHISPER_MODEL", "small").strip(),
        whisper_compute_type=os.environ.get("WHISPER_COMPUTE_TYPE", "int8").strip(),
        whisper_device=os.environ.get("WHISPER_DEVICE", "auto").strip(),
        # STT initial_prompt: DEFAULT EMPTY. A long/keyword-heavy initial_prompt
        # strongly biases faster-whisper → turns everyday speech into nonsense
        # ("hello how are you" → garbage output). Browser SR is unbiased so the
        # screen looks correct, but the Whisper transcript sent to the LLM would be
        # corrupted. Heavy technical dictation can enable term-bias via the
        # WHISPER_PROMPT env / runtime setting.
        whisper_prompt=os.environ.get("WHISPER_PROMPT", "").strip(),
        tts_engine=os.environ.get("AKANA_TTS_ENGINE", "").strip().lower(),
        llm_provider=os.environ.get("LLM_PROVIDER", "").strip().lower(),
        claude_bin=os.environ.get("CLAUDE_BIN", "").strip() or DEFAULTS["claude_bin"],
        claude_model=os.environ.get("CLAUDE_MODEL", "").strip() or DEFAULTS["claude_model"],
        claude_bridge_timeout=_float_env(
            # lo/hi mirror SCHEMA["claude_bridge_timeout"] (min=60, max=7_200).
            "CLAUDE_BRIDGE_TIMEOUT", DEFAULTS["claude_bridge_timeout"], lo=60.0, hi=7_200.0
        ),
        llm_chat_titles=os.environ.get("AKANA_LLM_CHAT_TITLES", "1").strip().lower()
        not in ("0", "false", "no", "off"),
        session_closer_enabled=os.environ.get("AKANA_SESSION_CLOSER_ENABLED", "1").strip().lower()
        not in ("0", "false", "no", "off"),
        session_closer_interval=_float_env(
            "AKANA_SESSION_CLOSER_INTERVAL", DEFAULTS["session_closer_interval"]
        ),
        session_closer_idle_minutes=_int_env(
            # 0 = "immediately close any currently idle conversation" (threshold=now);
            # the panel enforces min=1, but env allows 0 as an advanced/test path.
            "AKANA_SESSION_CLOSER_IDLE_MINUTES", DEFAULTS["session_closer_idle_minutes"], lo=0
        ),
        session_closer_char_threshold=_int_env(
            "AKANA_SESSION_CLOSER_CHAR_THRESHOLD", DEFAULTS["session_closer_char_threshold"], lo=0
        ),
        session_closer_max_chars=_int_env(
            "AKANA_SESSION_CLOSER_MAX_CHARS", DEFAULTS["session_closer_max_chars"], lo=200
        ),
        session_summary_inject_enabled=os.environ.get(
            "AKANA_SESSION_SUMMARY_INJECT", "1"
        ).strip().lower()
        not in ("0", "false", "no", "off"),
        session_summary_inject_max_chars=_int_env(
            "AKANA_SESSION_SUMMARY_INJECT_MAX_CHARS",
            DEFAULTS["session_summary_inject_max_chars"], lo=0
        ),
        summary_consolidation_enabled=os.environ.get(
            "AKANA_SUMMARY_CONSOLIDATION_ENABLED", "1"
        ).strip().lower()
        not in ("0", "false", "no", "off"),
        summary_consolidation_interval=_float_env(
            "AKANA_SUMMARY_CONSOLIDATION_INTERVAL", DEFAULTS["summary_consolidation_interval"]
        ),
        summary_consolidation_min_overlap=_int_env(
            "AKANA_SUMMARY_CONSOLIDATION_MIN_OVERLAP",
            DEFAULTS["summary_consolidation_min_overlap"], lo=1
        ),
        telegram_enabled=os.environ.get("AKANA_TELEGRAM_ENABLED", "0").strip().lower()
        in ("1", "true", "yes", "on"),
        telegram_bot_token=_secret("AKANA_TELEGRAM_BOT_TOKEN"),
        telegram_allowed_chat_ids=parse_telegram_allowed_chat_ids(
            os.environ.get("AKANA_TELEGRAM_ALLOWED_CHAT_IDS", "")
        ),
        ollama_url=os.environ.get("AKANA_OLLAMA_URL", "").strip() or DEFAULTS["ollama_url"],
        ollama_model=os.environ.get("AKANA_OLLAMA_MODEL", "").strip() or DEFAULTS["ollama_model"],
        file_roots=parse_file_roots(os.environ.get("AKANA_FILE_ROOTS", "")),
        uploads_enabled=os.environ.get("AKANA_UPLOADS_ENABLED", "1").strip().lower()
        not in ("0", "false", "no", "off"),
        upload_max_mb=_float_env("AKANA_UPLOAD_MAX_MB", DEFAULTS["upload_max_mb"]),
        gemini_live_enabled=os.environ.get("AKANA_GEMINI_LIVE_ENABLED", "0").strip().lower()
        in ("1", "true", "yes", "on"),
        gemini_live_model=os.environ.get("AKANA_GEMINI_LIVE_MODEL", "").strip()
        or DEFAULTS["gemini_live_model"],
        gemini_live_voice=os.environ.get("AKANA_GEMINI_LIVE_VOICE", "").strip()
        or DEFAULTS["gemini_live_voice"],
        openai_realtime_enabled=os.environ.get("AKANA_OPENAI_REALTIME_ENABLED", "0").strip().lower()
        in ("1", "true", "yes", "on"),
        openai_realtime_model=os.environ.get("AKANA_OPENAI_REALTIME_MODEL", "").strip()
        or DEFAULTS["openai_realtime_model"],
        openai_realtime_voice=os.environ.get("AKANA_OPENAI_REALTIME_VOICE", "").strip()
        or DEFAULTS["openai_realtime_voice"],
    )
    _warn_if_auth_disabled(settings)
    return settings


def chat_agent_cwd(data_dir: Path) -> Path:
    """Minimal cwd for Cursor chat agent (avoids repo filesystem tools on Akana code)."""
    p = (data_dir / "agent_chat").resolve()
    p.mkdir(parents=True, exist_ok=True)
    readme = p / "README.md"
    if not readme.is_file():
        readme.write_text(
            "# Akana chat agent\n\n"
            "Chat mode — no access to the codebase. Personal memory is handled server-side.\n",
            encoding="utf-8",
        )
    return p


def ensure_data_dirs(data_dir: Path) -> None:
    for sub in (
        "db",
        "logs",
        "audit",
        "conversations",
        "run",
        "voices",
        "skills",
        "credentials",
        "vault",
    ):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
    chat_agent_cwd(data_dir)
