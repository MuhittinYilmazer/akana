"""Turkish natural-language time-expression parser (deterministic, no LLM).

Split out of ``tools.py`` — this module owns the Turkish "when" vocabulary
("bugün"/today, "dün"/yesterday, "geçen hafta"/last week, "mart ayında"/in
March, "son 7 gün"/last 7 days, "3 gün önce"/3 days ago …) and turns it into
an inclusive ``(from, to)`` ISO-UTC pair. Day/week/month boundaries are
computed in Turkey local time (fixed +03:00 since 2016, no DST) and converted
to the store format (ISO-UTC, millisecond-Z).

``tools.py`` re-exports :func:`parse_time_range` (and consumes
:data:`DATE_ONLY_RE`/:data:`TimeEdge` for :func:`~akana.memory.tools.
parse_time_bound`) so existing callers are unaffected by the split.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from re import compile as _compile
from typing import Literal

from akana.memory.terms import fold_text

__all__ = ["TimeEdge", "DATE_ONLY_RE", "parse_time_range"]

TimeEdge = Literal["start", "end"]

# Turkish natural-language time expressions (deterministic, no LLM). Day/week/month
# boundaries are computed in Turkey local time (fixed +03:00 since 2016, no DST)
# and converted to the store format (ISO-UTC, millisecond-Z).
_TR_TZ = timezone(timedelta(hours=3))

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
DATE_ONLY_RE = _compile(r"^\d{4}-\d{2}-\d{2}$")

_RELATIVE_RE = _compile(r"^relative:(\d+)([hdw])$", flags=0)


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _day_span(first: date, last: date | None = None) -> tuple[str, str]:
    """TR-local days ``[first, last]`` → an inclusive (start, end) ISO-UTC pair."""
    start = datetime(first.year, first.month, first.day, tzinfo=_TR_TZ)
    end_excl = datetime.combine((last or first) + timedelta(days=1), datetime.min.time(), tzinfo=_TR_TZ)
    return _iso_utc(start), _iso_utc(end_excl - timedelta(milliseconds=1))


def _month_span(year: int, month: int) -> tuple[str, str]:
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    last = date(next_y, next_m, 1) - timedelta(days=1)
    return _day_span(date(year, month, 1), last)


def parse_time_range(value: str | None, *, now: datetime | None = None) -> tuple[str, str] | None:
    """Turkish natural-language time expression → an inclusive ``(from, to)`` ISO-UTC pair.

    Deterministic and LLM-free: "bugün" (today), "dün" (yesterday), "bu/geçen hafta"
    (this/last week), "bu/geçen ay" (this/last month), "bu/geçen yıl" (this/last year),
    "son <n> saat/gün/hafta/ay" (last N hours/days/weeks/months), "<n> gün önce"
    (N days ago), and month names ("mart", "martta", "mart ayında", "mart 2025").
    Accent-free spelling is also recognized ("gecen hafta"). ``relative:<n><h|d|w>`` →
    (that point, now). A month name without a year uses the current year; if the month
    has not started yet, the previous year ("aralık" asked in June → last December).
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
    today = now_utc.astimezone(_TR_TZ).date()
    text = " ".join(fold_text(value).translate(_TR_ASCII).translate(_APOSTROPHES).split())

    if text == "bugun":
        return _day_span(today)
    if text == "dun":
        return _day_span(today - timedelta(days=1))
    monday = today - timedelta(days=today.weekday())
    if text == "bu hafta":
        return _day_span(monday, monday + timedelta(days=6))
    if text == "gecen hafta":
        return _day_span(monday - timedelta(days=7), monday - timedelta(days=1))
    if text == "bu ay":
        return _month_span(today.year, today.month)
    if text == "gecen ay":
        y, mth = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        return _month_span(y, mth)
    if text == "bu yil":
        return _day_span(date(today.year, 1, 1), date(today.year, 12, 31))
    if text == "gecen yil":
        return _day_span(date(today.year - 1, 1, 1), date(today.year - 1, 12, 31))

    m = _TR_LAST_RE.match(text)
    if m:  # "son 7 gün" ("last 7 days") → a sliding window (up to now)
        n, unit = int(m.group(1)), m.group(2)
        delta = {
            "saat": timedelta(hours=n),
            "gun": timedelta(days=n),
            "hafta": timedelta(weeks=n),
            "ay": timedelta(days=30 * n),  # not a calendar month, a 30-day approximation
        }[unit]
        return _iso_utc(now_utc - delta), _iso_utc(now_utc)
    m = _TR_AGO_RE.match(text)
    if m:  # "3 gün önce" ("3 days ago") → the whole of that day
        return _day_span(today - timedelta(days=int(m.group(1))))
    m = _TR_MONTH_RE.match(text)
    if m:
        month = _TR_MONTHS[m.group(2)]
        year_s = m.group(1) or m.group(3)
        year = int(year_s) if year_s else today.year
        if not year_s and date(year, month, 1) > today:
            year -= 1  # "aralık" in June → last December
        return _month_span(year, month)
    return None
