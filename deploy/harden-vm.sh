#!/usr/bin/env bash
# Dalila — one-shot VM hardening for unattended, multi-month operation.
#
# Run this ONCE on the production VM after the bot is installed. It is
# idempotent (safe to re-run) and only touches OS-level longevity knobs that
# the bot can't manage from inside its own process:
#
#   1. Cap journald so system logs can't fill the disk over months.
#   2. Guarantee a swap file (the e2-micro has 1 GB RAM; swap absorbs the
#      Node.js spikes from the `claude` CLI without OOM-killing the bot).
#   3. Rotate the bot's own ~/dalila/logs/*.log files.
#   4. Install a weekly cron that re-shallows the local .git so the constant
#      site commits don't grow the clone unbounded (origin keeps full history).
#
# In-process upkeep (WAL truncate, query-planner stats, disk-usage warnings)
# is handled by the bot's nightly maintenance job — see scheduler._maintenance_job.
#
# Usage:  bash ~/dalila/deploy/harden-vm.sh
#
set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/dalila}"
SWAP_FILE="/swapfile"

log() { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()  { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }

# ── 1. Cap journald ─────────────────────────────────────────────────────────
# Without a cap, journald's persistent store grows to 10% of the disk by
# default — on a 10 GB boot disk that's ~1 GB of logs slowly eaten from free
# space. 200 MB is plenty to debug a week of bot activity.
log "1/4  Cap systemd journal at 200 MB"
sudo mkdir -p /etc/systemd/journald.conf.d
sudo tee /etc/systemd/journald.conf.d/dalila-cap.conf >/dev/null <<'EOF'
# Installed by dalila/deploy/harden-vm.sh — bound the journal so logs can't
# fill the small boot disk during months of unattended operation.
[Journal]
SystemMaxUse=200M
SystemKeepFree=500M
MaxRetentionSec=2week
EOF
sudo systemctl restart systemd-journald
# Reclaim anything already over the new cap immediately.
sudo journalctl --vacuum-size=200M >/dev/null 2>&1 || true
ok "journald capped at 200 MB (was up to ~10% of disk)"

# ── 2. Swap file ────────────────────────────────────────────────────────────
log "2/4  Ensure 2 GB swap"
if ! sudo swapon --show | grep -q "$SWAP_FILE" 2>/dev/null; then
    if [[ ! -f "$SWAP_FILE" ]]; then
        sudo fallocate -l 2G "$SWAP_FILE" || sudo dd if=/dev/zero of="$SWAP_FILE" bs=1M count=2048
        sudo chmod 600 "$SWAP_FILE"
        sudo mkswap "$SWAP_FILE"
    fi
    sudo swapon "$SWAP_FILE"
    grep -q "$SWAP_FILE" /etc/fstab || echo "$SWAP_FILE none swap sw 0 0" | sudo tee -a /etc/fstab >/dev/null
    ok "2 GB swap enabled"
else
    ok "swap already active"
fi

# ── 3. Rotate the bot's own log files ───────────────────────────────────────
log "3/4  logrotate for $REPO_DIR/logs"
if [[ -d "$REPO_DIR/logs" ]]; then
    sudo tee /etc/logrotate.d/dalila >/dev/null <<EOF
$REPO_DIR/logs/*.log {
    weekly
    rotate 4
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF
    ok "logrotate configured (4 weekly, compressed)"
else
    ok "no $REPO_DIR/logs dir — bot logs to journald only; skipping"
fi

# ── 4. Weekly .git re-shallow cron ──────────────────────────────────────────
# The bot commits the regenerated site constantly; git keeps every version
# forever, so the local clone grows without bound. reshallow_repo.sh drops old
# local history (origin keeps it all). It is a no-op unless local main is clean
# and in sync, so it can never discard an in-flight publish.
log "4/4  Weekly re-shallow cron"
RESHALLOW="$REPO_DIR/scripts/reshallow_repo.sh"
if [[ -f "$RESHALLOW" ]]; then
    chmod +x "$RESHALLOW"
    CRON_LINE="17 3 * * 0 $RESHALLOW >> /tmp/dalila-reshallow.log 2>&1"
    # Install idempotently: strip any prior dalila reshallow line, then re-add.
    ( crontab -l 2>/dev/null | grep -v "reshallow_repo.sh" ; echo "$CRON_LINE" ) | crontab -
    ok "weekly re-shallow scheduled (Sun 03:17)"
else
    ok "reshallow_repo.sh not found; skipping cron (pull latest repo first)"
fi

cat <<EOF


────────────────────────────────────────────────────────────
✓ VM hardened for unattended operation.
────────────────────────────────────────────────────────────
In-process upkeep (WAL truncate, planner stats, disk warnings) runs nightly
inside the bot at 04:00 GST. To watch it:

    journalctl -u dalila@\$USER -f | grep -i maintenance

If 'maintenance: DISK …% full' warnings appear, resize the boot disk
(Always-Free allows up to 30 GB at \$0) — that's the one thing this script
can't do for you because it needs the GCP console / gcloud.
────────────────────────────────────────────────────────────
EOF
