import argparse
import sqlite3
import sys
from pathlib import Path

import clock

DB_PATH = Path(__file__).parent / "wsb_comments.db"

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Most-mentioned tickers/coins for a stored day's WSB megathreads."
    )
    parser.add_argument(
        "--date",
        default=clock.market_today().isoformat(),
        metavar="YYYY-MM-DD",
        help="Which day to report on (default: today, US Eastern).",
    )
    parser.add_argument(
        "--by-thread",
        action="store_true",
        help="Break the counts out by source thread (daily / moves) instead of merging.",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)

    threads = conn.execute(
        "SELECT kind, thread_id, title FROM daily_threads WHERE date = ? ORDER BY kind",
        (args.date,),
    ).fetchall()
    if not threads:
        recent_dates = [
            row[0] for row in conn.execute(
                "SELECT DISTINCT date FROM daily_threads ORDER BY date DESC LIMIT 5"
            )
        ]
        print(f"No thread recorded for {args.date}.")
        if recent_dates:
            print(f"Available: {', '.join(recent_dates)}  (use --date)")
        return

    for kind, _thread_id, title in threads:
        print(f"[{kind}] {title}")
    print()

    group_cols = "m.symbol, dt.kind" if args.by_thread else "m.symbol"
    rows = conn.execute(
        f"""
        SELECT
            m.symbol,
            {"dt.kind," if args.by_thread else ""}
            GROUP_CONCAT(DISTINCT m.confidence) AS confidences,
            COUNT(DISTINCT m.comment_id) AS mentions
        FROM mentions m
        JOIN comments c ON c.id = m.comment_id
        JOIN daily_threads dt ON dt.thread_id = c.thread_id
        WHERE dt.date = ? AND m.symbol != '__none__'
        GROUP BY {group_cols}
        ORDER BY mentions DESC, m.symbol ASC
        """,
        (args.date,),
    ).fetchall()

    if not rows:
        print("No ticker/coin mentions found yet.")
        return

    if args.by_thread:
        print(f"{'Symbol':<8}{'Src':<8}{'Mentions':<10}{'Confidence'}")
        for symbol, kind, confidence, mentions in rows:
            print(f"{symbol:<8}{kind:<8}{mentions:<10}{confidence}")
    else:
        print(f"{'Symbol':<8}{'Mentions':<10}{'Confidence'}")
        for symbol, confidence, mentions in rows:
            print(f"{symbol:<8}{mentions:<10}{confidence}")

    conn.close()


if __name__ == "__main__":
    main()
