#!/usr/bin/env python3
"""Retention & housekeeping for the digger store.

    python maintain.py                       # archive closed threads + VACUUM
    python maintain.py --prune-bodies 120    # also null body text older than 120d
    python maintain.py --no-vacuum
    python maintain.py --report              # just print the size accounting

Steps, in order:
  1. archive  -- snapshot every closed-but-unarchived thread to gzipped JSON
     (``<db dir>/archive/YYYY/MM/``), then null its comments' raw_json.
  2. prune    -- (opt-in) null body + raw_json for comments older than N days
     that have already been through regex extraction. Trends and entity counts
     survive; the drill-down text does not.
  3. vacuum   -- reclaim the freed pages.

Wired to a weekly systemd timer by scripts/install.sh.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import store

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default=os.environ.get("HERMES_DB", str(store.DEFAULT_DB)))
    p.add_argument("--archive-dir", default=os.environ.get("HERMES_ARCHIVE_DIR"),
                   help="default: <db dir>/archive")
    p.add_argument("--prune-bodies", type=int, default=None, metavar="DAYS",
                   help="null body text for extracted comments older than DAYS")
    p.add_argument("--no-vacuum", action="store_true")
    p.add_argument("--report", action="store_true", help="print size accounting and exit")
    args = p.parse_args()

    conn = store.connect(args.db)

    if args.report:
        print(json.dumps(store.db_size_report(conn), indent=2))
        return

    before = store.db_size_report(conn)["bytes"]

    archive_dir = args.archive_dir or str(Path(args.db).resolve().parent / "archive")
    pending = store.pending_archive(conn)
    for tid in pending:
        path = store.archive_thread(conn, tid, archive_dir)
        print(f"archived {tid} -> {path}")
    print(f"archived {len(pending)} thread(s)")

    if args.prune_bodies is not None:
        n = store.prune_bodies(conn, args.prune_bodies)
        print(f"pruned body text from {n} comment(s) older than {args.prune_bodies}d")

    if not args.no_vacuum:
        store.vacuum(conn)
        after = store.db_size_report(conn)["bytes"]
        print(f"vacuum: {before / 1e6:.1f} MB -> {after / 1e6:.1f} MB")

    print(json.dumps(store.db_size_report(conn), indent=2))


if __name__ == "__main__":
    main()
