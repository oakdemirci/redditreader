#!/usr/bin/env bash
# One-glance status: timers, last run, store watermarks, disk.
#   bash scripts/healthcheck.sh
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/hermes-digger}"
DB="${HERMES_DB:-$APP_DIR/hermes.db}"
PY="${PYTHON:-$APP_DIR/.venv/bin/python}"
[[ -x "$PY" ]] || PY="$(command -v python3 || command -v python || true)"

echo "=== timers ==="
systemctl list-timers --no-pager 'hermes-*' 2>/dev/null | sed -n '1,6p' || echo "(systemd not available)"

echo
echo "=== last digger run (journal) ==="
journalctl -u hermes-digger.service -n 10 --no-pager -o cat 2>/dev/null || echo "(no journal)"

echo
echo "=== store ==="
if [[ -n "$PY" && -f "$DB" ]]; then
    "$PY" "$APP_DIR/ingest.py" --db "$DB" --stats
else
    echo "(no python or db yet -- run the backfill)"
fi

echo
echo "=== disk ==="
df -h "$APP_DIR" 2>/dev/null | awk 'NR==1 || NR==2'
[[ -f "$DB" ]] && echo "db:      $(du -h "$DB" | cut -f1)"
[[ -d "$APP_DIR/backups" ]] && echo "backups: $(du -sh "$APP_DIR/backups" | cut -f1) ($(ls -1 "$APP_DIR"/backups/hermes-*.db.gz 2>/dev/null | wc -l) files)"

# non-zero exit if the newest run wasn't ok/partial or the timer is dead
last="$("$PY" - "$DB" <<'PY' 2>/dev/null
import sqlite3, sys
try:
    c = sqlite3.connect(sys.argv[1])
    row = c.execute("SELECT status FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    print(row[0] if row else "none")
except Exception:
    print("error")
PY
)"
echo
case "$last" in
    ok|partial) echo "status: OK (last run: $last)"; exit 0 ;;
    none)       echo "status: no runs yet"; exit 0 ;;
    *)          echo "status: CHECK (last run: $last)"; exit 1 ;;
esac
