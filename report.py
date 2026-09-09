#!/usr/bin/env python3
"""Most-mentioned tickers/coins for a stored day's WSB threads.

Thin CLI over ``hermes_api`` reading the Arctic Shift store (``hermes.db``).

    python report.py                       # today (US Eastern)
    python report.py --date 2026-09-02
    python report.py --by-thread            # split counts by source thread
    python report.py --symbol AAPL          # the actual comments behind a row
    python report.py --include-flair        # also count gain/loss/discussion posts
"""

from __future__ import annotations

import argparse
import sys

import clock
import hermes_api
import store

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--date", default=clock.market_today().isoformat(), metavar="YYYY-MM-DD",
                   help="which trading day to report on (default: today, US Eastern)")
    p.add_argument("--by-thread", action="store_true",
                   help="break counts out by source thread instead of merging")
    p.add_argument("--symbol", metavar="SYM",
                   help="show the stored comments behind this symbol (all days) "
                        "instead of the summary table")
    p.add_argument("--days", type=int, default=None, metavar="N",
                   help="with --symbol, limit to the last N trading days")
    p.add_argument("--include-flair", nargs="?", const="gain,loss,discussion",
                   default=None, metavar="KINDS",
                   help="also count flaired standalone posts (default: gain,loss,discussion)")
    p.add_argument("--db", default=str(store.DEFAULT_DB))
    p.add_argument("--entity-mode", choices=hermes_api.ENTITY_MODES, default="regex")
    p.add_argument("--llm", action="store_true", help="shorthand for --entity-mode best")
    args = p.parse_args()

    mode = "best" if args.llm else args.entity_mode
    conn = store.connect(args.db)

    if args.symbol:
        print(hermes_api.symbol_detail(conn, args.symbol, days=args.days,
                                       entity_mode=mode).to_text())
        return

    kinds = list(hermes_api.MEGA_KINDS)
    if args.include_flair is not None:
        kinds += hermes_api.expand_kinds(args.include_flair.split(","))

    report = hermes_api.day_report(conn, args.date, kinds=kinds,
                                   by_thread=args.by_thread, entity_mode=mode)
    if not report.threads:
        recent = [r["trading_day"] for r in conn.execute(
            "SELECT DISTINCT trading_day FROM threads ORDER BY trading_day DESC LIMIT 5"
        )]
        print(f"No thread recorded for {args.date}.")
        if recent:
            print(f"Available: {', '.join(recent)}  (use --date)")
        return
    print(report.to_text())


if __name__ == "__main__":
    main()
