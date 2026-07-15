"""ScheduleEngine — the model-facing tool surface (create / list / cancel / update).

This is the SINGLE SOURCE for the schedule tool schemas AND the tool logic. Both
the MCP surface (:mod:`akana_server.schedule_mcp` → claude/cursor) and the native
function-calling surface (:mod:`akana_server.orchestrator.schedule_tools` →
openai/gemini/ollama) are derived from here, so the two never diverge — mirroring
how ``vault_mcp.tools`` single-sources the vault tools.

Names follow the MCP charset (underscores); descriptions are model-facing
(English-first) and tell the model WHEN to reach for each tool.

Datetime input: for a one-shot (``kind="once"``) schedule the ``when`` field
accepts an ISO-8601 datetime OR a short natural phrase — English ("tomorrow
09:00", "today 18:00") and Turkish ("yarın 09:00", "pazartesi 08:30") — resolved
in Turkey local time (+03:00, no DST). Recurring kinds take structured values
(``interval`` = seconds, ``daily``/``weekly`` = ``"HH:MM"`` [+ ``weekday``]), so
there is no ambiguity to parse.

SAFETY: creating a schedule that delivers over a connector requires that
connector to already be enabled (a schedule pointed at a disabled channel is
rejected up front); the total number of schedules is capped in the store
(``MAX_SCHEDULES``).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from akana_server.schedule.model import (
    CREATED_BY,
    DELIVERY_MODES,
    Delivery,
)
from akana_server.schedule.store import (
    TR_TZ,
    ScheduleStore,
    ScheduleValidationError,
    now_tr,
    parse_iso,
    to_iso,
)

log = logging.getLogger(__name__)

__all__ = [
    "ScheduleTools",
    "schedule_schemas",
    "resolve_once_when",
    "SCHEDULE_SCHEMAS",
]

#: Short-field input cap (title/kind/channel etc.). The ``prompt`` is NOT clipped
#: — it is the instruction the model wants run and can be long.
_ARG_MAX = 256


def _clip(value: Any, limit: int = _ARG_MAX) -> str:
    return str(value or "").strip()[:limit]


def _fold(text: str) -> str:
    """Lowercase + strip Turkish diacritics so 'yarın'/'yarin' and
    'pazartesi' match regardless of accents/keyboard."""
    table = str.maketrans({"ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u"})
    return text.strip().lower().translate(table)


# Day/weekday vocab (English + accent-folded Turkish).
_RELATIVE_DAYS: dict[str, int] = {
    "today": 0, "bugun": 0,
    "tomorrow": 1, "yarin": 1,
}
_WEEKDAY_NAMES: dict[str, int] = {
    "monday": 0, "pazartesi": 0,
    "tuesday": 1, "sali": 1,
    "wednesday": 2, "carsamba": 2,
    "thursday": 3, "persembe": 3,
    "friday": 4, "cuma": 4,
    "saturday": 5, "cumartesi": 5,
    "sunday": 6, "pazar": 6,
}
_TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
#: Default fire time when a natural phrase names a day but no clock time AND the
#: phrase carries no digits at all (a pure "yarın"/"tomorrow"). If it DOES carry
#: digits we could not bind, we raise instead — see :func:`_clock_for_day`.
_DEFAULT_HOUR = 9

#: Apostrophe variants a keyboard/iOS may emit for the Turkish locative ("8'de").
_APOS = "'’‘ʼ`´"
#: Dotted clock time ("18.30" → 18:30).
_DOTTED_TIME_RE = re.compile(r"\b([01]?\d|2[0-3])\.([0-5]\d)\b")
#: Turkish locative on an hour: "8'de" / "8'da" / "8'te" (suffix optional).
_APOS_TIME_RE = re.compile(rf"\b([01]?\d|2[0-3])[{_APOS}](?:de|da|te|ta)?\b")
#: "saat 8" / "saat 08:30".
_SAAT_TIME_RE = re.compile(r"\bsaat\s+([01]?\d|2[0-3])(?::([0-5]\d))?\b")
#: A day-part word + hour: "sabah 8", "akşam 7", "öğlen 12", "gece 11".
_PERIOD_TIME_RE = re.compile(
    r"\b(sabah|ogleyin|oglen|ogle|aksam|gece)\s+([01]?\d|2[0-3])(?::([0-5]\d))?\b"
)
#: A standalone 1–2 digit hour ("yarın 8" → 08:00). Only consulted alongside a
#: named day, so a bare "8" never silently becomes a time on its own.
_BARE_HOUR_RE = re.compile(r"\b([01]?\d|2[0-3])\b")

#: Relative-offset unit → seconds (English + accent-folded Turkish).
_EN_UNIT_SECONDS: dict[str, int] = {
    "second": 1, "seconds": 1,
    "minute": 60, "minutes": 60,
    "hour": 3600, "hours": 3600,
    "day": 86400, "days": 86400,
}
_TR_UNIT_SECONDS: dict[str, int] = {
    "saniye": 1, "dakika": 60, "saat": 3600, "gun": 86400,
}
#: Small Turkish number-word map so "bir saat sonra" / "yarım saat sonra" work
#: without the user typing a digit ("yarım" = ½, "buçuk" = +½ is handled inline).
_TR_NUM_WORDS: dict[str, float] = {
    "yarim": 0.5, "bir": 1, "iki": 2, "uc": 3, "dort": 4, "bes": 5,
    "alti": 6, "yedi": 7, "sekiz": 8, "dokuz": 9, "on": 10,
}
_EN_IN_N_RE = re.compile(r"\bin\s+(\d+)\s+(seconds?|minutes?|hours?|days?)\b")
_EN_IN_A_RE = re.compile(r"\bin\s+(?:a|an)\s+(second|minute|hour|day)\b")
_TR_SONRA_UNIT_RE = re.compile(r"(saniye|dakika|saat|gun)\s+sonra\b")
_TRAILING_NUM_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*$")
_TRAILING_WORD_RE = re.compile(r"([a-z]+)\s*$")


def _apply_period(period: str, hh: int) -> int:
    """Shift an hour written under a day-part word into 24h form.

    "sabah" (morning) is as-written; "öğle(n)"/"akşam"/"gece" push a sub-noon hour
    into the afternoon/evening/night (``+12``) so "akşam 7" → 19, "gece 11" → 23,
    "öğlen 12" → 12 (already ≥12, unchanged)."""
    if period == "sabah":
        return hh
    return hh + 12 if hh < 12 else hh


def _parse_clock(folded: str) -> tuple[int, int] | None:
    """An explicit clock time from a folded phrase, or ``None``.

    Tries, in order: a day-part word + hour ("akşam 7" → 19:00, checked FIRST so
    the AM/PM shift wins over a bare-looking number), ``HH:MM``, dotted ``HH.MM``,
    the Turkish locative ("8'de"), and "saat 8". Returns ``(hour, minute)``."""
    m = _PERIOD_TIME_RE.search(folded)
    if m:
        hh = _apply_period(m.group(1), int(m.group(2)))
        mm = int(m.group(3)) if m.group(3) else 0
        if 0 <= hh <= 23:
            return hh, mm
    m = _TIME_RE.search(folded)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _DOTTED_TIME_RE.search(folded)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _APOS_TIME_RE.search(folded)
    if m:
        return int(m.group(1)), 0
    m = _SAAT_TIME_RE.search(folded)
    if m:
        return int(m.group(1)), int(m.group(2)) if m.group(2) else 0
    return None


def _bare_hour(folded: str) -> int | None:
    """A lone 1–2 digit hour (0–23) — "yarın 8" → 8. ``None`` if there is no
    standalone number, or the number is not a valid hour (so the caller can flag
    the unbindable digits rather than default to 09:00)."""
    m = _BARE_HOUR_RE.search(folded)
    return int(m.group(1)) if m else None


def _clock_for_day(
    clock: tuple[int, int] | None, folded: str, has_digit: bool
) -> tuple[int, int]:
    """The ``(hour, minute)`` to attach to a NAMED day (today/tomorrow/weekday).

    Priority: an explicit clock form → a bare hour ("yarın 8") → the 09:00 default.
    CRITICAL (BUG 2): the 09:00 default is used ONLY for a pure day word with NO
    digits at all. When the phrase carries digits we could not bind to a valid time
    ("yarın 30", "yarın 8:99"), we RAISE — silently firing at 09:00 is a
    wrong-but-plausible time, worse than a clear error the model can correct."""
    if clock is not None:
        return clock
    bare = _bare_hour(folded)
    if bare is not None:
        return bare, 0
    if has_digit:
        raise ScheduleValidationError(
            "I couldn't read a valid clock time from that; give it as HH:MM "
            "(e.g. 'yarın 18:30', 'tomorrow 08:00')"
        )
    return _DEFAULT_HOUR, 0


def _parse_relative(folded: str, ref: datetime) -> datetime | None:
    """A relative offset from ``ref`` ("in 5 minutes", "in an hour", "5 dakika
    sonra", "1 saat sonra", "yarım saat sonra", "bir buçuk saat sonra"), or ``None``.

    Checked BEFORE clock parsing so "1 saat sonra" is read as *now + 1 hour*, not
    as the clock time "1". Turkish quantity may be a digit, a number word, "yarım"
    (½), or "<n> buçuk" (n + ½)."""
    m = _EN_IN_N_RE.search(folded)
    if m:
        secs = int(m.group(1)) * _EN_UNIT_SECONDS[m.group(2)]
        return ref + timedelta(seconds=secs) if secs > 0 else None
    m = _EN_IN_A_RE.search(folded)
    if m:
        return ref + timedelta(seconds=_EN_UNIT_SECONDS[m.group(1)])
    m = _TR_SONRA_UNIT_RE.search(folded)
    if m:
        unit_secs = _TR_UNIT_SECONDS[m.group(1)]
        prefix = folded[: m.start()].strip()
        half = False
        if prefix.endswith("bucuk"):  # "<n> buçuk <unit> sonra" → n + ½
            half = True
            prefix = prefix[: -len("bucuk")].strip()
        qty: float | None = None
        dm = _TRAILING_NUM_RE.search(prefix)
        if dm:
            qty = float(dm.group(1).replace(",", "."))
        else:
            wm = _TRAILING_WORD_RE.search(prefix)
            if wm and wm.group(1) in _TR_NUM_WORDS:
                qty = _TR_NUM_WORDS[wm.group(1)]
        if qty is None and half:
            qty = 0.0  # bare "buçuk saat sonra" → half a unit
        if qty is None:
            return None
        secs = (qty + (0.5 if half else 0.0)) * unit_secs
        return ref + timedelta(seconds=secs) if secs > 0 else None
    return None


def resolve_once_when(when: str, *, now: datetime | None = None) -> str:
    """Resolve a one-shot ``when`` to a stored ISO string (Turkey local time).

    Resolution order:

    1. an ISO-8601 datetime/date is used verbatim;
    2. a relative offset from now ("in 5 minutes", "in an hour", "5 dakika sonra",
       "1 saat sonra", "yarım saat sonra") — computed from ``now``;
    3. a named day (today / tomorrow / weekday) plus a clock time in any supported
       form (``HH:MM``, dotted ``18.30``, "8'de", "saat 8", "akşam 7"/"gece 11"),
       or a bare hour after the day ("yarın 8"), or — only for a *pure* day word
       with no digits — the 09:00 default;
    4. a bare clock time on its own → today, or tomorrow if already past;
    5. the Turkish date-range parser as a last resort (its range START).

    Raises :class:`ScheduleValidationError` when nothing matches, OR when the
    phrase names a day but carries digits that could not be bound to a valid clock
    time (never silently defaults to 09:00 in that case — see BUG 2). The model
    then learns to pass ISO/HH:MM."""
    raw = (when or "").strip()
    if not raw:
        raise ScheduleValidationError("a 'when' datetime is required for a one-shot schedule")
    # 1) Direct ISO / date — used verbatim.
    try:
        return to_iso(parse_iso(raw))
    except ScheduleValidationError:
        pass
    ref = (now or now_tr()).astimezone(TR_TZ)
    folded = _fold(raw)
    has_digit = any(c.isdigit() for c in raw)

    # 2) Relative offset from now (before clock parsing — see _parse_relative).
    rel = _parse_relative(folded, ref)
    if rel is not None:
        return to_iso(rel)

    # 3) Any explicit clock time in the phrase (None if it names no time).
    clock = _parse_clock(folded)

    # 3a) Relative day word (today/tomorrow).
    for word, offset in _RELATIVE_DAYS.items():
        if re.search(rf"\b{word}\b", folded):
            hh, mm = _clock_for_day(clock, folded, has_digit)
            target = (ref + timedelta(days=offset)).replace(
                hour=hh, minute=mm, second=0, microsecond=0
            )
            return to_iso(target)
    # 3b) Weekday name → next occurrence.
    for word, wd in _WEEKDAY_NAMES.items():
        if re.search(rf"\b{word}\b", folded):
            hh, mm = _clock_for_day(clock, folded, has_digit)
            candidate = ref.replace(hour=hh, minute=mm, second=0, microsecond=0)
            days_ahead = (wd - candidate.weekday()) % 7
            candidate += timedelta(days=days_ahead)
            if candidate <= ref:
                candidate += timedelta(days=7)
            return to_iso(candidate)
    # 4) Bare clock time only → today, or tomorrow if already past.
    if clock is not None:
        hh, mm = clock
        candidate = ref.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= ref:
            candidate += timedelta(days=1)
        return to_iso(candidate)
    # 5) Turkish date-range fallback (reuse of the memory NL parser; range START).
    try:
        from datetime import timezone

        from akana.memory.time_expressions import parse_time_range

        rng = parse_time_range(raw, now=ref.astimezone(timezone.utc))
        if rng:
            return to_iso(parse_iso(rng[0]))
    except Exception:  # pragma: no cover - the fallback is best-effort
        pass
    raise ScheduleValidationError(
        f"could not understand the time {when!r}; pass an ISO datetime "
        "(e.g. 2026-07-12T09:00) or a phrase like 'tomorrow 09:00', "
        "'in 2 hours', or '5 dakika sonra'"
    )


def _parse_weekday(value: Any) -> int | None:
    """Coerce a weekday arg (int 0–6 or a day name) to 0–6, or ``None``."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = _fold(str(value))
    if s.isdigit():
        return int(s)
    return _WEEKDAY_NAMES.get(s)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _telegram_enabled(data_dir: Path | str) -> bool:
    """Best-effort 'is the telegram connector enabled' — runtime store OR env.

    Used for the CREATE-time safety gate. Reads the runtime store first
    (where the UI persists the toggle) and falls back to the env kill switch, so
    the check is right whether the channel was enabled from Settings or ``.env``.
    """
    try:
        from akana_server.runtime_settings import get_runtime

        if bool(get_runtime("telegram_enabled", SimpleNamespace(data_dir=data_dir))):
            return True
    except Exception:  # pragma: no cover - resolution failure → env fallback
        pass
    return os.environ.get("AKANA_TELEGRAM_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _connector_enabled(data_dir: Path | str, channel: str) -> bool:
    """Whether ``channel`` is an enabled outbound connector (SAFETY gate).

    Only Telegram exists today; an unknown channel is treated as not-enabled."""
    if channel == "telegram":
        return _telegram_enabled(data_dir)
    return False


# --------------------------------------------------------------------------- #
# Tool schemas (SINGLE SOURCE — MCP + native surfaces both derive from these)
# --------------------------------------------------------------------------- #

_DELIVERY_PROPS: dict[str, Any] = {
    "delivery_mode": {
        "type": "string",
        "enum": ["thread", "connector", "both"],
        "description": (
            "Where the result goes: 'thread' (default) appends it to a chat "
            "conversation the user can read in the web UI; 'connector' pushes it "
            "to an outside channel (e.g. Telegram); 'both' does both."
        ),
    },
    "channel": {
        "type": "string",
        "description": "Connector id for connector/both delivery, e.g. 'telegram'. The connector must already be enabled.",
    },
    "chat_id": {
        "type": "string",
        "description": "The connector-side chat id to send to (required for connector/both).",
    },
    "conversation_id": {
        "type": "string",
        "description": "Optional existing chat thread id to append into; omit to let a new thread be created from the title.",
    },
}

SCHEDULE_SCHEMAS: tuple[dict[str, Any], ...] = (
    {
        "name": "schedule_create",
        "description": (
            "Create a reminder or recurring scheduled prompt. Provide EXACTLY ONE of "
            "'message' or 'prompt'. Use 'message' for a plain reminder — the exact "
            "words are delivered verbatim with no LLM turn (fast, and it says exactly "
            "what you set): pick it for 'remind me to X', 'X diye hatırlat', "
            "'... hatırlat'. Use 'prompt' only when real work should run at fire time "
            "(e.g. 'her sabah haberleri özetle' / 'summarize the news every morning') "
            "— it runs as a full LLM turn and the result is delivered. By default the "
            "result is posted into THIS conversation (set separate_thread=true only if "
            "the user wants its own thread; connector delivery via 'channel' also opts "
            "out). Use it for reminders ('remind me in an hour', '5 dakika sonra'), a "
            "recurring briefing, or work to do later."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short label for the schedule (also the created thread's title).",
                },
                "message": {
                    "type": "string",
                    "description": (
                        "The exact reminder text to deliver VERBATIM when it fires (no "
                        "LLM turn). Use this for plain reminders — 'remind me to call "
                        "mom' → message='Call mom', 'suyu iç diye hatırlat' → "
                        "message='Suyu iç'. Provide EITHER message OR prompt, not both."
                    ),
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "What Akana should DO when the schedule fires, run as a full "
                        "LLM turn (its result is delivered). Use this ONLY when real "
                        "work is wanted, not for a plain reminder. Provide EITHER "
                        "prompt OR message, not both."
                    ),
                },
                "kind": {
                    "type": "string",
                    "enum": ["once", "interval", "daily", "weekly"],
                    "description": "once=a single time; interval=every N seconds; daily=every day; weekly=every week.",
                },
                "when": {
                    "type": "string",
                    "description": (
                        "once: an ISO datetime (2026-07-12T09:00), a relative phrase "
                        "('in 2 hours', '5 dakika sonra', 'in an hour'), or a day+time "
                        "('tomorrow 09:00', 'yarın 08:30', 'akşam 7'). interval: the "
                        "number of seconds. daily/weekly: a time as 'HH:MM'."
                    ),
                },
                "weekday": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 6,
                    "description": "For kind=weekly: 0=Monday … 6=Sunday.",
                },
                **_DELIVERY_PROPS,
                "separate_thread": {
                    "type": "boolean",
                    "description": (
                        "true = deliver fires into a separate thread instead of "
                        "posting into this conversation (default false)."
                    ),
                },
                "language": {
                    "type": "string",
                    "description": "Language for the fired turn ('en' or 'tr'); defaults to the current setting.",
                },
            },
            "required": ["title", "kind", "when"],
        },
    },
    {
        "name": "schedule_list",
        "description": (
            "List all schedules Akana currently has (reminders + recurring prompts), "
            "with their next run time and enabled state. Call it to answer 'what "
            "reminders do I have?' or before cancelling/updating one."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "schedule_cancel",
        "description": (
            "Delete a schedule by its id (get the id from schedule_list first). "
            "Cancelling one that does not exist is a no-op."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The schedule id to cancel."}
            },
            "required": ["id"],
        },
    },
    {
        "name": "schedule_update",
        "description": (
            "Update an existing schedule by id — change its title, prompt, timing "
            "('when'/'weekday'), delivery, or enable/disable it. Use schedule_list "
            "first to find the id. Re-enabling a stale schedule rolls it forward so "
            "it does not immediately fire a backlog."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The schedule id to update."},
                "title": {"type": "string", "description": "New title."},
                "prompt": {"type": "string", "description": "New instruction to run when it fires (an LLM turn)."},
                "message": {"type": "string", "description": "New verbatim reminder text (delivered as-is, no LLM turn)."},
                "kind": {
                    "type": "string",
                    "enum": ["once", "interval", "daily", "weekly"],
                    "description": "Change the recurrence kind. Requires 'when' too (its meaning differs per kind).",
                },
                "enabled": {"type": "boolean", "description": "Enable (true) or pause (false) the schedule."},
                "when": {"type": "string", "description": "New timing (same forms as schedule_create)."},
                "weekday": {"type": "integer", "minimum": 0, "maximum": 6, "description": "New weekday (weekly)."},
                **_DELIVERY_PROPS,
            },
            "required": ["id"],
        },
    },
)


def schedule_schemas() -> list[dict[str, Any]]:
    """Copy of the MCP-format schemas (name, description, ``input_schema``)."""
    return [dict(s) for s in SCHEDULE_SCHEMAS]


# --------------------------------------------------------------------------- #
# Tool logic
# --------------------------------------------------------------------------- #


class ScheduleTools:
    """``data_dir``-scoped schedule tools. An error IS a result: a bad request
    returns ``{"error": "..."}`` (never raises at the tool boundary), mirroring
    :class:`akana_server.vault_mcp.tools.VaultTools`.

    ``created_by`` tags who is creating schedules through this instance — the
    model surfaces (MCP/native) use ``"assistant"``; the REST layer talks to the
    store directly with ``"user"``.
    """

    def __init__(
        self,
        data_dir: Path | str,
        *,
        created_by: str = "assistant",
        origin_conversation: str | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._store = ScheduleStore(data_dir)
        self._created_by = created_by if created_by in CREATED_BY else "assistant"
        #: SAME-CHAT default: the conversation this tool surface was invoked FROM
        #: (native dispatch passes it per call; the MCP child gets it via the
        #: AKANA_CONVERSATION_ID env). A create with no explicit delivery target
        #: defaults to injecting fires into this conversation.
        self._origin_conversation = (origin_conversation or "").strip() or None

    @property
    def _handlers(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        return {
            "schedule_create": self._tool_create,
            "schedule_list": self._tool_list,
            "schedule_cancel": self._tool_cancel,
            "schedule_update": self._tool_update,
        }

    def handle_tool_call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = self._handlers.get(name)
        if handler is None:
            return {"error": f"unknown tool: {name}"}
        try:
            return handler(args if isinstance(args, dict) else {})
        except ScheduleValidationError as e:
            return {"error": f"invalid request: {e}"[:300]}
        except Exception as e:  # noqa: BLE001 - a tool error must not break the turn
            log.warning("schedule tool failed: %s", name, exc_info=True)
            return {"error": f"{type(e).__name__}: {e}"[:300]}

    def _default_language(self) -> str:
        try:
            from akana_server.runtime_settings import resolve_language

            return resolve_language(SimpleNamespace(data_dir=self._data_dir))
        except Exception:
            return "en"

    def _delivery_from_args(
        self, args: dict[str, Any], base: Delivery | None = None
    ) -> Delivery:
        """Build (or merge over ``base``) a Delivery from flat tool args."""
        b = base or Delivery()
        mode = _clip(args.get("delivery_mode", args.get("mode", b.mode))).lower() or "thread"
        conv = args.get("conversation_id", b.conversation_id)
        return Delivery(
            mode=mode if mode in DELIVERY_MODES else "thread",
            channel=_clip(args.get("channel", b.channel)).lower(),
            chat_id=_clip(args.get("chat_id", b.chat_id)),
            conversation_id=(str(conv).strip() or None) if conv else None,
        )

    def _resolve_once_when_guarded(self, when_raw: str) -> str:
        """Resolve a one-shot ``when`` AND reject a time already in the past.

        Shared by the create tool and (via it) the REST create path. Without this
        guard a ``once`` whose resolved time is in the past sails through the store
        (``parse_iso`` accepts it) and becomes due IMMEDIATELY — the reminder fires
        the instant it is created (BUG 3). Policy:

        * more than a 60s grace INTO the past → error naming the resolved time, so
          the model can correct it (e.g. it picked yesterday by mistake);
        * within the grace window (a hair in the past, or 'right now') → nudge to
          now + 5s so it fires cleanly on the next poll instead of racing the write.
        """
        now = now_tr()
        resolved_iso = resolve_once_when(when_raw, now=now)
        resolved = parse_iso(resolved_iso)
        delta = (resolved - now).total_seconds()
        if delta < -60:
            raise ScheduleValidationError(
                f"that time ({resolved_iso}) is in the past; pick a future time"
            )
        if delta <= 0:
            # Basically-now / within grace: fire a few seconds out, deterministically.
            return to_iso(now + timedelta(seconds=5))
        return resolved_iso

    def _tool_create(self, args: dict[str, Any]) -> dict[str, Any]:
        title = _clip(args.get("title"))
        prompt = str(args.get("prompt") or "").strip()
        # BUG 9 — verbatim reminder body. Exactly one of prompt/message is expected;
        # the store's _validate_spec enforces that (and a helpful error if not).
        message = str(args.get("message") or "").strip()
        kind = _clip(args.get("kind")).lower()
        when_raw = str(args.get("when") or "").strip()
        weekday = _parse_weekday(args.get("weekday"))
        delivery = self._delivery_from_args(args)
        # SAME-CHAT default: created from a live conversation with NO explicit
        # delivery target → fires are injected into that conversation (assistant
        # message, busy-safe) instead of spawning a separate engine-owned thread.
        # ``separate_thread: true`` (or an explicit conversation_id/connector
        # target in args) opts out.
        if (
            self._origin_conversation
            and delivery.mode == "thread"
            and not delivery.conversation_id
            and not args.get("separate_thread")
        ):
            delivery = Delivery(
                mode="thread",
                channel=delivery.channel,
                chat_id=delivery.chat_id,
                conversation_id=self._origin_conversation,
                same_chat=True,
            )
        language = _clip(args.get("language")).lower() or self._default_language()

        # SAFETY: a connector-delivery schedule requires the connector enabled NOW.
        if delivery.mode in ("connector", "both") and not _connector_enabled(
            self._data_dir, delivery.channel
        ):
            return {
                "error": (
                    f"the '{delivery.channel or '?'}' connector is not enabled; "
                    "enable it first, or use thread delivery"
                )
            }

        when = self._resolve_once_when_guarded(when_raw) if kind == "once" else when_raw
        item = self._store.create(
            title=title,
            prompt=prompt,
            message=message,
            kind=kind,
            when=when,
            weekday=weekday,
            delivery=delivery,
            created_by=self._created_by,
            language=language,
        )
        return {"status": "created", "schedule": item.public_dict()}

    def _tool_list(self, args: dict[str, Any]) -> dict[str, Any]:
        items = self._store.load()
        return {
            "schedules": [i.public_dict() for i in items],
            "count": len(items),
        }

    def _tool_cancel(self, args: dict[str, Any]) -> dict[str, Any]:
        sid = _clip(args.get("id"))
        if not sid:
            return {"error": "id is empty"}
        removed = self._store.cancel(sid)
        return {
            "id": sid,
            "removed": removed,
            "status": "cancelled" if removed else "absent",
        }

    def _tool_update(self, args: dict[str, Any]) -> dict[str, Any]:
        sid = _clip(args.get("id"))
        if not sid:
            return {"error": "id is empty"}
        existing = self._store.get(sid)
        if existing is None:
            return {"error": f"no schedule with id {sid}"}
        kwargs: dict[str, Any] = {}
        if "title" in args:
            kwargs["title"] = str(args["title"] or "").strip()
        if "prompt" in args:
            kwargs["prompt"] = str(args["prompt"] or "").strip()
        if "message" in args:  # BUG 9 — allow switching a schedule to verbatim delivery
            kwargs["message"] = str(args["message"] or "").strip()
        if "kind" in args:  # BUG 6 — kind is now patchable (store enforces it needs 'when')
            kwargs["kind"] = _clip(args["kind"]).lower()
        if "enabled" in args:
            kwargs["enabled"] = _as_bool(args["enabled"])
        if "weekday" in args:
            # BUG 7 — an unparseable weekday NAME must error, not silently keep the
            # old weekday while reporting success. Empty/absent → leave it unchanged.
            raw_wd = args["weekday"]
            wd = _parse_weekday(raw_wd)
            if wd is None:
                if str("" if raw_wd is None else raw_wd).strip() != "":
                    return {
                        "error": (
                            f"could not understand weekday {raw_wd!r}; use 0–6 "
                            "(0=Monday … 6=Sunday) or a day name"
                        )
                    }
            else:
                kwargs["weekday"] = wd
        if "when" in args:
            raw = str(args["when"] or "").strip()
            # Resolve natural-language 'when' against the TARGET kind: if this same
            # update is also changing kind→once, honor that (not the stale kind).
            target_kind = kwargs.get("kind", existing.kind)
            kwargs["when"] = (
                self._resolve_once_when_guarded(raw) if target_kind == "once" else raw
            )
        if any(k in args for k in ("delivery_mode", "mode", "channel", "chat_id", "conversation_id")):
            merged = self._delivery_from_args(args, base=existing.delivery)
            if merged.mode in ("connector", "both") and not _connector_enabled(
                self._data_dir, merged.channel
            ):
                return {
                    "error": (
                        f"the '{merged.channel or '?'}' connector is not enabled; "
                        "enable it first, or use thread delivery"
                    )
                }
            kwargs["delivery"] = merged
        updated = self._store.update(sid, **kwargs)
        if updated is None:
            return {"error": f"no schedule with id {sid}"}
        return {"status": "updated", "schedule": updated.public_dict()}
