import html
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 1. Configuration
TARGET_SUBREDDIT = "wallstreetbets"
USER_AGENT = "windows:com.oakdemirci.redditdig:v1.0 (by u/WhiteBlackSmith_2021)"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
FETCH_LIMIT = 100  # max comments Reddit's RSS returns per request
DB_PATH = Path(__file__).parent / "wsb_comments.db"

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})


def get_with_retry(url: str, max_retries: int = 3) -> requests.Response:
    """Reddit's anonymous RSS endpoints allow roughly one request per minute per IP."""
    for attempt in range(max_retries):
        response = session.get(url)
        if response.status_code != 429:
            response.raise_for_status()
            return response

        wait_seconds = int(float(response.headers.get("x-ratelimit-reset", 60))) + 1
        print(f"Rate limited, waiting {wait_seconds}s before retrying...")
        time.sleep(wait_seconds)

    response.raise_for_status()
    return response


def parse_atom(response: requests.Response) -> ET.Element:
    response.encoding = "utf-8"  # Reddit's Atom feeds are UTF-8; requests sometimes mis-detects this
    return ET.fromstring(response.text)


def strip_html(raw_html: str) -> str:
    text = re.sub(r"<!--.*?-->", "", raw_html, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_threads (
            date TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            title TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS comments (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            author TEXT NOT NULL,
            body TEXT NOT NULL,
            posted_at TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def get_cached_thread_id(conn: sqlite3.Connection, today_str: str) -> str | None:
    row = conn.execute("SELECT thread_id FROM daily_threads WHERE date = ?", (today_str,)).fetchone()
    return row[0] if row else None


def find_daily_discussion_id(subreddit: str) -> tuple[str, str] | None:
    """Search the subreddit's public RSS feed for today's Daily Discussion thread."""
    today = date.today()
    month_name = today.strftime("%B")
    month_day = f"{month_name} {today.day}"
    month_day_padded = f"{month_name} {today.day:02d}"

    response = get_with_retry(f"https://www.reddit.com/r/{subreddit}/.rss")

    root = parse_atom(response)
    for entry in root.findall("atom:entry", ATOM_NS):
        title = entry.findtext("atom:title", default="", namespaces=ATOM_NS)
        entry_id = entry.findtext("atom:id", default="", namespaces=ATOM_NS)

        if (
            entry_id.startswith("t3_")
            and "Daily Discussion" in title
            and (month_day in title or month_day_padded in title)
        ):
            return entry_id.removeprefix("t3_"), title

    return None


def fetch_new_comments(conn: sqlite3.Connection, subreddit: str, thread_id: str) -> int:
    """Fetch the newest comments and insert any not already stored. Returns count of new rows."""
    url = f"https://www.reddit.com/r/{subreddit}/comments/{thread_id}/.rss?limit={FETCH_LIMIT}&sort=new"
    response = get_with_retry(url)
    root = parse_atom(response)

    fetched_at = datetime.now(timezone.utc).isoformat()
    new_count = 0

    for entry in root.findall("atom:entry", ATOM_NS):
        entry_id = entry.findtext("atom:id", default="", namespaces=ATOM_NS)
        if not entry_id.startswith("t1_"):
            continue  # skip the submission entry itself, keep only comments

        author = entry.findtext("atom:author/atom:name", default="Deleted", namespaces=ATOM_NS)
        raw_content = entry.findtext("atom:content", default="", namespaces=ATOM_NS)
        body = strip_html(raw_content)
        posted_at = entry.findtext("atom:updated", default="", namespaces=ATOM_NS)

        cursor = conn.execute(
            "INSERT OR IGNORE INTO comments (id, thread_id, author, body, posted_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (entry_id, thread_id, author, body, posted_at, fetched_at),
        )
        if cursor.rowcount:
            new_count += 1

    conn.commit()
    return new_count


def main() -> None:
    today_str = date.today().isoformat()
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    thread_id = get_cached_thread_id(conn, today_str)
    if thread_id is None:
        result = find_daily_discussion_id(TARGET_SUBREDDIT)
        if not result:
            print("Could not locate today's Daily Discussion thread.")
            return

        thread_id, title = result
        conn.execute(
            "INSERT OR REPLACE INTO daily_threads (date, thread_id, title) VALUES (?, ?, ?)",
            (today_str, thread_id, title),
        )
        conn.commit()
        print(f"Located Thread: {title}\n")
        time.sleep(1)  # be polite between requests

    new_count = fetch_new_comments(conn, TARGET_SUBREDDIT, thread_id)
    total_count = conn.execute(
        "SELECT COUNT(*) FROM comments WHERE thread_id = ?", (thread_id,)
    ).fetchone()[0]

    print(f"Added {new_count} new comment(s). Total stored for today: {total_count}.")
    conn.close()


if __name__ == "__main__":
    main()
