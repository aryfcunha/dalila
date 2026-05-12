"""Pipeline orchestration: ingest → prefilter → classify → digest."""

from __future__ import annotations

import logging
from datetime import datetime

from dalila import db
from dalila.classifier import classify
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


def run_classify(limit: int = 100) -> dict:
    """Classify up to `limit` prefilter-passed items not yet classified.

    Distinguishes *transient* errors (rate limit, network) from *terminal* errors
    (malformed response, can't parse JSON after retry):
      - Transient → abort the batch immediately, leave items unclassified for retry.
      - Terminal  → mark the item with classifier_error so we don't infinite-loop.
    """
    cfg = get_config()
    classified = 0
    errors = 0
    rate_limited = False

    with db.connect() as conn:
        if db.todays_classifier_call_count(conn) >= cfg.daily_classifier_call_cap:
            log.warning(
                "daily classifier cap (%d) reached; skipping classify run",
                cfg.daily_classifier_call_cap,
            )
            return {"classified": 0, "errors": 0, "capped": True}

        rows = db.unclassified_items(conn, limit=limit)
        log.info("classifying %d items", len(rows))

        for row in rows:
            try:
                c = classify(
                    title=row["title"],
                    body=row["body"],
                    url=row["url"],
                )
                db.save_classification(conn, row["id"], c)
                classified += 1
            except Exception as exc:
                err_msg = f"{type(exc).__name__}: {exc}"
                lower = err_msg.lower()
                # Transient: do NOT mark the item; abort batch and let next tick retry.
                if any(t in lower for t in ("you've hit your limit", "rate limit", "rate_limit",
                                            "timeout", "connection", "503", "529")):
                    log.warning("transient error — aborting batch, item %d stays in queue: %s",
                                row["id"], err_msg[:200])
                    rate_limited = True
                    break
                # Terminal: mark so we don't keep retrying malformed responses
                errors += 1
                log.warning("classify failed for item %d (marking): %s", row["id"], err_msg)
                db.save_classifier_error(conn, row["id"], err_msg)

    return {"classified": classified, "errors": errors, "rate_limited": rate_limited, "capped": False}


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
