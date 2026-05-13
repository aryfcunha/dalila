"""Pipeline orchestration: ingest → prefilter → classify → digest."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import pytz

from dalila import db
from dalila.classifier import classify_batch
from dalila.config import (
    get_config,
    load_entity_aliases,
    load_prefilter_keywords,
)
from dalila.editor import compose_deep_dive, compose_digest
from dalila.ingestors.base import ingest_source, iter_enabled_sources
from dalila.models import RawItem
from dalila.simhash import is_near_duplicate

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


# Matches Claude Code's rate-limit phrasing, e.g.
#   "You've hit your limit — resets 5:30pm (Asia/Dubai)"
#   "You've hit your limit, resets at 17:30 (UTC)"
# Captures the clock time and timezone separately.
_RATE_LIMIT_RESET_RE = re.compile(
    r"resets\s*(?:at\s*)?(\d{1,2}):(\d{2})\s*(am|pm)?\s*\(?([A-Za-z_/]+(?:/[A-Za-z_]+)?)?",
    re.IGNORECASE,
)


def parse_rate_limit_reset(err_msg: str, now: datetime | None = None) -> datetime | None:
    """Extract the reset time from a Claude rate-limit error.

    Returns a UTC `datetime` for when the limit clears, or `None` if the
    message doesn't carry a parseable reset time. If the parsed clock time
    has already passed today in the given timezone, we roll to tomorrow.
    """
    m = _RATE_LIMIT_RESET_RE.search(err_msg)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2))
    ampm = (m.group(3) or "").lower()
    tz_name = m.group(4) or "UTC"

    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0

    try:
        tz = pytz.timezone(tz_name)
    except pytz.UnknownTimeZoneError:
        return None

    now = now or datetime.now(timezone.utc)
    local_now = now.astimezone(tz)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate = candidate + timedelta(days=1)
    return candidate.astimezone(timezone.utc)


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
    rate_limit_reset_at: datetime | None = None
    rate_limit_message: str | None = None
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
                    rate_limit_message = err_msg
                    rate_limit_reset_at = parse_rate_limit_reset(err_msg)
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
                            rate_limit_message = inner_msg
                            rate_limit_reset_at = parse_rate_limit_reset(inner_msg)
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
        "rate_limit_reset_at": rate_limit_reset_at,
        "rate_limit_message": rate_limit_message,
        "batches_done": batches_done,
        "capped": False,
    }


def _dedupe_by_simhash(items: list[dict], threshold: int = 12) -> list[dict]:
    """Drop near-duplicate titles (cross-outlet reposts of the same story).

    Items are already sorted by score in items_for_digest, so the first
    occurrence of a cluster is the best one — we keep that and drop later
    items whose title-simhash is within `threshold` Hamming bits.

    Threshold rationale (see simhash.py docstring for measurements): 12 bits
    on a 64-bit hash catches cross-outlet reposts (~6-10 bits apart in
    practice) without merging unrelated stories (~28-40 bits apart).
    """
    kept: list[dict] = []
    dropped = 0
    for item in items:
        sh = item.get("title_simhash")
        if not sh:
            kept.append(item)
            continue
        is_dup = any(is_near_duplicate(sh, k.get("title_simhash"), threshold) for k in kept)
        if is_dup:
            dropped += 1
            continue
        kept.append(item)
    if dropped:
        log.info("digest dedup: dropped %d near-duplicate items (simhash ≤%d bits)", dropped, threshold)
    return kept


def run_deep_dive(topic: str, since_hours: int = 720, max_items: int = 20) -> tuple[str, list[int]]:
    """Compose a deep-dive on `topic` over recent classified items.

    Returns (markdown, [item_ids]) for the bot to send and to register so
    `link N` resolves to the items the deep-dive actually drew on.
    """
    with db.connect() as conn:
        items = db.items_matching_topic(conn, topic, since_hours=since_hours, limit=max_items)
    text, ids = compose_deep_dive(topic, items)
    return text, ids


def run_compose_digest(since_hours: int = 24, min_relevance: float = 0.4, max_items: int = 25) -> tuple[int, str]:
    """Compose today's digest. Returns (digest_id, content)."""
    with db.connect() as conn:
        items = db.items_for_digest(conn, since_hours=since_hours, min_relevance=min_relevance)
        items = _dedupe_by_simhash(items)
        items = items[:max_items]
        content, _ = compose_digest(items)
        cfg = get_config()
        # Use cfg.timezone for the date label saved alongside the digest
        import pytz
        tz = pytz.timezone(cfg.timezone)
        date_label = datetime.now(tz).strftime("%A %d %B %Y")
        digest_id = db.save_digest(conn, date_label=date_label, content=content, item_ids=[i["id"] for i in items])
    return digest_id, content


def run_render_html_digest(
    since_hours: int = 24, min_relevance: float = 0.4, max_items: int = 25,
    when: datetime | None = None,
) -> tuple[str, list[dict]]:
    """Render an HTML digest for a single day.

    No LLM call — pure presentation over already-classified items.
    Returns (html_string, items_used).
    """
    from dalila.html_digest import render_digest
    with db.connect() as conn:
        items = db.items_for_digest(conn, since_hours=since_hours, min_relevance=min_relevance)
        total = len(items)
        items = _dedupe_by_simhash(items)
        items = items[:max_items]
    html_str = render_digest(items, when=when, total_ingested=total)
    return html_str, items


def run_publish_site(out_dir: "Path") -> dict:
    """Generate the full static site at `out_dir`:
        index.html                           — archive landing page
        about.html                           — project info, sources, subscribe
        digests/<YYYY-MM-DD>.html            — one per persisted digest

    Uses already-persisted digests (db.digests) where available — re-rendering
    each as an HTML page from the items they referenced. The most recent day
    also picks up items classified TODAY that haven't been formally digested
    yet (so the index always reflects the latest classified data).
    """
    from pathlib import Path
    import json as _json
    from dalila.config import load_sources
    from dalila.html_digest import render_about, render_digest, render_index

    out_dir = Path(out_dir)
    digests_dir = out_dir / "digests"
    digests_dir.mkdir(parents=True, exist_ok=True)

    written: list[dict] = []

    with db.connect() as conn:
        # Pull persisted digests, newest first.
        rows = conn.execute(
            "SELECT id, composed_at, date_label, item_ids_json FROM digests ORDER BY composed_at DESC LIMIT 60"
        ).fetchall()

        for row in rows:
            try:
                item_ids = _json.loads(row["item_ids_json"]) or []
            except Exception:
                item_ids = []
            if not item_ids:
                continue
            items = _items_by_ids(conn, item_ids)
            if not items:
                continue
            # composed_at is ISO; pick the calendar date from it
            try:
                composed = datetime.fromisoformat(row["composed_at"].replace("Z", "+00:00"))
            except Exception:
                composed = datetime.now(timezone.utc)
            slug = composed.strftime("%Y-%m-%d")
            html_str = render_digest(items, when=composed, total_ingested=len(items))
            (digests_dir / f"{slug}.html").write_text(html_str, encoding="utf-8")
            written.append({
                "slug": slug,
                "date_label": row["date_label"],
                "preview": _preview_text(items, n=2),
            })

    # If no persisted digests, render today from live items so the site isn't empty.
    if not written:
        html_str, items = run_render_html_digest()
        if items:
            slug = datetime.now().strftime("%Y-%m-%d")
            (digests_dir / f"{slug}.html").write_text(html_str, encoding="utf-8")
            written.append({
                "slug": slug,
                "date_label": slug,
                "preview": _preview_text(items, n=2),
            })

    # Index (archive)
    index_html = render_index(written)
    (out_dir / "index.html").write_text(index_html, encoding="utf-8")

    # About
    sources = load_sources()
    about_html = render_about(sources)
    (out_dir / "about.html").write_text(about_html, encoding="utf-8")

    return {
        "out_dir": str(out_dir),
        "digests": len(written),
        "wrote": [str(out_dir / "index.html"), str(out_dir / "about.html")] +
                 [str(digests_dir / f"{d['slug']}.html") for d in written],
    }


def _items_by_ids(conn, ids: list[int]) -> list[dict]:
    """Fetch persisted item rows for a digest, preserving order."""
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT i.id, i.title, i.url, i.body, i.one_line_summary, i.category,
                   i.uae_relevance, i.severity, i.entities_json, i.doctrine_relation,
                   i.policy_sector, i.country_focus_json,
                   s.name AS source_name
            FROM items i LEFT JOIN sources s ON s.id = i.source_id
            WHERE i.id IN ({placeholders})""",
        ids,
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    out: list[dict] = []
    import json as _json
    for iid in ids:
        r = by_id.get(iid)
        if r is None:
            continue
        try:
            entities = _json.loads(r["entities_json"]) if r["entities_json"] else []
        except Exception:
            entities = []
        try:
            countries = _json.loads(r["country_focus_json"]) if "country_focus_json" in r.keys() and r["country_focus_json"] else []
        except Exception:
            countries = []
        out.append({
            "id": r["id"],
            "title": r["title"],
            "url": r["url"],
            "summary": r["one_line_summary"] or (r["body"] or "")[:200],
            "category": r["category"],
            "source": r["source_name"],
            "uae_relevance": r["uae_relevance"],
            "severity": r["severity"],
            "entities": entities,
            "doctrine_relation": r["doctrine_relation"],
            "policy_sector": r["policy_sector"],
            "country_focus": countries,
        })
    return out


def _preview_text(items: list[dict], *, n: int = 2) -> str:
    """Short prose preview for the archive index — first N item titles, joined."""
    titles = [i.get("title") or "" for i in items[:n]]
    return " · ".join(t[:80] for t in titles if t)
