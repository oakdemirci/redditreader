#!/usr/bin/env python3
"""Regex ticker/coin extraction -> the `entities` table (source='regex').

This is the cheap first pass that `tickers.py` has always done, moved onto the
new store. The digger runs it after each ingest slice; a one-shot backfill
covers everything already stored:

    python extract.py                 # scan every comment with no regex entities
    python extract.py --rescan        # wipe source='regex' rows and redo
    python extract.py --stats

Phase 6 adds an `llm` source alongside this; the reports count
``COUNT(DISTINCT comment_id)`` so a comment matched by both tiers is one mention.
"""

from __future__ import annotations

import argparse
import sys

import store
import tickers

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SOURCE = "regex"
PROMPT_VER = "regex-v1"          # bump when tickers.py's rules change materially
COMMIT_EVERY = 500


def _classify_type(symbol: str) -> str:
    return "crypto" if symbol in tickers.CRYPTO_SYMBOLS else "stock"


def extract_pending(conn, known_symbols: set[str], *, limit: int | None = None,
                    verbose: bool = True) -> int:
    """Scan every comment lacking a regex `entities` row. Returns the count."""
    rows = store.comments_without_entities(conn, SOURCE, limit)
    for index, (comment_id, body) in enumerate(rows, start=1):
        found = [
            {"symbol": sym, "type": _classify_type(sym), "tier": tier}
            for sym, tier in tickers.extract_symbols(body or "", known_symbols)
        ]
        store.save_entities(conn, comment_id, found, source=SOURCE, prompt_ver=PROMPT_VER)
        if index % COMMIT_EVERY == 0:
            conn.commit()
            if verbose:
                print(f"  ...{index}/{len(rows)}")
    conn.commit()
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--db", default=str(store.DEFAULT_DB))
    parser.add_argument("--rescan", action="store_true",
                        help="delete all source='regex' rows first, then re-extract")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()

    conn = store.connect(args.db)

    if args.stats:
        n_ent = conn.execute(
            "SELECT COUNT(*) FROM entities WHERE source='regex' AND symbol!='__none__'"
        ).fetchone()[0]
        n_scanned = conn.execute(
            "SELECT COUNT(DISTINCT comment_id) FROM entities WHERE source='regex'"
        ).fetchone()[0]
        n_total = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
        print(f"regex entities: {n_ent} mentions across {n_scanned}/{n_total} comments scanned")
        return

    if args.rescan:
        conn.execute("DELETE FROM entities WHERE source = 'regex'")
        conn.commit()
        print("cleared source='regex' rows")

    known = tickers.load_known_stock_symbols()
    n = extract_pending(conn, known, limit=args.limit)
    print(f"scanned {n} comment(s) for ticker/coin mentions")


if __name__ == "__main__":
    main()
