import argparse
import html
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

import tickers

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 1. Configuration
TARGET_SUBREDDIT = "wallstreetbets"
USER_AGENT = "windows:com.oakdemirci.redditdig:v1.0 (by u/WhiteBlackSmith_2021)"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
FETCH_LIMIT = 100  # max comments Reddit's RSS returns per request
DB_PATH = Path(__file__).parent / "wsb_comments.db"

DAILY_TITLE_PREFIX = "Daily Discussion Thread for"
DAILY_DATE_RE = re.compile(
    r"Daily Discussion Thread for\s+([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})"
)
DEFAULT_BACKFILL_DAYS = 7
# A finished thread never gets new comments, so one pass of `sort=new` only sees
# its last ~100. Pulling several sort orders widens the (still partial) snapshot:
# `new`/`old` grab the tail/head, `top`/`controversial` grab the most-voted.
BACKFILL_SORTS = ("new", "old", "top", "controversial")

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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mentions (
            comment_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            confidence TEXT NOT NULL,
            PRIMARY KEY (comment_id, symbol),
            FOREIGN KEY (comment_id) REFERENCES comments(id)
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


def fetch_comments(
    conn: sqlite3.Connection, subreddit: str, thread_id: str, sort: str = "new"
) -> int:
    """Fetch one page of comments in the given sort order, inserting any not
    already stored. Returns the count of new rows."""
    url = (
        f"https://www.reddit.com/r/{subreddit}/comments/{thread_id}/.rss"
        f"?limit={FETCH_LIMIT}&sort={sort}"
    )
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


def parse_daily_thread_date(title: str) -> date | None:
    """Pull the calendar date out of a 'Daily Discussion Thread for September 3, 2026' title."""
    match = DAILY_DATE_RE.search(title)
    if not match:
        return None

    month_name, day, year = match.groups()
    try:
        return datetime.strptime(f"{month_name} {int(day)} {year}", "%B %d %Y").date()
    except ValueError:
        return None


def find_recent_daily_threads(subreddit: str, days_back: int) -> list[tuple[str, str, str]]:
    """Search the subreddit's public RSS for recent Daily Discussion threads.

    Returns (date_iso, thread_id, title) tuples, newest first, restricted to
    threads dated within `days_back` days of today.
    """
    url = (
        f"https://www.reddit.com/r/{subreddit}/search.rss"
        f"?q=%22Daily+Discussion+Thread%22&restrict_sr=1&sort=new&limit=100"
    )
    root = parse_atom(get_with_retry(url))

    cutoff = date.today() - timedelta(days=days_back)
    results: list[tuple[str, str, str]] = []
    for entry in root.findall("atom:entry", ATOM_NS):
        title = entry.findtext("atom:title", default="", namespaces=ATOM_NS)
        entry_id = entry.findtext("atom:id", default="", namespaces=ATOM_NS)
        if not entry_id.startswith("t3_") or not title.startswith(DAILY_TITLE_PREFIX):
            continue

        thread_date = parse_daily_thread_date(title)
        if thread_date is None or not (cutoff <= thread_date <= date.today()):
            continue

        results.append((thread_date.isoformat(), entry_id.removeprefix("t3_"), title))

    return results


def extract_pending_mentions(conn: sqlite3.Connection, known_stock_symbols: set[str]) -> int:
    """Extract ticker/coin mentions for any stored comment not yet processed."""
    rows = conn.execute(
        """
        SELECT id, body FROM comments
        WHERE id NOT IN (SELECT DISTINCT comment_id FROM mentions)
        """
    ).fetchall()

    for comment_id, body in rows:
        found = tickers.extract_symbols(body, known_stock_symbols)
        if not found:
            # mark as processed with no mentions found, so it isn't rescanned every run
            found = [("__none__", "n/a")]

        for symbol, confidence in found:
            conn.execute(
                "INSERT OR IGNORE INTO mentions (comment_id, symbol, confidence) VALUES (?, ?, ?)",
                (comment_id, symbol, confidence),
            )

    conn.commit()
    return len(rows)


def run_live(conn: sqlite3.Connection) -> None:
    """One incremental pass over today's Daily Discussion thread (the scheduled path)."""
    today_str = date.today().isoformat()

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

    new_count = fetch_comments(conn, TARGET_SUBREDDIT, thread_id, sort="new")
    total_count = conn.execute(
        "SELECT COUNT(*) FROM comments WHERE thread_id = ?", (thread_id,)
    ).fetchone()[0]
    print(f"Added {new_count} new comment(s). Total stored for today: {total_count}.")


def run_backfill(conn: sqlite3.Connection, days_back: int) -> None:
    """Grab whatever's still reachable from the last `days_back` days of threads.

    Coverage is necessarily partial: for a thread that's no longer taking
    comments, RSS only exposes ~100 per sort order, so busy days lose the middle.
    """
    threads = find_recent_daily_threads(TARGET_SUBREDDIT, days_back)
    if not threads:
        print("No recent Daily Discussion threads found via search.")
        return

    print(f"Found {len(threads)} Daily Discussion thread(s) within {days_back} days.\n")
    for date_iso, thread_id, title in threads:
        conn.execute(
            "INSERT OR REPLACE INTO daily_threads (date, thread_id, title) VALUES (?, ?, ?)",
            (date_iso, thread_id, title),
        )
        conn.commit()

        new_count = 0
        for sort in BACKFILL_SORTS:
            # Anonymous RSS tolerates ~1 request per 5s; exceed it and Reddit
            # stretches the cooldown to ~50s. Pacing here keeps the whole run
            # to a few minutes. get_with_retry still covers any 429 that slips.
            time.sleep(7)
            new_count += fetch_comments(conn, TARGET_SUBREDDIT, thread_id, sort=sort)

        stored = conn.execute(
            "SELECT COUNT(*) FROM comments WHERE thread_id = ?", (thread_id,)
        ).fetchone()[0]
        print(f"  {date_iso}  +{new_count:>4} new, {stored:>4} stored  {title}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Incrementally archive r/wallstreetbets' Daily Discussion comments."
    )
    parser.add_argument(
        "--backfill",
        nargs="?",
        type=int,
        const=DEFAULT_BACKFILL_DAYS,
        default=None,
        metavar="DAYS",
        help=(
            f"Instead of the live run, backfill recent Daily Discussion threads "
            f"(default {DEFAULT_BACKFILL_DAYS} days). Coverage is partial — RSS "
            "returns at most ~100 comments per sort order for a finished thread."
        ),
    )
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    if args.backfill is not None:
        run_backfill(conn, args.backfill)
    else:
        run_live(conn)

    known_stock_symbols = tickers.load_known_stock_symbols()
    processed = extract_pending_mentions(conn, known_stock_symbols)
    if processed:
        print(f"Scanned {processed} new comment(s) for ticker/coin mentions.")

    conn.close()


if __name__ == "__main__":
    main()
