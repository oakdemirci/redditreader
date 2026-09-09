#!/usr/bin/env bash
# Provision the Hermes WSB digger on a fresh Debian/Ubuntu box (Hetzner).
# Idempotent -- safe to re-run after a `git pull`.
#
#   sudo APP_DIR=/opt/hermes-digger bash scripts/install.sh
#
# Assumes the repo is already checked out at $APP_DIR (clone it there first with
# whatever GitHub auth you use). Set REPO_URL to have this script clone it.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/hermes-digger}"
APP_USER="${APP_USER:-hermes}"
REPO_URL="${REPO_URL:-}"

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }

echo "==> packages"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip sqlite3 git ca-certificates

echo "==> service user: $APP_USER"
id "$APP_USER" &>/dev/null || useradd --system --home-dir "$APP_DIR" \
    --shell /usr/sbin/nologin "$APP_USER"

echo "==> code at $APP_DIR"
if [[ -n "$REPO_URL" && ! -d "$APP_DIR/.git" ]]; then
    git clone "$REPO_URL" "$APP_DIR"
fi
[[ -f "$APP_DIR/digger.py" ]] || { echo "no checkout at $APP_DIR (set REPO_URL or clone first)"; exit 1; }
git config --global --add safe.directory "$APP_DIR" || true

echo "==> virtualenv"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> config + dirs"
[[ -f "$APP_DIR/.env" ]] || { cp "$APP_DIR/.env.example" "$APP_DIR/.env"; echo "   wrote .env from example -- review it"; }
mkdir -p "$APP_DIR/backups"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod +x "$APP_DIR"/scripts/*.sh

echo "==> systemd units"
for unit in hermes-digger.service hermes-digger.timer \
            hermes-digger-backup.service hermes-digger-backup.timer; do
    install -m644 "$APP_DIR/systemd/$unit" /etc/systemd/system/
done
install -m644 "$APP_DIR/systemd/logrotate-hermes-digger" /etc/logrotate.d/hermes-digger
# keep journald from growing without bound
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=500M\n' > /etc/systemd/journald.conf.d/hermes-digger.conf
systemctl restart systemd-journald
systemctl daemon-reload
systemctl enable --now hermes-digger.timer hermes-digger-backup.timer

cat <<EOF

installed. next steps:
  1. review   $APP_DIR/.env
  2. backfill (one-shot, ~a few minutes per day of history):
       sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/digger.py --backfill 7
  3. verify:
       bash $APP_DIR/scripts/healthcheck.sh
  4. the timer fires hourly at :05 from here on.
EOF
