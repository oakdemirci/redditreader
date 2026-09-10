#!/usr/bin/env python3
"""LLM ticker/coin extraction -> `entities` (source='llm').

The second pass after `extract.py`'s regex tier. It disambiguates collisions
(is "BE" Bloom Energy or the word "be"?) and catches names the regex can't
(Palantir -> PLTR, "all in on nvidia" -> NVDA). Only comments that look
finance-y are sent; the rest get a cheap `__none__` sentinel so they aren't
re-checked.

    python enrich_entities.py                 # scan pending, honour the daily budget
    python enrich_entities.py --dry-run       # show what would be sent, no API
    python enrich_entities.py --limit 500
    python enrich_entities.py --stats

With no DEEPSEEK_API_KEY set this is a no-op. Cached per comment-hash +
prompt version, so a re-run at the same version makes zero API calls. Wired
into `digger.py` after the regex pass (disable with `--no-llm`).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

import llm
import store
import tickers

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TASK = "entities"
PROMPT_VER = "entities-v1"          # bump on any prompt/rule change -> full re-run
BATCH = int(os.environ.get("HERMES_LLM_BATCH", "30"))
MIN_CONFIDENCE = float(os.environ.get("HERMES_LLM_MIN_CONFIDENCE", "0.55"))
BODY_CLIP = 600

CASHTAG_RE = re.compile(r"\$[A-Za-z]{1,6}\b")
FINANCE_RE = re.compile(
    r"\b(calls?|puts?|shares?|stock|stocks|ticker|tickers|bought|buying|sold|"
    r"selling|position|positions|long|short|squeeze|earnings|strike|expir\w*|"
    r"leaps?|dividend|yolo|baghold\w*|moon|hodl|portfolio|options?|contracts?|"
    r"premarket|afterhours|all[\s-]?in|loaded up|stack\w*|"
    r"bitcoin|ethereum|crypto|altcoin|token|coin|solana|dogecoin)\b",
    re.I,
)

SYSTEM = (
    "You extract publicly-traded stock tickers, cryptocurrencies and tokens "
    "mentioned in Reddit r/wallstreetbets comments.\n"
    "Rules:\n"
    "- Return the symbol UPPERCASE (AAPL, BTC). Map names to tickers "
    "(Palantir->PLTR, nvidia->NVDA, bitcoin->BTC).\n"
    '- type is "stock", "crypto" or "token".\n'
    "- Only include a symbol the comment is genuinely about. Ignore words that "
    "merely look like tickers (BE, TIME, OR, ANY, IT, ARE) unless the context "
    "clearly means the security.\n"
    "- confidence 0..1 = how sure you are it is a real security reference.\n"
    "- A comment about no security gets an empty symbols list.\n"
    'Respond ONLY with JSON: {"results":[{"comment_id":"<id>","symbols":'
    '[{"ticker":"AAPL","name":"Apple Inc.","type":"stock","confidence":0.9}]}]}'
)


def worth_calling(body: str, known: set[str]) -> bool:
    if not body:
        return False
    if CASHTAG_RE.search(body) or FINANCE_RE.search(body):
        return True
    return bool(tickers.extract_symbols(body, known))


def _user_prompt(batch: list[tuple[str, str]]) -> str:
    items = [
        {"comment_id": cid, "text": (body or "")[:BODY_CLIP]}
        for cid, body in batch
    ]
    return "Extract from these comments:\n" + json.dumps(items, ensure_ascii=False)


def _rows_from_symbols(symbols: list[dict]) -> list[dict]:
    out = []
    for s in symbols or []:
        sym = str(s.get("ticker") or s.get("symbol") or "").upper().strip().lstrip("$")
        conf = s.get("confidence")
        if not sym or not re.fullmatch(r"[A-Z]{1,6}", sym):
            continue
        if conf is not None and conf < MIN_CONFIDENCE:
            continue
        out.append({
            "symbol": sym,
            "name": s.get("name"),
            "type": (s.get("type") or "").lower() or None,
            "tier": "llm",
            "confidence": conf,
        })
    return out


def enrich_pending(conn, known: set[str], *, limit: int | None = None,
                   budget_usd: float | None = None, dry_run: bool = False,
                   verbose: bool = True) -> dict:
    if not dry_run and not llm.available():
        if verbose:
            print("no DEEPSEEK_API_KEY -- skipping LLM entity extraction")
        return {"scanned": 0, "sent": 0, "calls": 0, "skipped_budget": False}

    candidates = store.comments_without_entities(conn, "llm", limit)
    to_send: list[tuple[str, str]] = []
    scanned = sent = calls = 0
    skipped_budget = False

    for cid, body in candidates:
        if not worth_calling(body, known):
            store.save_entities(conn, cid, [], source="llm", model=llm.MODEL,
                                prompt_ver=PROMPT_VER)
            scanned += 1
            continue
        cached = llm.cache_get(conn, llm.body_hash(body or ""), PROMPT_VER, TASK)
        if cached is not None:
            store.save_entities(conn, cid, _rows_from_symbols(cached), source="llm",
                                model=llm.MODEL, prompt_ver=PROMPT_VER)
            scanned += 1
            continue
        to_send.append((cid, body or ""))

    if dry_run:
        for i in range(0, len(to_send), BATCH):
            batch = to_send[i:i + BATCH]
            print(f"--- batch {i // BATCH + 1} ({len(batch)} comments) ---")
            print(_user_prompt(batch)[:1200])
        return {"scanned": scanned, "sent": len(to_send),
                "calls": (len(to_send) + BATCH - 1) // BATCH, "skipped_budget": False}

    for i in range(0, len(to_send), BATCH):
        batch = to_send[i:i + BATCH]
        try:
            reply = llm.chat_json(
                conn, task=TASK, prompt_ver=PROMPT_VER, system=SYSTEM,
                user=_user_prompt(batch), n_items=len(batch), budget_usd=budget_usd,
            )
        except llm.BudgetExceeded as exc:
            if verbose:
                print(f"stopping: {exc} ({len(to_send) - i} comment(s) left for next run)")
            skipped_budget = True
            break
        except llm.LLMError as exc:
            if verbose:
                print(f"stopping: {exc} (check DEEPSEEK_API_KEY / HERMES_LLM_MODEL)")
            skipped_budget = True   # leave the rest for a later run
            break
        calls += 1
        by_id = {r.get("comment_id"): r.get("symbols", []) for r in reply.get("results", [])}
        for cid, body in batch:
            symbols = by_id.get(cid, [])
            llm.cache_put(conn, llm.body_hash(body), PROMPT_VER, TASK, symbols)
            store.save_entities(conn, cid, _rows_from_symbols(symbols), source="llm",
                                model=llm.MODEL, prompt_ver=PROMPT_VER)
            scanned += 1
            sent += 1

    return {"scanned": scanned, "sent": sent, "calls": calls,
            "skipped_budget": skipped_budget}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--db", default=os.environ.get("HERMES_DB", str(store.DEFAULT_DB)))
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--budget", type=float, default=None, metavar="USD",
                   help="override HERMES_LLM_DAILY_USD for this run")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--rescan", action="store_true",
                   help="delete all source='llm' entity rows first")
    p.add_argument("--stats", action="store_true")
    args = p.parse_args()

    conn = store.connect(args.db)

    if args.stats:
        print(json.dumps(llm.spend_summary(conn), indent=2))
        n = conn.execute(
            "SELECT COUNT(*) FROM entities WHERE source='llm' AND symbol!='__none__'"
        ).fetchone()[0]
        scanned = conn.execute(
            "SELECT COUNT(DISTINCT comment_id) FROM entities WHERE source='llm'"
        ).fetchone()[0]
        print(f"llm entities: {n} mentions across {scanned} comments scanned")
        return

    if args.rescan:
        conn.execute("DELETE FROM entities WHERE source = 'llm'")
        conn.commit()
        print("cleared source='llm' rows")

    result = enrich_pending(conn, tickers.load_known_stock_symbols(),
                            limit=args.limit, budget_usd=args.budget,
                            dry_run=args.dry_run)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
