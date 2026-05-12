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

# When the classifier hits the rate limit, back off this long before next attempt.
# Beats the default 5-minute scheduler cadence — most rate-limit windows on the
# Claude Code Pro/Max plan are 1h or 5h. One hour is a conservative middle.
_RATE_LIMIT_BACKOFF = timedelta(hours=1)
_classify_paused_until: datetime | None = None


def _ingest_job() -> None:
    log.info("scheduler: ingest tick")
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
        _classify_paused_until = now + _RATE_LIMIT_BACKOFF
        log.warning("rate-limited — pausing scheduled classify until %s",
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
    log.info("digest #%d composed, %d chars", digest_id, len(content))
    await bot.broadcast_digest(app, content, digest_id)


def attach_jobs(scheduler: AsyncIOScheduler, app: Application) -> None:
    cfg = get_config()
    tz = pytz.timezone(cfg.timezone)

    # Ingest every N minutes
    scheduler.add_job(
        _ingest_job,
        trigger=IntervalTrigger(minutes=cfg.ingest_interval_minutes),
        id="ingest",
        replace_existing=True,
        next_run_time=None,  # don't fire immediately on bot start
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
    log.info("jobs scheduled: ingest every %dm, classify every 5m, digest daily at %s %s",
             cfg.ingest_interval_minutes, cfg.digest_time, cfg.timezone)
