"""Anchor "today" to US market time.

WSB names each thread "Daily Discussion Thread for <US calendar date>", so the
scraper and the reports must agree on which calendar day that is regardless of
the machine's own timezone (the Hetzner box runs UTC; a laptop runs local time).
Everything that means "which trading day" goes through here.
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")


def market_now() -> datetime:
    """Current time in US Eastern (what WSB's thread titles are keyed to)."""
    return datetime.now(MARKET_TZ)


def market_today() -> date:
    """Today's US Eastern calendar date."""
    return market_now().date()


def utc_hours_ago(hours: float) -> str:
    """ISO-8601 UTC timestamp `hours` in the past, for comparing against stored
    `posted_at` values (which Reddit hands us in UTC)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return cutoff.replace(microsecond=0).isoformat()
