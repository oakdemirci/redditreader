"""Phase 7 plumbing tests -- symbol-day sentiment, cache, budget, re-run
stability, and the trend()/symbol_detail() integration. HTTP faked at
`requests.post`.

    python tests/test_enrich_sentiment.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["DEEPSEEK_API_KEY"] = "test-key"
os.environ["HERMES_LLM_DAILY_USD"] = "1.00"
os.environ["HERMES_LLM_PRICE_IN"] = "1"      # $1/1M tokens -> ~$0.0002 per fake call
os.environ["HERMES_LLM_PRICE_OUT"] = "1"
os.environ["HERMES_LLM_PRICE_IN_CACHED"] = "1"

import clock              # noqa: E402
import enrich_sentiment   # noqa: E402
import extract            # noqa: E402
import hermes_api         # noqa: E402
import llm                # noqa: E402
import store              # noqa: E402

_CALLS = {"n": 0}


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def _fake_post(url, headers=None, json=None, timeout=None):
    _CALLS["n"] += 1
    system = json["messages"][0]["content"]
    user = json["messages"][1]["content"].lower()
    if "for each r/wallstreetbets comment" in system.lower():   # per-comment mode
        items = __import__("json").loads(
            json["messages"][1]["content"][json["messages"][1]["content"].index("["):])
        out = {"results": [{"comment_id": it["comment_id"], "label": "buy", "confidence": 0.8}
                           for it in items]}
    else:                                                       # symbol-day mode
        label = "neutral"
        if "call" in user or "bull" in user or "loading up" in user:
            label = "buy"
        if "put" in user or "short" in user or "bear" in user:
            label = "sell" if label != "buy" else "neutral"
        out = {"label": label, "confidence": 0.72, "rationale": "test rationale"}
    return _Resp({
        "choices": [{"message": {"content": __import__("json").dumps(out)}}],
        "usage": {"prompt_tokens": 200, "completion_tokens": 0},
    })


llm.requests.post = _fake_post  # type: ignore


def _seed():
    _CALLS["n"] = 0
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    conn = store.connect(path)
    today = clock.market_today()
    days = [today - timedelta(days=1), today]
    for di, day in enumerate(days):
        conn.execute(
            "INSERT INTO threads (id,kind,subreddit,title,created_utc,first_seen,"
            "is_open,trading_day,post_json) VALUES (?,?,?,?,?,?,1,?,'{}')",
            (f"t{di}", "daily", "wallstreetbets",
             f"Daily Discussion Thread for {day}", 1700000000 + di * 86400,
             "2020-01-01T00:00:00+00:00", day.isoformat()),
        )
    # AAAA: 5 bullish comments today; BBBB: 4 bearish today; CCCC: 2 (below min)
    rows = []
    for i in range(5):
        rows.append((f"a{i}", "t1", "AAAA calls printing, loading up"))
    for i in range(4):
        rows.append((f"b{i}", "t1", "BBBB puts, this is going to crash, short it"))
    for i in range(2):
        rows.append((f"c{i}", "t1", "CCCC idk maybe"))
    for i in range(3):
        rows.append((f"y{i}", "t0", "AAAA calls yesterday too"))
    for cid, tid, body in rows:
        conn.execute(
            "INSERT INTO comments (id,thread_id,parent_id,author,created_utc,body,retrieved_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, tid, "t3_" + tid, "u", 1700000000, body, "x"),
        )
    conn.commit()
    extract.extract_pending(conn, {"AAAA", "BBBB", "CCCC"}, verbose=False)
    return conn, days


def test_symbol_day_scoring():
    conn, days = _seed()
    r = enrich_sentiment.enrich_symbol_days(conn, days=2, min_mentions=3,
                                            entity_mode="regex", verbose=False)
    # AAAA today, AAAA yesterday, BBBB today = 3 symbol-days; CCCC (2) excluded
    assert r["scored"] == 3, r
    assert _CALLS["n"] == 3

    aaa = store.sentiment_history(conn, "AAAA")
    assert {row["label"] for row in aaa} == {"buy"}
    bbb = store.sentiment_history(conn, "BBBB")
    assert bbb[0]["label"] == "sell"
    assert store.has_symbol_day_sentiment(conn, days[1].isoformat(), "AAAA",
                                          enrich_sentiment.PROMPT_VER)


def test_rerun_is_cached_and_stable():
    conn, days = _seed()
    enrich_sentiment.enrich_symbol_days(conn, days=2, min_mentions=3, verbose=False)
    n1 = _CALLS["n"]
    before = [(x["window_start"], x["label"]) for x in store.sentiment_history(conn, "AAAA")]

    # same comment set -> skipped by has_symbol_day_sentiment, no calls
    enrich_sentiment.enrich_symbol_days(conn, days=2, min_mentions=3, verbose=False)
    assert _CALLS["n"] == n1

    # --force re-scores but the cache still serves it -> label identical, still no API
    enrich_sentiment.enrich_symbol_days(conn, days=2, min_mentions=3, force=True, verbose=False)
    assert _CALLS["n"] == n1, "cache should cover a forced re-run of the same comment set"
    after = [(x["window_start"], x["label"]) for x in store.sentiment_history(conn, "AAAA")]
    assert before == after


def test_budget_stop():
    conn, _ = _seed()
    conn.execute("INSERT INTO llm_calls (created_at, task, model, prompt_ver, ok, cost_usd) "
                 "VALUES (strftime('%Y-%m-%dT%H:%M:%S','now'),'sentiment','x','v',1,2.0)")
    conn.commit()
    r = enrich_sentiment.enrich_symbol_days(conn, days=2, min_mentions=3, verbose=False)
    assert r["skipped_budget"] is True and r["scored"] == 0


def test_trend_and_symbol_detail_show_sentiment():
    conn, days = _seed()
    enrich_sentiment.enrich_symbol_days(conn, days=2, min_mentions=3, verbose=False)

    rep = hermes_api.trend(conn, days=2, min_total=1, stale_days=0, with_sentiment=True)
    flags = {r.symbol: r.flags for r in rep.rows}
    assert "BUY" in flags["AAAA"], flags
    assert "SELL" in flags["BBBB"], flags

    det = hermes_api.symbol_detail(conn, "AAAA", with_sentiment=True)
    assert det.sentiment and det.sentiment[0].label == "buy"
    assert "BUY" in det.to_text()


def test_per_comment_mode():
    conn, days = _seed()
    r = enrich_sentiment.enrich_comments(conn, symbol="AAAA", day=days[1].isoformat(),
                                         entity_mode="regex", verbose=False)
    assert r["scored"] == 5
    rows = conn.execute("SELECT label FROM sentiment WHERE scope='comment' AND symbol='AAAA'").fetchall()
    assert len(rows) == 5 and all(x[0] == "buy" for x in rows)


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
