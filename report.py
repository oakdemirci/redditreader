import sqlite3
import sys
from datetime import date
from pathlib import Path

DB_PATH = Path(__file__).parent / "wsb_comments.db"

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    today_str = date.today().isoformat()

    thread = conn.execute(
        "SELECT thread_id, title FROM daily_threads WHERE date = ?", (today_str,)
    ).fetchone()
    if not thread:
        print("No thread recorded for today yet.")
        return

    thread_id, title = thread
    print(f"{title}\n")

    rows = conn.execute(
        """
        SELECT
            m.symbol,
            GROUP_CONCAT(DISTINCT m.confidence) AS confidences,
            COUNT(DISTINCT m.comment_id) AS mentions
        FROM mentions m
        JOIN comments c ON c.id = m.comment_id
        WHERE c.thread_id = ? AND m.symbol != '__none__'
        GROUP BY m.symbol
        ORDER BY mentions DESC, m.symbol ASC
        """,
        (thread_id,),
    ).fetchall()

    if not rows:
        print("No ticker/coin mentions found yet.")
        return

    print(f"{'Symbol':<8}{'Mentions':<10}{'Confidence'}")
    for symbol, confidence, mentions in rows:
        print(f"{symbol:<8}{mentions:<10}{confidence}")

    conn.close()


if __name__ == "__main__":
    main()
