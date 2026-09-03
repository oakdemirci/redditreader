# mydiggerapp

A small personal script that fetches r/wallstreetbets' pinned "Daily Discussion"
thread for the current day via Reddit's public RSS feeds and prints the top
comments (author, body preview) for personal reading.

## What it does

- Reads Reddit's public, unauthenticated Atom/RSS feeds (`/r/<subreddit>/.rss`
  and `/r/<subreddit>/comments/<id>/.rss`) — the same feature used by any RSS
  reader, no login or API app required.
- Looks up r/wallstreetbets' feed to find today's Daily Discussion thread.
- Prints the top-level comments from that thread.
- Automatically waits and retries if Reddit's anonymous rate limit
  (roughly one request per minute per IP) is hit.

## What it does NOT do

- No posting, commenting, voting, messaging, or moderation actions.
- No writes of any kind — strictly read-only.
- Targets a single subreddit (r/wallstreetbets) only.
- Does not redistribute or republish fetched data anywhere public.

## Setup

```bash
pip install -r requirements.txt
python mydigger.py
```

No credentials or environment variables needed.

## Note on comment scores

Reddit's RSS feeds don't expose comment vote scores, so comments are listed
in the order Reddit's feed returns them rather than sorted by score.
