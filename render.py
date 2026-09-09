#!/usr/bin/env python3
"""Markdown renderers for chat / the Phase 8 MCP server.

    python render.py thread 1wbh9od --depth 3 --top 8
    python render.py trend --days 5 --top 15
    python render.py symbol AEO

Three views:
  * thread digest -- a depth- and score-limited walk of the comment tree
  * trend table   -- the SYM table inside a code fence (Telegram renders no
                     Markdown tables, but monospace keeps the columns aligned)
  * symbol report -- the comments behind one symbol, newest first

`hermes_api` owns the queries and the maths; this module only formats.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import hermes_api
import store

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_INDENT = "  "


def _clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _when(epoch) -> str:
    if not epoch:
        return "?"
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%m-%d %H:%M")


# --------------------------------------------------------------------------- #
#  thread digest                                                               #
# --------------------------------------------------------------------------- #
def thread_digest(tree: dict, *, max_depth: int = 4, max_replies: int = 4,
                  body_chars: int = 240, min_score: int | None = None,
                  top: int | None = None, details: bool = False) -> str:
    meta = tree["meta"]
    out: list[str] = [
        f"### [{meta['kind']}] {meta['title']}",
        f"r/{meta['subreddit']} · {meta.get('trading_day') or meta.get('created_utc')} · "
        f"{meta['stored_comments']} comments · {meta['permalink']}",
        "",
    ]

    roots = tree["comments"]
    if min_score is not None:
        roots = [c for c in roots if (c.get("score") or 0) >= min_score]
    roots = sorted(roots, key=lambda c: -(c.get("score") or 0))
    shown, hidden = (roots[:top], roots[top:]) if top else (roots, [])

    for node in shown:
        _emit(node, out, depth=0, max_depth=max_depth, max_replies=max_replies,
              body_chars=body_chars, min_score=min_score, details=details)
    if hidden:
        out.append(f"\n_({len(hidden)} more top-level comments)_")
    return "\n".join(out).rstrip()


def _emit(node: dict, out: list[str], *, depth: int, max_depth: int,
          max_replies: int, body_chars: int, min_score, details: bool) -> None:
    pad = _INDENT * depth
    author = node.get("author") or "[deleted]"
    score = node.get("score")
    head = f"{pad}- **u/{author}**" + (f" ({score:+d})" if isinstance(score, int) else "")
    head += f" · {_when(node.get('created_utc'))}"
    out.append(head)
    body = _clip(node.get("body") or "", body_chars)
    if body:
        out.append(f"{pad}{_INDENT}{body}")

    kids = node.get("replies") or []
    if min_score is not None:
        kids = [k for k in kids if (k.get("score") or 0) >= min_score]
    kids = sorted(kids, key=lambda c: -(c.get("score") or 0))
    if not kids:
        return

    if depth + 1 >= max_depth:
        n = _count(kids)
        if details:
            out.append(f"{pad}{_INDENT}<details><summary>{n} more in thread</summary>")
            for k in kids:
                _emit(k, out, depth=depth + 1, max_depth=depth + 99,
                      max_replies=max_replies, body_chars=body_chars,
                      min_score=min_score, details=False)
            out.append(f"{pad}{_INDENT}</details>")
        else:
            out.append(f"{pad}{_INDENT}_({n} more repl{'y' if n == 1 else 'ies'})_")
        return

    for k in kids[:max_replies]:
        _emit(k, out, depth=depth + 1, max_depth=max_depth, max_replies=max_replies,
              body_chars=body_chars, min_score=min_score, details=details)
    if len(kids) > max_replies:
        extra = _count(kids[max_replies:])
        out.append(f"{pad}{_INDENT}_({extra} more in this subthread)_")


def _count(nodes: list[dict]) -> int:
    return sum(1 + _count(n.get("replies") or []) for n in nodes)


# --------------------------------------------------------------------------- #
#  trend & symbol                                                              #
# --------------------------------------------------------------------------- #
def trend_block(report) -> str:
    """The SYM table in a code fence (Telegram-safe)."""
    if not report.shown_days:
        return "_No thread data in this window yet._"
    body = report.to_text()
    return f"```\n{body}\n```"


def symbol_block(detail, *, limit: int = 15) -> str:
    lines: list[str] = []
    if getattr(detail, "sentiment", None):
        lines.append(f"**{detail.symbol}** sentiment (LLM):")
        for s in detail.sentiment[:7]:
            conf = f" {s.confidence:.0%}" if s.confidence is not None else ""
            lines.append(f"- `{s.day}` **{s.label.upper()}**{conf} — {s.rationale or ''}")
        lines.append("")
    if not detail.comments:
        lines.append(f"_No stored comments mention {detail.symbol}._")
        return "\n".join(lines)
    n = len(detail.comments)
    recent = detail.comments[-limit:][::-1]
    lines += [f"**{detail.symbol}** — {n} mention{'s' if n != 1 else ''} "
              f"(showing {len(recent)} most recent)", ""]
    for c in recent:
        lines.append(f"- _{c.trading_day} {c.kind}_ · u/{c.author or '[deleted]'} "
                     f"· `{c.tier or '-'}`")
        lines.append(f"  {_clip(c.body, 200)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  CLI                                                                         #
# --------------------------------------------------------------------------- #
def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default=str(store.DEFAULT_DB))
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("thread", help="comment-tree digest")
    t.add_argument("id")
    t.add_argument("--depth", type=int, default=4)
    t.add_argument("--replies", type=int, default=4)
    t.add_argument("--body-chars", type=int, default=240)
    t.add_argument("--min-score", type=int, default=None)
    t.add_argument("--top", type=int, default=None)
    t.add_argument("--details", action="store_true", help="use <details> for deep subtrees")

    tr = sub.add_parser("trend", help="SYM table")
    tr.add_argument("--days", type=int, default=5)
    tr.add_argument("--top", type=int, default=None)
    tr.add_argument("--stale-days", type=int, default=2)
    tr.add_argument("--include-flair", nargs="?", const="gain,loss,discussion", default=None)
    tr.add_argument("--llm", action="store_true", help="LLM-blended tier + sentiment tags")

    s = sub.add_parser("symbol", help="comments behind a symbol")
    s.add_argument("symbol")
    s.add_argument("--days", type=int, default=None)
    s.add_argument("--limit", type=int, default=15)

    args = p.parse_args()
    conn = store.connect(args.db)

    if args.cmd == "thread":
        tree = store.get_tree(conn, args.id)
        print(thread_digest(tree, max_depth=args.depth, max_replies=args.replies,
                            body_chars=args.body_chars, min_score=args.min_score,
                            top=args.top, details=args.details))
    elif args.cmd == "trend":
        kinds = list(hermes_api.MEGA_KINDS)
        if args.include_flair is not None:
            kinds += hermes_api.expand_kinds(args.include_flair.split(","))
        report = hermes_api.trend(conn, days=args.days, top_n=args.top,
                                  stale_days=args.stale_days, kinds=kinds,
                                  entity_mode="best" if args.llm else "regex",
                                  with_sentiment=args.llm)
        print(trend_block(report))
    elif args.cmd == "symbol":
        print(symbol_block(
            hermes_api.symbol_detail(conn, args.symbol, days=args.days,
                                     with_sentiment=True),
            limit=args.limit))


if __name__ == "__main__":
    main()
