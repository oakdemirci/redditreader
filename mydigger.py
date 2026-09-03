import html
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 1. Configuration
TARGET_SUBREDDIT = "wallstreetbets"
USER_AGENT = "windows:com.oakdemirci.redditdig:v1.0 (by u/WhiteBlackSmith_2021)"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

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
            print(f"Located Thread: {title}\n")
            return entry_id.removeprefix("t3_"), title

    return None


def print_top_comments(subreddit: str, thread_id: str, limit: int = 15) -> None:
    response = get_with_retry(f"https://www.reddit.com/r/{subreddit}/comments/{thread_id}/.rss?limit={limit}")

    root = parse_atom(response)
    shown = 0
    for entry in root.findall("atom:entry", ATOM_NS):
        entry_id = entry.findtext("atom:id", default="", namespaces=ATOM_NS)
        if not entry_id.startswith("t1_"):
            continue  # skip the submission entry itself, keep only comments

        author = entry.findtext("atom:author/atom:name", default="Deleted", namespaces=ATOM_NS)
        raw_content = entry.findtext("atom:content", default="", namespaces=ATOM_NS)
        body = strip_html(raw_content)

        display_body = body if len(body) < 150 else body[:147] + "..."
        print(f"{author}: {display_body}")

        shown += 1
        if shown >= limit:
            break


def main() -> None:
    print(f"Locating Thread: {TARGET_SUBREDDIT}\n")
    result = find_daily_discussion_id(TARGET_SUBREDDIT)

    if not result:
        print("Could not locate today's Daily Discussion thread.")
        return

    thread_id, _ = result
    time.sleep(1)  # be polite between requests
    print_top_comments(TARGET_SUBREDDIT, thread_id)


if __name__ == "__main__":
    main()
