#!/usr/bin/env bash
# Snapshot the digger's SQLite store. Uses the online-backup API (safe against a
# live writer, unlike a plain cp of a WAL database). Keeps the newest $BACKUP_KEEP.
#
#   bash scripts/backup.sh
#   BACKUP_KEEP=14 bash scripts/backup.sh
#
# Off-box upgrade path: point restic at $BACKUP_DIR on a second timer, e.g.
#   restic -r sftp:u123@u123.your-storagebox.de:hermes backup "$BACKUP_DIR"
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/hermes-digger}"
DB="${HERMES_DB:-$APP_DIR/hermes.db}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
BACKUP_KEEP="${BACKUP_KEEP:-7}"
PY="${PYTHON:-$APP_DIR/.venv/bin/python}"
[[ -x "$PY" ]] || PY="$(command -v python3 || command -v python)"

[[ -f "$DB" ]] || { echo "no database at $DB"; exit 1; }
mkdir -p "$BACKUP_DIR"

ts="$(date -u +%Y%m%dT%H%M%SZ)"
out="$BACKUP_DIR/hermes-$ts.db"

"$PY" - "$DB" "$out" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
    s.backup(d)
    ok = d.execute("PRAGMA integrity_check").fetchone()[0]
if ok != "ok":
    sys.exit(f"integrity_check failed: {ok}")
print("integrity_check ok")
PY

gzip -f "$out"
echo "wrote $out.gz ($(du -h "$out.gz" | cut -f1))"

mapfile -t old < <(ls -1t "$BACKUP_DIR"/hermes-*.db.gz 2>/dev/null | tail -n +$((BACKUP_KEEP + 1)))
if ((${#old[@]})); then
    rm -f "${old[@]}"
    echo "pruned ${#old[@]} old backup(s)"
fi
echo "$(ls -1 "$BACKUP_DIR"/hermes-*.db.gz 2>/dev/null | wc -l) backup(s) retained"
