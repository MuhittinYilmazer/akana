"""runtime_settings.schema — settings catalog (single source of truth).

REST (GET /settings/runtime) and the UI form are generated from :data:`SCHEMA`
in this module; each setting's type, bounds, description, and category live here.
``AKANA_*`` env names appear as full literal strings here
(``.env.example`` drift guard scans them with AST).
"""

from __future__ import annotations

from akana_server.settings_defaults import DEFAULTS
from .spec import RuntimeSettingSpec

CATEGORIES: tuple[dict[str, str], ...] = (
    {"id": "genel", "label": "General"},
    {"id": "zamanlama", "label": "Session Maintenance"},
    {"id": "ozet", "label": "Session Summaries"},
    {"id": "beceri", "label": "Skill Injection"},
    {"id": "planlayici", "label": "Context"},
    {"id": "dosya", "label": "File Roots"},
    {"id": "yukleme", "label": "Image Upload"},
    {"id": "telegram", "label": "Telegram"},
    {"id": "otonom", "label": "Autonomous Mode (Claude)"},
    {"id": "ag", "label": "Network Resilience"},
    {"id": "araclar", "label": "Tools"},
    {"id": "ses", "label": "Voice & Wake"},
)


def _skill_inject_env(raw: str) -> bool:
    # turn_injection.py historical semantics: any value other than "0/false/off" is on.
    return raw.strip().lower() not in {"0", "false", "off"}


def _tool_flag_env(raw: str) -> bool:
    # memory_tools/vault_tools historical semantics: anything other than "0/false/off" is on.
    return raw.strip().lower() not in {"0", "false", "off"}


_SPECS: tuple[RuntimeSettingSpec, ...] = (
    RuntimeSettingSpec(
        key="language",
        type="str",
        label="Language",
        description=(
            "The interface, voice, and Akana's default persona use this language: «en» "
            "(English, default) or «tr» (Turkish). The open-source release is "
            "English-first; selecting «tr» switches the UI/voice/persona to Turkish."
        ),
        category="genel",
        env_var="AKANA_LANGUAGE",
        default="en",
        options=("en", "tr"),
        # ``hidden=True``: the spec stays (the Overview/General language picker PUTs
        # this same key, and the i18n boot reconcile reads it), but it is NOT rendered
        # in the runtime form — showing the same language picker in two tabs was
        # confusing. ``genel`` has no other member, so that category drops out entirely.
        hidden=True,
    ),
    RuntimeSettingSpec(
        key="llm_chat_titles",
        type="bool",
        label="AI chat titles",
        description=(
            "When on, a new chat's title is summarized from your first message by the LLM "
            "(in your selected language). When off, the title is just the first line of your "
            "message, truncated — no LLM call is made."
        ),
        category="genel",
        env_var="AKANA_LLM_CHAT_TITLES",
        default=DEFAULTS["llm_chat_titles"],
        settings_attr="llm_chat_titles",
    ),
    RuntimeSettingSpec(
        key="whisper_prompt",
        type="str",
        label="Speech recognition term glossary",
        description=(
            "Context for Whisper (initial_prompt): write the technical terms/names you "
            "use often here — it transcribes them MORE ACCURATELY in mixed-language speech. "
            "The model/language is unchanged, speed is unaffected. Do NOT write a LONG/"
            "keyword-heavy prompt — it biases Whisper and degrades everyday speech. "
            "Empty = unbiased (recommended)."
        ),
        category="ses",
        env_var="WHISPER_PROMPT",
        default="",
        settings_attr="whisper_prompt",
    ),
    # -- scheduling ---------------------------------------------------------------
    # ``hidden=True``: session summarization is gated by the ``session_summary`` flag
    # (memory_settings.yaml), which gates BOTH the closer and consolidation crons
    # (session_closer_service.run_once / summary_consolidation_service.run_once). That flag now
    # defaults ON for everyone (its Memory Studio toggle was removed — see
    # akana.memory.settings). Showing a second, near-identical master switch here was confusing —
    # two switches for one job. This spec STAYS as the env-level kill switch
    # (``AKANA_SESSION_CLOSER_ENABLED=0`` hard-disables the scan loop in ``session_closer_active``)
    # and PUT/reset still work; it is just not rendered in the editable form. ``zamanlama`` keeps
    # its trigger knobs (interval/idle/turn/char), so the category does not drop out.
    RuntimeSettingSpec(
        key="session_closer_enabled",
        type="bool",
        label="Session-closing scan loop (env kill switch)",
        description=(
            "Env-level master for the idle/long-chat summary scan loop "
            "(AKANA_SESSION_CLOSER_ENABLED=0 hard-disables it). The memory-level flag is "
            "``session_summary`` (default ON); both must be on for summaries to run."
        ),
        category="zamanlama",
        env_var="AKANA_SESSION_CLOSER_ENABLED",
        default=DEFAULTS["session_closer_enabled"],
        settings_attr="session_closer_enabled",
        hidden=True,
    ),
    RuntimeSettingSpec(
        key="session_closer_interval",
        type="float",
        label="Session-closing scan interval (seconds)",
        description="The idle-chat scan runs at this interval. 0 = off (minimum 30 sec).",
        category="zamanlama",
        env_var="AKANA_SESSION_CLOSER_INTERVAL",
        default=DEFAULTS["session_closer_interval"],
        settings_attr="session_closer_interval",
        min=0,
        max=86_400,
        unit="sn",
    ),
    RuntimeSettingSpec(
        key="session_closer_idle_minutes",
        type="int",
        label="Idle chat threshold (minutes)",
        description="A chat that has received no messages for this long is considered 'idle' and summarized.",
        category="zamanlama",
        env_var="AKANA_SESSION_CLOSER_IDLE_MINUTES",
        default=DEFAULTS["session_closer_idle_minutes"],
        settings_attr="session_closer_idle_minutes",
        min=1,
        max=10_080,
        unit="dk",
    ),
    RuntimeSettingSpec(
        key="session_closer_turn_threshold",
        type="int",
        label="Long-chat summary threshold (turns)",
        description=(
            "Once a chat accumulates this many new turns it is summarized without "
            "waiting for it to go idle — catching a long, still-active chat early. "
            "0 = off (idle is the only trigger)."
        ),
        category="zamanlama",
        env_var="AKANA_SESSION_CLOSER_TURN_THRESHOLD",
        default=20,
        min=0,
        max=1000,
        unit="tur",
    ),
    RuntimeSettingSpec(
        key="session_closer_char_threshold",
        type="int",
        label="Long-chat summary threshold (characters)",
        description=(
            "A content-aware companion to the turn threshold: once the NEW user/"
            "assistant text accumulated since the last summary exceeds this many "
            "characters, the chat is summarized without waiting for it to go idle — "
            "so a few dense turns trigger too (turn count is content-blind). "
            "0 = off (turn/idle triggers only)."
        ),
        category="zamanlama",
        env_var="AKANA_SESSION_CLOSER_CHAR_THRESHOLD",
        default=DEFAULTS["session_closer_char_threshold"],
        min=0,
        max=200_000,
        unit="karakter",
    ),
    # -- session summaries (content + recall + consolidation) --------------------
    RuntimeSettingSpec(
        key="session_closer_max_chars",
        type="int",
        label="Summarization chunk size (characters)",
        description=(
            "The transcript is fed to the summarizer in chunks of this many characters "
            "(and a single very long message is clipped to it). Larger = more context "
            "per LLM call but a heavier, slower call; smaller = cheaper calls but more "
            "of them on a cold-start multi-chunk pass."
        ),
        category="ozet",
        env_var="AKANA_SESSION_CLOSER_MAX_CHARS",
        default=DEFAULTS["session_closer_max_chars"],
        settings_attr="session_closer_max_chars",
        min=200,
        max=200_000,
        unit="karakter",
    ),
    RuntimeSettingSpec(
        key="session_summary_inject_enabled",
        type="bool",
        label="Prior-context recall enabled",
        description=(
            "At the start of each turn the rolling session summary for the active chat "
            "is folded back into the prompt as a compact «Prior context» block, so the "
            "model resumes a long chat with its earlier decisions/open items in hand "
            "even after older turns scroll out of the window."
        ),
        category="ozet",
        env_var="AKANA_SESSION_SUMMARY_INJECT",
        default=DEFAULTS["session_summary_inject_enabled"],
        settings_attr="session_summary_inject_enabled",
    ),
    RuntimeSettingSpec(
        key="session_summary_inject_max_chars",
        type="int",
        label="Prior-context recall budget (characters)",
        description=(
            "Hard cap on the «Prior context» block injected each turn — a long rolling "
            "summary is clipped to this many characters so recall can never silently "
            "eat the turn's context budget. 0 = no cap (inject the whole summary)."
        ),
        category="ozet",
        env_var="AKANA_SESSION_SUMMARY_INJECT_MAX_CHARS",
        default=DEFAULTS["session_summary_inject_max_chars"],
        settings_attr="session_summary_inject_max_chars",
        min=0,
        max=50_000,
        unit="karakter",
    ),
    RuntimeSettingSpec(
        key="summary_consolidation_enabled",
        type="bool",
        label="Summary consolidation enabled",
        description=(
            "A background pass clusters related session summaries and stages a single "
            "consolidated memory candidate, so recurring threads across many chats "
            "collapse into one durable note instead of N scattered ones."
        ),
        category="ozet",
        env_var="AKANA_SUMMARY_CONSOLIDATION_ENABLED",
        default=DEFAULTS["summary_consolidation_enabled"],
        settings_attr="summary_consolidation_enabled",
    ),
    RuntimeSettingSpec(
        key="summary_consolidation_interval",
        type="float",
        label="Summary consolidation interval (seconds)",
        description="The summary-clustering pass runs at this interval. 0 = off (minimum 300 sec).",
        category="ozet",
        env_var="AKANA_SUMMARY_CONSOLIDATION_INTERVAL",
        default=DEFAULTS["summary_consolidation_interval"],
        settings_attr="summary_consolidation_interval",
        min=0,
        max=86_400,
        unit="sn",
    ),
    RuntimeSettingSpec(
        key="summary_consolidation_min_overlap",
        type="int",
        label="Consolidation overlap threshold (shared tokens)",
        description=(
            "How many shared topical words two session summaries must have in common "
            "before they are clustered into one consolidated topic. Higher = stricter "
            "(only very-related summaries merge); lower = more aggressive grouping."
        ),
        category="ozet",
        env_var="AKANA_SUMMARY_CONSOLIDATION_MIN_OVERLAP",
        default=DEFAULTS["summary_consolidation_min_overlap"],
        settings_attr="summary_consolidation_min_overlap",
        min=1,
        max=20,
        unit="kelime",
    ),
    # -- skills -------------------------------------------------------------------
    RuntimeSettingSpec(
        key="skill_inject_enabled",
        type="bool",
        label="Skill injection enabled",
        description="Automatic per-turn skill (SKILL.md) injection (WI-1).",
        category="beceri",
        env_var="AKANA_SKILL_INJECT",
        default=True,
        env_parse=_skill_inject_env,
    ),
    RuntimeSettingSpec(
        key="skill_catalog_enabled",
        type="bool",
        label="Skill catalog (system prompt)",
        description=(
            "A compact inventory of installed skills/packs (title + triggers) is added "
            "to every turn's system prompt; «Can you do X?» is answered against the "
            "actual inventory (WI-2). Nothing is added when the registry is empty."
        ),
        category="beceri",
        env_var="AKANA_SKILL_CATALOG",
        default=True,
        env_parse=_skill_inject_env,
    ),
    RuntimeSettingSpec(
        key="skill_inject_threshold",
        type="float",
        label="Injection RRF threshold",
        description=(
            "Minimum RRF score required for non-trigger matches "
            "(0.03 ≈ at least two search layers rank the same skill highly)."
        ),
        category="beceri",
        env_var="AKANA_SKILL_INJECT_THRESHOLD",
        default=0.03,
        min=0,
        max=1,
    ),
    RuntimeSettingSpec(
        key="skill_inject_max",
        type="int",
        label="Max skills per turn",
        description="Upper limit on skills injected into the prompt in a single chat turn.",
        category="beceri",
        env_var="AKANA_SKILL_INJECT_MAX",
        default=1,
        min=1,
        max=10,
    ),
    RuntimeSettingSpec(
        key="skill_catalog_max_entries",
        type="int",
        label="Catalog entry ceiling",
        description=(
            "Max number of installed capabilities listed in the system-prompt catalog "
            "(WI-2). The default (256) covers a large install; if you install more, raise "
            "this. When the limit is hit the catalog appends a visible «(+N more)» note — "
            "entries are never dropped silently."
        ),
        category="beceri",
        env_var="AKANA_SKILL_CATALOG_MAX_ENTRIES",
        default=256,
        min=16,
        max=2048,
    ),
    RuntimeSettingSpec(
        key="skill_catalog_max_chars",
        type="int",
        label="Catalog character ceiling",
        description=(
            "Max size (characters) of the installed-capabilities catalog block in the "
            "system prompt. Bounds how much of every turn's prompt the inventory can use; "
            "overflow is summarized with a visible «(+N more)» note. Raising it toward the "
            "context budget lets larger installs list every capability."
        ),
        category="beceri",
        env_var="AKANA_SKILL_CATALOG_MAX_CHARS",
        default=20_000,
        min=2_000,
        max=200_000,
    ),
    RuntimeSettingSpec(
        key="skill_suggest_timeout_s",
        type="float",
        label="Suggestion search time budget (seconds)",
        description="If the skill-suggestion search exceeds this time, the turn proceeds without injection.",
        category="beceri",
        env_var="AKANA_SKILL_SUGGEST_TIMEOUT_S",
        default=1.5,
        min=0.1,
        max=60,
        unit="sn",
    ),
    # -- context ------------------------------------------------------------------
    RuntimeSettingSpec(
        key="context_max_chars",
        type="int",
        label="Context character budget",
        description=(
            "Total character limit for system + history + user text; if exceeded, "
            "history is trimmed first, then the skill block. 0 = unlimited."
        ),
        category="planlayici",
        env_var="AKANA_CONTEXT_MAX_CHARS",
        default=120_000,
        min=0,
        max=2_000_000,
        unit="karakter",
    ),
    # -- file roots ---------------------------------------------------------------
    RuntimeSettingSpec(
        key="file_roots",
        type="paths",
        label="File tools allowed roots",
        description=(
            "The roots Akana's own file tools (list/read) can access, "
            "separated by ';' or new lines. Empty = FileEngine disabled."
        ),
        category="dosya",
        env_var="AKANA_FILE_ROOTS",
        default=DEFAULTS["file_roots"],
        settings_attr="file_roots",
    ),
    # -- uploads ------------------------------------------------------------------
    RuntimeSettingSpec(
        key="uploads_enabled",
        type="bool",
        label="File upload enabled",
        description="When disabled, POST /uploads is immediately rejected with 403.",
        category="yukleme",
        env_var="AKANA_UPLOADS_ENABLED",
        default=DEFAULTS["uploads_enabled"],
        settings_attr="uploads_enabled",
    ),
    RuntimeSettingSpec(
        key="upload_max_mb",
        type="float",
        label="Per-file size limit (MB)",
        description="A file exceeding the limit is rejected without being fully read into memory.",
        category="yukleme",
        env_var="AKANA_UPLOAD_MAX_MB",
        default=DEFAULTS["upload_max_mb"],
        settings_attr="upload_max_mb",
        min=0.1,
        max=500,
        unit="MB",
    ),
    # -- telegram (restart required — connector lifecycle is set up at startup) ---
    RuntimeSettingSpec(
        key="telegram_enabled",
        type="bool",
        label="Telegram bridge enabled",
        description=(
            "Telegram bot polling. Because the connector lifecycle is set up at "
            "server startup, a change is applied ON RESTART."
        ),
        category="telegram",
        env_var="AKANA_TELEGRAM_ENABLED",
        default=DEFAULTS["telegram_enabled"],
        settings_attr="telegram_enabled",
        restart_required=True,
        # Owned by the Channels tab's live Telegram panel (PUT /connectors/telegram
        # reloads without a restart); hidden from the generic Runtime form so the
        # bridge has a single management surface. PUT validation/apply still works.
        hidden=True,
    ),
    RuntimeSettingSpec(
        key="telegram_allowed_chat_ids",
        type="csv",
        label="Allowed Telegram chat ids",
        description=(
            "Comma-separated allowlist; messages from chats not on the list are "
            "ignored. Empty = nobody can write. Applied on restart."
        ),
        category="telegram",
        env_var="AKANA_TELEGRAM_ALLOWED_CHAT_IDS",
        default=DEFAULTS["telegram_allowed_chat_ids"],
        settings_attr="telegram_allowed_chat_ids",
        restart_required=True,
        # Edited from the Channels tab's Telegram panel (live reload); hidden from
        # the generic Runtime form to keep one surface. PUT still validates/applies.
        hidden=True,
    ),
    # -- LLM bridge timeout (long tasks — no restart) ----------------------------
    RuntimeSettingSpec(
        key="bridge_timeout",
        type="float",
        label="Cursor bridge idle timeout (seconds)",
        description=(
            "The maximum seconds to wait between two events in an LLM turn "
            "(long tool calls, Gemini pull, etc.). If exceeded, «bridge daemon timed "
            "out». The daemon sends a heartbeat; if that is still not enough, increase the value."
        ),
        category="ag",
        env_var="CURSOR_BRIDGE_TIMEOUT",
        default=DEFAULTS["bridge_timeout"],
        settings_attr="bridge_timeout",
        min=60,
        max=7_200,
        unit="sn",
    ),
    RuntimeSettingSpec(
        key="claude_bridge_timeout",
        type="float",
        label="Claude CLI idle timeout (seconds)",
        description="Turn timeout while waiting for a tool/response on the Claude provider.",
        category="ag",
        env_var="CLAUDE_BRIDGE_TIMEOUT",
        default=DEFAULTS["claude_bridge_timeout"],
        settings_attr="claude_bridge_timeout",
        min=60,
        max=7_200,
        unit="sn",
    ),
    # -- autonomous continuation (Claude provider — deep multi-turn workflows) ---
    # OFF BY DEFAULT (owner decision): every message is a single `claude` run, so when
    # Akana ends a turn with a question it stops and waits for the user's reply instead
    # of auto-resuming and answering itself. Turning this ON opts back into the loop —
    # the Claude agent keeps working across several `--resume` runs until the task is
    # genuinely done (it emits a completion sentinel, stops making tool calls, or a
    # ceiling below is hit). A conversational reply (no tool calls) always ends in ONE run.
    RuntimeSettingSpec(
        key="agent_autocontinue",
        type="bool",
        label="Autonomous continuation (Claude)",
        description=(
            "OFF by default: every message is a single run, so when Akana asks you "
            "something it stops and waits for your reply. Turn this ON only for deep, "
            "Claude-Code-style workflows where the Claude agent keeps working across "
            "multiple turns on its own. Other providers ignore this."
        ),
        category="otonom",
        env_var="AKANA_AGENT_AUTOCONTINUE",
        default=False,
    ),
    RuntimeSettingSpec(
        key="agent_max_continue_iters",
        type="int",
        label="Max continuation runs",
        description=(
            "Upper bound on how many Claude runs a single message may chain through "
            "auto-continuation. The hard ceiling that stops a runaway loop."
        ),
        category="otonom",
        env_var="AKANA_AGENT_MAX_CONTINUE_ITERS",
        default=25,
        min=1,
        max=100,
    ),
    RuntimeSettingSpec(
        key="agent_continue_deadline",
        type="float",
        label="Continuation wall-clock budget (seconds)",
        description=(
            "Total time across ALL auto-continuation runs for one message. When "
            "exceeded, the turn finishes at the next run boundary. 0 = off (only the "
            "run-count cap applies)."
        ),
        category="otonom",
        env_var="AKANA_AGENT_CONTINUE_DEADLINE",
        default=0.0,
        min=0,
        max=7_200,
        unit="sn",
    ),
    # -- LLM hang protection (FREEZE — incident: "page freezes, restart required") ---
    # The existing bridge_timeout (30 min, intended for long tool calls) is too
    # generous to catch a hang. These two knobs add a TIGHTER ceiling on top of
    # the call; they only combine with min() (never relax an existing limit),
    # 0 = disabled (reverts exactly to current behavior). DEFAULT OFF (0): user
    # preference — long thinking/tool calls must NEVER be cut short; only the 30-min
    # bridge ceiling remains as the last resort. If freezing occurs, these knobs
    # (UI/env) can be enabled.
    RuntimeSettingSpec(
        key="llm_idle_timeout",
        type="float",
        label="LLM stream idle-hang ceiling (seconds)",
        description=(
            "In an LLM STREAM, the maximum seconds to wait between two new chunks "
            "(delta/tool/heartbeat). If the stream STOPS producing chunks and hangs, "
            "the turn ends cleanly with «LLM_TIMEOUT» (504); the bridge process group "
            "is killed. A slow but progressing stream is UNAFFECTED (each chunk resets "
            "the counter). 0 = off (only the existing bridge_timeout applies)."
        ),
        category="ag",
        env_var="AKANA_LLM_IDLE_TIMEOUT",
        default=0.0,
        min=0,
        max=3_600,
        unit="sn",
    ),
    RuntimeSettingSpec(
        key="llm_total_timeout",
        type="float",
        label="LLM blocking-call total-time ceiling (seconds)",
        description=(
            "A non-streaming (one-shot) LLM call takes at most this many seconds "
            "(end to end). If exceeded, a clean «LLM_TIMEOUT» (504); the bridge process "
            "is killed. Affects only the blocking path (complete_chat); streaming uses "
            "the idle ceiling. 0 = off (only the existing bridge_timeout applies)."
        ),
        category="ag",
        env_var="AKANA_LLM_TOTAL_TIMEOUT",
        default=0.0,
        min=0,
        max=7_200,
        unit="sn",
    ),
    # Applied live (no restart): the Ollama driver is rebuilt per request and reads
    # this via get_runtime, so a change takes effect on the next message.
    RuntimeSettingSpec(
        key="ollama_timeout",
        type="float",
        label="Ollama generation timeout (seconds)",
        description=(
            "For the local Ollama provider, the maximum seconds to wait for the model to "
            "produce the NEXT token while streaming a reply. A cold model load or a slow "
            "CPU can exceed the old fixed 300 s and end the turn with «ollama request timed "
            "out». 0 = no limit (never time out — the default): a slow but progressing "
            "reply is always allowed to finish. The connection-open timeout is separate and "
            "always short, so an unreachable Ollama server still fails fast."
        ),
        category="ag",
        env_var="AKANA_OLLAMA_TIMEOUT",
        default=0.0,
        min=0,
        max=7_200,
        unit="sn",
    ),
    # Applied live (no restart): the OpenAI driver is rebuilt per request and reads
    # this via get_runtime, so a change takes effect on the next message.
    RuntimeSettingSpec(
        key="openai_timeout",
        type="float",
        label="OpenAI generation timeout (seconds)",
        description=(
            "For the OpenAI provider, the maximum seconds to wait for the model to "
            "produce the NEXT token while streaming a reply (and the per-request "
            "ceiling on one-shot calls). A deep-reasoning model can be silent for "
            "minutes before its first token; if it exceeds this, the turn ends with "
            "«openai request timed out». 0 = no limit (never time out): a slow but "
            "progressing reply is always allowed to finish. The connection-open "
            "timeout is separate and always short, so an unreachable endpoint still "
            "fails fast. Default preserves the historical fixed 300 s."
        ),
        category="ag",
        env_var="AKANA_OPENAI_TIMEOUT",
        default=300.0,
        min=0,
        max=7_200,
        unit="sn",
    ),
    # -- network resilience (NetworkEngine F0 — no restart, env-only fallback) ---
    RuntimeSettingSpec(
        key="network_max_retries",
        type="int",
        label="Max attempts",
        description=(
            "On a transient network error (timeout/5xx/429), how many times an LLM call "
            "is attempted at most. 1 = no retry. Auth/permanent errors are never retried."
        ),
        category="ag",
        env_var="AKANA_NETWORK_MAX_RETRIES",
        default=3,
        min=1,
        max=10,
    ),
    RuntimeSettingSpec(
        key="network_base_delay",
        type="float",
        label="Initial backoff delay (seconds)",
        description="The first wait of exponential backoff; doubles on each attempt.",
        category="ag",
        env_var="AKANA_NETWORK_BASE_DELAY",
        default=0.5,
        min=0,
        max=60,
        unit="sn",
    ),
    RuntimeSettingSpec(
        key="network_max_delay",
        type="float",
        label="Backoff ceiling (seconds)",
        description="The maximum wait of exponential backoff for a single attempt.",
        category="ag",
        env_var="AKANA_NETWORK_MAX_DELAY",
        default=8.0,
        min=0,
        max=300,
        unit="sn",
    ),
    RuntimeSettingSpec(
        key="network_total_timeout",
        type="float",
        label="Total retry time budget (seconds)",
        description="The total time of all attempts cannot exceed this budget. 0 = unlimited.",
        category="ag",
        env_var="AKANA_NETWORK_TOTAL_TIMEOUT",
        default=60.0,
        min=0,
        max=3_600,
        unit="sn",
    ),
    RuntimeSettingSpec(
        key="network_jitter",
        type="float",
        label="Backoff jitter ratio",
        description=(
            "±this ratio of randomness is added to the delay (spreads out thundering "
            "herds). 0 = no jitter."
        ),
        category="ag",
        env_var="AKANA_NETWORK_JITTER",
        default=0.25,
        min=0,
        max=1,
    ),
    RuntimeSettingSpec(
        key="network_breaker_threshold",
        type="int",
        label="Circuit breaker error threshold",
        description=(
            "When a provider hits this many consecutive errors, the circuit 'opens' "
            "(no call is made, fast-fail). 0 = circuit breaker off."
        ),
        category="ag",
        env_var="AKANA_NETWORK_BREAKER_THRESHOLD",
        default=5,
        min=0,
        max=100,
    ),
    RuntimeSettingSpec(
        key="network_breaker_cooldown",
        type="float",
        label="Circuit breaker cooldown (seconds)",
        description="Wait after the circuit opens until the single-attempt probe window.",
        category="ag",
        env_var="AKANA_NETWORK_BREAKER_COOLDOWN",
        default=30.0,
        min=0,
        max=3_600,
        unit="sn",
    ),
    # -- schedule engine (reminders + recurring prompts) --------------------------
    # The engine's poll cadence: how often the background loop checks for due
    # schedules and fires them. It does nothing when no schedule is due, so the loop
    # is always on (per the owner mandate there is NO separate schedule_enabled
    # kill switch — a proactive feature that is opt-in via CREATING a schedule).
    RuntimeSettingSpec(
        key="schedule_poll_seconds",
        type="float",
        label="Schedule check interval (seconds)",
        description=(
            "How often the schedule engine checks for due reminders / recurring "
            "prompts and fires them. Lower = more punctual firing but more frequent "
            "wake-ups; the default (30s) is a good balance. Firing itself is "
            "unaffected — a due schedule runs on the next check."
        ),
        category="ag",
        env_var="AKANA_SCHEDULE_POLL",
        default=30.0,
        min=5,
        max=600,
        unit="sn",
    ),
    # -- tools --------------------------------------------------------------------
    RuntimeSettingSpec(
        key="memory_tools_enabled",
        type="bool",
        label="Memory tools (MCP) enabled",
        description=(
            "Exposes the akana_memory MCP tools (memory_search/remember/forget) "
            "to the model. When disabled, the model cannot access memory via tools."
        ),
        category="araclar",
        env_var="AKANA_MEMORY_TOOLS",
        default=True,
        env_parse=_tool_flag_env,
    ),
    RuntimeSettingSpec(
        key="vault_tools_enabled",
        type="bool",
        label="Secure-vault tools (MCP) enabled",
        description=(
            "Exposes the akana_vault MCP read tools (vault_list/vault_get/"
            "vault_get_credential) to the model so it can discover and use stored "
            "secrets. When disabled, the model cannot read the vault via tools."
        ),
        category="araclar",
        env_var="AKANA_VAULT_TOOLS",
        default=True,
        env_parse=_tool_flag_env,
    ),
    RuntimeSettingSpec(
        key="schedule_tools_enabled",
        type="bool",
        label="Schedule tools (MCP) enabled",
        description=(
            "Exposes the akana_schedule tools (schedule_create/list/cancel/update) "
            "to the model, so it can set reminders and recurring scheduled prompts "
            "on your behalf. When disabled, the model cannot create schedules "
            "(existing ones still fire; you can still manage them yourself)."
        ),
        category="araclar",
        env_var="AKANA_SCHEDULE_TOOLS",
        default=True,
        env_parse=_tool_flag_env,
    ),
    # -- audio & wake -------------------------------------------------------------
    # wake_threshold single source of truth: runtime store > env (WAKE_THRESHOLD) >
    # default. At startup, apply_runtime_overrides mirrors this value to
    # Settings.wake_threshold; the live consumer (voice/wake.py + /voice/* status)
    # reads settings only, so a threshold changed from the UI takes effect without
    # restart.
    # ``hidden=True``: the spec stays (PUT validation + apply still need it), but it is
    # NOT rendered in the generic settings form — the voice panel has its own
    # «HEY AKANA» slider that PUTs the same key, so showing it twice was confusing.
    RuntimeSettingSpec(
        key="wake_threshold",
        type="float",
        label="Wake word threshold",
        description=(
            "Triggers when the server-side «hey akana» score exceeds this threshold; "
            "a lower value is more sensitive (more false triggers), a higher value is "
            "stricter. The browser-side fallback also reads this value."
        ),
        category="ses",
        env_var="WAKE_THRESHOLD",
        default=DEFAULTS["wake_threshold"],
        settings_attr="wake_threshold",
        min=0.01,
        max=1.0,
        hidden=True,
    ),
    # wake_min_frames: the sustain gate paired with the threshold above. Since the
    # score is a probability capped at 1.0, the threshold alone cannot be pushed
    # "higher" to reject more — this requires N consecutive hot frames instead of a
    # single peak, which is the real lever against false wakes. Same store>env>default
    # resolution and live (no-restart) apply as wake_threshold. ``hidden=True``: the
    # voice panel owns the slider (PUTs this key), so it is not shown in the generic form.
    RuntimeSettingSpec(
        key="wake_min_frames",
        type="int",
        label="Wake word sustain (frames)",
        description=(
            "How many consecutive ~80 ms frames must stay at/above the «hey akana» "
            "threshold before it triggers. 1 = fire on a single peak frame (most false "
            "wakes); higher is stricter. The peak-score meter is unaffected."
        ),
        category="ses",
        env_var="WAKE_MIN_FRAMES",
        default=DEFAULTS["wake_min_frames"],
        settings_attr="wake_min_frames",
        min=1,
        max=10,
        hidden=True,
    ),
    # -- Gemini Live (full-duplex native-audio voice, Phase 2) --------------------
    # When provider==gemini, the voice toggle switches from turn-based to Live WS.
    # These three settings are consumed where they live (voice/gemini_live.py +
    # /ws/voice/live); NO restart needed — WS connection reads live settings at
    # open time. Default OFF (privacy: audio streams to Google, opt-in).
    RuntimeSettingSpec(
        key="gemini_live_enabled",
        type="bool",
        label="Gemini Live (real-time voice) enabled",
        description=(
            "When provider 'Gemini' is selected, the voice-chat button switches to "
            "full-duplex Live mode (mic → Google → voice, uninterrupted). WHEN OFF, "
            "voice stays classic turn-based (Whisper→text→TTS) on every provider. Audio "
            "streams to the Google cloud — with your own gemini_api_key, opt-in."
        ),
        category="ses",
        env_var="AKANA_GEMINI_LIVE_ENABLED",
        default=DEFAULTS["gemini_live_enabled"],
        settings_attr="gemini_live_enabled",
    ),
    RuntimeSettingSpec(
        key="gemini_live_model",
        type="str",
        label="Gemini Live model",
        description=(
            "The Live native-audio model name (preview). Empty = default "
            "'models/gemini-2.5-flash-native-audio-latest'. Affects only the Live voice "
            "surface; text chat uses the separate 'Gemini model'."
        ),
        category="ses",
        env_var="AKANA_GEMINI_LIVE_MODEL",
        default=DEFAULTS["gemini_live_model"],
        settings_attr="gemini_live_model",
    ),
    RuntimeSettingSpec(
        key="gemini_live_voice",
        type="str",
        label="Gemini Live voice",
        description=(
            "The preset voice name for the Live response. Pick from the list; all are "
            "multilingual (including Turkish). Empty = default 'Charon'."
        ),
        category="ses",
        env_var="AKANA_GEMINI_LIVE_VOICE",
        default=DEFAULTS["gemini_live_voice"],
        settings_attr="gemini_live_voice",
        # Gemini Live native-audio preset voices (preview). Use <select> instead of
        # free text — avoids silent fallback from a misspelled voice name.
        options=("Aoede", "Charon", "Fenrir", "Kore", "Leda", "Orus", "Puck", "Zephyr"),
    ),
    # OpenAI Realtime (twin of Gemini Live) — when provider==openai, the voice toggle
    # switches from turn-based to Realtime WS (/ws/voice/realtime). All three consumed
    # where they live (voice/openai_realtime.py); NO restart needed. Default OFF
    # (privacy: audio streams to OpenAI, opt-in).
    RuntimeSettingSpec(
        key="openai_realtime_enabled",
        type="bool",
        label="OpenAI Realtime (real-time voice) enabled",
        description=(
            "When provider 'OpenAI' is selected, the voice-chat button switches to "
            "full-duplex Realtime mode (mic → OpenAI → voice, uninterrupted). WHEN OFF, "
            "voice stays classic turn-based (Whisper→text→TTS) on every provider. Audio "
            "streams to the OpenAI cloud — with your own openai_api_key, opt-in."
        ),
        category="ses",
        env_var="AKANA_OPENAI_REALTIME_ENABLED",
        default=DEFAULTS["openai_realtime_enabled"],
        settings_attr="openai_realtime_enabled",
    ),
    RuntimeSettingSpec(
        key="openai_realtime_model",
        type="str",
        label="OpenAI Realtime model",
        description=(
            "The Realtime model name. Empty = default 'gpt-4o-realtime-preview'. Affects "
            "only the Realtime voice surface; text chat uses the separate 'OpenAI model'."
        ),
        category="ses",
        env_var="AKANA_OPENAI_REALTIME_MODEL",
        default=DEFAULTS["openai_realtime_model"],
        settings_attr="openai_realtime_model",
    ),
    RuntimeSettingSpec(
        key="openai_realtime_voice",
        type="str",
        label="OpenAI Realtime voice",
        description=(
            "The preset voice name for the Realtime response. Pick from the list; all are "
            "multilingual (including Turkish). Empty = default 'alloy'. NOTE: 'marin'/'cedar' "
            "work ONLY with the GA model ('gpt-realtime'); invalid on the BETA preview model."
        ),
        category="ses",
        env_var="AKANA_OPENAI_REALTIME_VOICE",
        default=DEFAULTS["openai_realtime_voice"],
        settings_attr="openai_realtime_voice",
        # OpenAI Realtime preset voices — use <select> instead of free text.
        options=(
            "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse",
            "marin", "cedar",
        ),
    ),
)

SCHEMA: dict[str, RuntimeSettingSpec] = {spec.key: spec for spec in _SPECS}
