"""Shared LLM client for the enrichment phases -- DeepSeek (OpenAI-compatible).

Keyless-safe: with no ``DEEPSEEK_API_KEY`` set, :func:`available` is False and
callers skip, so the digger keeps running without spend. Every call lands in
``llm_calls`` (tokens + cost) and per-input results are cached in ``llm_cache``
keyed by content hash + prompt version, so a re-run at the same version makes no
API calls. A daily USD budget (``HERMES_LLM_DAILY_USD``) caps spend.

Only dependency is ``requests`` -- the endpoint is a plain POST.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone

import requests

BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
MODEL = os.environ.get("HERMES_LLM_MODEL", "deepseek-chat")  # -> deepseek-flash server-side
DAILY_BUDGET_USD = float(os.environ.get("HERMES_LLM_DAILY_USD", "1.00"))

# USD per 1M tokens. DeepSeek V4-Flash, off-peak cache-miss (checked 2026-09);
# peak (01-04 & 06-10 UTC Mon-Fri) is ~2x. llm_calls stores raw token counts so
# the real cost can always be recomputed -- these are estimates for the ledger.
PRICE_IN = float(os.environ.get("HERMES_LLM_PRICE_IN", "0.22"))
PRICE_IN_CACHED = float(os.environ.get("HERMES_LLM_PRICE_IN_CACHED", "0.03"))
PRICE_OUT = float(os.environ.get("HERMES_LLM_PRICE_OUT", "0.66"))

MAX_RETRIES = 5


class BudgetExceeded(RuntimeError):
    """The day's LLM spend has reached the cap."""


class LLMError(RuntimeError):
    """The API call failed or returned something unusable. Callers treat the
    enrichment step as best-effort and carry on."""


def api_key() -> str:
    return os.environ.get("DEEPSEEK_API_KEY", "").strip()


def available() -> bool:
    return bool(api_key())


def body_hash(text: str) -> str:
    return hashlib.sha256(" ".join((text or "").split()).encode()).hexdigest()[:32]


def cost_of(usage: dict) -> float:
    cached = usage.get("prompt_cache_hit_tokens") or 0
    prompt = usage.get("prompt_tokens") or 0
    fresh = max(prompt - cached, 0)
    out = usage.get("completion_tokens") or 0
    return (fresh * PRICE_IN + cached * PRICE_IN_CACHED + out * PRICE_OUT) / 1_000_000


def spent_today(conn) -> float:
    day = datetime.now(timezone.utc).date().isoformat()
    return conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls "
        "WHERE ok = 1 AND substr(created_at, 1, 10) = ?", (day,)
    ).fetchone()[0] or 0.0


def spend_summary(conn) -> dict:
    rows = conn.execute(
        "SELECT substr(created_at,1,10) d, COUNT(*) n, "
        "SUM(tokens_in) ti, SUM(tokens_out) to_, ROUND(SUM(cost_usd),4) usd "
        "FROM llm_calls WHERE ok = 1 GROUP BY d ORDER BY d DESC LIMIT 14"
    ).fetchall()
    return {
        "model": MODEL,
        "budget_usd_per_day": DAILY_BUDGET_USD,
        "spent_today": round(spent_today(conn), 4),
        "by_day": [dict(zip(("day", "calls", "tokens_in", "tokens_out", "usd"), r)) for r in rows],
    }


def _log(conn, *, created_at, task, prompt_ver, n_items, usage, cost, ok, error):
    conn.execute(
        "INSERT INTO llm_calls (created_at, task, model, prompt_ver, n_items, "
        "tokens_in, tokens_out, cost_usd, ok, error) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (created_at, task, MODEL, prompt_ver, n_items,
         usage.get("prompt_tokens"), usage.get("completion_tokens"), cost, ok, error),
    )
    conn.commit()


def chat_json(conn, *, task: str, prompt_ver: str, system: str, user: str,
              n_items: int | None = None, temperature: float = 0.0,
              max_tokens: int = 4096, timeout: int = 120,
              budget_usd: float | None = None) -> dict:
    """One JSON-mode chat completion. Logs to ``llm_calls``. Raises
    :class:`BudgetExceeded` when the day's spend is already at the cap."""
    if not available():
        raise RuntimeError("no DEEPSEEK_API_KEY")
    cap = DAILY_BUDGET_USD if budget_usd is None else budget_usd
    if spent_today(conn) >= cap:
        raise BudgetExceeded(f"daily LLM budget ${cap:.2f} reached")

    payload = {
        "model": MODEL,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    started = datetime.now(timezone.utc).isoformat()
    backoff = 5.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                f"{BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {api_key()}"},
                json=payload, timeout=timeout,
            )
        except requests.RequestException as exc:
            if attempt == MAX_RETRIES:
                _log(conn, created_at=started, task=task, prompt_ver=prompt_ver,
                     n_items=n_items, usage={}, cost=0.0, ok=0, error=f"network: {exc}")
                raise
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        if resp.status_code in (429, 500, 502, 503, 529):
            if attempt == MAX_RETRIES:
                _log(conn, created_at=started, task=task, prompt_ver=prompt_ver,
                     n_items=n_items, usage={}, cost=0.0, ok=0,
                     error=f"HTTP {resp.status_code} (retries exhausted)")
                raise LLMError(f"{task}: HTTP {resp.status_code} after {MAX_RETRIES} tries")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        if resp.status_code >= 400:
            body = (getattr(resp, "text", "") or "")[:200]
            _log(conn, created_at=started, task=task, prompt_ver=prompt_ver,
                 n_items=n_items, usage={}, cost=0.0, ok=0,
                 error=f"HTTP {resp.status_code} {body!r}")
            raise LLMError(f"{task}: HTTP {resp.status_code} {body!r}")

        try:
            data = resp.json()
            content = data["choices"][0]["message"].get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            snippet = (getattr(resp, "text", "") or "")[:200]
            _log(conn, created_at=started, task=task, prompt_ver=prompt_ver,
                 n_items=n_items, usage={}, cost=0.0, ok=0,
                 error=f"unexpected response: {exc} :: {snippet!r}")
            raise LLMError(f"{task}: unexpected API response ({exc})")

        usage = data.get("usage", {})
        cost = cost_of(usage)
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            _log(conn, created_at=started, task=task, prompt_ver=prompt_ver,
                 n_items=n_items, usage=usage, cost=cost, ok=0,
                 error=f"non-JSON content: {content[:120]!r}")
            raise LLMError(f"{task}: model did not return JSON ({exc})")
        _log(conn, created_at=started, task=task, prompt_ver=prompt_ver,
             n_items=n_items, usage=usage, cost=cost, ok=1, error=None)
        return parsed

    raise RuntimeError(f"{task}: unreachable")


# --- per-input cache ----------------------------------------------------- #
def cache_get(conn, input_hash: str, prompt_ver: str, task: str):
    row = conn.execute(
        "SELECT response_json FROM llm_cache WHERE input_hash=? AND prompt_ver=? AND task=?",
        (input_hash, prompt_ver, task),
    ).fetchone()
    return json.loads(row[0]) if row else None


def cache_put(conn, input_hash: str, prompt_ver: str, task: str, response) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO llm_cache (input_hash, prompt_ver, task, response_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (input_hash, prompt_ver, task, json.dumps(response, ensure_ascii=False),
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
