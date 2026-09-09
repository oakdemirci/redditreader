#!/usr/bin/env python3
"""Day-over-day ticker/coin trends from the archived WSB threads.

Thin CLI over ``hermes_api.trend`` reading the Arctic Shift store (``hermes.db``).
For each symbol in the window it shows total mentions, how many were $-cashtags
(reliable) vs barewords (noisy), the day it first appeared, a per-day sparkline,
mentions in the last few hours, and flags:

  NEW   first seen within the last 2 days
  HOT   today's share-of-voice is >= 2x the prior days' average (momentum)
  $     at least one $-cashtag mention (higher confidence it's really a ticker)

Symbols with no mention in the last ``--stale-days`` days are hidden; pass
``--stale-days 0`` to see everything in the window.

    python trend.py
    python trend.py --days 14 --min 3
    python trend.py --top 15 --cashtags-only
    python trend.py --include-flair            # also count gain/loss/discussion posts
    python trend.py --markdown                 # chat-friendly table
"""

from __future__ import annotations

import argparse
import sys

import hermes_api
import store

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--days", type=int, default=5, help="window size in days (default 5)")
    p.add_argument("--min", type=int, default=2, dest="min_total",
                   help="hide symbols with fewer than this many mentions (default 2)")
    p.add_argument("--recent-hours", type=float, default=4.0,
                   help="look-back for the intraday velocity column (default 4)")
    p.add_argument("--stale-days", type=int, default=2,
                   help="hide symbols with no mention in this many recent days "
                        "(default 2; 0 disables)")
    p.add_argument("--top", type=int, default=None, metavar="N",
                   help="only print the top N rows (ranked by relevance)")
    p.add_argument("--cashtags-only", action="store_true",
                   help="drop bareword-only symbols (keep those with a real $TICKER)")
    p.add_argument("--include-flair", nargs="?", const="gain,loss,discussion",
                   default=None, metavar="KINDS",
                   help="also count flaired standalone posts (default: gain,loss,discussion)")
    p.add_argument("--db", default=str(store.DEFAULT_DB))
    p.add_argument("--markdown", action="store_true", help="render a markdown table")
    args = p.parse_args()

    kinds = list(hermes_api.MEGA_KINDS)
    if args.include_flair is not None:
        kinds += hermes_api.expand_kinds(args.include_flair.split(","))

    conn = store.connect(args.db)
    report = hermes_api.trend(
        conn, days=args.days, min_total=args.min_total, recent_hours=args.recent_hours,
        stale_days=args.stale_days, top_n=args.top, cashtags_only=args.cashtags_only,
        kinds=kinds,
    )
    if not report.shown_days:
        print("No thread data in this window yet.")
        return
    print(report.to_markdown() if args.markdown else report.to_text())


if __name__ == "__main__":
    main()
