# mydiggerapp

A personal script that incrementally archives r/wallstreetbets' pinned "Daily
Discussion" thread over the course of a day, using Reddit's public RSS feeds.

## How it works

- Reads Reddit's public, unauthenticated Atom/RSS feeds (`/r/<subreddit>/.rss`
  and `/r/<subreddit>/comments/<id>/.rss`) — the same feature used by any RSS
  reader, no login or API app required.
- On first run of the day, looks up today's Daily Discussion thread and
  caches its ID in `wsb_comments.db` (SQLite) so later runs skip that lookup.
- Each run fetches the newest 100 comments (`sort=new`) and inserts any not
  already stored, deduplicated by comment ID.
- Automatically waits and retries if Reddit's anonymous rate limit
  (roughly one request per minute per IP) is hit.

Because Reddit's RSS feeds only ever return one page of results, a single
run can't capture a whole day of comments on a fast-moving thread — it has
to be run repeatedly throughout the day so each run's "delta" builds up the
full picture in the database. Run it on a schedule (see below).

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

## Running on a schedule (e.g. Hetzner)

Every 10 minutes comfortably covers WSB's typical comment volume on the
Daily Discussion thread, with margin for busier days (the 100-comment page
size covers roughly 1.5-2+ hours at typical rates). Add to crontab:

```cron
*/10 * * * * cd /path/to/mydiggerapp && /path/to/venv/bin/python mydigger.py >> digger.log 2>&1
```

## Data

Comments accumulate in `wsb_comments.db` (SQLite), table `comments`
(`id`, `thread_id`, `author`, `body`, `posted_at`, `fetched_at`). Query it
directly with any SQLite client for analysis.

Note: Reddit's RSS feeds don't expose comment vote/score data at all, so
there's no way to capture or backfill upvotes/downvotes with this approach.

## Ticker/coin mention extraction

Every stored comment is scanned for referenced stock tickers and major
cryptocurrencies (`tickers.py`), with two confidence levels:

- **cashtag** — `$TICKER` mentions (WSB's own convention). High confidence.
- **bareword** — plain all-caps words matched against the real NASDAQ/NYSE
  symbol list (auto-downloaded and cached weekly) or a curated list of major
  crypto symbols, after filtering out common WSB slang and English words
  that coincide with real ticker symbols (`STOPWORDS` in `tickers.py`).

Bareword matching can never be perfectly precise — plain English inevitably
collides with some real ticker symbols. Treat cashtag mentions as reliable
and bareword mentions as "possible," and extend `STOPWORDS` if you spot new
false positives.

Run `python report.py` to print today's most-mentioned symbols:

```bash
python report.py
```
