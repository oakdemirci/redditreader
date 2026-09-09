"""WSB's thread calendar: which trading day a thread's comments belong to.

`trend.py` merges, for each trading day, the Daily Discussion thread and that
evening's "What Are Your Moves Tomorrow" thread (whose title names *tomorrow*);
the Weekend thread is filed under its Saturday. The Arctic Shift pipeline stores
comments by `created_utc`, so it needs the same title -> day mapping to line up
with how the reports have always counted.

Ported from `mydigger.py` (kept standalone so the two pipelines stay
independent; the date parsing is locale-independent and rarely changes).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import clock

MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}
TITLE_DATE_RE = re.compile(r"\b([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{4})\b")
WEEKEND_RANGE_RE = re.compile(r"Weekend of\s+([A-Z][a-z]+)\s+(\d{1,2})")


def parse_title_date(title: str) -> date | None:
    """Pull a '<Month> <D>, <YYYY>' date out of a title."""
    match = TITLE_DATE_RE.search(title or "")
    if not match:
        return None
    month = MONTHS.get(match.group(1))
    if month is None:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(2)))
    except ValueError:
        return None


def parse_weekend_saturday(title: str, ref: date) -> date | None:
    """The Saturday of a '...Weekend of <Month> <D1>-<D2>' title. The title has
    no year, so pick the calendar year that lands it within ~20 days of `ref`."""
    match = WEEKEND_RANGE_RE.search(title or "")
    if not match:
        return None
    month = MONTHS.get(match.group(1))
    if month is None:
        return None
    day = int(match.group(2))
    for year in (ref.year, ref.year + 1, ref.year - 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if abs((candidate - ref).days) <= 20:
            return candidate
    return None


def created_date(created_utc: int | float | None) -> date:
    """US/Eastern calendar date of an epoch timestamp (WSB's own frame of
    reference). Falls back to today if the timestamp is missing."""
    if not created_utc:
        return clock.market_today()
    return datetime.fromtimestamp(created_utc, clock.MARKET_TZ).date()


def trading_day(kind: str, title: str, created_utc: int | float | None) -> date:
    """The trading day `kind`/`title`'s comments should be counted under.

    * ``daily``   -> the date in the title
    * ``moves``   -> the date in the title minus one day (title names tomorrow)
    * ``weekend`` -> the Saturday of the weekend range
    * anything else (flaired standalone posts) -> its own US/Eastern creation day
    """
    fallback = created_date(created_utc)
    if kind == "daily":
        return parse_title_date(title) or fallback
    if kind == "moves":
        parsed = parse_title_date(title)
        return (parsed - timedelta(days=1)) if parsed else fallback
    if kind == "weekend":
        return parse_weekend_saturday(title, fallback) or fallback
    return fallback
