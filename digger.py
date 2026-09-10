#!/usr/bin/env python3
"""Hourly delta digger -- the automatic ingest job.

Every hour it advances two watermarks:

* ``meta.discovery_through`` -- how far the front-page scan for *new* threads
  (megathreads by title, plus the configured flairs) has reached.
* ``threads.comments_through`` (per thread) -- how far that thread's comments
  have been confirmed fetched.

Work is done in **hourly slices**. A healthy run processes exactly one slice
``[floor(now)-1h, floor(now))``. After downtime the slices pending since the last
watermark are replayed oldest-first (``--catch-up``), each committing its own
progress, so a crash mid-catch-up simply resumes.

A per-thread fetch failure does **not** advance that thread's watermark, so the
gap is re-fetched next run (Arctic Shift ``after``/``before`` + the idempotent
upsert make the overlap harmless). Only a failure in the discovery scan itself
holds a slice back.

    python digger.py                     # process the one pending hour (warn if behind)
    python digger.py --catch-up          # drain every pending hour
    python digger.py --catch-up --max-hours 12
    python digger.py --since 2026-09-08T00:00   # re-process from an explicit point
    python ingest.py --stats             # inspect watermarks / last run

Runs under a stale-aware lock (``digger.lock``) so an overlapping timer tick
can't collide with a slow run. See ``systemd/`` for the timer unit and
``docs/PLAN.md`` for the wider plan.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import enrich_entities
import extract
import llm
import store
import tickers
from arctic import Archive, ArchiveError
from ingest import DEFAULT_KINDS, classify, parse_kinds, parse_time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_LOOKBACK_HOURS = 48   # fresh DB: start this far back (covers live megathreads)
CLOSE_MAX_AGE_HOURS = 48      # a thread older than this...
CLOSE_QUIET_HOURS = 12        # ...with nothing newer than this is closed
LOCK_PATH = store.DEFAULT_DB.parent / "digger.lock"
LOCK_STALE_SECONDS = 60 * 60


# --------------------------------------------------------------------------- #
#  time helpers                                                                #
# --------------------------------------------------------------------------- #
def _floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).replace(microsecond=0).isoformat()


def _epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp())


def hourly_slices(start: int, end: int):
    while start < end:
        yield start, min(start + 3600, end)
        start += 3600


def compute_start(conn, now: datetime) -> int:
    """Epoch to resume the discovery scan from."""
    wm = store.get_meta(conn, "discovery_through")
    if wm:
        return _epoch(wm)
    last = store.last_successful_end(conn)
    if last:
        return _epoch(last)
    return int((_floor_hour(now) - timedelta(hours=DEFAULT_LOOKBACK_HOURS)).timestamp())


# --------------------------------------------------------------------------- #
#  lock                                                                        #
# --------------------------------------------------------------------------- #
def acquire_lock() -> bool:
    if LOCK_PATH.exists():
        if time.time() - LOCK_PATH.stat().st_mtime < LOCK_STALE_SECONDS:
            return False
        LOCK_PATH.unlink(missing_ok=True)  # previous run crashed without cleaning up
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def release_lock() -> None:
    LOCK_PATH.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
#  one slice                                                                   #
# --------------------------------------------------------------------------- #
def process_slice(conn, arc: Archive, subreddit: str, kinds: set[str],
                  start: int, end: int, log, known_symbols: set[str] | None = None,
                  archive_dir=None, use_llm: bool = False) -> str:
    run_id = store.start_run(conn, _iso(start), _iso(end))
    discovered = 0
    fetched_threads = 0
    new_comments = 0
    failures: list[str] = []
    t0 = time.monotonic()
    try:
        # 1. discover threads created in this hour. The -1 second of overlap
        #    (here and in the comment fetch below) means a row landing exactly on
        #    a slice boundary can't fall through the crack between `before`
        #    (exclusive client-side) and the next slice's `after` (exclusive);
        #    the idempotent upserts absorb the re-read.
        for post in arc.iter_posts(subreddit, start - 1, end):
            kind = classify(post, kinds)
            if kind:
                store.upsert_thread(conn, kind, post, is_open=True)
                discovered += 1
                log(f"discovered kind={kind} id={post['id']} "
                    f"title={(post.get('title') or '')[:60]!r}")
        store.set_meta(conn, "discovery_through", _iso(end))

        # 2. one windowed sweep of the whole subreddit's comments in [start, end),
        #    bucketed to the threads we track. One query covers every open thread
        #    and avoids the per-thread `link_id` form that Arctic Shift 422s.
        #    (A thread discovered here with older history gets it via `--backfill`,
        #     not from a normal hourly slice.)
        open_threads = {t["id"]: t for t in store.open_threads(conn)}
        if open_threads:
            try:
                by_thread = arc.comments_in_window(
                    subreddit, start - 1, end, link_ids=set(open_threads)
                )
            except ArchiveError as exc:
                failures.append(f"comment sweep: {exc}")
                log(f"comment sweep {_iso(start)}..{_iso(end)} status=error err={exc!r}")
                by_thread = {}

            for tid, comments in by_thread.items():
                kind = open_threads[tid]["kind"] if tid in open_threads else "?"
                inserted, updated = store.upsert_comments(conn, tid, comments)
                store.set_comments_through(conn, tid, end)
                fetched_threads += 1
                new_comments += inserted
                log(f"thread id={tid} kind={kind} fetched={len(comments)} "
                    f"new={inserted} upd={updated}")
            # threads with nothing new this window still advance their watermark
            for tid in open_threads.keys() - by_thread.keys():
                store.set_comments_through(conn, tid, end)

        # 3. regex ticker/coin extraction over the comments just added, then
        #    (opt-in, budget-capped) the LLM disambiguation pass. Enrichment is
        #    best-effort -- the comments are already stored, so a failure here
        #    must not fail the slice.
        if known_symbols is not None:
            try:
                scanned = extract.extract_pending(conn, known_symbols, verbose=False)
                if scanned:
                    log(f"extracted entities from {scanned} new comment(s)")
            except Exception as exc:  # noqa: BLE001
                log(f"regex extraction failed (non-fatal): {exc!r}")
        if use_llm and llm.available():
            try:
                r = enrich_entities.enrich_pending(conn, known_symbols or set(), verbose=False)
                if r["scanned"]:
                    log(f"llm entities: {r['scanned']} scanned, {r['calls']} call(s)"
                        + (" (budget hit)" if r["skipped_budget"] else ""))
            except Exception as exc:  # noqa: BLE001
                log(f"llm entity pass failed (non-fatal): {exc!r}")

        # 4. retire threads that are old and quiet, then snapshot + shrink them
        closed = store.close_stale_threads(
            conn, end, max_age_hours=CLOSE_MAX_AGE_HOURS, quiet_hours=CLOSE_QUIET_HOURS
        )
        for cid in closed:
            log(f"closed id={cid}")
        if archive_dir is not None:
            for tid in store.pending_archive(conn):
                path = store.archive_thread(conn, tid, archive_dir)
                if path:
                    log(f"archived id={tid} -> {path}")

        status = "partial" if failures else "ok"
        store.finish_run(conn, run_id, status, n_threads=fetched_threads,
                         n_comments_new=new_comments, error="; ".join(failures) or None)
    except BaseException as exc:  # noqa: BLE001 - record, then re-raise
        store.finish_run(conn, run_id, "error", n_threads=fetched_threads,
                         n_comments_new=new_comments, error=repr(exc))
        log(f"slice {_iso(start)}..{_iso(end)} status=error "
            f"dur={time.monotonic() - t0:.0f}s err={exc!r}")
        raise

    log(f"slice {_iso(start)}..{_iso(end)} status={status} discovered={discovered} "
        f"threads={fetched_threads} new_comments={new_comments} closed={len(closed)} "
        f"dur={time.monotonic() - t0:.0f}s")
    return status


# --------------------------------------------------------------------------- #
#  driver                                                                      #
# --------------------------------------------------------------------------- #
def run(conn, arc: Archive, subreddit: str, kinds: set[str], *,
        now: datetime | None = None, catch_up: bool = False,
        max_hours: int | None = None, since: int | None = None,
        backfill_days: int | None = None, archive_dir=None, use_llm: bool = False,
        log=print) -> int:
    now = now or datetime.now(timezone.utc)
    target = int(_floor_hour(now).timestamp())
    known = tickers.load_known_stock_symbols()

    if backfill_days:
        # day-by-day slices: each is one bounded comment sweep (~1 day of the
        # subreddit) that commits its own progress, so a Ctrl-C resumes and the
        # API is never hit with a giant range. Leaves watermarks at `target`.
        start = target - backfill_days * 86400
        slices = [(s, min(s + 86400, target)) for s in range(start, target, 86400)]
        log(f"backfill {_iso(start)}..{_iso(target)} ({backfill_days}d, "
            f"{len(slices)} daily slices)")
        for s, e in slices:
            process_slice(conn, arc, subreddit, kinds, s, e, log,
                          known_symbols=known, archive_dir=archive_dir, use_llm=use_llm)
        return len(slices)

    start = since if since is not None else compute_start(conn, now)

    if start >= target:
        log(f"up to date (watermark {_iso(start)} >= {_iso(target)})")
        return 0

    pending = list(hourly_slices(start, target))
    if not catch_up and len(pending) > 1:
        log(f"WARNING: {len(pending)} hours pending since {_iso(start)}; "
            f"processing 1 (run with --catch-up to drain)")
        pending = pending[:1]
    elif max_hours:
        pending = pending[:max_hours]

    for slice_start, slice_end in pending:
        process_slice(conn, arc, subreddit, kinds, slice_start, slice_end, log,
                      known_symbols=known, archive_dir=archive_dir, use_llm=use_llm)
    return len(pending)


def _make_logger():
    def log(msg: str) -> None:
        print(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)
    return log


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Hourly delta digger for r/wallstreetbets (Arctic Shift -> SQLite).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Defaults for --db/--subreddit/--kinds come from HERMES_DB / "
               "HERMES_SUBREDDIT / HERMES_KINDS when set (systemd EnvironmentFile).",
    )
    parser.add_argument("--db", default=os.environ.get("HERMES_DB", str(store.DEFAULT_DB)))
    parser.add_argument("--subreddit",
                        default=os.environ.get("HERMES_SUBREDDIT", "wallstreetbets"))
    parser.add_argument("--kinds", default=os.environ.get("HERMES_KINDS", DEFAULT_KINDS),
                        help=f"megathread kinds and/or flair names (default: {DEFAULT_KINDS})")
    parser.add_argument("--catch-up", action="store_true",
                        help="process every pending hour, not just the oldest one")
    parser.add_argument("--max-hours", type=int, default=None, metavar="N",
                        help="with --catch-up, stop after N slices this invocation")
    parser.add_argument("--since", metavar="TS",
                        help="ignore the stored watermark and start here (ISO or epoch)")
    parser.add_argument("--backfill", type=int, default=None, metavar="DAYS",
                        help="one-shot: sweep the last DAYS days in a single pass, then "
                             "leave the watermarks at now for the hourly timer")
    parser.add_argument("--archive-dir",
                        default=os.environ.get("HERMES_ARCHIVE_DIR"),
                        help="gzipped JSON snapshot dir for closed threads "
                             "(default: <db dir>/archive; also HERMES_ARCHIVE_DIR). "
                             "'none' disables archival")
    parser.add_argument("--min-interval", type=float, default=2.0, metavar="SEC")
    parser.add_argument("--no-llm", action="store_true",
                        help="skip the LLM entity pass even if DEEPSEEK_API_KEY is set")
    parser.add_argument("--ignore-lock", action="store_true",
                        help="run even if digger.lock is held (use only when sure)")
    args = parser.parse_args()

    log = _make_logger()
    if not args.ignore_lock and not acquire_lock():
        log("another digger run holds the lock; exiting")
        return

    try:
        conn = store.connect(args.db)
        arc = Archive(min_interval=args.min_interval, verbose=False)
        since = parse_time(args.since) if args.since else None

        archive_dir = args.archive_dir
        if archive_dir is None:
            archive_dir = str(Path(args.db).resolve().parent / "archive")
        elif archive_dir.lower() == "none":
            archive_dir = None

        n = run(conn, arc, args.subreddit, parse_kinds(args.kinds),
                catch_up=args.catch_up, max_hours=args.max_hours, since=since,
                backfill_days=args.backfill, archive_dir=archive_dir,
                use_llm=not args.no_llm, log=log)
        log(f"done ({n} slice(s))")
        conn.close()
    finally:
        if not args.ignore_lock:
            release_lock()


if __name__ == "__main__":
    try:
        main()
    except (ArchiveError, KeyboardInterrupt) as exc:
        sys.exit(f"aborted: {exc}")
