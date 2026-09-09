"""Read-only query layer over the digger's SQLite store.

Everything the reports (`trend.py`, `report.py`) and, later, the Hermes MCP
server (Phase 8) need to ask about the archive lives here as plain functions
that return dataclasses. Each dataclass renders itself for a terminal
(`.to_text()`) or a chat message (`.to_markdown()`).

The trend maths is ported verbatim from the original `trend.py`: mentions are
counted per **trading day** (`threads.trading_day` -- the Daily Discussion and
that evening's "moves" thread merge), the `HOT` flag compares *share of voice*
(mentions per comment) so partial and full days stay comparable, and the row
score mixes recency, today's volume and cashtag confirmation.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import clock
import store

MEGA_KINDS: tuple[str, ...] = ("daily", "moves", "weekend")

ENTITY_MODES = ("regex", "llm", "best")


def _entity_where(mode: str) -> str:
    """SQL predicate on alias ``e`` selecting which entity tier(s) to count.

    * ``regex`` -- the cheap first pass only (default; matches pre-Phase-6).
    * ``llm``   -- the DeepSeek tier only.
    * ``best``  -- LLM rows where the comment was LLM-scanned (so the LLM can
      veto a bad regex bareword), else the regex rows.
    """
    if mode == "llm":
        return "e.source = 'llm'"
    if mode == "best":
        return ("(e.source = 'llm' OR (e.source = 'regex' AND e.comment_id NOT IN "
                "(SELECT comment_id FROM entities WHERE source = 'llm')))")
    return "e.source = 'regex'"

# A full live trading day runs into the thousands of comments; well below that is
# a partial sample (a backfill, or a day still in progress early on).
PARTIAL_COMMENT_THRESHOLD = 800


def _placeholders(values) -> str:
    return ",".join("?" * len(values))


def expand_kinds(names) -> list[str]:
    """'daily','gain' -> 'daily','flair:gain'. Megathread names pass through;
    anything else becomes a flair kind."""
    out = []
    for n in names:
        n = n.strip().lower()
        if not n:
            continue
        out.append(n if n in MEGA_KINDS else f"flair:{n}")
    return out


def _connect(db) -> sqlite3.Connection:
    return db if isinstance(db, sqlite3.Connection) else store.connect(db)


# --------------------------------------------------------------------------- #
#  trend                                                                       #
# --------------------------------------------------------------------------- #
_SENT_TOKEN = {"buy": "BUY", "sell": "SELL", "neutral": "NEU"}


@dataclass
class TrendRow:
    symbol: str
    total: int
    cashtags: int
    first_seen: str
    per_day: dict[str, int]
    today_n: int
    recent_n: int
    flags: list[str]
    score: int
    sentiment: str | None = None            # buy | sell | neutral (latest symbol_day)
    sentiment_conf: float | None = None


@dataclass
class TrendReport:
    window_days: list[str]
    shown_days: list[str]
    today: str
    coverage: list[tuple[str, int, str, str]]   # day, count, kinds, tag
    rows: list[TrendRow]
    total_matching: int
    recent_hours: float
    stale_days: int
    top_n: int | None
    generated_et: str

    # -- rendering ------------------------------------------------------- #
    def _header(self) -> str:
        recent_label = f"{self.recent_hours:g}h"
        day_labels = "".join(f"{d[5:]:>6}" for d in self.shown_days)
        return (f"{'SYM':<6}{'TOT':>4}{'$':>3}  {'1ST':<6}{day_labels}"
                f"{recent_label:>6}  FLAGS")

    def to_text(self) -> str:
        L: list[str] = []
        L.append(f"WSB ticker trends — {len(self.window_days)}-day window, "
                 f"as of {self.generated_et}")
        if self.today != self.window_days[-1]:
            L.append(f"(latest day with data: {self.today})")
        L.append("")
        L.append("Coverage (comments archived per day, both threads merged):")
        for day, count, kinds, tag in self.coverage:
            L.append(f"  {day}   {count:>5}   {kinds:<12}{tag}")
        L.append("")

        if self.top_n is not None:
            L.append(f"Showing top {len(self.rows)} of {self.total_matching} "
                     f"(by relevance score)\n")

        header = self._header()
        L.append(header)
        L.append("-" * len(header))
        if not self.rows:
            L.append("(nothing above the --min threshold yet)")
            return "\n".join(L)

        for r in self.rows:
            spark = "".join(f"{r.per_day.get(d, 0) or '·':>6}" for d in self.shown_days)
            L.append(f"{r.symbol:<6}{r.total:>4}{r.cashtags:>3}  {r.first_seen[5:]:<6}"
                     f"{spark}{r.recent_n:>6}  {' '.join(r.flags)}")

        recent_label = f"{self.recent_hours:g}h"
        L.append(
            f"\nColumns: TOT=mentions in window, $=how many were cashtags, "
            f"1ST=first-seen date,\nthen mentions per day, then mentions in the "
            f"last {recent_label}.\n"
            "Notes: 'partial' days hold far fewer comments than a full day, so raw\n"
            "counts aren't comparable — HOT uses share-of-voice (mentions per comment).\n"
            "No '$' = bareword-only match; words like TIME/BE/OR/UP collide with real tickers.\n"
            f"Stale symbols (nothing in {self.stale_days} day(s)) are hidden; use --stale-days 0\n"
            "to see them, or `report.py --symbol SYM` to inspect the comments behind a row."
        )
        return "\n".join(L)

    def to_markdown(self) -> str:
        L = [f"**WSB ticker trends** — {len(self.window_days)}-day window, "
             f"as of {self.generated_et}"]
        if self.today != self.window_days[-1]:
            L.append(f"_latest day with data: {self.today}_")
        L.append("")
        cols = "SYM | TOT | $ | 1ST | " + " | ".join(d[5:] for d in self.shown_days) + \
               f" | {self.recent_hours:g}h | FLAGS"
        L.append(cols)
        L.append(" | ".join(["---"] * (5 + len(self.shown_days) + 1)))
        for r in self.rows:
            spark = " | ".join(str(r.per_day.get(d, 0)) for d in self.shown_days)
            L.append(f"{r.symbol} | {r.total} | {r.cashtags} | {r.first_seen[5:]} | "
                     f"{spark} | {r.recent_n} | {' '.join(r.flags)}")
        return "\n".join(L)


def _window_days(days: int) -> list[str]:
    today = clock.market_today()
    return [(today - timedelta(days=n)).isoformat() for n in range(days - 1, -1, -1)]


def _sov(mentions: int, comments: int) -> float:
    return mentions / comments if comments else 0.0


def trend(db, *, days: int = 5, min_total: int = 2, recent_hours: float = 4.0,
          stale_days: int = 2, top_n: int | None = None,
          cashtags_only: bool = False, kinds=MEGA_KINDS,
          entity_mode: str = "regex", with_sentiment: bool = False) -> TrendReport:
    conn = _connect(db)
    kinds = list(kinds)
    window = _window_days(days)
    start = window[0]
    kph = _placeholders(kinds)
    ent = _entity_where(entity_mode)

    # comments per trading day (+ which thread kinds contributed), merged
    counts: dict[str, int] = {}
    kinds_by_day: dict[str, str] = {}
    for day, n, klist in conn.execute(
        f"""SELECT t.trading_day, COUNT(c.id), GROUP_CONCAT(DISTINCT t.kind)
            FROM threads t LEFT JOIN comments c ON c.thread_id = t.id
            WHERE t.trading_day >= ? AND t.kind IN ({kph})
            GROUP BY t.trading_day""",
        [start, *kinds],
    ):
        counts[day] = n
        kinds_by_day[day] = "+".join(sorted((klist or "").split(",")))

    # mentions per (day, symbol), under the chosen entity tier
    by_symbol: dict[str, dict[str, int]] = defaultdict(dict)
    for day, symbol, mentions in conn.execute(
        f"""SELECT t.trading_day, e.symbol, COUNT(DISTINCT e.comment_id)
            FROM entities e
            JOIN comments c ON c.id = e.comment_id
            JOIN threads t ON t.id = c.thread_id
            WHERE t.trading_day >= ? AND e.symbol != '__none__' AND t.kind IN ({kph})
              AND {ent}
            GROUP BY t.trading_day, e.symbol""",
        [start, *kinds],
    ):
        by_symbol[symbol][day] = mentions

    # cashtag confirmation is always a regex-tier signal, independent of mode
    cashtags: dict[str, int] = defaultdict(int)
    for symbol, ct in conn.execute(
        f"""SELECT e.symbol, COUNT(DISTINCT e.comment_id)
            FROM entities e
            JOIN comments c ON c.id = e.comment_id
            JOIN threads t ON t.id = c.thread_id
            WHERE t.trading_day >= ? AND t.kind IN ({kph})
              AND e.source = 'regex' AND e.tier = 'cashtag'
            GROUP BY e.symbol""",
        [start, *kinds],
    ):
        cashtags[symbol] = ct

    recent_cutoff = int(datetime.now(timezone.utc).timestamp() - recent_hours * 3600)
    recent = dict(conn.execute(
        f"""SELECT e.symbol, COUNT(DISTINCT e.comment_id)
            FROM entities e
            JOIN comments c ON c.id = e.comment_id
            JOIN threads t ON t.id = c.thread_id
            WHERE c.created_utc >= ? AND e.symbol != '__none__' AND t.kind IN ({kph})
              AND {ent}
            GROUP BY e.symbol""",
        [recent_cutoff, *kinds],
    ))

    shown_days = [d for d in window if d in counts]
    today = shown_days[-1] if shown_days else window[-1]
    market_today = clock.market_today()

    coverage: list[tuple[str, int, str, str]] = []
    for day in shown_days:
        c = counts[day]
        tag = ("live (in progress)" if day == today
               else "partial" if c < PARTIAL_COMMENT_THRESHOLD else "full")
        coverage.append((day, c, kinds_by_day.get(day, ""), tag))

    scored: list[TrendRow] = []
    for symbol, per_day in by_symbol.items():
        total = sum(per_day.values())
        if total < min_total:
            continue
        first_seen, last_seen = min(per_day), max(per_day)
        today_n = per_day.get(today, 0)
        ct = cashtags.get(symbol, 0)

        if stale_days and (date.fromisoformat(today) - date.fromisoformat(last_seen)).days >= stale_days:
            continue
        if cashtags_only and ct == 0:
            continue

        prior = [_sov(n, counts.get(d, 0)) for d, n in per_day.items() if d < today]
        prior_avg = sum(prior) / len(prior) if prior else 0.0
        today_sov = _sov(today_n, counts.get(today, 0))

        flags: list[str] = []
        if (market_today - date.fromisoformat(first_seen)).days <= 2:
            flags.append("NEW")
        if prior_avg > 0 and today_n >= 2 and today_sov >= 2 * prior_avg:
            flags.append("HOT")
        if ct > 0:
            flags.append("$")

        recent_n = recent.get(symbol, 0)
        score = (recent_n * 4 + today_n * 2 + min(ct, 5)
                 + (3 if "HOT" in flags else 0) + (2 if "NEW" in flags else 0))
        scored.append(TrendRow(symbol, total, ct, first_seen, dict(per_day),
                               today_n, recent_n, flags, score))

    scored.sort(key=lambda r: (-r.score, -r.total, r.symbol))
    total_matching = len(scored)
    if top_n is not None:
        scored = scored[:top_n]

    if with_sentiment and scored:
        sent = store.latest_symbol_sentiment(conn, [r.symbol for r in scored], today)
        for r in scored:
            row = sent.get(r.symbol)
            if row is not None:
                r.sentiment = row["label"]
                r.sentiment_conf = row["confidence"]
                r.flags.append(_SENT_TOKEN.get(row["label"], "NEU"))

    return TrendReport(
        window_days=window, shown_days=shown_days, today=today, coverage=coverage,
        rows=scored, total_matching=total_matching, recent_hours=recent_hours,
        stale_days=stale_days, top_n=top_n,
        generated_et=clock.market_now().strftime("%Y-%m-%d %H:%M ET"),
    )


# --------------------------------------------------------------------------- #
#  single-symbol drill-down                                                    #
# --------------------------------------------------------------------------- #
@dataclass
class SymbolComment:
    trading_day: str
    kind: str
    tier: str | None
    author: str | None
    created_utc: int | None
    body: str

    @property
    def when(self) -> str:
        if not self.created_utc:
            return "?"
        return datetime.fromtimestamp(self.created_utc, timezone.utc).strftime("%Y-%m-%d %H:%M")


@dataclass
class DaySentiment:
    day: str
    label: str
    confidence: float | None
    rationale: str | None


@dataclass
class SymbolDetail:
    symbol: str
    comments: list[SymbolComment]
    sentiment: list[DaySentiment] = field(default_factory=list)

    def to_text(self) -> str:
        L: list[str] = []
        if self.sentiment:
            L.append(f"Sentiment for {self.symbol} (LLM, per day):")
            for s in self.sentiment:
                conf = f" {s.confidence:.2f}" if s.confidence is not None else ""
                L.append(f"  {s.day}  {s.label.upper():<8}{conf}  {s.rationale or ''}")
            L.append("")
        if not self.comments:
            L.append(f"No stored comments mention {self.symbol}.")
            return "\n".join(L)
        L.append(f"{len(self.comments)} comment(s) mentioning {self.symbol}:\n")
        for c in self.comments:
            L.append(f"[{c.trading_day} {c.kind}] [{c.tier or '-'}] {c.when}  {c.author}")
            L.append(f"  {c.body}\n")
        return "\n".join(L)


def symbol_sentiment(db, symbol: str, *, limit: int = 14) -> list[DaySentiment]:
    conn = _connect(db)
    return [
        DaySentiment(r["window_start"], r["label"], r["confidence"], r["rationale"])
        for r in store.sentiment_history(conn, symbol, limit)
    ]


def symbol_detail(db, symbol: str, *, days: int | None = None,
                  entity_mode: str = "regex",
                  with_sentiment: bool = False) -> SymbolDetail:
    conn = _connect(db)
    symbol = symbol.upper()
    sql = [f"""SELECT DISTINCT t.trading_day, t.kind, e.tier, c.author, c.created_utc, c.body
              FROM entities e
              JOIN comments c ON c.id = e.comment_id
              JOIN threads t ON t.id = c.thread_id
              WHERE e.symbol = ? AND {_entity_where(entity_mode)}"""]
    params: list = [symbol]
    if days:
        cutoff = (clock.market_today() - timedelta(days=days - 1)).isoformat()
        sql.append("AND t.trading_day >= ?")
        params.append(cutoff)
    sql.append("ORDER BY c.created_utc")
    rows = conn.execute(" ".join(sql), params).fetchall()
    return SymbolDetail(
        symbol,
        [SymbolComment(r["trading_day"], r["kind"], r["tier"], r["author"],
                       r["created_utc"], r["body"] or "") for r in rows],
        sentiment=symbol_sentiment(conn, symbol) if with_sentiment else [],
    )


# --------------------------------------------------------------------------- #
#  one day's most-mentioned symbols (report.py's default view)                 #
# --------------------------------------------------------------------------- #
@dataclass
class DayReport:
    day: str
    threads: list[tuple[str, str]]              # (kind, title)
    rows: list[tuple[str, str, int]] | list[tuple[str, str, str, int]]
    by_thread: bool

    def to_text(self) -> str:
        if not self.threads:
            return f"No thread recorded for {self.day}."
        L = [f"[{k}] {t}" for k, t in self.threads] + [""]
        if not self.rows:
            L.append("No ticker/coin mentions found yet.")
            return "\n".join(L)
        if self.by_thread:
            L.append(f"{'Symbol':<8}{'Src':<10}{'Mentions':<10}{'Tiers'}")
            for symbol, kind, tiers, n in self.rows:
                L.append(f"{symbol:<8}{kind:<10}{n:<10}{tiers}")
        else:
            L.append(f"{'Symbol':<8}{'Mentions':<10}{'Tiers'}")
            for symbol, tiers, n in self.rows:
                L.append(f"{symbol:<8}{n:<10}{tiers}")
        return "\n".join(L)


def day_report(db, day: str, *, kinds=MEGA_KINDS, by_thread: bool = False,
               entity_mode: str = "regex") -> DayReport:
    conn = _connect(db)
    kinds = list(kinds)
    kph = _placeholders(kinds)
    ent = _entity_where(entity_mode)
    threads = [
        (r["kind"], r["title"]) for r in conn.execute(
            f"SELECT kind, title FROM threads WHERE trading_day = ? AND kind IN ({kph}) "
            f"ORDER BY kind", [day, *kinds])
    ]
    group = "e.symbol, t.kind" if by_thread else "e.symbol"
    select = "e.symbol, " + ("t.kind, " if by_thread else "")
    rows = conn.execute(
        f"""SELECT {select}
                   GROUP_CONCAT(DISTINCT e.tier) AS tiers,
                   COUNT(DISTINCT e.comment_id) AS mentions
            FROM entities e
            JOIN comments c ON c.id = e.comment_id
            JOIN threads t ON t.id = c.thread_id
            WHERE t.trading_day = ? AND e.symbol != '__none__' AND t.kind IN ({kph})
              AND {ent}
            GROUP BY {group}
            ORDER BY mentions DESC, e.symbol ASC""",
        [day, *kinds],
    ).fetchall()
    return DayReport(day, threads, [tuple(r) for r in rows], by_thread)


# --------------------------------------------------------------------------- #
#  free-text / filtered comment search                                         #
# --------------------------------------------------------------------------- #
@dataclass
class FoundComment:
    id: str
    thread_id: str
    kind: str
    title: str
    trading_day: str
    author: str | None
    created_utc: int | None
    score: int | None
    body: str

    @property
    def when(self) -> str:
        if not self.created_utc:
            return "?"
        return datetime.fromtimestamp(self.created_utc, timezone.utc).strftime("%Y-%m-%d %H:%M")


@dataclass
class SearchResult:
    comments: list[FoundComment]
    truncated: bool

    def to_text(self) -> str:
        if not self.comments:
            return "no matching comments"
        L = []
        for c in self.comments:
            L.append(f"[{c.trading_day} {c.kind}] {c.when}  {c.author}  (score {c.score})")
            L.append(f"  {c.body}\n")
        if self.truncated:
            L.append("… more results (raise limit)")
        return "\n".join(L)


def search_comments(db, *, query: str | None = None, symbol: str | None = None,
                    since=None, until=None, kind: str | None = None,
                    limit: int = 100) -> SearchResult:
    conn = _connect(db)
    where = ["1=1"]
    params: list = []
    if symbol:
        where.append("c.id IN (SELECT comment_id FROM entities WHERE symbol = ?)")
        params.append(symbol.upper())
    if query:
        where.append("c.body LIKE '%' || ? || '%'")
        params.append(query)
    if since is not None:
        where.append("c.created_utc >= ?")
        params.append(_epoch(since))
    if until is not None:
        where.append("c.created_utc < ?")
        params.append(_epoch(until))
    if kind:
        where.append("t.kind = ?")
        params.append(kind)
    rows = conn.execute(
        f"""SELECT c.id, c.author, c.created_utc, c.score, c.body,
                   t.id AS thread_id, t.kind, t.title, t.trading_day
            FROM comments c JOIN threads t ON t.id = c.thread_id
            WHERE {' AND '.join(where)}
            ORDER BY c.created_utc DESC
            LIMIT ?""",
        [*params, limit + 1],
    ).fetchall()
    truncated = len(rows) > limit
    return SearchResult([
        FoundComment(r["id"], r["thread_id"], r["kind"], r["title"], r["trading_day"],
                     r["author"], r["created_utc"], r["score"], r["body"] or "")
        for r in rows[:limit]
    ], truncated)


# --------------------------------------------------------------------------- #
#  threads & run status                                                        #
# --------------------------------------------------------------------------- #
@dataclass
class ThreadInfo:
    id: str
    kind: str
    title: str
    trading_day: str
    created_utc: int | None
    is_open: bool
    n_comments: int


def threads(db, *, since=None, kind: str | None = None) -> list[ThreadInfo]:
    conn = _connect(db)
    where = ["1=1"]
    params: list = []
    if since is not None:
        # accept an ISO date, or ISO/epoch datetime
        s = str(since)
        if len(s) == 10 and s[4] == "-":
            where.append("trading_day >= ?")
            params.append(s)
        else:
            where.append("created_utc >= ?")
            params.append(_epoch(since))
    if kind:
        where.append("kind = ?")
        params.append(kind)
    rows = conn.execute(
        f"""SELECT id, kind, title, trading_day, created_utc, is_open,
                   (SELECT COUNT(*) FROM comments WHERE thread_id = threads.id) AS n
            FROM threads WHERE {' AND '.join(where)}
            ORDER BY created_utc DESC""",
        params,
    ).fetchall()
    return [ThreadInfo(r["id"], r["kind"], r["title"], r["trading_day"],
                       r["created_utc"], bool(r["is_open"]), r["n"]) for r in rows]


@dataclass
class RunStatus:
    stats: dict
    recent_runs: list[dict]

    def to_text(self) -> str:
        s = self.stats
        L = [
            f"threads: {s['threads']} ({s['threads_open']} open)   comments: {s['comments']}",
            f"discovery watermark: {s['discovery_through'] or '—'}",
        ]
        for r in self.recent_runs:
            L.append(f"  {r['window_start']} -> {r['window_end']}  {r['status']:<8} "
                     f"thr={r['n_threads']} new={r['n_comments_new']}"
                     + (f"  {r['error']}" if r["error"] else ""))
        return "\n".join(L)


def run_status(db, *, limit: int = 10) -> RunStatus:
    conn = _connect(db)
    recent = [dict(r) for r in conn.execute(
        "SELECT window_start, window_end, status, n_threads, n_comments_new, error "
        "FROM runs ORDER BY id DESC LIMIT ?", (limit,)
    )]
    return RunStatus(store.stats(conn), recent)


# --------------------------------------------------------------------------- #
def _epoch(value) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    iso = s.replace(" ", "T")
    if len(iso) == 10:
        iso += "T00:00:00"
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())
