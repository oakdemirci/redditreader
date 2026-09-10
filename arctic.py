"""Keyless client for the Arctic Shift Reddit archive.

Reddit has closed every no-auth path this project used to rely on: script-app
registration is disabled, ``www.reddit.com/....json`` returns 403 for anonymous
clients, and ``old.reddit.com`` 302-redirects them to a login page. Only the
RSS/Atom feeds still work without a login, and those expose at most the newest
~100 comments of a thread with no reply structure and no scores.

Arctic Shift (https://arctic-shift.photon-reddit.com) is a public, key-less
mirror of Reddit's data that ingests continuously and normally stays within
minutes-to-an-hour of live. Every field Reddit exposes on a post or comment is
preserved verbatim. This module wraps the handful of endpoints the digger needs;
:mod:`store` persists what it returns and :mod:`ingest` orchestrates.

See ``docs/PLAN.md`` and the ``arctic-shift-archive`` memory note for the wider
picture (throttling behaviour, endpoint quirks).
"""

from __future__ import annotations

import re
import time

import requests

API_ROOT = "https://arctic-shift.photon-reddit.com/api"
USER_AGENT = "wsb-digger/1.0 (personal research archive; contact via repo owner)"

PAGE = 100  # Arctic Shift's max `limit` for the search endpoints
ID_BATCH = 500  # max ids per posts/ids or comments/ids call


def bare_id(value: str) -> str:
    """``t3_1wbh9od`` / ``1wbh9od`` / a full permalink -> ``1wbh9od``."""
    match = re.search(r"/comments/([a-z0-9]+)", value, re.I)
    if match:
        return match.group(1)
    return value.rsplit("_", 1)[-1].strip("/")


def _looks_numeric(text: str) -> bool:
    return bool(text) and text.replace(".", "", 1).isdigit()


def _yn(flag: bool) -> str:
    return "true" if flag else "false"


class ArchiveError(RuntimeError):
    """A hard error from the archive, or retries exhausted."""


class Archive:
    """Thin, polite client for the Arctic Shift REST API.

    The service throttles two ways: a hard HTTP 429 with an ``X-RateLimit-Reset``
    header, and a soft ``200 OK`` whose body is ``{"data": null, "error":
    "Timeout. Maybe slow down a bit"}`` when a query is too heavy or too frequent.
    Both are retried here with exponential backoff, and a soft hit also widens the
    minimum spacing between requests for the rest of the process.
    """

    def __init__(self, min_interval: float = 2.0, max_retries: int = 8,
                 verbose: bool = True):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.verbose = verbose
        self._last_request = 0.0

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"   . {msg}")

    def _pace(self) -> None:
        gap = time.monotonic() - self._last_request
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)

    def get(self, path: str, **params) -> object:
        """GET ``/api/<path>`` and return the decoded ``data`` field.

        Returns ``None`` or ``[]`` when the archive legitimately has no result;
        raises :class:`ArchiveError` on a hard error or after exhausting retries.
        """
        params = {k: v for k, v in params.items() if v is not None}
        url = f"{API_ROOT}/{path}"
        backoff = 5.0

        for attempt in range(1, self.max_retries + 1):
            self._pace()
            try:
                resp = self.session.get(url, params=params, timeout=90)
                self._last_request = time.monotonic()
            except requests.RequestException as exc:
                self._log(f"network error: {exc} -- retry {attempt}/{self.max_retries} in {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 180)
                continue

            if resp.status_code == 429:
                reset = resp.headers.get("X-RateLimit-Reset", "")
                wait = float(reset) + 1 if _looks_numeric(reset) else backoff
                self._log(f"429 rate limited -- waiting {wait:.0f}s")
                time.sleep(wait)
                backoff = min(backoff * 2, 180)
                continue

            if resp.status_code >= 500:
                self._log(f"HTTP {resp.status_code} -- retry {attempt}/{self.max_retries} in {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 180)
                continue

            try:
                body = resp.json()
            except ValueError:
                body = None

            # The soft rate limit ("Timeout. Maybe slow down a bit") arrives with
            # varying status codes -- 200 on light endpoints, 422 on heavy ones --
            # so check the body before trusting the status.
            if isinstance(body, dict) and body.get("data") is None and body.get("error"):
                err = str(body["error"])
                if "slow down" in err.lower() or "timeout" in err.lower():
                    self.min_interval = min(self.min_interval + 1.0, 20.0)
                    self._log(f'archive: "{err}" -- easing off to {self.min_interval:.0f}s spacing, '
                              f"retry {attempt}/{self.max_retries} in {backoff:.0f}s")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 180)
                    continue
                raise ArchiveError(f"{path}: {err} (params={params})")

            if not resp.ok:
                raise ArchiveError(f"{path}: HTTP {resp.status_code} {resp.text[:200]!r} (params={params})")

            return body.get("data") if isinstance(body, dict) else body

        raise ArchiveError(f"{path}: gave up after {self.max_retries} attempts (params={params})")

    # -- higher-level helpers ------------------------------------------------ #
    def posts_by_id(self, ids) -> list[dict]:
        """Full submission objects for the given ids (any id form), batched."""
        ids = [bare_id(i) for i in ids]
        out: list[dict] = []
        for i in range(0, len(ids), ID_BATCH):
            chunk = ids[i:i + ID_BATCH]
            out.extend(self.get("posts/ids", ids=",".join(chunk)) or [])
        return out

    def iter_posts(self, subreddit: str, after: int, before: int):
        """Yield every post in ``subreddit`` created in [after, before], oldest
        first. Pages on ``created_utc``; overlaps each page by a second and
        de-dupes so a burst of posts sharing one timestamp can't be split."""
        seen: set[str] = set()
        cursor = after
        while True:
            batch = self.get("posts/search", subreddit=subreddit, after=cursor,
                             before=before, sort="asc", limit=PAGE)
            if not batch:
                return
            fresh = [p for p in batch if p["id"] not in seen]
            for post in fresh:
                seen.add(post["id"])
                yield post
            if len(batch) < PAGE:
                return
            newest = batch[-1]["created_utc"]
            # step back a second so items sharing `newest` aren't skipped; if the
            # whole page was one second, step forward to avoid a stall.
            cursor = newest + 1 if not fresh else newest - 1

    def comments(self, link_id: str, *, md2html: bool = False,
                 after: int | None = None, before: int | None = None,
                 cap: int | None = None, on_progress=None) -> list[dict]:
        """Comments on a submission, oldest first, de-duped by id.

        ``after`` / ``before`` bound ``created_utc`` (for hourly delta runs);
        omit both for the whole thread. ``before`` is applied client-side --
        ``comments/search`` rejects ``after`` and ``before`` together on a
        ``link_id`` query -- by paging from ``after`` and stopping once the feed
        reaches it. ``cap`` stops early after roughly that many. ``on_progress
        (count)`` is called after each page.
        """
        link_id = bare_id(link_id)
        collected: dict[str, dict] = {}
        cursor = after
        while True:
            batch = self.get("comments/search", link_id=link_id, after=cursor,
                             sort="asc", limit=PAGE, md2html=_yn(md2html))
            if not batch:
                break
            added = 0
            newest = cursor or 0
            reached_end = False
            for comment in batch:
                ts = comment.get("created_utc") or 0
                newest = max(newest, ts)
                if before is not None and ts >= before:
                    reached_end = True
                    continue
                if comment["id"] not in collected:
                    collected[comment["id"]] = comment
                    added += 1
            if on_progress:
                on_progress(len(collected))
            else:
                self._log(f"{len(collected)} comments so far")
            if reached_end:
                break
            if cap and len(collected) >= cap:
                self._log(f"stopping at cap={cap}")
                break
            if len(batch) < PAGE:
                break
            if added == 0:
                # a full page entirely within one already-seen second: the only
                # way forward is to skip past it (>100 comments in one second is
                # rare even on WSB; note it if it happens).
                self._log(f"WARNING: >{PAGE} comments at t={newest}; some may be skipped")
                cursor = newest + 1
            else:
                cursor = newest - 1
        return list(collected.values())

    def comments_in_window(self, subreddit: str, after: int, before: int, *,
                           link_ids=None, md2html: bool = False,
                           on_progress=None) -> dict[str, list[dict]]:
        """Every comment in ``subreddit`` with ``created_utc`` in [after, before),
        grouped by bare thread id.

        Uses the subreddit + time-range form of ``comments/search`` -- the
        per-thread ``link_id`` form is unreliable (Arctic Shift throttles it hard
        and 422s), whereas this one query covers *all* tracked threads at once.
        ``link_ids`` (an iterable of thread ids, any form) restricts the result to
        those threads; comments for other threads are still paged past but dropped.
        """
        want = None if link_ids is None else {bare_id(x) for x in link_ids}
        out: dict[str, list[dict]] = {}
        seen: set[str] = set()
        cursor = after
        total = 0
        while True:
            batch = self.get("comments/search", subreddit=subreddit, after=cursor,
                             before=before, sort="asc", limit=PAGE, md2html=_yn(md2html))
            if not batch:
                break
            added = 0
            newest = cursor or 0
            for comment in batch:
                ts = comment.get("created_utc") or 0
                newest = max(newest, ts)
                if comment["id"] in seen:
                    continue
                seen.add(comment["id"])
                added += 1
                tid = bare_id(comment.get("link_id") or "")
                if want is not None and tid not in want:
                    continue
                out.setdefault(tid, []).append(comment)
                total += 1
            if on_progress:
                on_progress(total)
            else:
                self._log(f"{total} in-scope comments, {len(seen)} scanned")
            if len(batch) < PAGE:
                break
            cursor = newest + 1 if added == 0 else newest - 1
        return out
