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

Projected size: structured columns + body ≈ **5–6 GB/year**; gzipped archive ≈
**1.5–2 GB/year**. Fits the 35 GB free on `/dev/sda1`. Revisit retention at 12 months.

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

### Phase 1 — SQLite store + ingest refactor

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

### Phase 2 — Hourly delta digger + scheduler

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

### Phase 3 — Trend engine ported to the new store + query API

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

### Phase 4 — Deploy the digger to Hetzner (interim runbook)

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

### Phase 5 — Retention + digest rendering

Add `render.py`: Markdown renderers for (a) a single thread as a collapsible
comment-tree digest, (b) the trend table, (c) a symbol report. Add retention to
`store.py`: after a thread is archived to `*.json.gz` (Phase 1 `--export-json`
wired into the digger on thread close), null its comments' `raw_json`; optional
`--prune-bodies-older-than DAYS`. Scheduled `VACUUM`. Document the projected DB
growth curve with real numbers from Phase 4 data.

**Acceptance:** archive round-trips (gz → same tree); DB size drops measurably
after a prune; rendered digests are readable in a Telegram message-width preview.

### Phase 6 — LLM entity extraction (DeepSeek) — requirement 5.1

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

### Phase 7 — LLM sentiment: buy / sell / neutral — requirement 5.2

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

### Phase 8 — MCP server + Hermes Agent + Telegram

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
