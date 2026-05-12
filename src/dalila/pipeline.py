"""Pipeline orchestration: ingest → prefilter → classify → digest."""

from __future__ import annotations

import logging
from datetime import datetime

from dalila import db
from dalila.classifier import classify_batch
from dalila.config import (
    get_config,
    load_entity_aliases,
    load_prefilter_keywords,
)
from dalila.editor import compose_digest
from dalila.ingestors.base import ingest_source, iter_enabled_sources
from dalila.models import RawItem

log = logging.getLogger(__name__)


def _prefilter_match(item: RawItem, keywords: list[str], aliases: list[str]) -> bool:
    """Cheap keyword/entity match before paying for a classifier call."""
    haystack = (item.title + " " + (item.body or "")).lower()
    for kw in keywords:
        if kw in haystack:
            return True
    for alias in aliases:
        if alias and alias in haystack:
            return True
    return False


def run_ingest() -> dict:
    """Pull from every enabled source, persist new items, return per-source stats."""
    keywords = load_prefilter_keywords()
    aliases = load_entity_aliases()
    stats: dict[str, dict] = {}

    with db.connect() as conn:
        for src in iter_enabled_sources():
            sid = src["id"]
            new_count = 0
            passed_count = 0
            error: str | None = None
            try:
                items = ingest_source(src)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                log.exception("ingest failed for %s", sid)
                items = []

            for it in items:
                # Source-level boost: UAE state/entity sources always pass the prefilter
                # because their full output is high-signal by definition.
                src_tags = src.get("tags") or []
                is_high_signal = any(t in ("uae", "state", "entity") for t in src_tags)
                passed = is_high_signal or _prefilter_match(it, keywords, aliases)

                row_id = db.insert_item(conn, it, prefilter_passed=passed)
                if row_id is not None:
                    new_count += 1
                    if passed:
                        passed_count += 1

            db.mark_source_polled(conn, sid, items_seen=len(items), error=error)
            stats[sid] = {
                "fetched": len(items),
                "new": new_count,
                "prefilter_passed": passed_count,
                "error": error,
            }
    return stats


def _is_transient_error(err_msg: str) -> bool:
    lower = err_msg.lower()
    return any(t in lower for t in (
        "you've hit your limit", "rate limit", "rate_limit",
        "timeout", "connection", "503", "529",
    ))


def run_classify(limit: int = 100, batch_size: int = 25) -> dict:
    """Classify up to `limit` prefilter-passed items in batches of `batch_size`.

    Each batch goes to Haiku in a single CLI call — far cheaper than one-call-
    per-item (amortises CLI process spawn + system-prompt re-send).

    Error handling:
      - Transient error (rate limit / network / timeout) → abort run, leave
        items unclassified, retry on next tick.
      - Terminal error (malformed JSON, count mismatch after retry) → fall back
        to one-at-a-time for that batch so we mark only the bad item, not
        the whole batch.
    """
    cfg = get_config()
    classified = 0
    errors = 0
    rate_limited = False
    batches_done = 0

    with db.connect() as conn:
        if db.todays_classifier_call_count(conn) >= cfg.daily_classifier_call_cap:
            log.warning("daily classifier cap (%d) reached; skipping classify run",
                        cfg.daily_classifier_call_cap)
            return {"classified": 0, "errors": 0, "capped": True}

        rows = db.unclassified_items(conn, limit=limit)
        log.info("classifying %d items in batches of %d (%d batches)",
                 len(rows), batch_size, (len(rows) + batch_size - 1) // batch_size)

        for batch_start in range(0, len(rows), batch_size):
            batch_rows = rows[batch_start : batch_start + batch_size]
            payload = [
                {"title": r["title"], "body": r["body"], "url": r["url"]}
                for r in batch_rows
            ]

            try:
                classifications = classify_batch(payload)
            except Exception as exc:
                err_msg = f"{type(exc).__name__}: {exc}"
                if _is_transient_error(err_msg):
                    log.warning("transient error on batch — aborting, %d items stay queued: %s",
                                len(batch_rows), err_msg[:200])
                    rate_limited = True
                    break
                # Terminal — could be a bad item poisoning the batch.
                # Fall back to one-at-a-time so we only blame the actual culprit.
                log.warning("batch failed (%s); retrying %d items individually", err_msg[:200], len(batch_rows))
                for r in batch_rows:
                    try:
                        [single] = classify_batch([{"title": r["title"], "body": r["body"], "url": r["url"]}])
                        db.save_classification(conn, r["id"], single)
                        classified += 1
                    except Exception as inner:
                        inner_msg = f"{type(inner).__name__}: {inner}"
                        if _is_transient_error(inner_msg):
                            log.warning("transient error mid-fallback — aborting: %s", inner_msg[:200])
                            rate_limited = True
                            break
                        errors += 1
                        log.warning("classify failed for item %d (marking): %s", r["id"], inner_msg)
                        db.save_classifier_error(conn, r["id"], inner_msg)
                if rate_limited:
                    break
                continue

            # Happy path: write all classifications back
            for r, c in zip(batch_rows, classifications):
                db.save_classification(conn, r["id"], c)
                classified += 1
            batches_done += 1
            log.info("batch %d done (%d items classified so far)", batches_done, classified)

    return {
        "classified": classified,
        "errors": errors,
        "rate_limited": rate_limited,
        "batches_done": batches_done,
        "capped": False,
    }


def run_compose_digest(since_hours: int = 24, min_relevance: float = 0.4, max_items: int = 25) -> tuple[int, str]:
    """Compose today's digest. Returns (digest_id, content)."""
    with db.connect() as conn:
        items = db.items_for_digest(conn, since_hours=since_hours, min_relevance=min_relevance)
        items = items[:max_items]
        content, _ = compose_digest(items)
        cfg = get_config()
        # Use cfg.timezone for the date label saved alongside the digest
        import pytz
        tz = pytz.timezone(cfg.timezone)
        date_label = datetime.now(tz).strftime("%A %d %B %Y")
        digest_id = db.save_digest(conn, date_label=date_label, content=content, item_ids=[i["id"] for i in items])
    return digest_id, content
