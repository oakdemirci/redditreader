#!/usr/bin/env python3
"""
wsb_tree.py -- archive r/wallstreetbets threads as full comment trees, no API key.

Since Phase 1 this is a thin wrapper over the real modules:

    arctic.py   keyless Arctic Shift client (Reddit's own no-auth paths are gone)
    store.py    SQLite system of record (flat comments + parent_id -> tree on read)
    ingest.py   discovery + orchestration + the canonical CLI

It keeps the original UX -- discover threads over the last N days (or one
``--thread``), then drop one pretty-printed JSON file per thread into ``--out``.
Every thread also lands in the SQLite store (``--db``). For delta windows, cron
scheduling and stats use ``ingest.py`` directly; see ``docs/PLAN.md``.

    python wsb_tree.py                        # last 2 days, default kinds
    python wsb_tree.py --days 7 --kinds daily,moves,weekend
    python wsb_tree.py --thread 1wbh9od       # one thread: id, t3_id, or URL
    python wsb_tree.py --list                 # just show what would be fetched
    python wsb_tree.py --subreddit stocks --kinds daily
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import store
from arctic import Archive, ArchiveError, bare_id
from ingest import DEFAULT_KINDS, classify, classify_any, ingest_thread, parse_kinds

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _write_json(conn, thread_id: str, out_dir: Path) -> Path:
    tree = store.get_tree(conn, thread_id)
    meta = tree["meta"]
    created = meta.get("created_utc") or 0
    stamp = datetime.fromtimestamp(created, timezone.utc).strftime("%Y%m%d") if created else "nodate"
    slug = meta["kind"].replace("flair:", "").replace(":", "-").replace(" ", "-")
    path = out_dir / f"{meta['subreddit']}_{stamp}_{slug}_{meta['thread_id']}.json"
    path.write_text(json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Archive r/wallstreetbets threads as full comment trees, no API key.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--subreddit", default="wallstreetbets")
    parser.add_argument("--days", type=int, default=2,
                        help="discovery window in days back from now (default: 2)")
    parser.add_argument("--kinds", default=DEFAULT_KINDS,
                        help=f"megathread kinds daily/moves/weekend and/or flair names "
                             f"(default: {DEFAULT_KINDS})")
    parser.add_argument("--thread", metavar="ID_OR_URL",
                        help="fetch one thread and exit (id, t3_ fullname, or URL)")
    parser.add_argument("--out", default="wsb_data", type=Path,
                        help="directory for the per-thread JSON files (default: wsb_data)")
    parser.add_argument("--db", default=str(store.DEFAULT_DB), help="SQLite path")
    parser.add_argument("--md2html", action="store_true",
                        help="store bodies as rendered HTML instead of raw markdown")
    parser.add_argument("--max-comments", type=int, default=None, metavar="N")
    parser.add_argument("--min-interval", type=float, default=2.0, metavar="SEC")
    parser.add_argument("--list", action="store_true",
                        help="only print the threads that would be fetched")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    arc = Archive(min_interval=args.min_interval, verbose=not args.quiet)
    wanted = parse_kinds(args.kinds)

    if args.thread:
        posts = arc.posts_by_id([args.thread])
        if not posts:
            sys.exit(f"thread {args.thread!r} not found in the archive")
        targets = [(classify_any(posts[0]), posts[0])]
    else:
        now = datetime.now(timezone.utc)
        after = int((now - timedelta(days=args.days)).timestamp())
        before = int(now.timestamp())
        print(f"Scanning r/{args.subreddit} for the last {args.days} day(s); "
              f"kinds: {', '.join(sorted(wanted))}")
        targets = []
        scanned = 0
        for post in arc.iter_posts(args.subreddit, after, before):
            scanned += 1
            kind = classify(post, wanted)
            if kind:
                targets.append((kind, post))
        targets.sort(key=lambda kp: kp[1].get("created_utc") or 0, reverse=True)
        print(f"  {scanned} posts scanned, {len(targets)} match")

    if not targets:
        print("Nothing to fetch.")
        return

    if args.list:
        for kind, post in targets:
            when = datetime.fromtimestamp(post.get("created_utc") or 0, timezone.utc)
            print(f"  {when:%Y-%m-%d %H:%M}  [{kind:<16}] {post['id']}  "
                  f"{(post.get('title') or '')[:70]}")
        return

    args.out.mkdir(parents=True, exist_ok=True)
    conn = store.connect(args.db)

    failures = 0
    for kind, post in targets:
        try:
            ingest_thread(conn, arc, post, kind, md2html=args.md2html,
                          cap=args.max_comments)
            path = _write_json(conn, post["id"], args.out)
            print(f"   -> {path.name}")
        except (ArchiveError, OSError) as exc:
            failures += 1
            print(f"   !! failed: {exc}")

    print(f"\nDone: {len(targets) - failures}/{len(targets)} thread(s) -> {args.out}/ "
          f"(+ {args.db})")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
