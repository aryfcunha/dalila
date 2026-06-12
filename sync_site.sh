#!/bin/bash
# Daily site pipeline. Deliberately NOT set -e for the git steps: a pull hiccup
# must not stop the pipeline+publish, and a publish must still be pushed even if
# something earlier complained. Failures are logged loudly instead.
cd /home/aryfcunha/dalila

echo "[$(date)] Pulling latest code..."
git pull --rebase --autostash || echo "[$(date)] WARN: git pull failed - continuing with local code"

echo "[$(date)] Running pipeline..."
if ! ./.venv/bin/python -m dalila run-pipeline; then
    echo "[$(date)] ERROR: run-pipeline failed - still attempting publish of existing data"
fi

echo "[$(date)] Publishing site..."
if ! ./.venv/bin/python -m dalila publish-site; then
    echo "[$(date)] ERROR: publish-site failed - nothing new to push"
fi

echo "[$(date)] Staging and pushing docs..."
git add docs/
if ! git diff --cached --quiet; then
    git commit -m "auto: daily site update $(date -u +'%Y-%m-%d %H:%M UTC')"
    if git push origin main; then
        echo "[$(date)] Changes pushed to GitHub."
    else
        echo "[$(date)] ERROR: git push FAILED - site will be stale until fixed"
    fi
else
    echo "[$(date)] No changes to docs."
fi
