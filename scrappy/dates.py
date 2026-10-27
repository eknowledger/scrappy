"""Convert between what the user edits ('YYYY-MM-DD') and what gamelist.xml holds ('YYYYMMDDT000000').

``to_gamelist`` accepts the numeric shapes a person types (2026-02-18, 2026/2/8, 2026.02, 2026, 20260218, ISO date-times)
and the shapes the Steam store page returns ('Feb 18, 2026', '18 Feb, 2026', 'Feb 2026'). A missing month or day
means January / the 1st. Anything else is refused with '' (never guessed): 'Coming soon', 'Q3 2026', impossible
days such as 2026-02-30, years outside 1970..2100, and day-first dates such as 18.02.2026 whose order is ambiguous.
An empty result means "no date": the saver skips empty fields, so it never blanks an existing date.
"""
from __future__ import annotations

import re
from datetime import datetime

MIN_YEAR, MAX_YEAR = 1970, 2100

# [0-9] and re.ASCII on purpose: \d would also accept other scripts' digits
_NUMERIC = re.compile(
    r"([0-9]{4})(?:[-/.]([0-9]{1,2})(?:[-/.]([0-9]{1,2}))?)?"
    r"(?:[T ][0-9]{1,2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?(?:Z|[+-][0-9]{2}:?[0-9]{2})?)?", re.ASCII)
_COMPACT = re.compile(r"([0-9]{4})([0-9]{2})([0-9]{2})(?:T[0-9]{6})?", re.ASCII)
_GAMELIST = re.compile(r"([0-9]{4})([0-9]{2})([0-9]{2})(?:T[0-9]{2}[0-9]{2}[0-9]{2}.*)?", re.ASCII | re.DOTALL)


def _valid(year, month, day) -> str:
    """'YYYYMMDD' for a real calendar day inside the supported years, else ''."""
    try:
        when = datetime(int(year), int(month), int(day))
    except ValueError:
        return ""
    return when.strftime("%Y%m%d") if MIN_YEAR <= when.year <= MAX_YEAR else ""


def to_gamelist(text) -> str:
    """'2026-02-18' / 'Feb 18, 2026' -> '20260218T000000'; '' when it is not a real date."""
    if not isinstance(text, str):
        return ""
    text = " ".join(text.split())
    if not text:
        return ""
    if m := _COMPACT.fullmatch(text):
        day = _valid(*m.groups())
    elif m := _NUMERIC.fullmatch(text):
        year, month, day_of_month = m.groups()
        day = _valid(year, month or 1, day_of_month or 1)
    else:
        from .providers.steam import parse_release_date          # word months; imported late to keep this module light
        digits = parse_release_date(text)
        return digits if digits and _valid(digits[:4], digits[4:6], digits[6:8]) else ""
    return day + "T000000" if day else ""


def from_gamelist(value) -> str:
    """'20260218T000000' -> '2026-02-18'; '' when empty or not a valid date."""
    if not isinstance(value, str):
        return ""
    m = _GAMELIST.fullmatch(value.strip())
    day = _valid(*m.groups()) if m else ""
    return f"{day[:4]}-{day[4:6]}-{day[6:]}" if day else ""
