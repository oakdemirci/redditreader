import os
from datetime import date

import requests

# 1. Configuration
TARGET_SUBREDDIT = "wallstreetbets"
USER_AGENT = "windows:com.oakdemirci.redditdig:v1.0 (by u/WhiteBlackSmith_2021)"

CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID")
CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET")

if not CLIENT_ID or not CLIENT_SECRET:
    raise SystemExit(
        "Missing REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET environment variables.\n"
        "Create a read-only 'script' app at https://www.reddit.com/prefs/apps "
        "and set both as environment variables before running this script."
    )


def get_access_token(session: requests.Session) -> str:
    response = session.post(
        "https://www.reddit.com/api/v1/access_token",
        auth=(CLIENT_ID, CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
    )
    response.raise_for_status()
    return response.json()["access_token"]


def find_daily_discussion_url(session: requests.Session, subreddit: str) -> str | None:
    """Search the subreddit's stickied hot posts for today's Daily Discussion thread."""
    today = date.today()
    month_name = today.strftime("%B")
    month_day = f"{month_name} {today.day}"
    month_day_padded = f"{month_name} {today.day:02d}"

    response = session.get(f"https://oauth.reddit.com/r/{subreddit}/hot?limit=10")
    response.raise_for_status()
    data = response.json()

    for post in data["data"]["children"]:
        post_data = post["data"]
        title = post_data.get("title", "")
        is_stickied = post_data.get("stickied", False)

        if is_stickied and "Daily Discussion" in title and (month_day in title or month_day_padded in title):
            print(f"Located Thread: {title}\n")
            return f"https://oauth.reddit.com{post_data['permalink']}"

    return None


def print_top_comments(session: requests.Session, thread_url: str, limit: int = 15) -> None:
    response = session.get(thread_url)
    response.raise_for_status()
    thread_data = response.json()

    # Reddit thread JSONs return an array: index [0] is the post itself, index [1] holds the comments
    comments = thread_data[1]["data"]["children"]

    for comment in comments[:limit]:
        if comment.get("kind") != "t1":
            continue  # skip "MoreComments" objects, which require separate API calls to expand

        comment_data = comment["data"]
        author = comment_data.get("author", "Deleted")
        score = comment_data.get("score", 0)
        body = comment_data.get("body", "")

        display_body = body if len(body) < 150 else body[:147] + "..."
        print(f"[{score} upvotes] u/{author}: {display_body}")


def main() -> None:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    access_token = get_access_token(session)
    session.headers.update({"Authorization": f"bearer {access_token}"})

    print(f"Locating Thread: {TARGET_SUBREDDIT}\n")
    thread_url = find_daily_discussion_url(session, TARGET_SUBREDDIT)

    if not thread_url:
        print("Could not locate today's Daily Discussion thread.")
        return

    print_top_comments(session, thread_url)


if __name__ == "__main__":
    main()
