#!/usr/bin/env bash
# Phase 8 acceptance proxy: exercise the MCP tools the way Hermes Agent would,
# covering the four "ask the bot" scenarios. Passing here means the query layer
# answers; the Telegram round-trip then only depends on Hermes + DeepSeek config
# (do the manual Telegram checks in docs/DEPLOY.md too).
#
#   bash scripts/ask-the-bot.sh                       # uses $HERMES_DB
#   HERMES_DB=/opt/hermes-digger/hermes.db bash scripts/ask-the-bot.sh
set -uo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
PY="${PYTHON:-$APP_DIR/.venv/bin/python}"
[[ -x "$PY" ]] || PY="$(command -v python3 || command -v python)"
export HERMES_DB="${HERMES_DB:-$APP_DIR/hermes.db}"
REQ="$(mktemp)"; trap 'rm -f "$REQ"' EXIT

call() {  # name  json-args   -- prints a block, returns non-zero on failure
    printf '%s\n%s\n' \
        '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{}}}' \
        "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":$2}}" \
        > "$REQ"
    local out
    out="$("$PY" "$APP_DIR/mcp_server.py" < "$REQ" 2>/dev/null)"
    MCP_NAME="$1" MCP_OUT="$out" "$PY" - <<'PY'
import json, os
name = os.environ["MCP_NAME"]
last = None
for line in os.environ["MCP_OUT"].splitlines():
    line = line.strip()
    if line:
        try:
            last = json.loads(line)
        except Exception:
            pass
r = (last or {}).get("result", {})
text = (r.get("content") or [{}])[0].get("text", "")
bad = bool(r.get("isError")) or "error" in (last or {}) or not text.strip()
print(f"--- {name} " + ("[ERROR]" if bad else "[ok]"))
print("\n".join(text.splitlines()[:6]))
raise SystemExit(1 if bad else 0)
PY
}

fails=0
echo "== 1. current trend =="
call get_trend '{"days":3,"top":8}' || ((fails++))
echo
echo "== 2. a symbol's sentiment =="
sym="$("$PY" - <<PY
import sqlite3, os
c = sqlite3.connect(os.environ["HERMES_DB"])
row = c.execute("SELECT symbol FROM entities WHERE symbol!='__none__' "
                "GROUP BY symbol ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
print(row[0] if row else "NVDA")
PY
)"
echo "(symbol: $sym)"
call get_sentiment "{\"symbol\":\"$sym\"}" || ((fails++))
echo
echo "== 3. a thread digest =="
call get_thread '{"thread":"daily","top":3,"depth":2}' || ((fails++))
echo
echo "== 4. free-form (search + drill) =="
call search_comments '{"query":"puts","limit":3}' || ((fails++))
call get_symbol "{\"symbol\":\"$sym\",\"limit\":3}" || ((fails++))

echo
if ((fails)); then
    echo "FAILED: $fails check(s)"
    exit 1
fi
echo "all MCP tool checks passed"
