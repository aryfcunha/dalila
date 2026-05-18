"""APScheduler jobs: ingest every N minutes, daily digest at 06:30 GST."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram.ext import Application

from dalila import bot, db
from dalila.config import get_config
from dalila.pipeline import run_classify, run_compose_digest, run_ingest

log = logging.getLogger(__name__)

# Fallback back-off used when the rate-limit error doesn't carry a parseable
# reset time. When Claude does tell us when it'll reset (e.g. "resets 5:30pm
# (Asia/Dubai)"), we honour that exactly instead — see pipeline.parse_rate_limit_reset.
_RATE_LIMIT_BACKOFF = timedelta(hours=1)
# Small cushion added on top of the parsed reset time so we don't fire the very
# instant the window opens (server clock skew, gradual roll-out).
_RATE_LIMIT_BUFFER = timedelta(seconds=60)
_classify_paused_until: datetime | None = None


_last_full_ingest_time: datetime | None = None

def _ingest_job() -> None:
    global _last_full_ingest_time
    now = datetime.now(timezone.utc)
    
    # Check if breaking news in last 4 hours (items OR market alerts)
    is_breaking_active = False
    with db.connect() as conn:
        cutoff = (now - timedelta(hours=4)).isoformat()
        res = conn.execute(
            "SELECT COUNT(*) FROM items WHERE is_breaking_candidate = 1 AND classified_at >= ?",
            (cutoff,)
        ).fetchone()[0]
        if res > 0:
            is_breaking_active = True
        # Also check if any market had a large move recently
        if not is_breaking_active:
            try:
                mres = conn.execute(
                    """SELECT COUNT(*) FROM prediction_market_history
                       WHERE recorded_at >= ? AND (
                           ABS(COALESCE(delta_1h,0)) >= 0.05
                           OR ABS(COALESCE(delta_24h,0)) >= 0.08
                       )""",
                    (cutoff,)
                ).fetchone()[0]
                if mres > 0:
                    is_breaking_active = True
            except Exception:
                pass  # table may not exist yet on first run

    cfg = get_config()
    interval = 5 if is_breaking_active else cfg.ingest_interval_minutes
    
    if _last_full_ingest_time and (now - _last_full_ingest_time).total_seconds() < interval * 60 - 10:
        return
        
    _last_full_ingest_time = now

    log.info("scheduler: ingest tick (interval=%dm, breaking=%s)", interval, is_breaking_active)
    stats = run_ingest()
    total_new = sum(s["new"] for s in stats.values())
    total_passed = sum(s["prefilter_passed"] for s in stats.values())
    log.info("ingest done: %d new (%d passed prefilter) across %d sources",
             total_new, total_passed, len(stats))


def _classify_job() -> None:
    global _classify_paused_until
    now = datetime.now(timezone.utc)
    if _classify_paused_until and now < _classify_paused_until:
        remaining = (_classify_paused_until - now).total_seconds()
        log.info("classify paused for %.0fs more (rate-limit back-off); skipping tick", remaining)
        return
    log.info("scheduler: classify tick")
    result = run_classify(limit=100)
    log.info("classify done: %s", result)
    if result.get("rate_limited"):
        reset_at = result.get("rate_limit_reset_at")
        if reset_at:
            _classify_paused_until = reset_at + _RATE_LIMIT_BUFFER
            log.warning("rate-limited — pausing scheduled classify until %s (parsed from error)",
                        _classify_paused_until.isoformat())
        else:
            _classify_paused_until = now + _RATE_LIMIT_BACKOFF
            log.warning("rate-limited (no reset time in error) — pausing scheduled classify until %s (fallback)",
                        _classify_paused_until.isoformat())


async def _digest_job(app: Application) -> None:
    log.info("scheduler: digest job firing")
    # Make sure we've classified anything still pending before composing
    run_classify(limit=200)
    try:
        digest_id, content = run_compose_digest()
    except Exception:
        log.exception("digest composition failed")
        return
    if digest_id == 0:
        log.info("digest skipped — not enough items above threshold for broadcast")
        return
    log.info("digest #%d composed, %d chars", digest_id, len(content))
    await bot.broadcast_digest(app, content, digest_id)

    # Regenerate the public static site (and git-push if configured) so the
    # newly-composed digest is web-visible immediately. Runs in a worker thread
    # so the blocking subprocess calls don't stall the event loop. Failures
    # here NEVER fail the digest — broadcast already succeeded.
    import asyncio
    try:
        await asyncio.to_thread(_publish_site_hook)
    except Exception:
        log.exception("publish-site hook failed")


def _publish_site_hook() -> None:
    """Regenerate static site + optionally git-push to GitHub.

    Output path is `~/dalila/docs` by default (matches GitHub Pages source
    config on `aryfcunha/dalila`: branch `main`, folder `/docs`).
    Override with `DALILA_SITE_OUT_DIR`.

    Set `DALILA_SITE_GIT_PUSH=1` to also stage/commit/push the rendered
    files. Off by default so dev boxes don't accidentally push.

    Defensive: every error is logged, none propagate. Publishing is a
    side-effect of digest delivery, not a precondition for it.
    """
    import os
    import subprocess
    from pathlib import Path

    from dalila.pipeline import run_publish_site

    out_dir = Path(
        os.getenv("DALILA_SITE_OUT_DIR")
        or (Path.home() / "dalila" / "docs")
    )

    # 1. Render the site
    try:
        result = run_publish_site(out_dir)
        log.info("site rendered to %s (%d digest pages)", out_dir, result.get("digests", 0))
    except Exception:
        log.exception("publish-site: render failed")
        return

    # 2. Stop here unless explicitly opted in to git push
    if os.getenv("DALILA_SITE_GIT_PUSH") != "1":
        return

    # 3. Find the git repo that contains the output directory
    repo = out_dir.resolve()
    for _ in range(8):
        if (repo / ".git").exists():
            break
        if repo.parent == repo:
            log.warning("publish-site: no .git found above %s; skipping push", out_dir)
            return
        repo = repo.parent

    try:
        rel_out = out_dir.resolve().relative_to(repo)
    except ValueError:
        log.warning("publish-site: out_dir %s not inside repo %s; skipping push", out_dir, repo)
        return

    # 4. Stage, commit (only if changed), push
    try:
        subprocess.run(["git", "add", "--", str(rel_out)],
                       cwd=repo, check=True, capture_output=True, text=True, timeout=30)
        # Check if anything is staged (exit 0 == no changes)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"],
                              cwd=repo, text=True, timeout=15)
        if diff.returncode == 0:
            log.info("publish-site: no site changes to commit")
            return
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        subprocess.run(["git", "commit", "-m", f"Publish site — {stamp}"],
                       cwd=repo, check=True, capture_output=True, text=True, timeout=30)
        subprocess.run(["git", "push", "origin", "main"],
                       cwd=repo, check=True, capture_output=True, text=True, timeout=120)
        log.info("publish-site: pushed to origin/main")
    except subprocess.CalledProcessError as exc:
        tail = ((exc.stderr or "") + (exc.stdout or "")).strip()[-500:]
        log.warning("publish-site: git failed (exit %d): %s", exc.returncode, tail)
    except subprocess.TimeoutExpired as exc:
        log.warning("publish-site: git timed out after %ss", exc.timeout)
    except Exception:
        log.exception("publish-site: unexpected error during git push")


def attach_jobs(scheduler: AsyncIOScheduler, app: Application) -> None:
    cfg = get_config()
    tz = pytz.timezone(cfg.timezone)

    # Ingest every 5 minutes (the _ingest_job itself backs off if no breaking news)
    scheduler.add_job(
        _ingest_job,
        trigger=IntervalTrigger(minutes=5),
        id="ingest",
        replace_existing=True,
    )

    # Classify every 5 minutes (cheap; only runs if there's a backlog)
    scheduler.add_job(
        _classify_job,
        trigger=IntervalTrigger(minutes=5),
        id="classify",
        replace_existing=True,
    )

    # Daily digest
    hour, minute = (int(x) for x in cfg.digest_time.split(":"))
    scheduler.add_job(
        _digest_job,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=tz),
        args=[app],
        id="digest",
        replace_existing=True,
    )

    # Convening pre-flight: once a day at 08:00 GST. Quiet on most days;
    # broadcasts a heads-up message when a tracked event starts within 7 days.
    scheduler.add_job(
        _convening_preflight_job,
        trigger=CronTrigger(hour=8, minute=0, timezone=tz),
        args=[app],
        id="convening_preflight",
        replace_existing=True,
    )

    # Doctrine pass: every 15 min, cheap when there's nothing pending.
    # Runs independently of the classify schedule so a doctrine rate-limit
    # doesn't block fresh classification (and vice versa).
    scheduler.add_job(
        _doctrine_job,
        trigger=IntervalTrigger(minutes=15),
        id="doctrine",
        replace_existing=True,
    )

    # Prediction markets: poll every 30 minutes for probability deltas.
    scheduler.add_job(
        _markets_job,
        trigger=IntervalTrigger(minutes=30),
        id="markets",
        replace_existing=True,
    )

    log.info(
        "jobs scheduled: ingest every 5m (dynamic), classify every 5m, "
        "doctrine every 15m, markets every 30m, digest daily at %s %s",
        cfg.digest_time, cfg.timezone,
    )



_doctrine_paused_until: datetime | None = None


def _markets_job() -> None:
    """Poll prediction markets for probability deltas. Logs large moves."""
    log.info("scheduler: markets tick")
    try:
        from dalila.ingestors.prediction_markets import poll_markets
        with db.connect() as conn:
            alerts = poll_markets(conn)
        if alerts:
            log.warning(
                "market alerts: %d large moves detected: %s",
                len(alerts),
                ", ".join(f"{a['question'][:40]} ({(a['delta_24h'] or 0)*100:+.1f}%)" for a in alerts),
            )
    except Exception:
        log.exception("markets job failed")


# Tracks event-ids we've already pre-flighted so we don't spam the user
# with the same heads-up every day in the 7-day window. Cleared on
# scheduler restart — that's fine, a duplicate notification across
# restarts is harmless and rare.
_preflighted_events: set[str] = set()


async def _convening_preflight_job(app: Application) -> None:
    """Broadcast a heads-up for any tracked convening event starting within 7 days.

    See `events.yaml`. UAE Foreign Aid Policy §7.4 treats convening as a
    doctrinal instrument — doctrine announcements cluster around these
    platforms, so the user wants context loaded in advance.
    """
    from dalila.config import load_convening_events
    from datetime import date

    today = date.today()
    upcoming: list[dict] = []
    for ev in load_convening_events():
        sd = ev.get("start_date")
        if not sd or ev.get("id") in _preflighted_events:
            continue
        try:
            start = date.fromisoformat(str(sd))
        except ValueError:
            continue
        days_out = (start - today).days
        if 0 <= days_out <= 7:
            ev["_days_out"] = days_out
            upcoming.append(ev)

    if not upcoming:
        return

    lines = ["📅 *Upcoming UAE-relevant convening events*", ""]
    for ev in upcoming:
        lines.append(
            f"*{ev.get('name', '')}* — starts in {ev['_days_out']} day(s) "
            f"({ev.get('start_date')})"
        )
        if ev.get("location"):
            lines.append(f"📍 {ev['location']}")
        focus = (ev.get("topic_focus") or "").strip()
        if focus:
            # Collapse multi-line YAML strings to single paragraph
            focus = " ".join(focus.split())
            lines.append(focus)
        if ev.get("url"):
            lines.append(ev["url"])
        lines.append("")
        _preflighted_events.add(ev["id"])

    text = "\n".join(lines)
    with db.connect() as conn:
        users = db.enabled_users(conn)
    for user in users:
        try:
            await app.bot.send_message(
                chat_id=user["chat_id"],
                text=text,
                parse_mode=__import__("telegram").constants.ParseMode.MARKDOWN,
            )
        except Exception as exc:
            log.warning("convening preflight delivery failed for %s: %s", user["chat_id"], exc)


def _doctrine_job() -> None:
    global _doctrine_paused_until
    now = datetime.now(timezone.utc)
    if _doctrine_paused_until and now < _doctrine_paused_until:
        return
    from dalila import doctrine as doctrine_module
    log.info("scheduler: doctrine tick")
    result = doctrine_module.run_pass(limit=10)
    log.info("doctrine done: %s", result)
    if result.get("rate_limited"):
        _doctrine_paused_until = now + _RATE_LIMIT_BACKOFF
        log.warning("doctrine rate-limited — paused until %s",
                    _doctrine_paused_until.isoformat())
