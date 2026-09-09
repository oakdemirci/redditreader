# Reddit digger → Hermes Agent: build plan

Status: **planning** · Created 2026-09-10

## Goal

Turn the current RSS-based `mydigger.py` pipeline into an automated service on a
Hetzner box that:

1. Digs r/wallstreetbets (and sub-threads) continuously from the **Arctic Shift**
   archive (keyless — see [`../memory` note `arctic-shift-archive`]), keeping full
   comment trees tagged by thread kind / name.
2. Runs on an **hourly delta schedule**: the 01:00 run covers 00:00–01:00, the
   02:00 run covers 01:00–02:00, etc., driven by the last successful run.
3. Persists to **SQLite** (system of record), with gzipped JSON as cold archive
   and Markdown only for presentation.
4. Enriches comments with a **DeepSeek** LLM: (5.1) extract stock / crypto / token
   names + tickers, (5.2) sentiment as `buy` / `sell` / `neutral`.
5. Is queried from Telegram via **NousResearch Hermes Agent**
   (<https://github.com/NousResearch/hermes-agent>), which we extend with an
   **MCP server** exposing the trend/query layer. We do **not** build our own
   Telegram bot — Hermes is the bot, the conversation, and the live LLM.

## Architecture

```
Arctic Shift API ──> digger.py (hourly, systemd timer)
                        │  upsert
                        ▼
                     SQLite  (threads, comments[flat +parent_id], runs,
                        │      entities, sentiment)    WAL mode
        ┌───────────────┼────────────────────┐
        ▼               ▼                    ▼
 enrich_entities   enrich_sentiment     hermes_api.py  (read-only query layer:
 (DeepSeek 5.1)    (DeepSeek 5.2)        trend / symbol / search / threads)
                                             │
                                             ▼
                                        mcp_server.py  (stdio MCP)
                                             │
                                             ▼
                                     Hermes Agent  ──> Telegram (allowlisted)
                                     (model = DeepSeek via OpenAI-compat endpoint)
```

- Full raw API objects are kept in gzipped per-thread JSON under
  `archive/YYYY/MM/`; SQLite holds structured columns + comment body.
- Tree is stored **flat** (`parent_id`) and reconstructed on read with a
  recursive CTE — no 25k-comment ceiling, no "load more" stubs.

## Persistence decision (requirement 4)

| Option    | Verdict      | Role |
|-----------|--------------|------|
| SQLite (WAL) | **primary** | Relational trend aggregation, dedup upserts, concurrent bot reads during a digger write. Already used in this repo. |
| JSON (`.json.gz`) | secondary | Per-thread cold archive for reprocessing / portability. Not queried. |
| Markdown  | not storage  | Render target for chat replies (thread digests, `SYM` table). |

Measured size (Phase 5, megathreads only): **~1.1 GB/year** live DB (weekly
archival nulls `raw_json` once threads close) + **~0.5 GB/year** gzipped archive +
up to ~1.5 GB of rolling backups. ~3 GB in year one, comfortable on the 35 GB
`/dev/sda1`. Without the archival step it would be ~11.5 GB/year. Full curve in
`docs/DEPLOY.md`.

## Delta / windowing (requirement 3)

- `runs(id, window_start, window_end, status, n_threads, n_comments_new,
  started_at, finished_at, error)`.
- Each run's window = `[last_successful_end, floor(now, 1h))`.
- Missed runs: next run widens to cover the gap, processed hour-by-hour so each
  window stays small.
- Arctic Shift `after` / `before` on `created_utc` map to the window directly;
  comments fetched per active thread within `[start, end)`.

---

## Phases

Self-contained; do them roughly in order. Dependency chain: 1 → 2 → 3 → 4, then
5–7 in any order, then 8 last. Each phase heading below is the prompt to start it.

### Phase 1 — SQLite store + ingest refactor  ✅ done (2026-09-10)

Shipped as `arctic.py` + `store.py` + `ingest.py`, `wsb_tree.py` reduced to a
wrapper. Notes for later phases:

* `store.get_tree()` reconstructs the tree in Python (not a recursive CTE) --
  simpler and it already matches the old output shape exactly. Revisit if Phase 8
  needs SQL-side subtree/depth slicing.
* `comments/search` **rejects `after` + `before` together on a `link_id` query**
  (422 with a misleading "slow down" body). `arctic.Archive.comments()` therefore
  sends only `after` and applies `before` client-side. Verified: a 1-hour slice
  of a busy daily thread returned exactly the in-window comments (~1500).
* A busy WSB daily runs ~1500 comments/hour -> ~15 pages/hour/thread. Fine.
* `store.last_successful_end()` counts both `ok` and `partial` runs. **Phase 2
  must decide partial-run semantics** (a per-thread failure currently advances
  the watermark past comments it never fetched).

Original spec:

Refactor the Arctic Shift ingest into a persistent SQLite store. Split
`wsb_tree.py` into flat modules matching this repo's style (root-level `.py`
files, no package): `arctic.py` (the HTTP client + retry/backoff, extracted
as-is), `store.py` (SQLite schema + upserts + tree reconstruction), `ingest.py`
(orchestration + CLI).

Schema: `threads(id, kind, subreddit, title, flair, created_utc, permalink,
first_seen, last_polled, is_open, post_json)`; `comments(id, thread_id,
parent_id, author, created_utc, score, body, edited, controversiality,
retrieved_at, raw_json)`; `runs(...)` as above; empty placeholder tables
`entities` and `sentiment` (columns designed now, filled in Phases 6–7). WAL
mode. Store comment bodies as columns; `raw_json` nullable so it can be dropped
after archival later.

`ingest.py` CLI: `--thread ID_OR_URL`, `--window START END` (ISO or epoch),
`--export-json DIR` (optional gzipped per-thread dump). Tree is a read-time
function `store.get_tree(thread_id)` using a recursive CTE, returning the same
nested `{...comment..., replies:[...]}` shape `wsb_tree.py` produces today, plus
`orphaned_comments`.

Keep the old `wsb_tree.py` working as a thin wrapper over the new modules
(JSON-file output path unchanged) so nothing that depends on it breaks.

**Acceptance:** ingest a known busy thread; `get_tree` round-trips with matching
counts and depth; re-running the same ingest is idempotent (0 new rows); `runs`
row written.

### Phase 2 — Hourly delta digger + scheduler  ✅ done (2026-09-10)

Shipped as `digger.py` + `systemd/hermes-digger.{service,timer}`. Design choices
worth carrying forward:

* **Two watermarks, not one.** `meta.discovery_through` (how far the new-thread
  scan reached) is global; `threads.comments_through` is per thread. This
  resolves the Phase 1 open question: a per-thread fetch failure leaves *that*
  thread's watermark alone, so its gap is re-pulled next run, while the slice
  still completes. Only a failure in the discovery scan itself holds a slice back.
* Work is done in **hourly slices**, each its own `runs` row + commit. A healthy
  tick does one slice; after downtime `--catch-up` replays the backlog
  oldest-first (resumable). Without `--catch-up` a behind digger advances one
  hour per tick and warns.
* Slices overlap by 1 second on the read side (`after = frm - 1`,
  `iter_posts(start - 1, ...)`) so a row on the exact boundary can't slip between
  an exclusive `before` and the next exclusive `after`. Idempotent upserts absorb
  the re-read.
* `schema_version` 1 -> 2 migration (`ALTER TABLE threads ADD comments_through`,
  backfilled from each thread's newest stored comment) runs automatically in
  `store.connect()`.
* Verified: 4 contiguous slices, correct per-hour comment deltas on a live daily
  (470 / 603 / 700 ...), a full re-run inserts nothing, the lock blocks a second
  instance.

Original spec:

Build `digger.py`: the automatic hourly job. Compute the window from the last
successful `runs` row (`[last_end, floor(now, 1h))`); on a gap > 1h, iterate
hour-by-hour so each window stays small. Discovery within a window: (a)
megathreads (`daily` / `moves` / `weekend`) created in the last ~48h and still
open, (b) flaired posts (`gain` / `loss` / `discussion`, configurable) created in
the window. For every tracked open thread, fetch comments with `created_utc` in
`[start, end)` via `arctic.py` pagination and upsert. Mark a thread
`is_open = 0` once it's ~48h old and quiet. One transaction per thread so a
mid-run crash leaves completed threads committed. Reuse the `mydigger.lock`
stale-lock pattern to prevent overlap.

Deliver `systemd/hermes-digger.service` + `hermes-digger.timer` (runs at `*:05`,
`Persistent=true`) and a `--catch-up` flag. Structured log lines per run (window,
threads, new comments, duration).

**Acceptance:** two consecutive real runs produce contiguous non-overlapping
windows; a deliberately deleted `runs` row triggers correct catch-up; killing the
process mid-run and rerunning resumes cleanly with no duplicates.

### Phase 3 — Trend engine ported to the new store + query API  ✅ done (2026-09-10)

Shipped: `hermes_api.py` (query layer), `extract.py` (regex tier -> `entities`),
`wsbcal.py` (trading-day resolver, ported from `mydigger.py`). `trend.py` and
`report.py` rewritten as thin CLIs over `hermes_api`, reading `hermes.db`.

* **Mentions live in `entities` now**, filled by the regex tier (`source='regex'`,
  `tier` in cashtag/bareword/unverified; `__none__` sentinel for scanned-empty).
  `digger.py` runs `extract.extract_pending()` after each slice; `ingest.py` runs
  it at the end unless `--no-extract`. Phase 6 adds `source='llm'` alongside;
  counts use `COUNT(DISTINCT comment_id)` so a comment matched by both tiers is
  one mention.
* **schema v2 -> v3**: `threads.trading_day` (backfilled via `wsbcal`), `entities`
  gains `tier` and drops `type NOT NULL` (rebuilt -- was empty). `_migrate()` now
  runs *before* `executescript(SCHEMA)` so new indexes don't reference
  not-yet-added columns.
* The trend maths (share-of-voice `HOT`, `NEW` window, `$` flag, relevance score
  + ordering, `PARTIAL_COMMENT_THRESHOLD`) is a line-for-line port. Locked down by
  `tests/test_hermes_api.py` (7 fixture tests: `python tests/test_hermes_api.py`
  or pytest).
* `hermes_api` also exposes `symbol_detail`, `search_comments`, `threads`,
  `day_report`, `run_status` -- each returns a dataclass with `.to_text()` /
  `.to_markdown()`, ready for the Phase 8 MCP server.
* `trend.py --include-flair[=gain,loss,discussion]` widens beyond megathreads.
* **`trend.py`/`report.py` now read `hermes.db`, not `wsb_comments.db`** -- the
  RSS pipeline's reporting is superseded (old versions in git history at
  `b0e6337`). `mydigger.py` still runs independently; retire-or-keep is the
  standing open item.

Original spec:

Port the business logic in `trend.py` and `report.py` to read the Phase 1
schema. Reproduce the `SYM` table exactly: `TOT` (mentions in window), `$`
(cashtag count), `1ST` (first-seen date), per-day columns, `4h` column, `FLAGS`
(`HOT` via share-of-voice = mentions per comment on partial-backfill-aware
denominators, `NEW`, `$`). Keep `--symbol SYM` drill-down (the actual comments
behind an entry), `--top`, `--cashtags-only`, `--stale-days`.

Wrap it in `hermes_api.py` — a plain-Python, read-only query layer with a stable
signature per function: `trend(window, top_n, cashtags_only, stale_days)`,
`symbol_detail(symbol, days)`, `search_comments(query=None, symbol=None,
since=None, until=None, limit=…)`, `threads(since=None, kind=None)`,
`run_status()`. Each returns structured data plus a `.to_text()` / markdown
renderer for chat.

**Acceptance:** for a data window that overlaps the current `wsb_comments.db`,
the new `trend()` output matches the existing `trend.py` output; query functions
have unit tests against a fixture DB.

### Phase 4 — Deploy the digger to Hetzner (interim runbook)  ✅ done (2026-09-10)

Shipped: [`docs/DEPLOY.md`](DEPLOY.md), `scripts/{install,backup,healthcheck}.sh`,
`systemd/hermes-digger-backup.{service,timer}` + `logrotate-hermes-digger`,
`.env.example`.

* `digger.py --backfill DAYS` -- one wide `process_slice` over the whole range
  (single discovery sweep + full per-thread fetch), leaving watermarks at `now`
  so the hourly timer continues cleanly.
* `digger.py` reads `HERMES_DB` / `HERMES_SUBREDDIT` / `HERMES_KINDS` as arg
  defaults; the service unit has `EnvironmentFile=-/opt/hermes-digger/.env`.
* `backup.sh` uses the sqlite online-backup API via Python (no `sqlite3` CLI
  dependency), gzips, `integrity_check`s, keeps newest `BACKUP_KEEP` (7). Daily
  timer at 03:30. Off-box (restic -> Storage Box) documented, not automated.
* `install.sh` is idempotent (re-run after `git pull`): packages, `hermes` user,
  venv, `.env` from example, units enabled, journald capped at 500 MB.
* `healthcheck.sh`: timers + last run + `ingest.py --stats` + disk; exit 1 if the
  last run isn't ok/partial.
* Verified locally: backup rotation (keep 2 of 3), restore drill
  (`integrity_check ok`, row counts match), `bash -n` on all scripts, backfill
  path. Box-side steps (systemd, journald) are documented but untested until a
  real deploy.

Original spec:

Produce a reproducible install runbook and helper scripts to run Phases 1–3 on
the Hetzner box, so real data starts accumulating while later phases are built.
Dedicated `hermes` user; `/opt/hermes-digger` with a venv; `.env` (no secrets
needed yet — Arctic Shift is keyless); the systemd timer from Phase 2 enabled;
`journald` logging + `logrotate`; `backup.sh` doing `sqlite3 .backup` to
`/opt/hermes-digger/backups/` with 7-day rotation (document the `restic`-to-
object-storage upgrade path); a one-shot `--backfill DAYS` for initial history.
Include a disk-usage check and a `systemctl status` / last-run smoke test.

**Acceptance:** from a clean checkout on the box, following the runbook yields a
firing timer, a growing DB, working `trend.py` / `hermes_api` queries over SSH,
and a tested backup+restore.

### Phase 5 — Retention + digest rendering  ✅ done (2026-09-10)

Shipped: `render.py` (thread digest / trend block / symbol block), `maintain.py`
+ `systemd/hermes-digger-maintenance.{service,timer}` (weekly Sun 04:15), store
retention helpers, schema v3 -> v4 (`threads.archived_at`).

* On thread close the digger snapshots to `archive/YYYY/MM/*.json.gz`
  (`--archive-dir` / `HERMES_ARCHIVE_DIR`, default `<db dir>/archive`) and nulls
  the comments' `raw_json`. `maintain.py` does the same for any straggler +
  `VACUUM` + optional `--prune-bodies DAYS`.
* **Measured (busy day, 11,307 comments): 39.8 MB with raw_json -> 3.7 MB after
  archival+VACUUM (11x), gz snapshot 1.8 MB.** Annualised ~1.1 GB/yr live DB +
  ~0.5 GB/yr archive; without archival it's ~11.5 GB/yr. Full curve in
  `docs/DEPLOY.md`.
* `render.py` targets Telegram: the SYM table goes in a code fence (no Markdown
  tables there), the thread digest is depth/score/`--top`-limited bullets with
  `_(N more)_` collapse (or `<details>` with `--details`). `store.get_tree`
  reconstructs fine from columns after `raw_json` is gone.
* `store.write_archive` is now the single gz writer (ingest's `--export-json`
  delegates to it).

Acceptance met: gz round-trips to the same id set as a live `get_tree`; DB
shrank 11x after a prune; digests render within Telegram width.

Original spec:

Add `render.py`: Markdown renderers for (a) a single thread as a collapsible
comment-tree digest, (b) the trend table, (c) a symbol report. Add retention to
`store.py`: after a thread is archived to `*.json.gz` (Phase 1 `--export-json`
wired into the digger on thread close), null its comments' `raw_json`; optional
`--prune-bodies-older-than DAYS`. Scheduled `VACUUM`. Document the projected DB
growth curve with real numbers from Phase 4 data.

**Acceptance:** archive round-trips (gz → same tree); DB size drops measurably
after a prune; rendered digests are readable in a Telegram message-width preview.

### Phase 6 — LLM entity extraction (DeepSeek) — requirement 5.1  ✅ done (2026-09-10)

Shipped: `llm.py` (shared client), `enrich_entities.py`, schema v4->v5
(`llm_calls` ledger + `llm_cache`), `hermes_api` `entity_mode` (regex/llm/best),
`tools/eval_entities.py` + a 40-row seed label set, `tests/test_enrich_entities.py`.

* `llm.py`: DeepSeek via plain `requests` (no `openai` dep), JSON mode, retry,
  per-call `llm_calls` logging with token+cost, daily USD cap
  (`HERMES_LLM_DAILY_USD`), per-input `llm_cache` keyed on body-hash + prompt
  version. `available()` is False with no key -> the whole pass is a no-op, so
  the digger stays keyless-safe.
* `enrich_entities.py`: pre-filter (cashtag OR finance keyword OR regex
  candidate), batch ~30, `PROMPT_VER = "entities-v1"`. Writes `source='llm'`,
  `tier='llm'`, with a `__none__` sentinel for comments it scanned and cleared
  (lets `best` mode veto a bad regex bareword). Wired into `digger.py` after the
  regex pass; `--no-llm` to skip.
* `entity_mode`: `regex` (default, matches pre-Phase-6 + the fixture tests),
  `llm`, `best` (llm where the comment was scanned, else regex). `$` cashtag
  confirmation is always computed from the regex tier regardless of mode.
  `trend.py --llm` / `report.py --llm` = `best`.
* **Eval (regex tier, 40-row seed): precision 1.00, recall 0.47, F1 0.64.** Every
  miss is a lowercase ticker ("meta", "spy") or a name ("Palantir", "bitcoin",
  "TSMC") -- exactly what the LLM tier is for. `regex+llm` mode needs a key to
  run; harness ready. **Estimated cost ~$0.02 per 1k comments sent (~35% pass
  rate) -> ~$2/month** for megathreads; `llm_calls` records the real numbers.
* Cache verified: re-run at the same `prompt_ver` makes zero API calls.

Acceptance: precision/recall harness + seed set (grow to ~200 and run with a key
for the full check); cost estimated + ledgered; zero-call re-run confirmed.

Original spec:

Build `enrich_entities.py` + shared LLM infra (`llm.py`: DeepSeek client via its
OpenAI-compatible endpoint, JSON-mode, batching, retry, `llm_calls` cost/token
log, nightly budget cap, prompt-version constant). Pre-filter comments to those
worth a call (regex candidate from `tickers.py` **or** a finance keyword). Batch
~20–40 comments per request; response schema `[{comment_id, symbols:[{ticker,
name, type: stock|crypto|token, confidence}]}]`. Write to `entities(comment_id,
symbol, type, confidence, source: regex|llm, model, prompt_ver)`. Merge with
`tickers.py`'s verified / unverified tiers — regex stays the cheap first pass,
LLM adds names ("Palantir", "nvidia") and disambiguates collisions
(TIME / BE / OR). Cache by `hash(body) + prompt_ver`; same version re-run is a
no-op.

Wire it into the digger as a post-ingest step over new comments only.

**Acceptance:** precision/recall spot-check against a ~200-comment hand-labeled
sample; measured cost per 1k comments; re-run with unchanged `prompt_ver` makes
zero API calls.

### Phase 7 — LLM sentiment: buy / sell / neutral — requirement 5.2  ✅ done (2026-09-10)

Shipped: `enrich_sentiment.py`, `sentiment` table filled (scope `symbol_day` +
`comment`), `hermes_api` sentiment wiring, `systemd/hermes-digger-sentiment.*`
(twice-daily, NOT in the hourly digger -- today's day is re-scored as it grows,
so hourly would just churn budget).

* **Granularity: `symbol_day` primary** (Phase 6 cost was tiny, so affordable).
  One call per (symbol, trading day) over a sample of that day's comments
  mentioning it; `--per-comment` for drill-down. `min_mentions` default 6.
* Cache key = `hash(symbol + sorted comment-ids) + prompt_ver` -> a closed day is
  called once; today re-calls only when its comment set changes. `--force`
  re-scores but still hits the cache if the set is unchanged (labels stable).
* `hermes_api.trend(with_sentiment=True)` appends `BUY`/`SELL`/`NEU` to a row's
  FLAGS (keeps the fixed-width table shape -- no new column). `symbol_detail
  (with_sentiment=True)` and `symbol_sentiment()` return the per-day history.
  `trend.py --sentiment`, `report.py --symbol` (always on), `render.py`.
* `store.save_sentiment` upserts via the existing expression unique index
  (`ON CONFLICT (scope, symbol, IFNULL(...))`).
* **Cost: ~75 symbol-days on a busy day at min-mentions 6 -> ~$0.0006/call ->
  ~$0.055/day -> ~$1.7/month** for megathreads; `--dry-run` previews the
  candidate list keylessly. Well under the daily cap.
* Tests: `tests/test_enrich_sentiment.py` (5) -- scoring, cache=stable-rerun,
  budget stop, trend/symbol_detail integration, per-comment mode.

Acceptance: labels stable at a fixed `prompt_ver` (cache + `--force` test);
`trend()` shows the tag; cost measured + capped. Manual agreement check needs a
key + the user.

Original spec:

Build `enrich_sentiment.py` on the Phase 6 LLM infra. Primary mode: per
`(symbol, thread)` or `(symbol, day)` aggregate — feed the symbol's mentions for
that window, get `{label: buy|sell|neutral, confidence, rationale}`. Optional
`--per-comment` mode for drill-down. Write to `sentiment(scope, symbol,
thread_id, window_start, window_end, label, confidence, rationale, model,
prompt_ver, created_at)`. Feed a sentiment column / flag into
`hermes_api.trend()` and `symbol_detail()`.

**Acceptance:** labels are stable across re-runs at the same `prompt_ver`; manual
agreement check on a sample; `trend()` shows a sentiment column; cost per
symbol-day measured and within budget cap.

### Phase 8 — MCP server + Hermes Agent + Telegram  ✅ done (2026-09-10)

Shipped: `mcp_server.py` (hand-rolled stdio MCP, stdlib only),
`mcp/hermes-wsb.mcp-config.yaml`, `systemd/hermes-agent.service`,
`scripts/ask-the-bot.sh`, `docs/DEPLOY.md` §7.

* `mcp_server.py`: newline-delimited JSON-RPC 2.0 on stdio, no SDK. Handles
  `initialize` (echoes a supported protocol version), `tools/list`, `tools/call`,
  `ping`, notifications. Seven read-only tools -> `hermes_api` + `render`, each
  returning `content` (text) + `structuredContent` (JSON). Opens `hermes.db`
  `PRAGMA query_only`. `--selftest` runs the whole handshake + every tool
  in-process; `--list` dumps schemas.
* Tools: `get_trend`, `get_symbol`, `get_sentiment` (symbol history or day
  board), `search_comments`, `get_thread` (id/url or daily/moves/weekend),
  `list_threads`, `run_status`.
* Hermes Agent is installed separately (`install.sh`, interactive); `/model` ->
  DeepSeek custom endpoint; `hermes gateway setup` for Telegram + chat-id
  allowlist + command approval; MCP registered via `~/.hermes/mcp-config.yaml`.
  `systemd/hermes-agent.service` keeps the gateway up.
* `scripts/ask-the-bot.sh` drives the MCP server through the four acceptance
  scenarios (trend / sentiment / thread digest / free-form search+drill) --
  passes against real data. The Telegram round-trip and the allowlist rejection
  are manual checks in DEPLOY.md §7f (need a bot token + the box).

Acceptance: MCP self-test green (9 checks); the four tool paths return correct
answers; Telegram end-to-end + non-allowlisted rejection documented for the box.
The digger/enrichment timers are untouched -- the MCP layer only reads.

Original spec:

Build `mcp_server.py` — a stdio MCP server wrapping `hermes_api.py` read-only
(its own WAL connection): tools `get_trend`, `get_symbol`, `get_sentiment`,
`search_comments`, `get_thread`, `list_threads`, `run_status`, each returning
structured JSON + a compact chat rendering.

Then the deployment runbook for the box: install Hermes Agent (`install.sh`;
needs Python 3.11, Node, ripgrep, ffmpeg, git); set `/model` to DeepSeek via its
OpenAI-compatible endpoint + `DEEPSEEK_API_KEY`; `hermes gateway setup` for
Telegram with a chat-ID allowlist and command approval; register `mcp_server.py`
as an MCP server in Hermes's config; systemd unit for the gateway. Add a short
"ask the bot" test script covering: current trend, a symbol's sentiment, a thread
digest, a free-form question.

**Acceptance:** from Telegram, an allowlisted user gets correct answers to all
four test questions end-to-end; non-allowlisted users are rejected; digger timer
and enrichment still running independently.

---

## Open items / decisions deferred

- Enrichment granularity for sentiment (per-comment vs symbol-day aggregate) —
  settle at Phase 7 start based on Phase 6 cost numbers.
- Whether to keep `mydigger.py` (RSS) running in parallel as a backfill safety
  net, or retire it once the Arctic Shift digger is proven on the box.
- Object-storage backups (restic → Hetzner Storage Box) — deferred to post-Phase 8.
