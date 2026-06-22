#!/usr/bin/env bash
#
# Keep the VM's local git history shallow so .git doesn't grow without bound.
#
# Why: the bot commits the regenerated site into main constantly (markets.html
# every ~30 min, plus index/archive/digest on every publish). Git keeps a full
# compressed copy of every version of those HTML files forever, so the local
# .git grows until the small boot disk fills up — which is what wedged the
# `claude` CLI (no room for its cache) and threw SQLite "database is locked".
#
# GitHub keeps the FULL history regardless (the archive, blame, every daily and
# market commit are all safe on origin). The VM only needs a recent slice. This
# script re-shallows the local clone to depth 1: the old history becomes
# unreachable locally and is reclaimed by gc, while origin is untouched.
#
# Safe by design: it does nothing unless local main is clean and exactly in
# sync with origin/main, so it can never discard an in-flight publish or an
# unpushed commit. If the bot is mid-publish when this fires, it simply skips
# and the next run picks it up. Gitignored files (dalila.db, .env) are never
# touched — reset --hard only affects tracked files.
#
# Run weekly via cron or a systemd timer, e.g.:
#   17 3 * * 0 /home/<user>/dalila/scripts/reshallow_repo.sh >> /tmp/dalila-reshallow.log 2>&1
#
set -euo pipefail

cd "$(dirname "$0")/.."   # repo root
export GIT_TERMINAL_PROMPT=0   # never block on a credential prompt

git fetch --quiet origin main

local_sha="$(git rev-parse main)"
remote_sha="$(git rev-parse origin/main)"
if [ "$local_sha" != "$remote_sha" ]; then
    echo "reshallow: local main ($local_sha) != origin/main ($remote_sha); skipping"
    exit 0
fi
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "reshallow: working tree dirty; skipping to avoid discarding changes"
    exit 0
fi

before="$(du -sh .git 2>/dev/null | cut -f1)"

# Re-shallow: fetch only the tip, drop everything reachable only via old
# history, then repack so the disk space is actually reclaimed.
git fetch --depth=1 origin main
git reset --hard origin/main
git reflog expire --expire=now --all
git gc --prune=now --quiet

after="$(du -sh .git 2>/dev/null | cut -f1)"
echo "reshallow: .git ${before} -> ${after} (origin retains full history)"
