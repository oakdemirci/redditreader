# Deploying to Hetzner

Sections 1-6 stand up the digger + reports (Phase 4). Section 7 wires up Hermes
Agent + Telegram (Phase 8). The digger runs fine on its own; do section 7 when
you want to query it from a phone.

Target: a small Debian/Ubuntu VM. Disk need is modest (~5-6 GB/year for the DB,
~2 GB/year for gzipped backups -- see `docs/PLAN.md`).

---

## 1. Get the code on the box

```bash
sudo mkdir -p /opt/hermes-digger
sudo chown "$USER" /opt/hermes-digger
git clone git@github.com:oakdemirci/redditreader.git /opt/hermes-digger
OR
git clone https://github.com/oakdemirci/redditreader.git /opt/hermes-digger
# (uses your GitHub auth; the repo's id_deploy key is one option -- add it to
#  ssh-agent or ~/.ssh/config as the identity for github.com)
```

## 2. Install

```bash
sudo APP_DIR=/opt/hermes-digger bash /opt/hermes-digger/scripts/install.sh
```

This installs `python3-venv`, `sqlite3`, `git`; creates the `hermes` system
user; builds `.venv`; copies `.env.example` -> `.env`; installs and enables
`hermes-digger.timer` (hourly at :05) and `hermes-digger-backup.timer` (daily
03:30); caps journald at 500 MB.

Review `/opt/hermes-digger/.env`:
* `HERMES_KINDS` -- megathreads only by default; add `gain,loss,discussion` to
  also track flaired standalone posts.
* `DEEPSEEK_API_KEY` -- optional. Empty = keyless (regex ticker extraction only).
  Set it to turn on the LLM passes:
  * `enrich_entities.py` (ticker disambiguation), run by the hourly digger;
  * `enrich_sentiment.py` (buy/sell/neutral per symbol per day), run by
    `hermes-digger-sentiment.timer` twice daily (16:20 & 23:20 UTC).
  Both are capped by `HERMES_LLM_DAILY_USD` (default $1/day). Real cost is
  ~$2/month entities + ~$1.7/month sentiment for megathreads. Then `trend.py
  --llm` / `report.py --llm` count the blended tier; `trend.py --sentiment` adds
  the BUY/SELL/NEU tag.

## 3. Backfill initial history

One-shot, runs as the `hermes` user. It does a single wide pass: one discovery
sweep over the range, then a **full comment fetch of every matched thread**.

Run it inside `tmux` / `screen` (or `systemd-run --scope`) so an SSH drop can't
kill it:

```bash
tmux new -s backfill
sudo -u hermes HERMES_KINDS=daily,moves,weekend,gain,loss,discussion \
    /opt/hermes-digger/.venv/bin/python /opt/hermes-digger/digger.py --backfill 7
```

Timing depends entirely on `HERMES_KINDS`:

* **megathreads only** (`daily,moves,weekend`): ~13 threads over 7 days, **20-40 min**.
* **+ `gain,loss,discussion`**: hundreds of threads, **1-4 hours**. Most flair
  posts are short and old, so consider `--backfill 2` or `3` for those and keep
  `--backfill 7` for the megathreads, or just let it run.

Every thread is checkpointed as it completes (`threads.comments_through`), so a
Ctrl-C is safe: **re-run the exact same `--backfill N`** to fill what's left
(already-done threads become a cheap delta). Don't let the hourly timer take over
after a *partial* backfill -- it only moves forward, so history for threads the
backfill never reached is lost once they age out (~48 h).

It leaves the watermarks at "now", so once complete the hourly timer continues
cleanly.

### Is the backfill still working?

From a second SSH session (WAL lets you read while it writes):

```bash
watch -n15 "sudo -u hermes sqlite3 -readonly /opt/hermes-digger/hermes.db \
'WITH w AS (SELECT strftime(\"%s\",window_end) e FROM runs ORDER BY id DESC LIMIT 1) \
 SELECT (SELECT COUNT(*) FROM comments) comments, \
        (SELECT COUNT(*) FROM threads) threads, \
        (SELECT SUM(comments_through>=(SELECT e FROM w)) FROM threads) threads_done;'"
```

`comments` climbing = it's consuming data. `threads_done / threads` ~= progress.
The `runs` row stays `status=running` with `n_comments_new=0` until the whole
pass finishes -- that's expected; look at the `comments` count, not the run row.
`pgrep -af 'digger.py --backfill'` confirms the process is alive.

## 4. Verify

```bash
bash /opt/hermes-digger/scripts/healthcheck.sh
```

Expect: the two timers listed with a `NEXT` time, the last run line
`status=ok`, `ingest.py --stats` showing a non-zero comment count and a recent
`discovery_through`, and a DB size. Exit code 0.

Watch the next scheduled tick:

```bash
systemctl list-timers hermes-digger.timer
journalctl -u hermes-digger.service -f      # wait for :05
```

Query over SSH:

```bash
cd /opt/hermes-digger
sudo -u hermes .venv/bin/python trend.py --days 3
sudo -u hermes .venv/bin/python report.py --symbol NVDA
```

## 5. Test the backup + restore

```bash
sudo -u hermes /opt/hermes-digger/scripts/backup.sh
ls -lh /opt/hermes-digger/backups/

# restore drill into a scratch copy (never overwrite the live DB in place):
cd /tmp
zcat /opt/hermes-digger/backups/hermes-*.db.gz | head -c0   # newest
latest=$(ls -1t /opt/hermes-digger/backups/hermes-*.db.gz | head -1)
zcat "$latest" > restored.db
sqlite3 restored.db 'PRAGMA integrity_check; SELECT COUNT(*) FROM comments;'
```

To actually roll back: stop the timer, replace the DB, restart.

```bash
sudo systemctl stop hermes-digger.timer
sudo -u hermes bash -c 'zcat "$0" > /opt/hermes-digger/hermes.db' "$latest"
sudo systemctl start hermes-digger.timer
```

## 6. Off-box backups (recommended, not automated here)

`scripts/backup.sh` only writes locally. For durability add `restic` on a
separate timer pushing `/opt/hermes-digger/backups` (or a `.dump` stream) to a
Hetzner Storage Box / object storage. Sketch:

```bash
restic -r sftp:u12345@u12345.your-storagebox.de:hermes init
restic -r sftp:u12345@u12345.your-storagebox.de:hermes backup /opt/hermes-digger/backups
restic ... forget --keep-daily 14 --keep-weekly 8 --prune
```

---

## Operations

| task | command |
|---|---|
| status | `bash scripts/healthcheck.sh` |
| live logs | `journalctl -u hermes-digger.service -f` |
| run now | `sudo systemctl start hermes-digger.service` |
| pause | `sudo systemctl stop hermes-digger.timer` |
| catch up a gap | it's automatic (`--catch-up` in the unit); or run `digger.py --catch-up` by hand |
| update code | `git -C /opt/hermes-digger pull && sudo bash scripts/install.sh` |
| widen coverage | edit `HERMES_KINDS` in `.env`, then `systemctl restart hermes-digger.timer` |
| re-extract mentions | `sudo -u hermes .venv/bin/python extract.py --rescan` |
| retention now | `sudo -u hermes .venv/bin/python maintain.py` |
| DB size accounting | `sudo -u hermes .venv/bin/python maintain.py --report` |
| LLM spend / status | `sudo -u hermes .venv/bin/python enrich_entities.py --stats` |
| LLM extraction now | `sudo -u hermes .venv/bin/python enrich_entities.py` |
| sentiment now | `sudo -u hermes .venv/bin/python enrich_sentiment.py` |
| preview sentiment cost | `sudo -u hermes .venv/bin/python enrich_sentiment.py --dry-run` |

### Troubleshooting

* **`another digger run holds the lock`** -- a previous run is still going or
  crashed. `digger.lock` in `/opt/hermes-digger` self-clears after 1 h; delete
  it if you're sure nothing is running.
* **`status=partial`** -- one thread's fetch failed (usually a rate-limit
  timeout). Its per-thread watermark isn't advanced, so the next run refills the
  gap. Persistent partials -> raise `--min-interval` (edit the unit's ExecStart
  or set it in a drop-in).
* **timer not firing** -- `systemctl status hermes-digger.timer`; check the box
  clock (`timedatectl`) is on UTC and correct.
* **backfill "runs forever"** -- with `gain,loss,discussion` in `HERMES_KINDS`
  it's hundreds of threads and takes hours (see §3). It's not stuck if the
  `comments` count keeps climbing. Safe to Ctrl-C and re-run the same
  `--backfill N`.
* **disk filling** -- `du -sh /opt/hermes-digger/*`; prune backups
  (`BACKUP_KEEP`), and see the retention work in Phase 5.

### Storage & retention

Measured on a busy day (2026-09-09, megathreads only): **11,307 comments**,
11,545 entity rows.

| state | size | per comment |
|---|---|---|
| DB with `raw_json` (fresh ingest) | 39.8 MB | ~3.5 KB |
| DB after archival (`raw_json` nulled) + `VACUUM` | 3.7 MB | ~330 B |
| gzipped JSON snapshot for the day | 1.8 MB | ~160 B |

`hermes-digger-maintenance.timer` (weekly, Sun 04:15) runs `maintain.py`:
snapshots every closed thread to `archive/YYYY/MM/*.json.gz`, nulls its comments'
`raw_json`, then `VACUUM`s. Threads close ~48 h after posting, so at most ~3 days
of comments sit at the fat size at once.

Annualised, megathreads only (~9k comments/day):

* **live DB** ≈ 1.1 GB/year + a ~100 MB working head
* **gz archive** ≈ 0.5 GB/year
* **backups** (7 daily gzips of the DB) ≈ up to ~1.5 GB once the DB is ~1 GB

~3 GB in year one; 2-3x that if `HERMES_KINDS` is widened to flaired posts.
Comfortable on the 35 GB box. **Without the weekly archival it would be
~11.5 GB/year of DB alone** -- so keep that timer enabled.

`maintain.py --prune-bodies DAYS` is the extra lever: it also nulls the comment
*text* for extracted comments older than DAYS (trend counts and entity rows
survive; `report.py --symbol` loses the quotes). Add it to the maintenance
unit's ExecStart only if the box actually gets tight.

### Relation to the old RSS pipeline

`mydigger.py` (RSS, `wsb_comments.db`, its own cron) is independent and can keep
running. Once this pipeline has a few full days on the box, retiring the old
cron is the call to make (`docs/PLAN.md` open items).

---

## 7. Hermes Agent + Telegram (Phase 8)

`mcp_server.py` is a stdio MCP server exposing the query layer as seven
read-only tools (`get_trend`, `get_symbol`, `get_sentiment`, `search_comments`,
`get_thread`, `list_threads`, `run_status`). Hermes Agent runs it as a
subprocess and its own model (DeepSeek) turns tool output into chat answers.

First, sanity-check the tools without Hermes:

```bash
sudo -u hermes /opt/hermes-digger/.venv/bin/python \
    /opt/hermes-digger/mcp_server.py --selftest
bash /opt/hermes-digger/scripts/ask-the-bot.sh      # the 4 acceptance scenarios
```

### 7a. Install Hermes Agent

As the `hermes` user (it installs into `~/.hermes` with its own venv; needs
Python 3.11, Node, ripgrep, ffmpeg, git):

```bash
sudo -u hermes -H bash -c 'curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash'
```

### 7b. Point it at DeepSeek

```bash
sudo -u hermes -H hermes    # opens a session
# in the session:
/model                      # pick "Custom endpoint": base https://api.deepseek.com/v1,
                            # model deepseek-v4-flash, key = your DEEPSEEK_API_KEY
```

(Same key as `.env`'s `DEEPSEEK_API_KEY`. The digger and Hermes can share it.)

### 7c. Telegram gateway + allowlist

```bash
sudo -u hermes -H hermes gateway setup     # paste the BotFather token; follow prompts
# DM the bot once to pair, then allowlist only your chat id and turn on
# command approval (the setup wizard covers both; see the Hermes docs for the
# exact prompts). Nobody outside the allowlist can use it.
```

### 7d. Register the MCP server

Merge `mcp/hermes-wsb.mcp-config.yaml` into `~/.hermes/mcp-config.yaml`:

```bash
sudo -u hermes -H bash -c 'cat /opt/hermes-digger/mcp/hermes-wsb.mcp-config.yaml >> ~/.hermes/mcp-config.yaml'
```

(or `hermes mcp add wsb --command /opt/hermes-digger/.venv/bin/python --args /opt/hermes-digger/mcp_server.py`).
Then in a Hermes session: `/reload-mcp`, and `/tools` should list the seven `wsb` tools.

### 7e. Keep the gateway running

```bash
sudo cp /opt/hermes-digger/systemd/hermes-agent.service /etc/systemd/system/
# check ExecStart matches where `hermes` actually landed (`which hermes` as the user)
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-agent.service
journalctl -u hermes-agent.service -f
```

### 7f. Acceptance -- ask the bot from Telegram

From your allowlisted chat:

1. *"what's trending on wsb"* -> `get_trend`
2. *"what's the sentiment on NVDA"* -> `get_sentiment`
3. *"summarise today's daily thread"* -> `get_thread`
4. *"is anyone bullish on oil right now?"* -> `search_comments` + `get_symbol`

Then send the same bot a message from a non-allowlisted account -> it must be
ignored. `bash scripts/healthcheck.sh` should still show the digger timer firing
and `enrich_*` untouched -- Hermes only reads.

