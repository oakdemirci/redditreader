"""SQLite persistence for the digger -- the system of record.

Design (see ``docs/PLAN.md``):

* Comments are stored **flat**, each keeping Reddit's verbatim ``parent_id``
  (``t3_<thread>`` for a top-level comment, ``t1_<comment>`` for a reply). The
  reply tree is reconstructed on read by :func:`get_tree` -- no 25k-comment
  ceiling and no "load more" stubs.
* ``comments.raw_json`` holds the full API object and is nullable: a later phase
  archives closed threads to gzipped JSON and then nulls it to reclaim space.
* ``runs`` drives the hourly delta schedule (each run covers
  ``[last_successful_end, now)``).
* ``entities`` and ``sentiment`` are created now with their final shape but are
  populated by the LLM-enrichment phases (6-7).

Everything is upsert-based and idempotent: re-ingesting a thread updates volatile
fields (score, body, edited) and inserts nothing new.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import wsbcal
from arctic import bare_id

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    id           TEXT PRIMARY KEY,          -- base-36, no t3_ prefix
    kind         TEXT NOT NULL,             -- daily | moves | weekend | flair:<name> | thread
    subreddit    TEXT NOT NULL,
    title        TEXT NOT NULL,
    flair        TEXT,
    created_utc  INTEGER,
    permalink    TEXT,
    first_seen   TEXT NOT NULL,             -- ISO-8601 UTC, first time we ingested it
    last_polled  TEXT,                      -- ISO-8601 UTC, last comment fetch
    is_open      INTEGER NOT NULL DEFAULT 1,-- 0 once the thread is old and quiet
    comments_through INTEGER,               -- epoch; comments confirmed fetched up to here
    trading_day  TEXT,                      -- YYYY-MM-DD the reports count this thread under
    post_json    TEXT NOT NULL              -- full submission object
);
CREATE INDEX IF NOT EXISTS threads_trading_day ON threads(trading_day);

CREATE TABLE IF NOT EXISTS comments (
    id               TEXT PRIMARY KEY,      -- base-36, no t1_ prefix
    thread_id        TEXT NOT NULL,
    parent_id        TEXT,                  -- verbatim: t3_<thread> or t1_<comment>
    author           TEXT,
    created_utc      INTEGER,
    score            INTEGER,
    body             TEXT,
    edited           TEXT,                  -- verbatim: 'False' or an epoch string
    controversiality INTEGER,
    retrieved_at     TEXT NOT NULL,         -- ISO-8601 UTC, last time this row was written
    raw_json         TEXT,                  -- full API object; nulled after archival
    FOREIGN KEY (thread_id) REFERENCES threads(id)
);
CREATE INDEX IF NOT EXISTS comments_thread  ON comments(thread_id);
CREATE INDEX IF NOT EXISTS comments_parent  ON comments(parent_id);
CREATE INDEX IF NOT EXISTS comments_created ON comments(created_utc);

CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    window_start   TEXT NOT NULL,           -- ISO-8601 UTC (inclusive)
    window_end     TEXT NOT NULL,           -- ISO-8601 UTC (exclusive)
    status         TEXT NOT NULL,           -- running | ok | partial | error
    n_threads      INTEGER NOT NULL DEFAULT 0,
    n_comments_new INTEGER NOT NULL DEFAULT 0,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    error          TEXT
);
CREATE INDEX IF NOT EXISTS runs_status_end ON runs(status, window_end);

-- Ticker / coin mentions. Phase 3 fills the regex tier (source='regex'); Phase 6
-- adds source='llm'. A comment scanned with nothing found gets one sentinel row
-- symbol='__none__' so it isn't rescanned.
CREATE TABLE IF NOT EXISTS entities (
    comment_id  TEXT NOT NULL,
    symbol      TEXT NOT NULL,              -- normalised ticker / handle, upper-case ('__none__' = scanned, empty)
    name        TEXT,                       -- 'Apple Inc.' etc. when the LLM supplies one
    type        TEXT,                       -- stock | crypto | token | NULL (sentinel)
    tier        TEXT,                       -- cashtag | bareword | unverified (regex) | llm
    confidence  REAL,                       -- LLM numeric score; NULL for regex
    source      TEXT NOT NULL,              -- regex | llm
    model       TEXT,
    prompt_ver  TEXT,
    PRIMARY KEY (comment_id, symbol, source)
);
CREATE INDEX IF NOT EXISTS entities_symbol ON entities(symbol);
CREATE INDEX IF NOT EXISTS entities_comment ON entities(comment_id);

-- Populated by Phase 7 (LLM sentiment). Shape frozen now.
CREATE TABLE IF NOT EXISTS sentiment (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scope        TEXT NOT NULL,             -- comment | symbol_thread | symbol_day
    symbol       TEXT NOT NULL,
    comment_id   TEXT,
    thread_id    TEXT,
    window_start TEXT,
    window_end   TEXT,
    label        TEXT NOT NULL,             -- buy | sell | neutral
    confidence   REAL,
    rationale    TEXT,
    model        TEXT,
    prompt_ver   TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sentiment_symbol ON sentiment(symbol);
CREATE UNIQUE INDEX IF NOT EXISTS sentiment_uniq ON sentiment(
    scope, symbol, IFNULL(comment_id, ''), IFNULL(thread_id, ''),
    IFNULL(window_start, ''), IFNULL(prompt_ver, '')
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

DEFAULT_DB = Path(__file__).parent / "hermes.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    """Open (creating if needed) the store with the project's standard pragmas."""
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn)          # ALTER old DBs before the indexes in SCHEMA reference new columns
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a pre-existing DB up to the current schema, additively. Runs before
    SCHEMA creates tables/indexes, so a brand-new DB (no `threads` table yet) is
    a no-op here and gets the current schema straight from SCHEMA."""
    have_tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "threads" not in have_tables:
        return

    tcols = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
    if "comments_through" not in tcols:  # v1 -> v2
        conn.execute("ALTER TABLE threads ADD COLUMN comments_through INTEGER")
        conn.execute(
            "UPDATE threads SET comments_through = COALESCE("
            "  (SELECT MAX(created_utc) FROM comments WHERE comments.thread_id = threads.id),"
            "  created_utc)"
        )

    if "trading_day" not in tcols:  # v2 -> v3
        conn.execute("ALTER TABLE threads ADD COLUMN trading_day TEXT")
        for tid, kind, title, created in conn.execute(
            "SELECT id, kind, title, created_utc FROM threads"
        ).fetchall():
            day = wsbcal.trading_day(kind, title or "", created).isoformat()
            conn.execute("UPDATE threads SET trading_day = ? WHERE id = ?", (day, tid))

    if "entities" in have_tables:
        ecols = {row[1] for row in conn.execute("PRAGMA table_info(entities)")}
        if "tier" not in ecols:  # v2 -> v3: entities gained `tier`, `type` now nullable.
            # entities is unpopulated before Phase 3, so a clean drop is safe;
            # SCHEMA recreates it right after this.
            conn.execute("DROP TABLE entities")
    conn.commit()


# --------------------------------------------------------------------------- #
#  writes                                                                      #
# --------------------------------------------------------------------------- #
def upsert_thread(conn: sqlite3.Connection, kind: str, post: dict,
                  *, is_open: bool = True) -> None:
    """Insert the thread or refresh its volatile fields, preserving first_seen."""
    tid = bare_id(post["id"])
    now = _now_iso()
    title = (post.get("title") or "").strip()
    day = wsbcal.trading_day(kind, title, post.get("created_utc")).isoformat()
    conn.execute(
        """
        INSERT INTO threads (id, kind, subreddit, title, flair, created_utc,
                             permalink, first_seen, last_polled, is_open,
                             comments_through, trading_day, post_json)
        VALUES (:id, :kind, :subreddit, :title, :flair, :created_utc,
                :permalink, :now, :now, :is_open, :created_utc, :trading_day, :post_json)
        ON CONFLICT(id) DO UPDATE SET
            kind        = excluded.kind,
            title       = excluded.title,
            flair       = excluded.flair,
            permalink   = excluded.permalink,
            is_open     = excluded.is_open,
            last_polled = excluded.last_polled,
            trading_day = excluded.trading_day,
            post_json   = excluded.post_json
        """,
        {
            "id": tid,
            "kind": kind,
            "subreddit": post.get("subreddit") or "",
            "title": title,
            "flair": post.get("link_flair_text"),
            "created_utc": post.get("created_utc"),
            "permalink": post.get("permalink"),
            "now": now,
            "is_open": 1 if is_open else 0,
            "trading_day": day,
            "post_json": json.dumps(post, ensure_ascii=False),
        },
    )
    conn.commit()


def upsert_comments(conn: sqlite3.Connection, thread_id: str,
                    comments: list[dict]) -> tuple[int, int]:
    """Store a batch of raw comment objects. Returns ``(inserted, updated)``.

    New rows are counted by diffing against the ids already stored for the
    thread, so a re-ingest that only refreshes scores/bodies reports 0 inserted.
    """
    tid = bare_id(thread_id)
    now = _now_iso()
    existing = {
        row[0] for row in conn.execute(
            "SELECT id FROM comments WHERE thread_id = ?", (tid,)
        )
    }

    rows = []
    for c in comments:
        rows.append({
            "id": c["id"],
            "thread_id": tid,
            "parent_id": c.get("parent_id"),
            "author": c.get("author"),
            "created_utc": c.get("created_utc"),
            "score": c.get("score"),
            "body": c.get("body"),
            "edited": _as_text(c.get("edited")),
            "controversiality": c.get("controversiality"),
            "retrieved_at": now,
            "raw_json": json.dumps(c, ensure_ascii=False),
        })

    conn.executemany(
        """
        INSERT INTO comments (id, thread_id, parent_id, author, created_utc, score,
                              body, edited, controversiality, retrieved_at, raw_json)
        VALUES (:id, :thread_id, :parent_id, :author, :created_utc, :score,
                :body, :edited, :controversiality, :retrieved_at, :raw_json)
        ON CONFLICT(id) DO UPDATE SET
            parent_id        = excluded.parent_id,
            author           = excluded.author,
            score            = excluded.score,
            body             = excluded.body,
            edited           = excluded.edited,
            controversiality = excluded.controversiality,
            retrieved_at     = excluded.retrieved_at,
            raw_json         = COALESCE(excluded.raw_json, comments.raw_json)
        """,
        rows,
    )
    conn.execute("UPDATE threads SET last_polled = ? WHERE id = ?", (now, tid))
    conn.commit()

    inserted = sum(1 for c in comments if c["id"] not in existing)
    return inserted, len(comments) - inserted


def _as_text(value) -> str | None:
    if value is None:
        return None
    return str(value)


# --- run bookkeeping ------------------------------------------------------- #
def last_successful_end(conn: sqlite3.Connection) -> str | None:
    """``window_end`` of the most recent ``ok``/``partial`` run, or None."""
    row = conn.execute(
        "SELECT window_end FROM runs WHERE status IN ('ok', 'partial') "
        "ORDER BY window_end DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def start_run(conn: sqlite3.Connection, window_start: str, window_end: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs (window_start, window_end, status, started_at) "
        "VALUES (?, ?, 'running', ?)",
        (window_start, window_end, _now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, *,
               n_threads: int = 0, n_comments_new: int = 0,
               error: str | None = None) -> None:
    conn.execute(
        "UPDATE runs SET status = ?, n_threads = ?, n_comments_new = ?, "
        "finished_at = ?, error = ? WHERE id = ?",
        (status, n_threads, n_comments_new, _now_iso(), error, run_id),
    )
    conn.commit()


# --- entity extraction (Phase 3 regex tier; Phase 6 adds llm) ------------ #
def comments_without_entities(conn: sqlite3.Connection, source: str,
                              limit: int | None = None) -> list[sqlite3.Row]:
    """Stored comments that have no `entities` row for `source` yet."""
    sql = (
        "SELECT c.id, c.body FROM comments c "
        "WHERE NOT EXISTS (SELECT 1 FROM entities e "
        "                  WHERE e.comment_id = c.id AND e.source = ?)"
    )
    params: list = [source]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def save_entities(conn: sqlite3.Connection, comment_id: str,
                  found: list[dict], *, source: str, model: str | None = None,
                  prompt_ver: str | None = None) -> None:
    """Replace this comment's rows for `source`. ``found`` items are
    ``{symbol, type, tier, confidence, name}`` (any optional). An empty list
    writes the ``__none__`` sentinel so the comment isn't rescanned."""
    conn.execute(
        "DELETE FROM entities WHERE comment_id = ? AND source = ?", (comment_id, source)
    )
    rows = found or [{"symbol": "__none__"}]
    conn.executemany(
        "INSERT OR IGNORE INTO entities "
        "(comment_id, symbol, name, type, tier, confidence, source, model, prompt_ver) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (comment_id, r["symbol"], r.get("name"), r.get("type"), r.get("tier"),
             r.get("confidence"), source, model, prompt_ver)
            for r in rows
        ],
    )


# --- watermarks & thread lifecycle (the hourly digger) ------------------- #
def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def open_threads(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Threads still accepting comments, oldest first."""
    return conn.execute(
        "SELECT id, kind, title, created_utc, comments_through, last_polled "
        "FROM threads WHERE is_open = 1 ORDER BY created_utc"
    ).fetchall()


def resume_ts(conn: sqlite3.Connection, thread_id: str) -> int:
    """Epoch to resume a thread's comment fetch from: its watermark, else the
    newest comment stored, else its creation time."""
    row = conn.execute(
        "SELECT comments_through, created_utc FROM threads WHERE id = ?",
        (bare_id(thread_id),),
    ).fetchone()
    if row and row["comments_through"] is not None:
        return row["comments_through"]
    newest = conn.execute(
        "SELECT MAX(created_utc) FROM comments WHERE thread_id = ?",
        (bare_id(thread_id),),
    ).fetchone()[0]
    if newest is not None:
        return newest
    return (row["created_utc"] if row else 0) or 0


def set_comments_through(conn: sqlite3.Connection, thread_id: str, epoch: int) -> None:
    """Advance the watermark (never move it backwards)."""
    conn.execute(
        "UPDATE threads SET comments_through = MAX(COALESCE(comments_through, 0), ?) "
        "WHERE id = ?",
        (epoch, bare_id(thread_id)),
    )
    conn.commit()


def close_stale_threads(conn: sqlite3.Connection, now_epoch: int, *,
                        max_age_hours: int = 48, quiet_hours: int = 12) -> list[str]:
    """Close threads older than ``max_age_hours`` whose newest comment is older
    than ``quiet_hours``. Returns the ids closed."""
    cutoff_age = now_epoch - max_age_hours * 3600
    cutoff_quiet = now_epoch - quiet_hours * 3600
    ids = [
        r[0] for r in conn.execute(
            """
            SELECT id FROM threads WHERE is_open = 1 AND created_utc < ?
              AND COALESCE(
                    (SELECT MAX(created_utc) FROM comments WHERE comments.thread_id = threads.id),
                    created_utc) < ?
            """,
            (cutoff_age, cutoff_quiet),
        )
    ]
    if ids:
        conn.executemany("UPDATE threads SET is_open = 0 WHERE id = ?", [(i,) for i in ids])
        conn.commit()
    return ids


# --------------------------------------------------------------------------- #
#  reads                                                                       #
# --------------------------------------------------------------------------- #
def get_post(conn: sqlite3.Connection, thread_id: str) -> dict | None:
    row = conn.execute(
        "SELECT post_json FROM threads WHERE id = ?", (bare_id(thread_id),)
    ).fetchone()
    return json.loads(row[0]) if row else None


def _comment_payload(row: sqlite3.Row) -> dict:
    """Full API object when raw_json survives; otherwise the stored columns."""
    if row["raw_json"]:
        return json.loads(row["raw_json"])
    return {
        "id": row["id"],
        "parent_id": row["parent_id"],
        "author": row["author"],
        "created_utc": row["created_utc"],
        "score": row["score"],
        "body": row["body"],
        "edited": row["edited"],
        "controversiality": row["controversiality"],
        "_raw_json_pruned": True,
    }


def get_tree(conn: sqlite3.Connection, thread_id: str) -> dict:
    """Reassemble a thread as ``{meta, post, comments, orphaned_comments}``.

    ``comments`` is the nested reply tree (each node gains a ``replies`` list);
    ``orphaned_comments`` holds replies whose ``t1_`` parent isn't stored (parent
    deleted and pruned by Reddit, or outside a capped fetch). Shape matches what
    ``wsb_tree.py`` wrote before Phase 1.
    """
    tid = bare_id(thread_id)
    thread = conn.execute("SELECT * FROM threads WHERE id = ?", (tid,)).fetchone()
    if thread is None:
        raise KeyError(f"thread {tid} not in store")

    rows = conn.execute(
        "SELECT * FROM comments WHERE thread_id = ? ORDER BY created_utc", (tid,)
    ).fetchall()

    nodes: dict[str, dict] = {}
    for row in rows:
        node = _comment_payload(row)
        node["replies"] = []
        nodes[row["id"]] = node

    post_fullname = "t3_" + tid
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
        for child in level:
            sort_recursive(child["replies"])

    sort_recursive(roots)
    sort_recursive(orphans)

    post = json.loads(thread["post_json"])
    permalink = post.get("permalink") or f"/comments/{tid}/"
    return {
        "meta": {
            "source": "arctic-shift",
            "thread_id": tid,
            "thread_fullname": post_fullname,
            "kind": thread["kind"],
            "subreddit": thread["subreddit"],
            "title": thread["title"],
            "permalink": "https://www.reddit.com" + permalink,
            "created_utc": thread["created_utc"],
            "is_open": bool(thread["is_open"]),
            "first_seen": thread["first_seen"],
            "last_polled": thread["last_polled"],
            "stored_comments": len(rows),
            "tree_top_level": len(roots),
            "orphaned_comments": len(orphans),
        },
        "post": post,
        "comments": roots,
        "orphaned_comments": orphans,
    }


def stats(conn: sqlite3.Connection) -> dict:
    thr = conn.execute(
        "SELECT COUNT(*) n, SUM(is_open) open FROM threads"
    ).fetchone()
    com = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
    last = conn.execute(
        "SELECT window_start, window_end, status, finished_at FROM runs "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {
        "threads": thr["n"] or 0,
        "threads_open": thr["open"] or 0,
        "comments": com,
        "discovery_through": get_meta(conn, "discovery_through"),
        "last_run": dict(last) if last else None,
        "last_successful_end": last_successful_end(conn),
    }
