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

import clock
import tickers

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 1. Configuration
TARGET_SUBREDDIT = "wallstreetbets"
USER_AGENT = "windows:com.oakdemirci.redditdig:v1.0 (by u/WhiteBlackSmith_2021)"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
FETCH_LIMIT = 100  # max comments Reddit's RSS returns per request
DB_PATH = Path(__file__).parent / "wsb_comments.db"

DAILY_TITLE_PREFIX = "Daily Discussion Thread for"

MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}
TITLE_DATE_RE = re.compile(r"\b([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{4})\b")
WEEKEND_RANGE_RE = re.compile(r"Weekend of\s+([A-Z][a-z]+)\s+(\d{1,2})")

# WSB's weekly rhythm, all times US Eastern:
#   Mon-Fri ~06:00   "Daily Discussion Thread for <D>"            -> the trading day
#   Mon-Thu ~16:00   "What Are Your Moves Tomorrow, <D+1>"        -> evening + overnight
#   Fri     ~16:00   "Weekend Discussion Thread ... Weekend of <Sat>-<Sun>"  -> Fri night..Sun
#   Sun     ~16:00   "What Are Your Moves Tomorrow, <Mon>"        -> Sun evening + overnight
# Every thread is collected and tagged with its `kind`; reports merge by default.
# `resolve(title, today)` maps a title to the trading day the thread belongs to
# (the 'moves' thread is titled for tomorrow; the weekend thread is filed under
# its Saturday, and its title carries no year so `today` disambiguates). A thread
# goes live at `belongs_date - posted_days_before`, `earliest_hour`:00 ET — used
# to avoid front-page requests hunting for a thread that can't exist yet.
KIND_DAILY = "daily"
KIND_MOVES = "moves"
KIND_WEEKEND = "weekend"


def parse_title_date(title: str) -> date | None:
    """Pull a '<Month> <D>, <YYYY>' date out of a title. Locale-independent."""
    match = TITLE_DATE_RE.search(title)
    if not match:
        return None
    month = MONTHS.get(match.group(1))
    if month is None:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(2)))
    except ValueError:
        return None


def parse_weekend_saturday(title: str, today: date) -> date | None:
    """The Saturday of a '...Weekend of <Month> <D1>-<D2>' title. No year in the
    title, so pick the calendar year that lands the date near `today`."""
    match = WEEKEND_RANGE_RE.search(title)
    if not match:
        return None
    month = MONTHS.get(match.group(1))
    if month is None:
        return None
    day = int(match.group(2))
    for year in (today.year, today.year + 1, today.year - 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if abs((candidate - today).days) <= 20:
            return candidate
    return None


def _dated_resolver(day_offset: int):
    def resolve(title: str, _today: date) -> date | None:
        parsed = parse_title_date(title)
        return None if parsed is None else parsed - timedelta(days=day_offset)

    return resolve


THREAD_SPECS: dict[str, dict] = {
    KIND_DAILY: {
        "prefix": DAILY_TITLE_PREFIX, "resolve": _dated_resolver(0),
        "posted_days_before": 0, "earliest_hour": 5,
    },
    KIND_MOVES: {
        "prefix": "What Are Your Moves Tomorrow", "resolve": _dated_resolver(1),
        "posted_days_before": 0, "earliest_hour": 15,
    },
    KIND_WEEKEND: {
        "prefix": "Weekend Discussion Thread", "resolve": parse_weekend_saturday,
        "posted_days_before": 1, "earliest_hour": 15,  # posted Friday afternoon
    },
}


def thread_is_live(kind: str, belongs_iso: str, now: datetime) -> bool:
    """Has WSB plausibly posted this thread yet, given its kind and trading day?"""
    spec = THREAD_SPECS[kind]
    posted_day = date.fromisoformat(belongs_iso) - timedelta(days=spec["posted_days_before"])
    if now.date() > posted_day:
        return True
    if now.date() == posted_day:
        return now.hour >= spec["earliest_hour"]
    return False

DEFAULT_BACKFILL_DAYS = 7
STALE_AFTER_HOURS = 12  # a thread with nothing new in this long is done; stop polling it
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
            date TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'daily',
            thread_id TEXT NOT NULL,
            title TEXT NOT NULL,
            PRIMARY KEY (date, kind)
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
    _migrate_daily_threads(conn)
    conn.commit()


def _migrate_daily_threads(conn: sqlite3.Connection) -> None:
    """v1 keyed `daily_threads` on `date` alone (one thread per day). v2 adds a
    `kind` column and keys on `(date, kind)` so the Daily Discussion and the
    'What Are Your Moves Tomorrow' thread can both be stored for the same day."""
    columns = [row[1] for row in conn.execute("PRAGMA table_info(daily_threads)")]
    if not columns or "kind" in columns:
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("ALTER TABLE daily_threads RENAME TO daily_threads_v1")
        conn.execute(
            """
            CREATE TABLE daily_threads (
                date TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'daily',
                thread_id TEXT NOT NULL,
                title TEXT NOT NULL,
                PRIMARY KEY (date, kind)
            )
            """
        )
        conn.execute(
            "INSERT INTO daily_threads (date, kind, thread_id, title) "
            "SELECT date, 'daily', thread_id, title FROM daily_threads_v1"
        )
        conn.execute("DROP TABLE daily_threads_v1")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    print("Migrated daily_threads to the (date, kind) schema.")


def find_megathreads(subreddit: str) -> list[tuple[str, str, str, str]]:
    """Scan the subreddit's front-page RSS for Daily / Moves / Weekend megathreads.

    Returns (date_iso, kind, thread_id, title) for every match within a few days
    of today, where date_iso is the trading day the thread belongs to.
    """
    today = clock.market_today()
    root = parse_atom(get_with_retry(f"https://www.reddit.com/r/{subreddit}/.rss"))

    found: list[tuple[str, str, str, str]] = []
    for entry in root.findall("atom:entry", ATOM_NS):
        entry_id = entry.findtext("atom:id", default="", namespaces=ATOM_NS)
        title = entry.findtext("atom:title", default="", namespaces=ATOM_NS)
        if not entry_id.startswith("t3_"):
            continue

        for kind, spec in THREAD_SPECS.items():
            if not title.startswith(spec["prefix"]):
                continue
            belongs = spec["resolve"](title, today)
            if belongs is not None and abs((belongs - today).days) <= 3:
                found.append(
                    (belongs.isoformat(), kind, entry_id.removeprefix("t3_"), title)
                )

    return found


def expected_threads(today: date) -> set[tuple[str, str]]:
    """(date_iso, kind) pairs WSB's schedule says should exist by now, so a run
    knows whether it's worth hitting the network to look for anything new."""
    weekday = today.weekday()  # Mon=0 .. Sun=6
    expected: set[tuple[str, str]] = set()
    if weekday <= 4:  # Mon-Fri: Daily Discussion
        expected.add((today.isoformat(), KIND_DAILY))
    if weekday <= 3 or weekday == 6:  # Mon-Thu, and Sun evening (for Monday)
        expected.add((today.isoformat(), KIND_MOVES))
    if weekday >= 4:  # Fri/Sat/Sun: this weekend's thread, filed under its Saturday
        saturday = today + timedelta(days=5 - weekday)  # Fri +1, Sat 0, Sun -1
        expected.add((saturday.isoformat(), KIND_WEEKEND))
    return expected


def fetch_comments(
    conn: sqlite3.Connection,
    subreddit: str,
    thread_id: str,
    sort: str = "new",
    detect_gap: bool = False,
) -> int:
    """Fetch one page of comments in the given sort order, inserting any not
    already stored. Returns the count of new rows.

    With `detect_gap` (the live `sort=new` path), warn if the thread outran the
    feed since last run: RSS only exposes the newest ~100 comments with no way
    to page back, so if every comment on a full page is newer than everything we
    already have, the ones in between were posted and lost. The fix is a shorter
    poll interval.
    """
    prior_latest = None
    if detect_gap:
        prior_latest = conn.execute(
            "SELECT MAX(posted_at) FROM comments WHERE thread_id = ?", (thread_id,)
        ).fetchone()[0]

    url = (
        f"https://www.reddit.com/r/{subreddit}/comments/{thread_id}/.rss"
        f"?limit={FETCH_LIMIT}&sort={sort}"
    )
    response = get_with_retry(url)
    root = parse_atom(response)

    fetched_at = datetime.now(timezone.utc).isoformat()
    new_count = 0
    page_size = 0
    oldest_on_page = None

    for entry in root.findall("atom:entry", ATOM_NS):
        entry_id = entry.findtext("atom:id", default="", namespaces=ATOM_NS)
        if not entry_id.startswith("t1_"):
            continue  # skip the submission entry itself, keep only comments

        author = entry.findtext("atom:author/atom:name", default="Deleted", namespaces=ATOM_NS)
        raw_content = entry.findtext("atom:content", default="", namespaces=ATOM_NS)
        body = strip_html(raw_content)
        posted_at = entry.findtext("atom:updated", default="", namespaces=ATOM_NS)

        page_size += 1
        if posted_at and (oldest_on_page is None or posted_at < oldest_on_page):
            oldest_on_page = posted_at

        cursor = conn.execute(
            "INSERT OR IGNORE INTO comments (id, thread_id, author, body, posted_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (entry_id, thread_id, author, body, posted_at, fetched_at),
        )
        if cursor.rowcount:
            new_count += 1

    conn.commit()

    if (
        detect_gap
        and prior_latest is not None
        and oldest_on_page is not None
        and page_size >= FETCH_LIMIT          # a full page — the feed likely had more
        and oldest_on_page > prior_latest     # ...and none of it overlaps what we stored
    ):
        print(
            f"WARNING: comment gap — oldest on this page ({oldest_on_page}) is newer "
            f"than the last stored ({prior_latest}); comments between them were missed. "
            f"Poll more often."
        )

    return new_count


def find_recent_daily_threads(subreddit: str, days_back: int) -> list[tuple[str, str, str]]:
    """Search the subreddit's public RSS for recent Daily Discussion threads.

    Returns (date_iso, thread_id, title) tuples, newest first, restricted to
    threads dated within `days_back` days of today. Backfill covers the Daily
    Discussion thread only — the 'moves' thread is collected going forward.
    """
    url = (
        f"https://www.reddit.com/r/{subreddit}/search.rss"
        f"?q=%22Daily+Discussion+Thread%22&restrict_sr=1&sort=new&limit=100"
    )
    root = parse_atom(get_with_retry(url))

    today = clock.market_today()
    cutoff = today - timedelta(days=days_back)
    results: list[tuple[str, str, str]] = []
    for entry in root.findall("atom:entry", ATOM_NS):
        title = entry.findtext("atom:title", default="", namespaces=ATOM_NS)
        entry_id = entry.findtext("atom:id", default="", namespaces=ATOM_NS)
        if not entry_id.startswith("t3_") or not title.startswith(DAILY_TITLE_PREFIX):
            continue

        thread_date = parse_title_date(title)
        if thread_date is None or not (cutoff <= thread_date <= today):
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


def discover_threads(conn: sqlite3.Connection) -> None:
    """Cache any megathread we don't have yet. Skips the network entirely unless
    the schedule says something we're missing should exist by now — so no
    front-page request at 3am hunting for a Daily Discussion that isn't posted."""
    now = clock.market_now()
    today = now.date()
    horizon = (today - timedelta(days=3)).isoformat()

    have = {
        (row[0], row[1])
        for row in conn.execute(
            "SELECT date, kind FROM daily_threads WHERE date >= ?", (horizon,)
        )
    }
    due = {
        (thread_date, kind)
        for thread_date, kind in expected_threads(today) - have
        if thread_is_live(kind, thread_date, now)
    }
    if not due:
        return

    try:
        listed = find_megathreads(TARGET_SUBREDDIT)
    except requests.RequestException as exc:
        print(f"Thread discovery failed: {exc}")
        return

    for thread_date, kind, thread_id, title in listed:
        if (thread_date, kind) in have:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO daily_threads (date, kind, thread_id, title) "
            "VALUES (?, ?, ?, ?)",
            (thread_date, kind, thread_id, title),
        )
        conn.commit()
        have.add((thread_date, kind))
        print(f"Located {kind} thread ({thread_date}): {title}")


def run_live(conn: sqlite3.Connection) -> None:
    """One incremental pass (the scheduled path): discover today's threads, then
    pull the newest comments from every recent thread that's still getting them
    (or was just discovered). A thread quiet for STALE_AFTER_HOURS is dropped."""
    today = clock.market_today()
    discover_threads(conn)

    candidates = conn.execute(
        "SELECT date, kind, thread_id, title FROM daily_threads WHERE date >= ? "
        "ORDER BY date, kind",
        ((today - timedelta(days=3)).isoformat(),),
    ).fetchall()
    if not candidates:
        print("No megathread located yet.")
        return

    ids = [row[2] for row in candidates]
    last_comment = dict(
        conn.execute(
            f"SELECT thread_id, MAX(posted_at) FROM comments "
            f"WHERE thread_id IN ({','.join('?' * len(ids))}) GROUP BY thread_id",
            ids,
        ).fetchall()
    )
    stale_before = clock.utc_hours_ago(STALE_AFTER_HOURS)
    active = [
        row for row in candidates
        if last_comment.get(row[2]) is None or last_comment[row[2]] >= stale_before
    ]

    for index, (tdate, kind, thread_id, _title) in enumerate(active):
        if index:
            time.sleep(3)  # space requests out under Reddit's anon rate limit
        try:
            new_count = fetch_comments(
                conn, TARGET_SUBREDDIT, thread_id, sort="new", detect_gap=True
            )
        except requests.RequestException as exc:
            # One thread failing (usually a 429 that outlasted the retries)
            # shouldn't cost us the others; the next run picks it up.
            print(f"  [{tdate} {kind}] fetch failed: {exc}")
            continue
        stored = conn.execute(
            "SELECT COUNT(*) FROM comments WHERE thread_id = ?", (thread_id,)
        ).fetchone()[0]
        print(f"  [{tdate} {kind}] +{new_count} new, {stored} stored")


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
            "INSERT OR REPLACE INTO daily_threads (date, kind, thread_id, title) "
            "VALUES (?, ?, ?, ?)",
            (date_iso, KIND_DAILY, thread_id, title),
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
