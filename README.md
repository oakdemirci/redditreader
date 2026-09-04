# mydiggerapp

A personal script that incrementally archives r/wallstreetbets' rolling
discussion megathreads over the course of a day, using Reddit's public RSS feeds.

## How it works

- Reads Reddit's public, unauthenticated Atom/RSS feeds (`/r/<subreddit>/.rss`
  and `/r/<subreddit>/comments/<id>/.rss`) — the same feature used by any RSS
  reader, no login or API app required.
- Tracks WSB's rolling megathreads, each tagged with its `kind` in the DB:
  - `daily` — "Daily Discussion Thread for &lt;D&gt;" (Mon–Fri ~06:00 ET, active the trading day)
  - `moves` — "What Are Your Moves Tomorrow, &lt;D+1&gt;" (Mon–Thu + Sun ~16:00 ET, evening + overnight)
  - `weekend` — "Weekend Discussion Thread ... Weekend of &lt;Sat&gt;-&lt;Sun&gt;" (Fri ~16:00 ET, Fri night–Sun)

  Each is filed under the trading day it belongs to: `moves` under the day it's
  posted (its title names tomorrow), `weekend` under its Saturday. So a weekday
  has `daily` + `moves`, Friday has `daily` + `weekend`, Saturday has `weekend`,
  Sunday has `weekend` + `moves` (for Monday). Reports merge a day's threads.
- Each run checks WSB's schedule and only hits the front-page feed if a thread
  it's missing should exist by now; new thread IDs are cached in
  `wsb_comments.db` (SQLite). It then fetches the newest 100 comments
  (`sort=new`) from every recent thread still receiving comments (one that's
  been quiet for 12h is dropped), and inserts any not already stored,
  deduplicated by comment ID.
- Automatically waits and retries if Reddit's anonymous rate limit
  (roughly one request per minute per IP) is hit.

Because Reddit's RSS feeds only ever return one page of results, a single
run can't capture a whole day of comments on a fast-moving thread — it has
to be run repeatedly throughout the day so each run's "delta" builds up the
full picture in the database. Run it on a schedule (see below).

### Comment gaps

RSS caps each request at the newest ~100 comments and offers no way to page
further back. If more than ~100 comments are posted between two runs (WSB's
thread spikes hard at the open and around 08:30 ET economic prints), the ones
in the middle are never on a page when the script looks — they're lost, not
just delayed. A short poll interval is the defence. Each live run checks
whether the thread outran the feed since last time and prints a
`WARNING: comment gap ...` line (to `digger.log`) when it did:

```bash
grep WARNING ~/redditreader/digger.log
```

Frequent warnings mean the interval is still too long, or it's time to move
the live fetch to the JSON endpoint (`.json?limit=500`).

## Backfilling recent days

```bash
python mydigger.py --backfill        # last 7 days
python mydigger.py --backfill 14     # last 14 days
```

Backfill finds recent **Daily Discussion** threads via the subreddit's public
search feed (`/r/<subreddit>/search.rss`) and pulls each one across four sort
orders (`new`, `old`, `top`, `controversial`), deduplicated by comment ID. It
does not backfill the `moves` thread — that one is only collected going forward.

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

WSB titles each thread with a US calendar date. All three scripts resolve
"today" in US Eastern time (`clock.py`), so a server running on UTC and a laptop
on local time always agree on which threads are "today's" — no `TZ=` prefix
needed on manual runs. This is why `requirements.txt` includes `tzdata` (Linux
has the tz database system-wide; Windows does not). The date parsing is
locale-independent (an explicit month-name table, not `strptime("%B")`).

## Running on a schedule (e.g. Hetzner)

On an average day the megathreads run ~10 comments/minute each — right at the
100-per-page limit for a 10-minute interval, and well over it during open/news
spikes. Poll every 3 minutes so a full page always overlaps the last run. Each
run makes ~2–4 requests (one per active thread, plus a discovery request while a
thread it expects is still unposted); Reddit's anonymous limit tolerates that.
`flock` keeps a slow run (rate-limit backoff) from overlapping the next:

```cron
*/3 * * * * /usr/bin/flock -n $HOME/redditreader/.lock $HOME/redditreader/.venv/bin/python $HOME/redditreader/mydigger.py >> $HOME/redditreader/digger.log 2>&1
```

Watch `digger.log` for `WARNING: comment gap` lines — those mean a thread still
outran the feed and some comments were lost (see "Comment gaps" above).

## Data

`wsb_comments.db` (SQLite):

- `daily_threads` (`date`, `kind`, `thread_id`, `title`) — one row per thread,
  keyed `(date, kind)` where `kind` is `daily`, `moves`, or `weekend`.
- `comments` (`id`, `thread_id`, `author`, `body`, `posted_at`, `fetched_at`) —
  join to `daily_threads` on `thread_id` to get the day and source kind.
- `mentions` (`comment_id`, `symbol`, `confidence`).

Query it directly with any SQLite client. To split totals by source thread:
`SELECT dt.kind, COUNT(*) FROM comments c JOIN daily_threads dt USING(thread_id) GROUP BY dt.kind`.

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
