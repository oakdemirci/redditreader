"""Fixture tests for hermes_api -- the ported trend/report logic.

Runnable two ways:
    python -m pytest tests/test_hermes_api.py
    python tests/test_hermes_api.py

The trend maths (share-of-voice HOT, NEW window, $ flag, relevance score and
ordering) is a line-for-line port of the original trend.py; these lock it down
against a hand-computed fixture.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import clock          # noqa: E402
import hermes_api     # noqa: E402
import store          # noqa: E402


def _seed():
    """A 3-day fixture: AAA trends hard on the latest day, BBB is a fresh one-off.

    day D-2 : 100 comments, AAA x5
    day D-1 : 100 comments, AAA x5
    day D0  : 100 comments, AAA x20 (3 of them $cashtags), BBB x2
    -> AAA prior share-of-voice avg 0.05, today 0.20  => HOT
       AAA first seen D-2 (<= 2 days ago)             => NEW
       AAA has cashtags                               => $
    """
    today = clock.market_today()
    days = [today - timedelta(days=2), today - timedelta(days=1), today]
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    conn = store.connect(path)

    base = 1_700_000_000  # fixed, well outside any --recent-hours window
    cid = 0
    for di, day in enumerate(days):
        tid = f"t{di}"
        conn.execute(
            "INSERT INTO threads (id, kind, subreddit, title, created_utc, "
            "first_seen, is_open, trading_day, post_json) "
            "VALUES (?, 'daily', 'wallstreetbets', ?, ?, '2020-01-01T00:00:00+00:00', 1, ?, '{}')",
            (tid, f"Daily Discussion Thread for {day}", base + di * 86400, day.isoformat()),
        )
        aaa = 5 if di < 2 else 20
        for n in range(100):
            cid += 1
            body = f"comment {cid}"
            if n < aaa:
                body = ("$AAA to the moon" if (di == 2 and n < 3) else "AAA looking good")
            if di == 2 and n >= aaa and n < aaa + 2:
                body = "BBB anyone?"
            conn.execute(
                "INSERT INTO comments (id, thread_id, parent_id, author, created_utc, "
                "body, retrieved_at) VALUES (?, ?, ?, 'u', ?, ?, '2020-01-01T00:00:00+00:00')",
                (f"c{cid}", tid, "t3_" + tid, base + di * 86400 + n, body),
            )
    conn.commit()

    # regex extraction over the fixture
    import extract
    extract.extract_pending(conn, {"AAA", "BBB"}, verbose=False)
    return conn, days


def test_trend_flags_and_order():
    conn, days = _seed()
    rep = hermes_api.trend(conn, days=3, min_total=2, stale_days=0)

    syms = [r.symbol for r in rep.rows]
    assert syms[:2] == ["AAA", "BBB"], syms  # AAA scores higher

    aaa = next(r for r in rep.rows if r.symbol == "AAA")
    assert aaa.total == 30
    assert aaa.per_day == {days[0].isoformat(): 5, days[1].isoformat(): 5,
                           days[2].isoformat(): 20}
    assert aaa.cashtags == 3
    assert aaa.flags == ["NEW", "HOT", "$"], aaa.flags
    assert aaa.recent_n == 0  # fixture timestamps are ancient

    bbb = next(r for r in rep.rows if r.symbol == "BBB")
    assert bbb.flags == ["NEW"], bbb.flags
    assert "HOT" not in bbb.flags

    # coverage classification
    tags = {day: tag for day, _c, _k, tag in rep.coverage}
    assert tags[days[0].isoformat()] == "partial"
    assert tags[days[2].isoformat()] == "live (in progress)"


def test_trend_text_render():
    conn, days = _seed()
    text = hermes_api.trend(conn, days=3, stale_days=0).to_text()
    assert "WSB ticker trends" in text
    assert "SYM" in text and "FLAGS" in text
    # AAA row: TOT=30, $=3, sparkline 5 / 5 / 20
    line = next(ln for ln in text.splitlines() if ln.startswith("AAA"))
    assert "30" in line and "NEW HOT $" in line


def test_min_total_and_cashtags_only():
    conn, _ = _seed()
    assert [r.symbol for r in hermes_api.trend(conn, days=3, min_total=25, stale_days=0).rows] == ["AAA"]
    assert [r.symbol for r in hermes_api.trend(conn, days=3, cashtags_only=True, stale_days=0).rows] == ["AAA"]


def test_symbol_detail():
    conn, _ = _seed()
    d = hermes_api.symbol_detail(conn, "aaa")
    assert d.symbol == "AAA"
    assert len(d.comments) == 30
    assert d.comments[0].created_utc <= d.comments[-1].created_utc
    assert "3 comment" not in d.to_text()  # sanity: it's 30
    assert hermes_api.symbol_detail(conn, "ZZZ").to_text().startswith("No stored comments")


def test_search_comments():
    conn, _ = _seed()
    assert hermes_api.search_comments(conn, query="BBB").comments[0].body == "BBB anyone?"
    r = hermes_api.search_comments(conn, symbol="AAA", limit=5)
    assert len(r.comments) == 5 and r.truncated
    assert hermes_api.search_comments(conn, query="nothingmatches").comments == []


def test_threads_and_run_status():
    conn, days = _seed()
    ts = hermes_api.threads(conn, kind="daily")
    assert len(ts) == 3
    assert ts[0].created_utc >= ts[-1].created_utc  # newest first
    assert all(t.n_comments == 100 for t in ts)

    rs = hermes_api.run_status(conn)
    assert rs.stats["threads"] == 3 and rs.stats["comments"] == 300


def test_day_report():
    conn, days = _seed()
    rep = hermes_api.day_report(conn, days[2].isoformat())
    top = rep.rows[0]
    assert top[0] == "AAA" and top[-1] == 20
    assert "AAA" in rep.to_text()


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
