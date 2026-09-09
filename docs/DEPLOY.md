# Deploying the digger to Hetzner (Phase 4)

Interim runbook: get `digger.py` + `trend.py` running on the box so real data
accumulates while Phases 5-8 are built. No secrets required yet -- Arctic Shift
is keyless. The full Hermes Agent / Telegram wiring is Phase 8.

Target: a small Debian/Ubuntu VM. Disk need is modest (~5-6 GB/year for the DB,
~2 GB/year for gzipped backups -- see `docs/PLAN.md`).

---

## 1. Get the code on the box

```bash
sudo mkdir -p /opt/hermes-digger
sudo chown "$USER" /opt/hermes-digger
git clone git@github.com:oakdemirci/redditreader.git /opt/hermes-digger
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

Review `/opt/hermes-digger/.env` -- especially `HERMES_KINDS` (megathreads only
by default; add `gain,loss,discussion` to also track flaired standalone posts).

## 3. Backfill initial history

One-shot, runs as the `hermes` user. A day of history is a few minutes
(discovery sweep + a full fetch of each thread); 7 days ~= 20-40 min.

```bash
sudo -u hermes /opt/hermes-digger/.venv/bin/python \
    /opt/hermes-digger/digger.py --backfill 7
```

It leaves the watermarks at "now", so the hourly timer takes over cleanly.

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
