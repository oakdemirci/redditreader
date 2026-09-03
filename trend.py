"""Day-over-day ticker/coin trends from the archived WSB Daily Discussion comments.

For each symbol in the window this shows: total mentions, how many were $-cashtags
(reliable) vs barewords (noisy), the day it first appeared, a per-day mention
sparkline, mentions in the last few hours, and flags:

  NEW   first seen within the last 2 days
  HOT   today's share-of-voice is >= 2x the prior days' average (momentum)
  $     at least one $-cashtag mention (higher confidence it's really a ticker)

Backfilled days hold only a partial sample (~370 comments) while live days
accumulate all day, so raw counts aren't comparable across them. The HOT flag
compares *share of voice* (mentions per comment) instead, which is.
"""

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import clock

DB_PATH = Path(__file__).parent / "wsb_comments.db"
# A full live trading day runs into the thousands of comments; anything well
# below that is a partial backfill sample and gets marked as such.
PARTIAL_COMMENT_THRESHOLD = 800

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def window_days(days: int) -> list[str]:
    today = clock.market_today()
    return [(today - timedelta(days=n)).isoformat() for n in range(days - 1, -1, -1)]


def load_comment_counts(conn: sqlite3.Connection, start: str) -> dict[str, int]:
    return dict(
        conn.execute(
            """
            SELECT d.date, COUNT(c.id)
            FROM daily_threads d
            LEFT JOIN comments c ON c.thread_id = d.thread_id
            WHERE d.date >= ?
            GROUP BY d.date
            """,
            (start,),
        ).fetchall()
    )


def load_mentions(
    conn: sqlite3.Connection, start: str
) -> tuple[dict[str, dict[str, int]], dict[str, int]]:
    """Returns (mentions_by_symbol_and_day, cashtag_count_by_symbol)."""
    rows = conn.execute(
        """
        SELECT d.date, m.symbol,
               COUNT(DISTINCT m.comment_id) AS mentions,
               SUM(CASE WHEN m.confidence = 'cashtag' THEN 1 ELSE 0 END) AS cashtags
        FROM mentions m
        JOIN comments c ON c.id = m.comment_id
        JOIN daily_threads d ON d.thread_id = c.thread_id
        WHERE d.date >= ? AND m.symbol != '__none__'
        GROUP BY d.date, m.symbol
        """,
        (start,),
    ).fetchall()

    by_symbol: dict[str, dict[str, int]] = defaultdict(dict)
    cashtags: dict[str, int] = defaultdict(int)
    for day, symbol, mentions, cashtag_count in rows:
        by_symbol[symbol][day] = mentions
        cashtags[symbol] += cashtag_count or 0
    return by_symbol, cashtags


def load_recent(conn: sqlite3.Connection, hours: float) -> dict[str, int]:
    return dict(
        conn.execute(
            """
            SELECT m.symbol, COUNT(DISTINCT m.comment_id)
            FROM mentions m
            JOIN comments c ON c.id = m.comment_id
            JOIN daily_threads d ON d.thread_id = c.thread_id
            WHERE d.date = ? AND c.posted_at >= ? AND m.symbol != '__none__'
            GROUP BY m.symbol
            """,
            (clock.market_today().isoformat(), clock.utc_hours_ago(hours)),
        ).fetchall()
    )


def share_of_voice(mentions: int, comments: int) -> float:
    return mentions / comments if comments else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=7, help="Window size in days (default 7).")
    parser.add_argument(
        "--min", type=int, default=2, dest="min_total",
        help="Hide symbols with fewer than this many mentions in the window (default 2).",
    )
    parser.add_argument(
        "--recent-hours", type=float, default=4.0,
        help="Look-back window for the intraday velocity column (default 4).",
    )
    args = parser.parse_args()

    days = window_days(args.days)
    today_str = days[-1]

    conn = sqlite3.connect(DB_PATH)
    comment_counts = load_comment_counts(conn, days[0])
    mentions_by_symbol, cashtag_counts = load_mentions(conn, days[0])
    recent = load_recent(conn, args.recent_hours)
    conn.close()

    now_et = clock.market_now().strftime("%Y-%m-%d %H:%M ET")
    print(f"WSB ticker trends — {args.days}-day window, as of {now_et}\n")

    print("Coverage (comments archived per thread):")
    for day in days:
        if day not in comment_counts:
            print(f"  {day}   (no thread stored)")
            continue
        count = comment_counts[day]
        if day == today_str:
            tag = "live (in progress)"
        elif count < PARTIAL_COMMENT_THRESHOLD:
            tag = "partial backfill"
        else:
            tag = "full"
        print(f"  {day}   {count:>5}   {tag}")
    print()

    scored = []
    for symbol, per_day in mentions_by_symbol.items():
        total = sum(per_day.values())
        if total < args.min_total:
            continue

        first_seen = min(per_day)
        today_n = per_day.get(today_str, 0)
        recent_n = recent.get(symbol, 0)
        cashtags = cashtag_counts.get(symbol, 0)

        prior_sov = [
            share_of_voice(n, comment_counts.get(d, 0))
            for d, n in per_day.items()
            if d < today_str
        ]
        prior_sov_avg = sum(prior_sov) / len(prior_sov) if prior_sov else 0.0
        today_sov = share_of_voice(today_n, comment_counts.get(today_str, 0))

        flags = []
        if (clock.market_today() - date.fromisoformat(first_seen)).days <= 2:
            flags.append("NEW")
        if prior_sov_avg > 0 and today_n >= 2 and today_sov >= 2 * prior_sov_avg:
            flags.append("HOT")
        if cashtags > 0:
            flags.append("$")

        score = (
            recent_n * 4
            + today_n * 2
            + min(cashtags, 5)
            + (3 if "HOT" in flags else 0)
            + (2 if "NEW" in flags else 0)
        )
        scored.append((score, symbol, total, cashtags, first_seen, per_day, today_n, recent_n, flags))

    scored.sort(key=lambda r: (-r[0], -r[2], r[1]))

    day_labels = "".join(f"{d[5:]:>6}" for d in days)  # MM-DD
    header = f"{'SYMBOL':<8}{'TOTAL':>6}{'$':>4}  {'FIRST':<11}{day_labels}{'RECENT':>8}   FLAGS"
    print(header)
    print("-" * len(header))
    if not scored:
        print("(nothing above the --min threshold yet)")
        return

    for _, symbol, total, cashtags, first_seen, per_day, _today_n, recent_n, flags in scored:
        spark = "".join(f"{per_day.get(d, 0) or '·':>6}" for d in days)
        print(
            f"{symbol:<8}{total:>6}{cashtags:>4}  {first_seen:<11}{spark}"
            f"{recent_n:>8}   {' '.join(flags)}"
        )

    print(
        "\nNotes: 'partial backfill' days hold ~370 comments vs thousands on a full day —\n"
        "raw counts aren't comparable, so HOT uses share-of-voice (mentions per comment).\n"
        "No '$' flag = bareword-only match; common words (TIME, BE, OR, UP...) collide with\n"
        "real tickers, so treat those as low confidence."
    )


if __name__ == "__main__":
    main()
