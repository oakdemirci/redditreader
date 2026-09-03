# mydiggerapp

A small personal script that fetches r/wallstreetbets' pinned "Daily Discussion"
thread for the current day via Reddit's official OAuth2 API and prints the top
15 top-level comments (score, author, body preview) for personal reading.

## What it does

- Authenticates read-only via OAuth2 `client_credentials` (app-only auth, no
  Reddit user login/password required).
- Looks up r/wallstreetbets' hot/stickied posts to find today's Daily
  Discussion thread.
- Prints the top-level comments from that thread.

## What it does NOT do

- No posting, commenting, voting, messaging, or moderation actions.
- No writes of any kind — strictly read-only.
- Targets a single subreddit (r/wallstreetbets) only.
- Does not redistribute or republish fetched data anywhere public.

## Setup

1. Create a read-only "script" app at https://www.reddit.com/prefs/apps
2. Set the following environment variables:
   - `REDDIT_CLIENT_ID`
   - `REDDIT_CLIENT_SECRET`
3. Install dependencies and run:

```bash
pip install -r requirements.txt
python mydigger.py
```
