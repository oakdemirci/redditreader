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

## Backfilling recent days

```bash
python mydigger.py --backfill        # last 7 days
python mydigger.py --backfill 14     # last 14 days
```

Backfill finds recent Daily Discussion threads via the subreddit's public
search feed (`/r/<subreddit>/search.rss`) and pulls each one across four sort
orders (`new`, `old`, `top`, `controversial`), deduplicated by comment ID.

Coverage of past days is **partial by design**: once a thread stops taking
comments, RSS exposes only ~100 per sort order, so a busy day yields roughly
250–400 of its several thousand comments (the head, the tail, and the
most-voted). Only days going forward — collected live throughout the day —
get full coverage. Backfill paces itself (~7s between requests) to stay under
Reddit's anonymous rate limit, so a 7-day run takes a few minutes.

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

## Which calendar day?

WSB titles each thread "Daily Discussion Thread for &lt;US date&gt;". All three
scripts resolve "today" in US Eastern time (`clock.py`), so a server running on
UTC and a laptop on local time always agree on which thread is "today's" — no
`TZ=` prefix needed on manual runs. This is why `requirements.txt` includes
`tzdata` (Linux has the tz database system-wide; Windows does not).

## Running on a schedule (e.g. Hetzner)

Every 10 minutes comfortably covers WSB's typical comment volume on the
Daily Discussion thread, with margin for busier days (the 100-comment page
size covers roughly 1.5-2+ hours at typical rates). Add to crontab:

```cron
*/10 * * * * cd $HOME/redditreader && $HOME/redditreader/.venv/bin/python mydigger.py >> $HOME/redditreader/digger.log 2>&1
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

Run `python report.py` to print today's most-mentioned symbols, or
`--date` for a specific stored day:

```bash
python report.py
python report.py --date 2026-09-02
```

## Trends over time

`python trend.py` compares symbols across the last several days. For each one
it shows total mentions in the window, how many were `$`-cashtags (the reliable
kind), the day it first appeared, a per-day mention sparkline, mentions in the
last few hours, and flags:

- **NEW** — first seen within the last 2 days.
- **HOT** — today's *share of voice* (mentions per comment) is at least twice
  the prior days' average. Share of voice is used instead of raw counts because
  backfilled days (~370 comments) and live days (thousands) aren't otherwise
  comparable.
- **$** — has at least one cashtag mention, so it's more likely a real ticker
  than an English-word collision.

```bash
python trend.py                 # 7-day window
python trend.py --days 14 --min 3
python trend.py --recent-hours 2
```

Momentum flags only get meaningful once you have a few full *live* days in the
database — a week of backfill alone isn't enough of a baseline.
