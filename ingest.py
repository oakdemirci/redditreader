#!/usr/bin/env python3
"""Pull r/wallstreetbets threads from Arctic Shift into the SQLite store.

This is the canonical, DB-first entry point (``wsb_tree.py`` is now a thin
compatibility wrapper that writes the old per-thread JSON files on top of this).

    python ingest.py --thread 1wbh9od                 # one thread, all comments
    python ingest.py --thread <url> --window 2026-09-09T00:00 2026-09-09T01:00
    python ingest.py --window 2026-09-08 2026-09-10   # discover + ingest a range
    python ingest.py --window 2026-09-08 2026-09-10 --kinds daily,moves
    python ingest.py --stats

Discovery classifies each post by title (megathreads) then flair:

    "Daily Discussion Thread for <date>"   -> daily
    "What Are Your Moves Tomorrow, <date>" -> moves
    "Weekend Discussion Thread ..."        -> weekend
    <link_flair_text>                      -> flair:<name>   (e.g. flair:gain)

Phase 1 discovery finds posts *created* inside the window; the hourly delta
digger (Phase 2) adds "megathreads from the last ~48h that are still open".
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import store
from arctic import Archive, ArchiveError, bare_id

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Megathreads are recognised by title, not flair: WSB gives the Daily Discussion,
# the "Moves Tomorrow" thread and the Weekend thread all the same flair.
MEGATHREADS: list[tuple[str, re.Pattern]] = [
    ("daily", re.compile(r"^\s*Daily Discussion Thread for\b", re.I)),
    ("moves", re.compile(r"^\s*What Are Your Moves Tomorrow\b", re.I)),
    ("weekend", re.compile(r"^\s*Weekend Discussion Thread\b", re.I)),
]
MEGA_KINDS = {kind for kind, _ in MEGATHREADS}
DEFAULT_KINDS = "daily,moves,weekend,gain,loss,discussion"


# --------------------------------------------------------------------------- #
#  helpers                                                                     #
# --------------------------------------------------------------------------- #
def parse_kinds(spec: str) -> set[str]:
    return {k.strip().lower() for k in spec.split(",") if k.strip()}


def classify(post: dict, wanted: set[str]) -> str | None:
    """Kind label if ``post`` is one the caller asked for, else None.

    Megathreads win over flair, so the Daily Discussion megathread is ``daily``
    and never ``flair:daily discussion``.
    """
    title = post.get("title") or ""
    for kind, pattern in MEGATHREADS:
        if kind in wanted and pattern.search(title):
            return kind
    flair = (post.get("link_flair_text") or "").strip()
    if flair and flair.lower() in {w for w in wanted if w not in MEGA_KINDS}:
        return f"flair:{flair.lower()}"
    return None


def classify_any(post: dict) -> str:
    """Best-effort kind for a directly-requested thread (no `wanted` filter)."""
    return classify(post, MEGA_KINDS | {(post.get("link_flair_text") or "").strip().lower()}) or "thread"


def parse_time(value: str) -> int:
    """Accept an epoch (seconds) or an ISO-8601 string; return epoch seconds.

    A bare date or a naive datetime is read as UTC.
    """
    value = value.strip()
    if re.fullmatch(r"\d{9,11}", value):
        return int(value)
    iso = value.replace(" ", "T")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", iso):
        iso += "T00:00:00"
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).replace(microsecond=0).isoformat()


def export_json(tree: dict, out_dir: Path) -> Path:
    """Write a thread's full tree as gzipped JSON under ``<out_dir>/YYYY/MM/``."""
    meta = tree["meta"]
    created = meta.get("created_utc") or 0
    when = datetime.fromtimestamp(created, timezone.utc) if created else datetime.now(timezone.utc)
    slug = meta["kind"].replace("flair:", "").replace(":", "-").replace(" ", "-")
    folder = out_dir / f"{when:%Y}" / f"{when:%m}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{meta['subreddit']}_{when:%Y%m%d}_{slug}_{meta['thread_id']}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(tree, fh, ensure_ascii=False, indent=2)
    return path


# --------------------------------------------------------------------------- #
#  ingest                                                                      #
# --------------------------------------------------------------------------- #
def ingest_thread(conn, arc: Archive, post: dict, kind: str, *,
                  md2html: bool = False, window: tuple[int, int] | None = None,
                  cap: int | None = None, export_dir: Path | None = None,
                  is_open: bool = True) -> dict:
    """Upsert one thread and its comments. Returns a small summary dict."""
    tid = bare_id(post["id"])
    title = (post.get("title") or "").strip()
    print(f"=> [{kind}] {tid}  {title[:70]}")

    store.upsert_thread(conn, kind, post, is_open=is_open)
    after, before = (window or (None, None))
    comments = arc.comments(tid, md2html=md2html, after=after, before=before, cap=cap)
    inserted, updated = store.upsert_comments(conn, tid, comments)

    reported = post.get("num_comments") or 0
    note = ""
    if window:
        note = f"  (window {_iso(after)}..{_iso(before)})"
    elif reported > len(comments) + 5:
        note = f"  (archive reports ~{reported}; may still be ingesting)"
    print(f"   {len(comments)} fetched: +{inserted} new, {updated} updated{note}")

    exported = None
    if export_dir is not None:
        exported = export_json(store.get_tree(conn, tid), export_dir)
        print(f"   archived -> {exported}")

    return {"thread_id": tid, "kind": kind, "fetched": len(comments),
            "inserted": inserted, "updated": updated, "exported": str(exported) if exported else None}


def ingest_window(conn, arc: Archive, subreddit: str, start: int, end: int,
                  kinds: set[str], *, md2html: bool = False,
                  comment_window: bool = False, export_dir: Path | None = None) -> dict:
    """Discover posts created in [start, end) and ingest the wanted ones.

    Records a ``runs`` row for the window. With ``comment_window`` only comments
    created in the same [start, end) are fetched (delta mode); otherwise each
    matched thread is fetched in full.
    """
    run_id = store.start_run(conn, _iso(start), _iso(end))
    total_new = 0
    threads = 0
    failures: list[str] = []
    try:
        matches: list[tuple[str, dict]] = []
        scanned = 0
        for post in arc.iter_posts(subreddit, start, end):
            scanned += 1
            kind = classify(post, kinds)
            if kind:
                matches.append((kind, post))
        print(f"scanned {scanned} posts in r/{subreddit} {_iso(start)}..{_iso(end)}; "
              f"{len(matches)} match {sorted(kinds)}")

        cw = (start, end) if comment_window else None
        for kind, post in sorted(matches, key=lambda kp: kp[1].get("created_utc") or 0):
            try:
                summary = ingest_thread(conn, arc, post, kind, md2html=md2html,
                                        window=cw, export_dir=export_dir)
            except ArchiveError as exc:
                # one thread failing (usually a 429 that outlasted the retries)
                # shouldn't cost the others; the next run picks it up.
                failures.append(f"{post['id']}: {exc}")
                print(f"   !! {post['id']} failed: {exc}")
                continue
            total_new += summary["inserted"]
            threads += 1

        status = "partial" if failures else "ok"
        store.finish_run(conn, run_id, status, n_threads=threads,
                         n_comments_new=total_new,
                         error="; ".join(failures) or None)
    except BaseException as exc:  # noqa: BLE001 - record, then re-raise
        store.finish_run(conn, run_id, "error", n_threads=threads,
                         n_comments_new=total_new, error=repr(exc))
        raise
    return {"run_id": run_id, "threads": threads, "new_comments": total_new,
            "failures": failures}


# --------------------------------------------------------------------------- #
#  CLI                                                                         #
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest r/wallstreetbets threads from Arctic Shift into SQLite.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default=str(store.DEFAULT_DB), help="SQLite path")
    parser.add_argument("--thread", metavar="ID_OR_URL",
                        help="ingest one thread (base-36 id, t3_ fullname, or URL)")
    parser.add_argument("--window", nargs=2, metavar=("START", "END"),
                        help="ISO-8601 or epoch bounds; with --thread limits which "
                             "comments are fetched, otherwise discovers posts "
                             "created in the range")
    parser.add_argument("--comment-window", action="store_true",
                        help="in discovery mode, also limit comment fetches to the "
                             "window (delta mode) instead of fetching each thread in full")
    parser.add_argument("--subreddit", default="wallstreetbets")
    parser.add_argument("--kinds", default=DEFAULT_KINDS,
                        help=f"comma list of megathread kinds and/or flair names "
                             f"(default: {DEFAULT_KINDS})")
    parser.add_argument("--export-json", metavar="DIR", type=Path,
                        help="also write each thread as gzipped JSON under DIR/YYYY/MM/")
    parser.add_argument("--md2html", action="store_true",
                        help="store bodies as rendered HTML instead of raw markdown")
    parser.add_argument("--max-comments", type=int, default=None, metavar="N")
    parser.add_argument("--min-interval", type=float, default=2.0, metavar="SEC")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--stats", action="store_true", help="print store stats and exit")
    args = parser.parse_args()

    conn = store.connect(args.db)

    if args.stats:
        print(json.dumps(store.stats(conn), indent=2))
        return

    arc = Archive(min_interval=args.min_interval, verbose=not args.quiet)
    window = None
    if args.window:
        window = (parse_time(args.window[0]), parse_time(args.window[1]))
        if window[0] >= window[1]:
            sys.exit("--window START must be before END")

    if args.thread:
        posts = arc.posts_by_id([args.thread])
        if not posts:
            sys.exit(f"thread {args.thread!r} not found in the archive")
        kind = classify_any(posts[0])
        ingest_thread(conn, arc, posts[0], kind, md2html=args.md2html,
                      window=window, cap=args.max_comments,
                      export_dir=args.export_json)
        return

    if not window:
        sys.exit("give --thread ID, or --window START END for discovery")

    result = ingest_window(conn, arc, args.subreddit, window[0], window[1],
                           parse_kinds(args.kinds), md2html=args.md2html,
                           comment_window=args.comment_window,
                           export_dir=args.export_json)
    print(f"\nrun {result['run_id']}: {result['threads']} thread(s), "
          f"+{result['new_comments']} new comment(s)")


if __name__ == "__main__":
    try:
        main()
    except (ArchiveError, KeyboardInterrupt) as exc:
        sys.exit(f"aborted: {exc}")
