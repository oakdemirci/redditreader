#!/usr/bin/env python3
"""Precision / recall for ticker extraction against a hand-labeled sample.

    python tools/eval_entities.py                      # regex tier, seed set
    python tools/eval_entities.py --mode regex+llm     # also run DeepSeek (needs a key)
    python tools/eval_entities.py --labels my_200.jsonl

Labels are JSONL: {"body": "...", "symbols": ["AAPL", "NVDA"]}  (canonical
uppercase tickers the comment is genuinely about; [] for none).

The seed set (`tools/eval_entities.seed.jsonl`, ~40 rows) is a starting point --
the Phase 6 acceptance target is ~200 hand-labeled comments. Grow it from real
data, e.g.:
    sqlite3 hermes.db "SELECT body FROM comments WHERE body IS NOT NULL \
        ORDER BY RANDOM() LIMIT 300" | ...   # then label by hand
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import enrich_entities  # noqa: E402
import extract          # noqa: E402
import llm              # noqa: E402
import store            # noqa: E402
import tickers          # noqa: E402

SEED = Path(__file__).parent / "eval_entities.seed.jsonl"


def _prf(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return {"precision": round(p, 3), "recall": round(r, 3), "f1": round(f1, 3),
            "tp": tp, "fp": fp, "fn": fn}


def _predict(labels: list[dict], mode: str, budget: float) -> list[set[str]]:
    """Run the store pipeline over the labeled bodies, return predicted symbol
    sets in `entity_mode`-'best' style (llm where scanned, else regex)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    conn = store.connect(path)
    known = tickers.load_known_stock_symbols()
    conn.execute("INSERT INTO threads (id,kind,subreddit,title,created_utc,first_seen,"
                 "is_open,trading_day,post_json) VALUES ('e','daily','wsb','t',1,'x',1,'2020-01-01','{}')")
    for i, row in enumerate(labels):
        conn.execute("INSERT INTO comments (id,thread_id,parent_id,author,created_utc,body,"
                     "retrieved_at) VALUES (?,?,?,?,?,?,?)",
                     (f"c{i}", "e", "t3_e", "u", 1, row["body"], "x"))
    conn.commit()

    extract.extract_pending(conn, known, verbose=False)
    use_llm = "llm" in mode
    if use_llm:
        if not llm.available():
            sys.exit("mode needs DEEPSEEK_API_KEY")
        enrich_entities.enrich_pending(conn, known, budget_usd=budget, verbose=True)

    preds = []
    for i in range(len(labels)):
        if use_llm:
            rows = conn.execute(
                "SELECT symbol FROM entities WHERE comment_id=? AND source='llm' AND symbol!='__none__'",
                (f"c{i}",)).fetchall()
            scanned = conn.execute(
                "SELECT 1 FROM entities WHERE comment_id=? AND source='llm' LIMIT 1", (f"c{i}",)).fetchone()
            if not scanned:
                rows = conn.execute(
                    "SELECT symbol FROM entities WHERE comment_id=? AND source='regex' AND symbol!='__none__'",
                    (f"c{i}",)).fetchall()
        else:
            rows = conn.execute(
                "SELECT symbol FROM entities WHERE comment_id=? AND source='regex' AND symbol!='__none__'",
                (f"c{i}",)).fetchall()
        preds.append({r[0] for r in rows})
    return preds


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--labels", type=Path, default=SEED)
    p.add_argument("--mode", choices=["regex", "regex+llm"], default="regex")
    p.add_argument("--budget", type=float, default=1.0)
    args = p.parse_args()

    labels = [json.loads(ln) for ln in args.labels.read_text(encoding="utf-8").splitlines() if ln.strip()]
    gold = [{s.upper() for s in row["symbols"]} for row in labels]
    preds = _predict(labels, args.mode, args.budget)

    tp = sum(len(g & q) for g, q in zip(gold, preds))
    fp = sum(len(q - g) for g, q in zip(gold, preds))
    fn = sum(len(g - q) for g, q in zip(gold, preds))
    exact = sum(g == q for g, q in zip(gold, preds))

    print(f"labels: {len(labels)}  mode: {args.mode}")
    print(json.dumps(_prf(tp, fp, fn), indent=2))
    print(f"exact-match comments: {exact}/{len(labels)}")
    print("\nmisses (gold -> predicted):")
    for row, g, q in zip(labels, gold, preds):
        if g != q:
            print(f"  {sorted(g)} -> {sorted(q)}   {row['body'][:70]}")


if __name__ == "__main__":
    main()
