#!/usr/bin/env bash
# Dalila — bootstrap script for a fresh Ubuntu 24.04 LTS VM (GCP e2-micro or equivalent).
#
# What this script does (idempotent — safe to re-run):
#   1. Installs system prerequisites (Python 3.12, git, build tools, Node.js for claude CLI)
#   2. Creates a 2 GB swap file (the e2-micro has only 1 GB RAM; Node.js spawned by
#      the classifier can spike briefly, swap absorbs that without OOM-killing us)
#   3. Installs the official Claude Code CLI
#   4. Clones (or updates) the Dalila repo into ~/dalila
#   5. Creates a venv and installs Dalila
#   6. Creates logs/ and a stub .env that the user fills in
#
# After this script finishes you still need to do three manual steps —
# the script prints them at the end. They can't be automated because they
# need interactive auth or secrets.

set -euo pipefail

REPO_URL="https://github.com/aryfcunha/dalila.git"
REPO_DIR="$HOME/dalila"
SWAP_FILE="/swapfile"

log() { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()  { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }

log "1/6  System prerequisites"
sudo apt-get update -q
sudo apt-get install -y -q \
    git build-essential curl ca-certificates \
    sqlite3 tzdata lsb-release

# Pick the newest Python available. We need ≥3.11 (the codebase uses
# int.bit_count(), str | None unions, etc.). On Ubuntu 24.04 that's python3.12;
# on Debian bookworm it's python3.11. Both are in default apt repos so we
# don't need an external PPA.
PY=""
# Use `apt-cache policy` and check for an installable Candidate. `apt-cache
# show` is too permissive on Debian — it can succeed for source-only
# packages that then fail at `apt-get install` time, which is exactly the
# failure mode we hit on bookworm with python3.12.
for candidate in python3.13 python3.12 python3.11; do
    cand_version="$(apt-cache policy "$candidate" 2>/dev/null | awk '/Candidate:/ {print $2}')"
    if [[ -n "$cand_version" && "$cand_version" != "(none)" ]]; then
        printf '   trying %s (candidate %s)...\n' "$candidate" "$cand_version"
        if sudo apt-get install -y -q "$candidate" "$candidate-venv" "$candidate-dev"; then
            PY="$candidate"
            break
        fi
    fi
done
if [[ -z "$PY" ]]; then
    echo "ERROR: no python ≥3.11 installable from apt." >&2
    echo "Distro: $(lsb_release -ds 2>/dev/null)" >&2
    echo "Probed: python3.13, python3.12, python3.11 — none had an installable candidate." >&2
    exit 1
fi
export PY
ok "system packages installed (using $PY)"

# Node.js 20 for the claude CLI. The Nodesource script works on both
# Debian and Ubuntu and pins to the current distro codename automatically.
if ! command -v node >/dev/null 2>&1; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo apt-get install -y -q nodejs
fi

log "2/6  Swap file (2 GB)"
if [[ ! -f "$SWAP_FILE" ]]; then
    sudo fallocate -l 2G "$SWAP_FILE"
    sudo chmod 600 "$SWAP_FILE"
    sudo mkswap "$SWAP_FILE"
    sudo swapon "$SWAP_FILE"
    echo "$SWAP_FILE none swap sw 0 0" | sudo tee -a /etc/fstab >/dev/null
    ok "2 GB swap created and enabled"
else
    ok "swap already configured"
fi

log "3/6  Claude Code CLI"
if ! command -v claude >/dev/null 2>&1; then
    curl -fsSL https://claude.ai/install.sh | bash
    # The installer writes to ~/.local/bin — ensure it's on PATH for future shells
    if ! grep -q 'HOME/.local/bin' "$HOME/.bashrc"; then
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
    fi
    export PATH="$HOME/.local/bin:$PATH"
fi
claude --version
ok "claude CLI installed"

log "4/6  Dalila repo"
if [[ -d "$REPO_DIR/.git" ]]; then
    git -C "$REPO_DIR" pull --ff-only
else
    git clone "$REPO_URL" "$REPO_DIR"
fi
ok "repo at $REPO_DIR"

log "5/6  Python venv + install (will take 2-4 min on e2-micro)"
cd "$REPO_DIR"

# Recreate venv if it's missing OR broken (no .venv/bin/python). Earlier
# script runs may have created a venv without pip — wipe and start over
# in that case, otherwise leave a working venv alone.
if [[ ! -x .venv/bin/python ]]; then
    rm -rf .venv
    "$PY" -m venv .venv
fi

# Debian/Ubuntu's pythonX-venv package can ship without bundled pip
# wheels (PEP 668 fallout) — `python -m venv` then leaves you with a
# venv that has python but no pip/setuptools. `ensurepip` fixes that.
# Idempotent — does nothing if pip is already present.
.venv/bin/python -m ensurepip --upgrade

# Verbose (no -q) so the user sees progress instead of staring at a
# blank line for 3 minutes. `--no-cache-dir` keeps RAM use lower on
# the 1 GB e2-micro — pip's wheel cache can balloon transient memory.
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install --no-cache-dir -e .
ok "Dalila installed in venv (python: $($PY --version))"

log "6/6  Runtime directories + stub .env"
mkdir -p "$REPO_DIR/logs"
if [[ ! -f "$REPO_DIR/.env" ]]; then
    cat > "$REPO_DIR/.env" <<'EOF'
# Dalila environment. Fill in the values below.
TELEGRAM_BOT_TOKEN=
DALILA_TIMEZONE=Asia/Dubai
DALILA_DIGEST_TIME=06:30
DALILA_INGEST_INTERVAL_MINUTES=30
# Optional — leave blank to skip ACLED ingestion:
ACLED_API_KEY=
ACLED_EMAIL=
# Optional — leave blank to skip IATI ingestion (UAE-related aid activities):
IATI_API_KEY=
EOF
    ok ".env stub created (you must edit this)"
else
    ok ".env already exists; leaving it alone"
fi

cat <<'EOF'


────────────────────────────────────────────────────────────
✓ Bootstrap complete. Three manual steps remain:
────────────────────────────────────────────────────────────

1) Authenticate Claude Code (one time, interactive):
       claude login
   It will print a URL — open it in your laptop browser,
   approve, and paste the code back into this terminal.

2) Edit ~/dalila/.env and set TELEGRAM_BOT_TOKEN
   (and ACLED_API_KEY/EMAIL if you have them):
       nano ~/dalila/.env

3) Initialise the DB and start the bot as a systemd service:
       cd ~/dalila
       .venv/bin/python -m dalila init
       .venv/bin/python -m dalila check     # verify everything is wired
       sudo cp deploy/dalila@.service /etc/systemd/system/
       sudo systemctl daemon-reload
       sudo systemctl enable --now "dalila@$USER"

Then:
       systemctl status "dalila@$USER"      # is it running?
       journalctl -u "dalila@$USER" -f      # tail live logs

EOF
