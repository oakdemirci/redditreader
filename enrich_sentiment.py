#!/usr/bin/env python3
"""LLM sentiment -> the `sentiment` table (requirement 5.2).

Primary mode is one call per (symbol, trading day): feed a sample of that day's
comments mentioning the symbol, get back {label: buy|sell|neutral, confidence,
rationale}. `--per-comment` does the finer-grained pass instead.

    python enrich_sentiment.py                 # last 3 days, symbols with >=4 mentions/day
    python enrich_sentiment.py --days 7 --min-mentions 3
    python enrich_sentiment.py --dry-run
    python enrich_sentiment.py --per-comment --symbol NVDA --day 2026-09-09
    python enrich_sentiment.py --stats

No-op without DEEPSEEK_API_KEY. Cached per (symbol + the exact comment set) +
prompt version, so a closed day is called once; today's still-growing day gets
re-called as new comments land. Budget-capped by HERMES_LLM_DAILY_USD.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta

import clock
import hermes_api
import llm
import store

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TASK = "sentiment"
PROMPT_VER = "sentiment-v1"
DEFAULT_DAYS = 3
DEFAULT_MIN_MENTIONS = 6          # below this a day's chatter is too thin to judge
SAMPLE = int(os.environ.get("HERMES_SENT_SAMPLE", "40"))
BODY_CLIP = 320
LABELS = {"buy", "sell", "neutral"}

SYSTEM_DAY = (
    "You judge how r/wallstreetbets commenters, in aggregate, are positioned on "
    "ONE security on ONE day, from a sample of comments that mention it.\n"
    '- "buy"  = net bullish: long, buying calls, accumulating, "loading up".\n'
    '- "sell" = net bearish: short, buying puts, exiting, calling a top.\n'
    '- "neutral" = mixed, no clear lean, or just news/discussion.\n'
    "Weigh conviction and upvote-worthy takes, not raw comment count. "
    "confidence 0..1. rationale = one short sentence.\n"
    'Respond ONLY JSON: {"label":"buy","confidence":0.7,"rationale":"..."}'
)

SYSTEM_COMMENT = (
    "For each r/wallstreetbets comment, classify the author's stance on the "
    "security they are discussing: buy (bullish/long/calls), sell "
    "(bearish/short/puts), or neutral (mixed/none).\n"
    'Respond ONLY JSON: {"results":[{"comment_id":"<id>","label":"buy","confidence":0.8}]}'
)


def _fingerprint(symbol: str, comment_ids: list[str]) -> str:
    return llm.body_hash(symbol + "|" + "|".join(sorted(comment_ids)))


def _coerce(label) -> str:
    label = str(label or "").strip().lower()
    return label if label in LABELS else "neutral"


# --------------------------------------------------------------------------- #
#  symbol-day mode                                                             #
# --------------------------------------------------------------------------- #
def enrich_symbol_days(conn, *, days: int = DEFAULT_DAYS,
                       min_mentions: int = DEFAULT_MIN_MENTIONS,
                       kinds=hermes_api.MEGA_KINDS, entity_mode: str = "best",
                       limit: int | None = None, budget_usd: float | None = None,
                       force: bool = False, dry_run: bool = False,
                       verbose: bool = True) -> dict:
    if not dry_run and not llm.available():
        if verbose:
            print("no DEEPSEEK_API_KEY -- skipping sentiment")
        return {"scored": 0, "calls": 0, "skipped_budget": False}

    kinds = list(kinds)
    ent = hermes_api._entity_where(entity_mode)
    since = (clock.market_today() - timedelta(days=days - 1)).isoformat()
    cands = store.symbol_day_candidates(conn, since, min_mentions=min_mentions,
                                        kinds=kinds, entity_where=ent)
    if limit:
        cands = cands[:limit]

    scored = calls = 0
    skipped_budget = False
    for day, symbol, n in cands:
        if not force and store.has_symbol_day_sentiment(conn, day, symbol, PROMPT_VER):
            continue
        rows = store.symbol_day_comments(conn, day, symbol, kinds=kinds,
                                         entity_where=ent, limit=SAMPLE)
        if not rows:
            continue
        cids = [r[0] for r in rows]
        fp = _fingerprint(symbol, cids)

        cached = llm.cache_get(conn, fp, PROMPT_VER, TASK)
        if cached is None:
            if dry_run:
                print(f"[dry] {day} {symbol}  ({n} mentions, {len(rows)} sampled)")
                calls += 1
                continue
            user = (f"Security: {symbol}\nDay: {day}\nComments:\n" + json.dumps(
                [b[:BODY_CLIP] for _, b, _ in rows], ensure_ascii=False))
            try:
                cached = llm.chat_json(conn, task=TASK, prompt_ver=PROMPT_VER,
                                       system=SYSTEM_DAY, user=user, n_items=len(rows),
                                       budget_usd=budget_usd, max_tokens=400)
            except (llm.BudgetExceeded, llm.LLMError) as exc:
                if verbose:
                    print(f"stopping: {exc}")
                skipped_budget = True
                break
            calls += 1
            llm.cache_put(conn, fp, PROMPT_VER, TASK, cached)

        store.save_sentiment(
            conn, scope="symbol_day", symbol=symbol,
            label=_coerce(cached.get("label")), confidence=cached.get("confidence"),
            rationale=(cached.get("rationale") or "")[:400],
            model=llm.MODEL, prompt_ver=PROMPT_VER,
            window_start=day, window_end=day,
        )
        scored += 1

    return {"scored": scored, "calls": calls, "skipped_budget": skipped_budget}


# --------------------------------------------------------------------------- #
#  per-comment mode                                                            #
# --------------------------------------------------------------------------- #
def enrich_comments(conn, *, symbol: str, day: str, kinds=hermes_api.MEGA_KINDS,
                    entity_mode: str = "best", batch: int = 25,
                    budget_usd: float | None = None, dry_run: bool = False,
                    verbose: bool = True) -> dict:
    if not dry_run and not llm.available():
        if verbose:
            print("no DEEPSEEK_API_KEY -- skipping sentiment")
        return {"scored": 0, "calls": 0}
    ent = hermes_api._entity_where(entity_mode)
    rows = store.symbol_day_comments(conn, day, symbol.upper(), kinds=list(kinds),
                                     entity_where=ent, limit=10_000)
    scored = calls = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        user = "Comments:\n" + json.dumps(
            [{"comment_id": cid, "text": (b or "")[:BODY_CLIP]} for cid, b, _ in chunk],
            ensure_ascii=False)
        if dry_run:
            print(f"[dry] {symbol} {day} batch {i // batch + 1} ({len(chunk)})")
            calls += 1
            continue
        try:
            reply = llm.chat_json(conn, task=TASK, prompt_ver=PROMPT_VER,
                                  system=SYSTEM_COMMENT, user=user, n_items=len(chunk),
                                  budget_usd=budget_usd)
        except (llm.BudgetExceeded, llm.LLMError) as exc:
            print(f"stopping: {exc}")
            break
        calls += 1
        by_id = {r.get("comment_id"): r for r in reply.get("results", [])}
        for cid, _b, _s in chunk:
            r = by_id.get(cid)
            if not r:
                continue
            store.save_sentiment(conn, scope="comment", symbol=symbol.upper(),
                                 label=_coerce(r.get("label")),
                                 confidence=r.get("confidence"), rationale=None,
                                 model=llm.MODEL, prompt_ver=PROMPT_VER,
                                 comment_id=cid, window_start=day)
            scored += 1
    return {"scored": scored, "calls": calls}


# --------------------------------------------------------------------------- #
def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--db", default=os.environ.get("HERMES_DB", str(store.DEFAULT_DB)))
    p.add_argument("--days", type=int, default=DEFAULT_DAYS)
    p.add_argument("--min-mentions", type=int, default=DEFAULT_MIN_MENTIONS)
    p.add_argument("--entity-mode", choices=hermes_api.ENTITY_MODES, default="best")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--budget", type=float, default=None, metavar="USD")
    p.add_argument("--force", action="store_true", help="re-score even if a row exists")
    p.add_argument("--per-comment", action="store_true")
    p.add_argument("--symbol", help="with --per-comment")
    p.add_argument("--day", help="with --per-comment (YYYY-MM-DD)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--stats", action="store_true")
    args = p.parse_args()

    conn = store.connect(args.db)

    if args.stats:
        print(json.dumps(llm.spend_summary(conn), indent=2))
        rows = conn.execute(
            "SELECT label, COUNT(*) FROM sentiment WHERE scope='symbol_day' GROUP BY label"
        ).fetchall()
        print("symbol_day sentiment:", dict(rows))
        return

    if args.per_comment:
        if not (args.symbol and args.day):
            sys.exit("--per-comment needs --symbol and --day")
        print(json.dumps(enrich_comments(conn, symbol=args.symbol, day=args.day,
                                         entity_mode=args.entity_mode,
                                         budget_usd=args.budget, dry_run=args.dry_run)))
        return

    print(json.dumps(enrich_symbol_days(
        conn, days=args.days, min_mentions=args.min_mentions,
        entity_mode=args.entity_mode, limit=args.limit, budget_usd=args.budget,
        force=args.force, dry_run=args.dry_run)))


if __name__ == "__main__":
    main()
