#!/usr/bin/env python3
"""Read-only MCP server over the digger store, for Hermes Agent (Phase 8).

Speaks the Model Context Protocol on stdio (newline-delimited JSON-RPC 2.0) --
no SDK, stdlib only. Exposes the `hermes_api` query layer as seven tools; Hermes
Agent (or any MCP client) calls them and its own model (DeepSeek) turns the
results into chat answers.

    python mcp_server.py                 # serve on stdio (what Hermes launches)
    python mcp_server.py --selftest      # run the handshake + every tool locally
    python mcp_server.py --list          # print the tool schemas as JSON

DB path: --db, else $HERMES_DB, else the default next to the code. The connection
is opened query-only.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sqlite3
import sys
import traceback

import hermes_api
import render
import store

# MCP is UTF-8 on stdio; be explicit (Windows dev boxes default to cp1252).
for _s in (sys.stdin, sys.stdout):
    try:
        _s.reconfigure(encoding="utf-8", newline="\n")
    except Exception:
        pass

SERVER_INFO = {"name": "hermes-wsb", "version": "1.0.0"}
DEFAULT_PROTOCOL = "2025-06-18"
SUPPORTED_PROTOCOLS = {"2025-06-18", "2025-03-26", "2024-11-05"}


def _asdict(obj):
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if isinstance(obj, (list, tuple)):
        return [_asdict(x) for x in obj]
    return obj


# --------------------------------------------------------------------------- #
#  tools                                                                       #
# --------------------------------------------------------------------------- #
def _kinds(include_flair) -> list[str]:
    k = list(hermes_api.MEGA_KINDS)
    if include_flair:
        names = include_flair if isinstance(include_flair, str) else "gain,loss,discussion"
        k += hermes_api.expand_kinds(names.split(","))
    return k


def tool_get_trend(conn, a: dict):
    rep = hermes_api.trend(
        conn, days=a.get("days", 5), top_n=a.get("top", 15),
        min_total=a.get("min_mentions", 2), stale_days=a.get("stale_days", 2),
        entity_mode=a.get("entity_mode", "best"),
        with_sentiment=a.get("sentiment", True),
        kinds=_kinds(a.get("include_flair")),
    )
    return rep.to_text(), {
        "generated_et": rep.generated_et, "today": rep.today,
        "coverage": [dict(zip(("day", "comments", "kinds", "tag"), c)) for c in rep.coverage],
        "rows": [_asdict(r) for r in rep.rows],
        "total_matching": rep.total_matching,
    }


def tool_get_symbol(conn, a: dict):
    d = hermes_api.symbol_detail(conn, a["symbol"], days=a.get("days"),
                                 entity_mode=a.get("entity_mode", "best"),
                                 with_sentiment=True)
    text = render.symbol_block(d, limit=a.get("limit", 15))
    return text, {
        "symbol": d.symbol,
        "mention_count": len(d.comments),
        "sentiment": [_asdict(s) for s in d.sentiment],
        "comments": [_asdict(c) for c in d.comments[-a.get("limit", 15):]],
    }


def tool_get_sentiment(conn, a: dict):
    if a.get("symbol"):
        hist = hermes_api.symbol_sentiment(conn, a["symbol"], limit=a.get("days", 14))
        lines = [f"{s.day}  {s.label.upper()}  {s.rationale or ''}" for s in hist]
        return (f"Sentiment history for {a['symbol'].upper()}:\n" + "\n".join(lines)
                if hist else f"no sentiment scored for {a['symbol'].upper()}",
                {"symbol": a["symbol"].upper(), "history": [_asdict(s) for s in hist]})
    board = hermes_api.sentiment_board(conn, day=a.get("day"), limit=a.get("limit", 30))
    return board.to_text(), {
        "day": board.day,
        "rows": [dict(zip(("symbol", "label", "confidence", "rationale"), r)) for r in board.rows],
    }


def tool_search_comments(conn, a: dict):
    res = hermes_api.search_comments(
        conn, query=a.get("query"), symbol=a.get("symbol"), since=a.get("since"),
        until=a.get("until"), kind=a.get("kind"), limit=a.get("limit", 20))
    return res.to_text(), {
        "truncated": res.truncated,
        "comments": [_asdict(c) for c in res.comments],
    }


def tool_get_thread(conn, a: dict):
    ref = str(a["thread"]).strip()
    if ref.lower() in hermes_api.MEGA_KINDS:
        rows = hermes_api.threads(conn, kind=ref.lower())
        if not rows:
            return f"no {ref} thread stored yet", {"error": "not found"}
        ref = rows[0].id
    tree = store.get_tree(conn, ref)
    text = render.thread_digest(tree, max_depth=a.get("depth", 3),
                                max_replies=a.get("replies", 5),
                                min_score=a.get("min_score"), top=a.get("top", 8))
    return text, {"meta": tree["meta"], "top_level": len(tree["comments"])}


def tool_list_threads(conn, a: dict):
    rows = hermes_api.threads(conn, since=a.get("since"), kind=a.get("kind"))
    rows = rows[:a.get("limit", 20)]
    text = "\n".join(f"{r.trading_day}  [{r.kind:<16}] {r.id}  {r.n_comments:>6}c  "
                     f"{'open' if r.is_open else 'closed'}  {r.title[:60]}" for r in rows) \
        or "no threads match"
    return text, {"threads": [_asdict(r) for r in rows]}


def tool_run_status(conn, a: dict):
    rs = hermes_api.run_status(conn, limit=a.get("limit", 8))
    return rs.to_text(), {"stats": rs.stats, "recent_runs": rs.recent_runs}


_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}

TOOLS = [
    {
        "name": "get_trend",
        "description": "Most-mentioned tickers/coins on r/wallstreetbets over the last N "
                       "trading days: per-day counts, cashtag confirmation, NEW/HOT flags, "
                       "and (default on) the LLM BUY/SELL/NEU tag.",
        "inputSchema": {"type": "object", "properties": {
            "days": {**_INT, "description": "window size (default 5)"},
            "top": {**_INT, "description": "rows to return (default 15)"},
            "min_mentions": _INT, "stale_days": _INT,
            "entity_mode": {"type": "string", "enum": list(hermes_api.ENTITY_MODES),
                            "description": "regex | llm | best (default best)"},
            "sentiment": {**_BOOL, "description": "append BUY/SELL/NEU (default true)"},
            "include_flair": {"description": "also count gain/loss/discussion posts",
                              "type": ["boolean", "string"]},
        }},
        "handler": tool_get_trend,
    },
    {
        "name": "get_symbol",
        "description": "Everything on one ticker/coin: recent comments that mention it "
                       "and its per-day LLM sentiment with rationale.",
        "inputSchema": {"type": "object", "required": ["symbol"], "properties": {
            "symbol": _STR, "days": _INT, "limit": _INT,
            "entity_mode": {"type": "string", "enum": list(hermes_api.ENTITY_MODES)},
        }},
        "handler": tool_get_symbol,
    },
    {
        "name": "get_sentiment",
        "description": "With `symbol`: that symbol's day-by-day buy/sell/neutral history. "
                       "Without: the latest day's sentiment board, bullish first.",
        "inputSchema": {"type": "object", "properties": {
            "symbol": _STR, "day": {**_STR, "description": "YYYY-MM-DD (board mode)"},
            "days": _INT, "limit": _INT,
        }},
        "handler": tool_get_sentiment,
    },
    {
        "name": "search_comments",
        "description": "Find stored comments by free text, and/or symbol, and/or time "
                       "range, and/or thread kind. Newest first.",
        "inputSchema": {"type": "object", "properties": {
            "query": _STR, "symbol": _STR,
            "since": {**_STR, "description": "ISO datetime/date or epoch"},
            "until": _STR, "kind": _STR, "limit": _INT,
        }},
        "handler": tool_search_comments,
    },
    {
        "name": "get_thread",
        "description": "A comment-tree digest for one thread. `thread` is a base-36 id, a "
                       "reddit URL, or one of daily/moves/weekend (latest of that kind).",
        "inputSchema": {"type": "object", "required": ["thread"], "properties": {
            "thread": _STR, "depth": _INT, "replies": _INT, "top": _INT, "min_score": _INT,
        }},
        "handler": tool_get_thread,
    },
    {
        "name": "list_threads",
        "description": "Threads in the store: id, kind, trading day, comment count, open/closed.",
        "inputSchema": {"type": "object", "properties": {
            "since": _STR, "kind": _STR, "limit": _INT,
        }},
        "handler": tool_list_threads,
    },
    {
        "name": "run_status",
        "description": "Digger health: thread/comment counts, the discovery watermark, "
                       "and the last few hourly runs.",
        "inputSchema": {"type": "object", "properties": {"limit": _INT}},
        "handler": tool_run_status,
    },
]
BY_NAME = {t["name"]: t for t in TOOLS}


# --------------------------------------------------------------------------- #
#  JSON-RPC / MCP plumbing                                                     #
# --------------------------------------------------------------------------- #
class Server:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.protocol = DEFAULT_PROTOCOL

    def handle(self, msg: dict):
        """Return a response dict, or None for notifications."""
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            requested = params.get("protocolVersion")
            self.protocol = requested if requested in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
            return self._ok(mid, {
                "protocolVersion": self.protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            })

        if method in ("notifications/initialized", "notifications/cancelled"):
            return None

        if method == "ping":
            return self._ok(mid, {})

        if method == "tools/list":
            return self._ok(mid, {"tools": [
                {k: t[k] for k in ("name", "description", "inputSchema")} for t in TOOLS
            ]})

        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            tool = BY_NAME.get(name)
            if tool is None:
                return self._err(mid, -32602, f"unknown tool: {name}")
            try:
                text, structured = tool["handler"](self.conn, args)
            except Exception as exc:  # tool failure -> result with isError, not protocol error
                return self._ok(mid, {
                    "content": [{"type": "text", "text": f"error: {exc}"}],
                    "isError": True,
                })
            return self._ok(mid, {
                "content": [{"type": "text", "text": text}],
                "structuredContent": structured if isinstance(structured, dict) else {"result": structured},
                "isError": False,
            })

        if mid is None:
            return None
        return self._err(mid, -32601, f"method not found: {method}")

    @staticmethod
    def _ok(mid, result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _err(mid, code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def serve_stdio(conn) -> None:
    server = Server(conn)
    out = sys.stdout

    def send(obj) -> bool:
        try:
            out.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
            out.flush()
            return True
        except (BrokenPipeError, OSError):
            return False   # client went away

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            if not send({"jsonrpc": "2.0", "id": None,
                         "error": {"code": -32700, "message": "parse error"}}):
                return
            continue
        try:
            resp = server.handle(msg)
        except Exception:
            resp = {"jsonrpc": "2.0", "id": msg.get("id"),
                    "error": {"code": -32603, "message": traceback.format_exc(limit=2)}}
        if resp is not None and not send(resp):
            return


# --------------------------------------------------------------------------- #
def _open(db_path) -> sqlite3.Connection:
    conn = store.connect(db_path)          # ensures schema, WAL
    conn.execute("PRAGMA query_only = 1")
    return conn


def _selftest(conn) -> int:
    server = Server(conn)
    script = [
        {"jsonrpc": "2.0", "id": 0, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "run_status", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "get_trend", "arguments": {"days": 2, "top": 5}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "list_threads", "arguments": {"limit": 3}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "get_sentiment", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "get_thread", "arguments": {"thread": "daily", "top": 2, "depth": 1}}},
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
         "params": {"name": "search_comments", "arguments": {"query": "calls", "limit": 2}}},
        {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
         "params": {"name": "nope", "arguments": {}}},
    ]
    failures = 0
    checks = 0
    for req in script:
        resp = server.handle(req)
        if "id" not in req:
            print(f"notify {req['method']}: ok")
            continue
        checks += 1
        tag = req.get("params", {}).get("name", req["method"])
        expect_error = tag == "nope"

        if expect_error:
            ok = resp is not None and "error" in resp
            print(f"{tag:<16} {'ok (rejected)' if ok else 'FAIL ' + str(resp)}")
            failures += not ok
            continue
        if resp is None or "error" in resp:
            failures += 1
            print(f"FAIL {tag}: {resp}")
            continue
        result = resp["result"]
        if req["method"] == "initialize":
            print(f"initialize: protocol {result['protocolVersion']}")
        elif req["method"] == "tools/list":
            print(f"tools/list: {len(result['tools'])} tools")
        else:  # tools/call
            head = (result["content"][0]["text"].splitlines() or [""])[0][:70]
            print(f"{tag:<16} isError={result.get('isError')}  {head}")
            failures += bool(result.get("isError"))
    print(f"\nselftest: {checks - failures} ok" + (f", {failures} FAILED" if failures else ""))
    return 1 if failures else 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default=os.environ.get("HERMES_DB", str(store.DEFAULT_DB)))
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--list", action="store_true", help="print tool schemas and exit")
    args = p.parse_args()

    if args.list:
        print(json.dumps([{k: t[k] for k in ("name", "description", "inputSchema")}
                          for t in TOOLS], indent=2))
        return

    conn = _open(args.db)
    if args.selftest:
        sys.exit(_selftest(conn))
    serve_stdio(conn)


if __name__ == "__main__":
    main()
