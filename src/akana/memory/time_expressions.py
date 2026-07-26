"""Bilingual natural-language time-expression parser (deterministic, no LLM).

Split out of ``tools.py`` — this module owns the "when" vocabulary in BOTH
shipped languages ("today"/"bugün", "yesterday"/"dün", "last week"/"geçen
hafta", "in March"/"mart ayında", "last 7 days"/"son 7 gün", "3 days ago"/"3
gün önce" …) and turns it into an inclusive ``(from, to)`` ISO-UTC pair.

The product ships English-default with Turkish on explicit choice, so the model
phrases a time bound in the conversation's own language; a Turkish-only table
made every time-scoped recall an ``invalid_request`` in English mode. Both
vocabularies are therefore always active — the parser does not consult the
language setting, because the caller's language and the phrase's language can
disagree (an English-mode user may still type "dün").

Day/week/month boundaries are a WALL-CLOCK question — "yesterday" means the
user's yesterday — so they are computed in the USER's local zone (see
:func:`_local_tz`) and converted to the store format (ISO-UTC, millisecond-Z).
Only the interpretation of a relative phrase moves with the zone: a timestamp
that already carries an offset keeps its instant, unchanged.

``tools.py`` re-exports :func:`parse_time_range` (and consumes
:data:`DATE_ONLY_RE`/:data:`TimeEdge` for :func:`~akana.memory.tools.
parse_time_bound`) so existing callers are unaffected by the split.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from re import compile as _compile
from typing import Literal

from akana.memory.terms import fold_text

__all__ = ["TimeEdge", "DATE_ONLY_RE", "parse_time_range"]

TimeEdge = Literal["start", "end"]

#: Last-resort zone: the historical hardcoded pin (Turkey, +03:00 since 2016, no DST).
_TR_TZ = timezone(timedelta(hours=3))

#: The server owns the single timezone resolution point (``AKANA_TIMEZONE`` — an
#: IANA name or a plain offset, since Windows ships no tz database > the host's
#: own current offset > +03:00). Looked up once and held as the FUNCTION, never
#: as a resolved zone: it must be called per parse so a long-running process
#: follows a DST transition.
_SERVER_LOCAL_TZ: Callable[[], tzinfo] | None = None
_SERVER_LOCAL_TZ_LOOKED_UP = False


def _local_tz() -> tzinfo:
    """The zone a relative phrase ("yesterday", "last week") is bucketed in.

    A fixed +03:00 gave every user outside UTC+3 a window shifted by hours —
    "what did I say yesterday" silently returned the wrong turns, or none at
    all near the boundary. The resolution point lives in
    ``akana_server.schedule.store.local_tz`` so the whole product answers "what
    zone is the user in?" once; the import is optional because this package is
    imported standalone (CLI, tests) and must not hard-depend on the server.
    Standalone the fallback is the host's own zone, then :data:`_TR_TZ`.
    """
    global _SERVER_LOCAL_TZ, _SERVER_LOCAL_TZ_LOOKED_UP
    if not _SERVER_LOCAL_TZ_LOOKED_UP:
        _SERVER_LOCAL_TZ_LOOKED_UP = True
        try:
            from akana_server.schedule.store import local_tz

            _SERVER_LOCAL_TZ = local_tz
        except Exception:  # running without the server package
            _SERVER_LOCAL_TZ = None
    if _SERVER_LOCAL_TZ is not None:
        try:
            return _SERVER_LOCAL_TZ()
        except Exception:  # a broken zone must never break a memory query
            pass
    try:
        return datetime.now().astimezone().tzinfo or _TR_TZ
    except Exception:  # pragma: no cover - a platform with no local zone at all
        return _TR_TZ

# Accent-free matching after fold_text: "GEÇEN" → "geçen" → "gecen" ("last/previous").
# The patterns are kept in ASCII so the user can type either "dün" or "dun" ("yesterday").
_TR_ASCII = str.maketrans({"ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u"})

# Apostrophe variants are stripped: keyboards/iOS produce a curly (’) and similar
# instead of a straight (') — so "Mart’ta" should match like "Mart'ta".
_APOSTROPHES = str.maketrans("", "", "'’‘ʼ`´")

_TR_MONTHS: dict[str, int] = {
    "ocak": 1, "subat": 2, "mart": 3, "nisan": 4, "mayis": 5, "haziran": 6,
    "temmuz": 7, "agustos": 8, "eylul": 9, "ekim": 10, "kasim": 11, "aralik": 12,
}

_TR_LAST_RE = _compile(r"^son (\d{1,3}) (saat|gun|hafta|ay)$")
_TR_AGO_RE = _compile(r"^(\d{1,3}) gun once$")
# "mart" | "martta" | "mart ayında" | "mart 2025" | "2025 mart" (suffix + year optional)
_TR_MONTH_RE = _compile(
    r"^(?:(\d{4}) )?(" + "|".join(_TR_MONTHS) + r")(?:ta|te|da|de)?"
    r"(?: (\d{4}))?(?: ay(?:i|inda))?$"
)

#: English month names + the unambiguous 3-letter abbreviations. "may" is only
#: listed in full (the abbreviation is the same string), and the alternation is
#: built longest-first so "march" wins over "mar".
_EN_MONTHS: dict[str, int] = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_EN_MONTH_ALT = "|".join(sorted(_EN_MONTHS, key=len, reverse=True))

# "last 7 days" / "past 3 hours" / "last 2 weeks" — a sliding window up to now.
_EN_LAST_RE = _compile(r"^(?:last|past) (\d{1,3}) (hour|day|week|month)s?$")
# "3 days ago" — the whole of that day (mirrors "3 gün önce").
_EN_AGO_RE = _compile(r"^(\d{1,3}) days? ago$")
# "march" | "in march" | "march 2025" | "2025 march"
_EN_MONTH_RE = _compile(
    r"^(?:in )?(?:(\d{4}) )?(" + _EN_MONTH_ALT + r")(?: (\d{4}))?$"
)
DATE_ONLY_RE = _compile(r"^\d{4}-\d{2}-\d{2}$")

_RELATIVE_RE = _compile(r"^relative:(\d+)([hdw])$", flags=0)

#: Fixed phrases → the one span each names. Both languages land on the same
#: token so the boundary math has a single implementation and EN/TR can never
#: drift apart ("last week" and "geçen hafta" must resolve identically).
_FIXED_SPANS: dict[str, str] = {
    "bugun": "today", "today": "today",
    "dun": "yesterday", "yesterday": "yesterday",
    "bu hafta": "this_week", "this week": "this_week",
    "gecen hafta": "last_week", "last week": "last_week",
    "bu ay": "this_month", "this month": "this_month",
    "gecen ay": "last_month", "last month": "last_month",
    "bu yil": "this_year", "this year": "this_year",
    "gecen yil": "last_year", "last year": "last_year",
}

#: "son <n> gun" / "last <n> days" — the unit each token measures. Turkish "ay"
#: and English "month" are a 30-day approximation, not a calendar month.
_WINDOW_UNITS: dict[str, str] = {
    "saat": "hours", "hour": "hours",
    "gun": "days", "day": "days",
    "hafta": "weeks", "week": "weeks",
    "ay": "months", "month": "months",
}


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _day_span(first: date, last: date | None = None, *, tz: tzinfo) -> tuple[str, str]:
    """Local days ``[first, last]`` → an inclusive (start, end) ISO-UTC pair.

    ``tz`` is passed in, not resolved here: one parse must use ONE zone for
    every boundary it computes, even across a DST transition mid-call.
    """
    start = datetime(first.year, first.month, first.day, tzinfo=tz)
    end_excl = datetime.combine((last or first) + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    return _iso_utc(start), _iso_utc(end_excl - timedelta(milliseconds=1))


def _month_span(year: int, month: int, *, tz: tzinfo) -> tuple[str, str]:
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    last = date(next_y, next_m, 1) - timedelta(days=1)
    return _day_span(date(year, month, 1), last, tz=tz)


def parse_time_range(value: str | None, *, now: datetime | None = None) -> tuple[str, str] | None:
    """Natural-language time expression (EN or TR) → an inclusive ``(from, to)`` ISO-UTC pair.

    Deterministic and LLM-free, in both shipped languages:
    "today"/"bugün", "yesterday"/"dün", "this|last week"/"bu|geçen hafta",
    "this|last month"/"bu|geçen ay", "this|last year"/"bu|geçen yıl",
    "last <n> hours|days|weeks|months"/"son <n> saat|gün|hafta|ay",
    "<n> days ago"/"<n> gün önce", and month names ("in March", "March 2025",
    "mart", "martta", "mart ayında"). Accent-free Turkish spelling is also
    recognized ("gecen hafta"). ``relative:<n><h|d|w>`` → (that point, now).
    A month name without a year uses the current year; if the month has not
    started yet, the previous year ("December" asked in June → last December).
    An unrecognized expression → ``None``.
    """
    if not value:
        return None
    now_utc = now if now is not None else datetime.now(UTC)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)
    m = _RELATIVE_RE.match(value.strip())
    if m:
        # Delegate the relative-point half of the contract to parse_time_point,
        # kept in tools.py (it is not Turkish-specific). Imported lazily to
        # avoid a circular import (tools.py imports parse_time_range from here).
        from akana.memory.tools import parse_time_point

        point = parse_time_point(value, now=now_utc)
        return (point, _iso_utc(now_utc)) if point else None
    # One zone for this whole parse (see _day_span): "yesterday" is the user's
    # yesterday, so the day grid is local, not a fixed +03:00.
    tz = _local_tz()
    today = now_utc.astimezone(tz).date()
    text = " ".join(fold_text(value).translate(_TR_ASCII).translate(_APOSTROPHES).split())

    span = _FIXED_SPANS.get(text)
    if span is not None:
        # The week starts Monday in BOTH languages: it is the store's grid, not a
        # locale preference, so "last week" and "geçen hafta" name the same days.
        monday = today - timedelta(days=today.weekday())
        if span == "today":
            return _day_span(today, tz=tz)
        if span == "yesterday":
            return _day_span(today - timedelta(days=1), tz=tz)
        if span == "this_week":
            return _day_span(monday, monday + timedelta(days=6), tz=tz)
        if span == "last_week":
            return _day_span(monday - timedelta(days=7), monday - timedelta(days=1), tz=tz)
        if span == "this_month":
            return _month_span(today.year, today.month, tz=tz)
        if span == "last_month":
            y, mth = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
            return _month_span(y, mth, tz=tz)
        if span == "this_year":
            return _day_span(date(today.year, 1, 1), date(today.year, 12, 31), tz=tz)
        if span == "last_year":
            return _day_span(date(today.year - 1, 1, 1), date(today.year - 1, 12, 31), tz=tz)

    m = _TR_LAST_RE.match(text) or _EN_LAST_RE.match(text)
    if m:  # "son 7 gün" / "last 7 days" → a sliding window (up to now)
        n, unit = int(m.group(1)), _WINDOW_UNITS[m.group(2)]
        delta = {
            "hours": timedelta(hours=n),
            "days": timedelta(days=n),
            "weeks": timedelta(weeks=n),
            "months": timedelta(days=30 * n),  # not a calendar month, a 30-day approximation
        }[unit]
        return _iso_utc(now_utc - delta), _iso_utc(now_utc)
    m = _TR_AGO_RE.match(text) or _EN_AGO_RE.match(text)
    if m:  # "3 gün önce" / "3 days ago" → the whole of that day
        return _day_span(today - timedelta(days=int(m.group(1))), tz=tz)
    m = _TR_MONTH_RE.match(text)
    months = _TR_MONTHS
    if m is None:
        m = _EN_MONTH_RE.match(text)
        months = _EN_MONTHS
    if m:
        month = months[m.group(2)]
        year_s = m.group(1) or m.group(3)
        year = int(year_s) if year_s else today.year
        if not year_s and date(year, month, 1) > today:
            year -= 1  # "aralık" / "December" asked in June → last December
        return _month_span(year, month, tz=tz)
    return None
