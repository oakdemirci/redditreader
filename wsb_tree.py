#!/usr/bin/env python3
"""
wsb_tree.py -- archive r/wallstreetbets threads as full comment trees, no API key.

Why this exists
---------------
Reddit has closed every no-auth path this project used to rely on:

  * script-app registration is disabled, so there is no way to get a new API key;
  * ``www.reddit.com/....json`` now returns 403 for anonymous clients;
  * ``old.reddit.com`` 302-redirects anonymous clients to a login page.

Only the RSS/Atom feeds still work without a login, and those expose at most the
newest ~100 comments of a thread, with no reply structure, no scores, and no way
to page backwards.  (``mydigger.py`` already squeezes what it can out of RSS.)

This script takes a different route: it reads the **Arctic Shift** archive at
https://arctic-shift.photon-reddit.com -- a public, key-less mirror of Reddit's
data that ingests continuously and normally stays within minutes-to-an-hour of
live.  Every field Reddit exposes on a post or comment is preserved verbatim.

What it collects
----------------
Megathreads, classified from their title:

  * ``Daily Discussion Thread for <date>``      -> kind ``daily``
  * ``What Are Your Moves Tomorrow, <date>``    -> kind ``moves``
  * ``Weekend Discussion Thread ...``           -> kind ``weekend``

Flaired standalone posts (default: Gain / Loss / Discussion; ``--kinds`` to
change).  A "Discussion"-flaired post is only kept when it is *not* one of the
megathreads above.

For every selected post it then downloads **all** comments -- including the
``[deleted]`` / ``[removed]`` placeholders Reddit keeps -- by paging through the
archive oldest-first, and rebuilds the reply tree locally from each comment's
``parent_id``.  Rebuilding from the flat list (rather than the archive's own
``/comments/tree`` endpoint) has no 25k-comment ceiling and never leaves
"load more comments" stubs.

Output
------
One JSON file per thread under ``--out`` (default ``./wsb_data``)::

    {
      "meta":   { thread_id, kind, permalink, comment_count, ... },
      "post":   { <the complete submission object> },
      "comments": [ { ...comment..., "replies": [ {...}, ... ] }, ... ],
      "orphaned_comments": [ ... ]     # replies whose parent isn't in the archive
    }

With ``--sqlite`` it also appends every post and comment (as JSON blobs, plus a
few indexed columns) to ``<out>/wsb_tree.db``.

Usage
-----
    python wsb_tree.py                        # yesterday+today, default kinds
    python wsb_tree.py --days 7               # widen the discovery window
    python wsb_tree.py --kinds daily,moves    # megathreads only
    python wsb_tree.py --kinds gain,loss,yolo,dd     # any set of flair names
    python wsb_tree.py --thread 1wbh9od       # one thread: id, t3_id, or full URL
    python wsb_tree.py --subreddit stocks --kinds daily     # other subreddits
    python wsb_tree.py --list                 # just show what would be fetched
    python wsb_tree.py --sqlite --md2html     # also write the DB; bodies as HTML

Only dependency: ``requests`` (already in requirements.txt).
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

API_ROOT = "https://arctic-shift.photon-reddit.com/api"
USER_AGENT = "wsb-tree/1.0 (personal research archive; contact via repo owner)"

PAGE = 100  # Arctic Shift's max `limit` for the search endpoints

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
#  Arctic Shift client                                                         #
# --------------------------------------------------------------------------- #
class Archive:
    """Thin, polite client for the Arctic Shift REST API.

    The service throttles two ways: a hard HTTP 429 with an ``X-RateLimit-Reset``
    header, and a soft ``200 OK`` whose body is ``{"data": null, "error":
    "Timeout. Maybe slow down a bit"}`` when a query is too heavy or too frequent.
    Both are retried here with exponential backoff, and a soft hit also widens the
    minimum spacing between requests for the rest of the run.
    """

    def __init__(self, min_interval: float = 2.0, max_retries: int = 8,
                 verbose: bool = True):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.verbose = verbose
        self._last_request = 0.0

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"   . {msg}")

    def _pace(self) -> None:
        gap = time.monotonic() - self._last_request
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)

    def get(self, path: str, **params) -> object:
        """GET ``/api/<path>`` and return the decoded ``data`` field.

        Returns ``None`` or ``[]`` when the archive legitimately has no result;
        raises ``RuntimeError`` on a hard error or after exhausting retries.
        """
        params = {k: v for k, v in params.items() if v is not None}
        url = f"{API_ROOT}/{path}"
        backoff = 5.0

        for attempt in range(1, self.max_retries + 1):
            self._pace()
            try:
                resp = self.session.get(url, params=params, timeout=90)
                self._last_request = time.monotonic()
            except requests.RequestException as exc:
                self._log(f"network error: {exc} -- retry {attempt}/{self.max_retries} in {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 180)
                continue

            if resp.status_code == 429:
                reset = resp.headers.get("X-RateLimit-Reset", "")
                wait = float(reset) + 1 if _looks_numeric(reset) else backoff
                self._log(f"429 rate limited -- waiting {wait:.0f}s")
                time.sleep(wait)
                backoff = min(backoff * 2, 180)
                continue

            if resp.status_code >= 500:
                self._log(f"HTTP {resp.status_code} -- retry {attempt}/{self.max_retries} in {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 180)
                continue

            resp.raise_for_status()
            body = resp.json()

            if isinstance(body, dict) and body.get("data") is None and body.get("error"):
                err = str(body["error"])
                if "slow down" in err.lower() or "timeout" in err.lower():
                    self.min_interval = min(self.min_interval + 1.0, 20.0)
                    self._log(f'archive: "{err}" -- easing off to {self.min_interval:.0f}s spacing, '
                              f"retry {attempt}/{self.max_retries} in {backoff:.0f}s")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 180)
                    continue
                raise RuntimeError(f"{path}: {err} (params={params})")

            return body.get("data") if isinstance(body, dict) else body

        raise RuntimeError(f"{path}: gave up after {self.max_retries} attempts (params={params})")

    # -- higher-level helpers --------------------------------------------- #
    def posts_by_id(self, *ids: str) -> list[dict]:
        return self.get("posts/ids", ids=",".join(_bare_id(i) for i in ids)) or []

    def iter_posts(self, subreddit: str, after: int, before: int):
        """Yield every post in ``subreddit`` created in [after, before], oldest
        first.  Pages on ``created_utc``; overlaps each page by a second and
        de-dupes so a burst of posts sharing one timestamp can't be split."""
        seen: set[str] = set()
        cursor = after
        while True:
            batch = self.get("posts/search", subreddit=subreddit, after=cursor,
                             before=before, sort="asc", limit=PAGE)
            if not batch:
                return
            fresh = [p for p in batch if p["id"] not in seen]
            for post in fresh:
                seen.add(post["id"])
                yield post
            if len(batch) < PAGE:
                return
            newest = batch[-1]["created_utc"]
            # step back one second so items sharing `newest` aren't skipped;
            # if the whole page was one second, step forward to avoid a stall.
            cursor = newest + 1 if not fresh else newest - 1

    def all_comments(self, link_id: str, md2html: bool,
                     cap: int | None = None) -> list[dict]:
        """Every comment on a submission, oldest first, de-duped by id."""
        link_id = _bare_id(link_id)
        collected: dict[str, dict] = {}
        cursor: int | None = None
        while True:
            batch = self.get("comments/search", link_id=link_id, after=cursor,
                             sort="asc", limit=PAGE, md2html=_yn(md2html))
            if not batch:
                break
            added = 0
            newest = cursor or 0
            for comment in batch:
                newest = max(newest, comment.get("created_utc") or 0)
                if comment["id"] not in collected:
                    collected[comment["id"]] = comment
                    added += 1
            self._log(f"{len(collected)} comments so far")
            if cap and len(collected) >= cap:
                self._log(f"stopping at --max-comments={cap}")
                break
            if len(batch) < PAGE:
                break
            if added == 0:
                # a full page entirely within one already-seen second: the only
                # way forward is to skip past it (>100 comments in one second is
                # rare even on WSB; note it if it happens).
                self._log(f"WARNING: >{PAGE} comments at t={newest}; some may be skipped")
                cursor = newest + 1
            else:
                cursor = newest - 1
        return list(collected.values())


# --------------------------------------------------------------------------- #
#  helpers                                                                     #
# --------------------------------------------------------------------------- #
def _looks_numeric(text: str) -> bool:
    return bool(text) and text.replace(".", "", 1).isdigit()


def _yn(flag: bool) -> str:
    return "true" if flag else "false"


def _bare_id(value: str) -> str:
    """``t3_1wbh9od`` / ``1wbh9od`` / a full permalink -> ``1wbh9od``."""
    match = re.search(r"/comments/([a-z0-9]+)", value, re.I)
    if match:
        return match.group(1)
    return value.rsplit("_", 1)[-1].strip("/")


def classify(post: dict, wanted: set[str]) -> str | None:
    """Return the kind label if ``post`` is one the caller asked for, else None.

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


def build_tree(post_id: str, comments: list[dict]) -> tuple[list[dict], list[dict]]:
    """Rebuild the reply tree from flat comments.

    Each returned comment gains a ``replies`` list.  ``(roots, orphans)`` where
    an orphan is a comment whose ``t1_`` parent isn't in ``comments`` (its parent
    was deleted and pruned by Reddit, or falls outside a capped fetch).
    """
    post_fullname = "t3_" + _bare_id(post_id)
    nodes: dict[str, dict] = {}
    for comment in comments:
        node = dict(comment)
        node["replies"] = []
        nodes[node["id"]] = node

    roots: list[dict] = []
    orphans: list[dict] = []
    for node in nodes.values():
        parent = node.get("parent_id") or ""
        if parent == post_fullname or parent.startswith("t3_"):
            roots.append(node)
        elif parent.startswith("t1_") and parent[3:] in nodes:
            nodes[parent[3:]]["replies"].append(node)
        else:
            orphans.append(node)

    def sort_recursive(level: list[dict]) -> None:
        level.sort(key=lambda n: n.get("created_utc") or 0)
        for node in level:
            sort_recursive(node["replies"])

    sort_recursive(roots)
    sort_recursive(orphans)
    return roots, orphans


def count_tree(level: list[dict]) -> int:
    return sum(1 + count_tree(node["replies"]) for node in level)


# --------------------------------------------------------------------------- #
#  optional SQLite sink                                                        #
# --------------------------------------------------------------------------- #
def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS posts (
               id TEXT PRIMARY KEY, subreddit TEXT, kind TEXT, created_utc INTEGER,
               title TEXT, permalink TEXT, fetched_at TEXT, data TEXT NOT NULL)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS comments (
               id TEXT PRIMARY KEY, link_id TEXT NOT NULL, parent_id TEXT,
               author TEXT, created_utc INTEGER, score INTEGER,
               fetched_at TEXT, data TEXT NOT NULL)"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS comments_link ON comments(link_id)")
    conn.commit()
    return conn


def store_db(conn: sqlite3.Connection, kind: str, post: dict,
             comments: list[dict], fetched_at: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO posts VALUES (?,?,?,?,?,?,?,?)",
        (post["id"], post.get("subreddit"), kind, post.get("created_utc"),
         post.get("title"), post.get("permalink"), fetched_at,
         json.dumps(post, ensure_ascii=False)),
    )
    conn.executemany(
        "INSERT OR REPLACE INTO comments VALUES (?,?,?,?,?,?,?,?)",
        [
            (c["id"], _bare_id(c.get("link_id") or post["id"]), c.get("parent_id"),
             c.get("author"), c.get("created_utc"), c.get("score"), fetched_at,
             json.dumps(c, ensure_ascii=False))
            for c in comments
        ],
    )
    conn.commit()


# --------------------------------------------------------------------------- #
#  per-thread harvest                                                          #
# --------------------------------------------------------------------------- #
def harvest(arc: Archive, kind: str, post: dict, out_dir: Path,
            db: sqlite3.Connection | None, args: argparse.Namespace) -> None:
    pid = post["id"]
    title = (post.get("title") or "").strip()
    print(f"\n=> [{kind}] {pid}  {title[:70]}")

    comments = arc.all_comments(pid, args.md2html, args.max_comments)
    roots, orphans = build_tree(pid, comments)
    fetched_at = datetime.now(timezone.utc).isoformat()

    permalink = post.get("permalink") or f"/r/{post.get('subreddit','')}/comments/{pid}/"
    document = {
        "meta": {
            "source": "arctic-shift",
            "api_root": API_ROOT,
            "fetched_at": fetched_at,
            "subreddit": post.get("subreddit"),
            "kind": kind,
            "thread_id": pid,
            "thread_fullname": "t3_" + pid,
            "title": title,
            "permalink": "https://www.reddit.com" + permalink,
            "created_utc": post.get("created_utc"),
            "reddit_num_comments": post.get("num_comments"),
            "downloaded_comments": len(comments),
            "tree_top_level": len(roots),
            "orphaned_comments": len(orphans),
            "body_format": "html" if args.md2html else "markdown",
        },
        "post": post,
        "comments": roots,
        "orphaned_comments": orphans,
    }

    created = post.get("created_utc") or 0
    stamp = datetime.fromtimestamp(created, timezone.utc).strftime("%Y%m%d") if created else "nodate"
    slug = kind.replace("flair:", "").replace(":", "-").replace(" ", "-")
    path = out_dir / f"{post.get('subreddit','sub')}_{stamp}_{slug}_{pid}.json"
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")

    # The archive's own num_comments lags (scores/counts settle ~36h after
    # posting), so only flag it when it claims meaningfully more than we pulled
    # -- that's the case worth investigating (a capped run, or archive lag).
    reported = post.get("num_comments") or 0
    gap = f"  (archive expects ~{reported}; may still be ingesting)" if reported > len(comments) + 5 else ""
    print(f"   {len(comments)} comments, {len(orphans)} orphaned{gap}")
    print(f"   -> {path.name}")

    if db is not None:
        store_db(db, kind, post, comments, fetched_at)


# --------------------------------------------------------------------------- #
#  discovery + entrypoint                                                      #
# --------------------------------------------------------------------------- #
def discover(arc: Archive, args: argparse.Namespace) -> list[tuple[str, dict]]:
    wanted = {k.strip().lower() for k in args.kinds.split(",") if k.strip()}
    now = datetime.now(timezone.utc)
    before = int(now.timestamp())
    after = int((now - timedelta(days=args.days)).timestamp())

    print(f"Scanning r/{args.subreddit} for the last {args.days} day(s); "
          f"kinds: {', '.join(sorted(wanted))}")
    targets: list[tuple[str, dict]] = []
    scanned = 0
    for post in arc.iter_posts(args.subreddit, after, before):
        scanned += 1
        kind = classify(post, wanted)
        if kind:
            targets.append((kind, post))
    print(f"  {scanned} posts scanned, {len(targets)} match")
    # newest first, and de-dupe should a post somehow appear twice
    seen: set[str] = set()
    unique: list[tuple[str, dict]] = []
    for kind, post in sorted(targets, key=lambda kp: kp[1].get("created_utc") or 0, reverse=True):
        if post["id"] not in seen:
            seen.add(post["id"])
            unique.append((kind, post))
    return unique


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Archive r/wallstreetbets threads as full comment trees, no API key.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--subreddit", default="wallstreetbets",
                        help="subreddit to scan (default: wallstreetbets)")
    parser.add_argument("--days", type=int, default=2,
                        help="discovery window in days back from now (default: 2 -- "
                             "today plus yesterday's overnight thread)")
    parser.add_argument("--kinds", default=DEFAULT_KINDS,
                        help=f"comma list; megathread kinds daily/moves/weekend plus any "
                             f"flair name, e.g. gain,loss,discussion,dd,yolo "
                             f"(default: {DEFAULT_KINDS})")
    parser.add_argument("--thread", metavar="ID_OR_URL",
                        help="fetch one thread and exit; accepts a base-36 id, a t3_ "
                             "fullname, or a full reddit URL")
    parser.add_argument("--out", default="wsb_data", type=Path,
                        help="output directory for the JSON files (default: wsb_data)")
    parser.add_argument("--sqlite", action="store_true",
                        help="also append posts and comments to <out>/wsb_tree.db")
    parser.add_argument("--md2html", action="store_true",
                        help="store comment/selftext bodies as rendered HTML instead of "
                             "raw markdown")
    parser.add_argument("--max-comments", type=int, default=None, metavar="N",
                        help="stop downloading a thread's comments after N (default: no cap)")
    parser.add_argument("--min-interval", type=float, default=2.0, metavar="SEC",
                        help="minimum seconds between archive requests (default: 2.0; the "
                             "client raises this on its own if throttled)")
    parser.add_argument("--list", action="store_true",
                        help="only print the threads that would be fetched")
    parser.add_argument("--quiet", action="store_true", help="less progress output")
    args = parser.parse_args()

    arc = Archive(min_interval=args.min_interval, verbose=not args.quiet)

    if args.thread:
        posts = arc.posts_by_id(args.thread)
        if not posts:
            sys.exit(f"thread {args.thread!r} not found in the archive")
        wanted = {k.strip().lower() for k in args.kinds.split(",") if k.strip()}
        kind = classify(posts[0], wanted) or "thread"
        targets = [(kind, posts[0])]
    else:
        targets = discover(arc, args)

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
    db = open_db(args.out / "wsb_tree.db") if args.sqlite else None

    failures = 0
    for kind, post in targets:
        try:
            harvest(arc, kind, post, args.out, db, args)
        except (RuntimeError, requests.RequestException) as exc:
            failures += 1
            print(f"   !! failed: {exc}")

    if db is not None:
        db.close()
    print(f"\nDone: {len(targets) - failures}/{len(targets)} thread(s) written to {args.out}/")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
