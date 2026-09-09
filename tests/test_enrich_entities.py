"""Phase 6 plumbing tests -- pre-filter, batching, cache, budget, entities write,
and the `entity_mode` blend in hermes_api. The HTTP call is faked at the
`requests.post` seam so llm.py's real logging / cost / cache code runs.

    python tests/test_enrich_entities.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["DEEPSEEK_API_KEY"] = "test-key"
os.environ["HERMES_LLM_DAILY_USD"] = "1.00"
os.environ["HERMES_LLM_PRICE_IN"] = "1000000"   # $1 per 1M tokens -> easy budget maths
os.environ["HERMES_LLM_PRICE_OUT"] = "0"
os.environ["HERMES_LLM_PRICE_IN_CACHED"] = "0"

import clock            # noqa: E402
import enrich_entities  # noqa: E402
import hermes_api       # noqa: E402
import llm              # noqa: E402
import store            # noqa: E402

_CALLS = {"n": 0}


class _FakeResp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _fake_post(url, headers=None, json=None, timeout=None):
    _CALLS["n"] += 1
    user = json["messages"][1]["content"]
    items = __import__("json").loads(user[user.index("["):])
    results = []
    for it in items:
        text = (it["text"] or "").lower()
        syms = []
        if "palantir" in text or "pltr" in text:
            syms.append({"ticker": "PLTR", "name": "Palantir", "type": "stock", "confidence": 0.95})
        if "nvidia" in text or "nvda" in text:
            syms.append({"ticker": "NVDA", "name": "NVIDIA", "type": "stock", "confidence": 0.9})
        if "bitcoin" in text:
            syms.append({"ticker": "BTC", "name": "Bitcoin", "type": "crypto", "confidence": 0.99})
        # "maybe BE worried" -> BE is the word, LLM returns nothing for that comment
        results.append({"comment_id": it["comment_id"], "symbols": syms})
    tokens = 100 * len(items)
    return _FakeResp({
        "choices": [{"message": {"content": __import__("json").dumps({"results": results})}}],
        "usage": {"prompt_tokens": tokens, "completion_tokens": 0},
    })


llm.requests.post = _fake_post  # type: ignore


def _seed():
    _CALLS["n"] = 0
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    conn = store.connect(path)
    today = clock.market_today().isoformat()
    conn.execute(
        "INSERT INTO threads (id, kind, subreddit, title, created_utc, first_seen, "
        "is_open, trading_day, post_json) VALUES ('t0','daily','wallstreetbets',"
        "?, 1700000000, '2020-01-01T00:00:00+00:00', 1, ?, '{}')",
        (f"Daily Discussion Thread for {today}", today),
    )
    rows = [
        ("c1", "just bought a ton of Palantir calls, all in"),   # finance + name
        ("c2", "nvidia is going to the moon after earnings"),     # finance + name
        ("c3", "this is the way"),                                # noise -> sentinel, no call
        ("c4", "lol"),                                            # noise
        ("c5", "should I maybe BE worried about my SPY puts"),    # regex sees BE+SPY; it's noise
        ("c6", "stacking bitcoin here"),                          # finance-ish + name
    ]
    for cid, body in rows:
        conn.execute(
            "INSERT INTO comments (id, thread_id, parent_id, author, created_utc, body, "
            "retrieved_at) VALUES (?, 't0', 't3_t0', 'u', 1700000000, ?, '2020-01-01T00:00:00+00:00')",
            (cid, body),
        )
    conn.commit()
    return conn


KNOWN = {"SPY", "BE", "NVDA", "PLTR"}


def test_prefilter_and_calls():
    conn = _seed()
    r = enrich_entities.enrich_pending(conn, KNOWN, verbose=False)
    assert r["scanned"] == 6
    # c3/c4 are noise -> sentinel, no API; the other 4 go in one batch -> 1 call
    assert _CALLS["n"] == 1, _CALLS
    assert r["calls"] == 1

    pltr = conn.execute("SELECT type, tier, name FROM entities WHERE comment_id='c1' "
                        "AND symbol='PLTR' AND source='llm'").fetchone()
    assert pltr and pltr["tier"] == "llm" and pltr["type"] == "stock"
    # noise comment got the sentinel
    assert conn.execute("SELECT symbol FROM entities WHERE comment_id='c3' AND source='llm'"
                        ).fetchone()["symbol"] == "__none__"
    # c5: LLM returned nothing for it -> sentinel, so 'best' mode can veto a bad "BE"
    assert conn.execute("SELECT symbol FROM entities WHERE comment_id='c5' AND source='llm'"
                        ).fetchone()["symbol"] == "__none__"


def test_cache_makes_rerun_free():
    conn = _seed()
    enrich_entities.enrich_pending(conn, KNOWN, verbose=False)
    first = _CALLS["n"]
    conn.execute("DELETE FROM entities WHERE source='llm'")  # force re-derivation
    conn.commit()
    enrich_entities.enrich_pending(conn, KNOWN, verbose=False)
    assert _CALLS["n"] == first, "second run should hit the cache, not the API"
    assert conn.execute("SELECT COUNT(*) FROM entities WHERE comment_id='c1' AND symbol='PLTR'"
                        ).fetchone()[0] == 1


def test_budget_stops_midway():
    conn = _seed()
    # price is $1/1M in-tokens; 4 comments * 100 tokens = 400 tokens = $0.0004/call.
    # set a tiny budget so the first call is allowed but the next is refused.
    conn.execute("INSERT INTO llm_calls (created_at, task, model, prompt_ver, ok, cost_usd) "
                 "VALUES (strftime('%Y-%m-%dT%H:%M:%S','now'), 'entities', 'x', 'v', 1, 1.5)")
    conn.commit()
    r = enrich_entities.enrich_pending(conn, KNOWN, verbose=False)
    assert r["skipped_budget"] is True, r
    assert r["calls"] == 0, r


def test_entity_mode_best_blends():
    conn = _seed()
    # regex tier
    import extract
    extract.extract_pending(conn, KNOWN, verbose=False)
    # llm tier
    enrich_entities.enrich_pending(conn, KNOWN, verbose=False)

    reg = {r.symbol for r in hermes_api.trend(conn, days=1, min_total=1, stale_days=0,
                                              entity_mode="regex").rows}
    best = {r.symbol for r in hermes_api.trend(conn, days=1, min_total=1, stale_days=0,
                                               entity_mode="best").rows}
    assert {"PLTR", "NVDA", "BTC"} <= best, best       # names the regex missed
    assert "BE" in reg, reg                            # regex barewords "BE" on c5
    assert "BE" not in best, best                      # llm scanned c5, found nothing -> vetoed


def test_stats_and_no_key(monkeypatch=None):
    conn = _seed()
    os.environ["DEEPSEEK_API_KEY"] = ""
    try:
        r = enrich_entities.enrich_pending(conn, KNOWN, verbose=False)
        assert r["scanned"] == 0 and r["calls"] == 0
    finally:
        os.environ["DEEPSEEK_API_KEY"] = "test-key"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
