import argparse
import sqlite3
import sys
from pathlib import Path

import clock

DB_PATH = Path(__file__).parent / "wsb_comments.db"

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def show_symbol_comments(conn: sqlite3.Connection, symbol: str) -> None:
    """Print every stored comment that triggered a mention of `symbol`, across
    all archived days, so a summary-table entry can be manually sanity-checked."""
    rows = conn.execute(
        """
        SELECT dt.date, dt.kind, m.confidence, c.author, c.posted_at, c.body
        FROM mentions m
        JOIN comments c ON c.id = m.comment_id
        JOIN daily_threads dt ON dt.thread_id = c.thread_id
        WHERE m.symbol = ?
        ORDER BY c.posted_at
        """,
        (symbol,),
    ).fetchall()

    if not rows:
        print(f"No stored comments mention {symbol}.")
        return

    print(f"{len(rows)} comment(s) mentioning {symbol}:\n")
    for date_str, kind, confidence, author, posted_at, body in rows:
        print(f"[{date_str} {kind}] [{confidence}] {posted_at}  {author}")
        print(f"  {body}\n")


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
    parser.add_argument(
        "--symbol",
        metavar="SYM",
        help="Show the actual stored comments behind this symbol's mentions "
        "(across all archived days, ignoring --date) instead of the summary table. "
        "Use this to sanity-check anything the summary flags as suspicious.",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)

    if args.symbol:
        show_symbol_comments(conn, args.symbol.upper())
        conn.close()
        return

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
