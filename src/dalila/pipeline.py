"""Pipeline orchestration: ingest → prefilter → classify → digest."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import pytz

from dalila import db
from dalila.classifier import classify_batch as classify_batch_claude
from dalila.config import (
    get_config,
    load_entity_aliases,
    load_prefilter_keywords,
)
from dalila.editor import compose_deep_dive, compose_digest
from dalila.ingestors.base import ingest_source, iter_enabled_sources
from dalila.models import RawItem, title_case_clean
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
                if it.title:
                    it.title = title_case_clean(it.title)
                
                # Source-level boost: UAE state/entity sources always pass the prefilter
                # because their full output is high-signal by definition.
                src_tags = src.get("tags") or []
                is_high_signal = any(t in ("state", "entity") for t in src_tags)
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
            # Flush the WAL between sources. Like classify, this connection is
            # held open across every source's (network-bound, multi-second)
            # fetch, which blocks SQLite's auto-checkpoint and lets the -wal
            # file grow. Best-effort TRUNCATE between sources keeps it bounded.
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
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


def _resolve_classifier(backend: str):
    """Return the classify_batch function for the chosen backend.

    `backend` is one of:
      - "claude" (default) → Haiku 4.5 via the `claude` CLI. The architectural
        default per CLAUDE.md.
      - "deepseek"         → DeepSeek `deepseek-chat` over HTTP. Opt-in,
        intended for cost-bounded historical backfills. Requires
        DEEPSEEK_API_KEY env var.
    """
    if backend == "deepseek":
        from dalila.deepseek import classify_batch as classify_batch_deepseek
        return classify_batch_deepseek
    if backend in ("claude", "haiku", ""):
        return classify_batch_claude
    raise ValueError(f"unknown classifier backend: {backend!r} (expected 'claude' or 'deepseek')")


def run_classify(
    limit: int = 100,
    batch_size: int = 25,
    *,
    backend: str = "claude",
    workers: int = 1,
) -> dict:
    """Classify up to `limit` prefilter-passed items in batches of `batch_size`.

    `backend` selects the LLM ("claude" = Haiku via CLI, default; "deepseek" =
    DeepSeek API for backfill jobs). See `_resolve_classifier`.

    `workers` runs that many batches concurrently. Useful for DeepSeek (paid
    per-call API, generous rate limits, output-token-bound latency of ~60s
    per batch); 4-6 workers cuts wall-clock 4-6x. Keep at 1 for Claude — the
    CLI shares a single rate-limit pool across calls so parallelism doesn't
    help and risks tripping the shared bucket.

    Each batch goes to the chosen model in a single call — far cheaper than
    one-call-per-item (amortises spawn + system-prompt re-send).

    Error handling (serial path):
      - Transient error (rate limit / network / timeout) → abort run, leave
        items unclassified, retry on next tick.
      - Terminal error (malformed JSON, count mismatch after retry) → fall back
        to one-at-a-time for that batch so we mark only the bad item, not
        the whole batch.

    Parallel path: errors are collected with their batch; rate-limit errors
    stop further submission but in-flight batches still drain. Per-item
    fallback retries happen serially after the pool drains.
    """
    classify_batch = _resolve_classifier(backend)
    cfg = get_config()
    classified = 0
    errors = 0
    rate_limited = False
    rate_limit_reset_at: datetime | None = None
    rate_limit_message: str | None = None
    batches_done = 0
    log.info("classify: using backend=%s workers=%d", backend, workers)

    with db.connect() as conn:
        if db.todays_classifier_call_count(conn) >= cfg.daily_classifier_call_cap:
            log.warning("daily classifier cap (%d) reached; skipping classify run",
                        cfg.daily_classifier_call_cap)
            return {"classified": 0, "errors": 0, "capped": True}

        rows = db.unclassified_items(conn, limit=limit)
        total_batches = (len(rows) + batch_size - 1) // batch_size
        log.info("classifying %d items in batches of %d (%d batches, %d worker%s)",
                 len(rows), batch_size, total_batches, workers, "s" if workers != 1 else "")

        batches = [rows[i : i + batch_size] for i in range(0, len(rows), batch_size)]

        # -------- Parallel path (DeepSeek) --------
        if workers > 1:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def _do(batch_rows):
                payload = [
                    {"title": r["title"], "body": r["body"], "url": r["url"]}
                    for r in batch_rows
                ]
                return classify_batch(payload)

            failed_batches: list[list] = []   # batches that hit terminal errors
            with ThreadPoolExecutor(max_workers=workers) as pool:
                fut_to_batch = {pool.submit(_do, b): b for b in batches}
                for fut in as_completed(fut_to_batch):
                    batch_rows = fut_to_batch[fut]
                    try:
                        classifications = fut.result()
                    except Exception as exc:
                        err_msg = f"{type(exc).__name__}: {exc}"
                        if _is_transient_error(err_msg):
                            log.warning("transient error on batch (parallel): %s", err_msg[:200])
                            rate_limited = True
                            rate_limit_message = err_msg
                            rate_limit_reset_at = parse_rate_limit_reset(err_msg)
                            # Don't break — let in-flight batches drain; new ones
                            # won't be submitted since we already submitted all upfront.
                            continue
                        # Terminal — retry individually after the pool drains.
                        log.warning("batch failed (%s); queueing %d items for serial retry",
                                    err_msg[:200], len(batch_rows))
                        failed_batches.append(batch_rows)
                        continue
                    # Happy path
                    for r, c in zip(batch_rows, classifications):
                        db.save_classification(conn, r["id"], c)
                        classified += 1
                    batches_done += 1
                    if batches_done % 5 == 0 or batches_done == total_batches:
                        log.info("batch %d/%d done (%d items classified)",
                                 batches_done, total_batches, classified)

            # Serial retry for terminal-error batches (one item at a time).
            for batch_rows in failed_batches:
                for r in batch_rows:
                    try:
                        [single] = classify_batch(
                            [{"title": r["title"], "body": r["body"], "url": r["url"]}]
                        )
                        db.save_classification(conn, r["id"], single)
                        classified += 1
                    except Exception as inner:
                        inner_msg = f"{type(inner).__name__}: {inner}"
                        if _is_transient_error(inner_msg):
                            rate_limited = True
                            rate_limit_message = inner_msg
                            rate_limit_reset_at = parse_rate_limit_reset(inner_msg)
                            break
                        errors += 1
                        db.save_classifier_error(conn, r["id"], inner_msg)
                if rate_limited:
                    break

            return {
                "classified": classified,
                "errors": errors,
                "rate_limited": rate_limited,
                "rate_limit_reset_at": rate_limit_reset_at,
                "rate_limit_message": rate_limit_message,
                "batches_done": batches_done,
                "capped": False,
            }

        # -------- Serial path (Claude — original behaviour) --------
        for batch_rows in batches:
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

            for r, c in zip(batch_rows, classifications):
                db.save_classification(conn, r["id"], c)
                classified += 1
            batches_done += 1
            log.info("batch %d done (%d items classified so far)", batches_done, classified)
            # Flush the WAL back into the main DB between batches. This
            # connection stays open across every multi-second LLM call for the
            # whole (minutes-long) run, so SQLite can't auto-checkpoint and the
            # -wal file grows without bound — it reached ~400 MB / 35k pages on
            # the VM and filled the disk. We're in autocommit, so each
            # save_classification above is already committed; TRUNCATE flushes
            # those frames into the DB and resets the WAL file to empty. If a
            # concurrent ingest/doctrine write holds the lock it returns busy
            # and we just retry after the next batch. Best-effort.
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            # Re-check daily cap after each batch so a large single call can't
            # blow past the limit set by the operator.
            if db.todays_classifier_call_count(conn) >= cfg.daily_classifier_call_cap:
                log.warning("daily classifier cap (%d) reached after %d items; stopping",
                            cfg.daily_classifier_call_cap, classified)
                break

    return {
        "classified": classified,
        "errors": errors,
        "rate_limited": rate_limited,
        "rate_limit_reset_at": rate_limit_reset_at,
        "rate_limit_message": rate_limit_message,
        "batches_done": batches_done,
        "capped": False,
    }


def run_backfill(
    since: "date",
    until: "date | None" = None,
    *,
    source: str | None = None,
    fetch_body: bool = True,
    concurrency: int = 12,
    max_per_source: int = 4000,
    gdelt_step_minutes: int = 60,
) -> dict:
    """Historical backfill from the dated archives of one or all backfill sources.

    Sources:
      - Sitemap-based (wam_en, the_national, gulf_news) — walks XML sitemaps,
        fetches each article HTML for title + meta-description.
      - gdelt_v2 — walks 15-min GKG slices (sampled hourly by default).
      - acled — paginated event-date range query.

    Items are inserted via `db.insert_item` so dedup (url_hash UNIQUE) is
    automatic — re-running a backfill is safe. Prefilter is honored exactly
    like the live ingest path (high-signal source tags auto-pass).
    """
    from datetime import date as _date
    from dalila.ingestors import sitemap as sitemap_mod
    from dalila.ingestors import gdelt as gdelt_mod
    from dalila.ingestors import acled as acled_mod

    until = until or _date.today()
    keywords = load_prefilter_keywords()
    aliases = load_entity_aliases()

    # Pull source registry so we can look up tags / register if missing.
    with db.connect() as conn:
        existing_src_rows = {
            r["id"]: r for r in conn.execute("SELECT id, name FROM sources").fetchall()
        }
    src_yaml_by_id = {s["id"]: s for s in iter_enabled_sources()}
    # Also include disabled sources from the yaml — wam_en is disabled by default
    # but we still want to backfill it.
    from dalila.config import load_sources as _load_sources
    for s in _load_sources():
        src_yaml_by_id.setdefault(s["id"], s)

    def _src_meta(sid: str) -> dict:
        return src_yaml_by_id.get(sid) or {"id": sid, "tags": []}

    # The set of sources to run.
    all_sources = list(sitemap_mod.BACKFILL_SOURCES.keys()) + ["gdelt_v2", "acled"]
    if source:
        if source not in all_sources:
            raise ValueError(
                f"unknown backfill source {source!r}; choices: {', '.join(all_sources)}"
            )
        targets = [source]
    else:
        targets = all_sources

    stats: dict[str, dict] = {}
    with db.connect() as conn:
        for sid in targets:
            log.info("backfill: starting source=%s since=%s until=%s", sid, since, until)
            meta = _src_meta(sid)
            tags = meta.get("tags") or []
            is_high_signal = any(t in ("uae", "state", "entity") for t in tags)

            # Make sure the source row exists in the DB (the items FK relies on it).
            if sid not in existing_src_rows:
                conn.execute(
                    "INSERT OR IGNORE INTO sources(id, name, kind, url, quality, enabled) "
                    "VALUES(?, ?, ?, ?, ?, ?)",
                    (sid, meta.get("name") or sid, meta.get("kind") or "backfill",
                     meta.get("url") or None, int(meta.get("quality", 3)),
                     1 if meta.get("enabled", True) else 0),
                )

            new_count = 0
            passed_count = 0
            seen_count = 0
            error: str | None = None
            try:
                if sid == "gdelt_v2":
                    iterator = gdelt_mod.iter_items_range(
                        sid, since, until,
                        step_minutes=gdelt_step_minutes,
                        concurrency=min(concurrency, 8),
                    )
                elif sid == "acled":
                    iterator = acled_mod.iter_items_range(sid, since, until)
                else:
                    # Per-source override: respect fetch_body unless the source
                    # already opted out via its BACKFILL_SOURCES recipe.
                    src_recipe = sitemap_mod.BACKFILL_SOURCES.get(sid, {}).copy()
                    src_recipe["fetch_body"] = fetch_body and src_recipe.get("fetch_body", True)
                    sitemap_mod.BACKFILL_SOURCES[sid] = src_recipe
                    iterator = sitemap_mod.iter_items(
                        sid, since, until,
                        max_per_source=max_per_source,
                        concurrency=concurrency,
                    )

                for it in iterator:
                    seen_count += 1
                    passed = is_high_signal or _prefilter_match(it, keywords, aliases)
                    row_id = db.insert_item(conn, it, prefilter_passed=passed)
                    if row_id is not None:
                        new_count += 1
                        if passed:
                            passed_count += 1
                    # Periodic commit so a long run doesn't lose everything on Ctrl-C.
                    if seen_count % 200 == 0:
                        try:
                            conn.commit()
                        except Exception:
                            pass
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                log.exception("backfill source=%s failed", sid)

            stats[sid] = {
                "fetched": seen_count,
                "new": new_count,
                "prefilter_passed": passed_count,
                "error": error,
            }
            log.info("backfill[%s] done: fetched=%d new=%d passed=%d error=%s",
                     sid, seen_count, new_count, passed_count, error or "-")
    return stats


def _dedupe_by_simhash(items: list[dict], threshold: int = 16) -> list[dict]:
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


def run_compose_digest(
    since_hours: int = 24,
    min_relevance: float = 0.4,
    max_items: int = 25,
    *,
    as_of: datetime | None = None,
    exclude_ids: set[int] | None = None,
    dedupe_against_recent_digests: bool = True,
) -> tuple[int, str]:
    """Compose a digest. Returns (digest_id, content).

    `as_of` defaults to now. Set to a past UTC datetime to backfill a
    historical brief — the editor receives items classified in the 24h
    window ending at `as_of`, and the saved date_label / composed_at
    reflect that moment.

    `exclude_ids` is a hard exclusion set (used during backfill to suppress
    items already used in a more-recent brief from the same run).

    `dedupe_against_recent_digests` (default True) additionally suppresses
    item IDs that appeared in any digest persisted in the last 7 days.
    """
    import pytz
    cfg = get_config()
    tz = pytz.timezone(cfg.timezone)

    end_utc = as_of or datetime.now(timezone.utc)
    end_local = end_utc.astimezone(tz)
    date_label = end_local.strftime("%A %d %B %Y")

    with db.connect() as conn:
        excluded: set[int] = set(exclude_ids or ())
        if dedupe_against_recent_digests:
            excluded |= db.previously_digested_item_ids(conn, since_hours=24 * 7)
        items = db.items_for_digest(
            conn,
            since_hours=since_hours,
            min_relevance=min_relevance,
            as_of=end_utc,
            exclude_ids=excluded or None,
        )
        items = _dedupe_by_simhash(items)
        items = items[:max_items]

        # Fetch market signals for the Telegram brief
        market_signals: list[dict] = []
        try:
            from dalila.ingestors.prediction_markets import get_market_signals
            market_signals = get_market_signals(conn, digest_items=items)
        except Exception as exc:
            log.warning("market signals unavailable for composer: %s", exc)

        content, _ = compose_digest(items, when=end_local, market_signals=market_signals)
        # If the editor returned an empty fallback (< 3 items), don't persist
        # a placeholder digest — clutters the DB and the archive iteration
        # logic already skips zero-item digests in publish-site. Returning
        # digest_id=0 signals "nothing useful here" to callers.
        if len(items) < 3:
            log.info("digest skipped (only %d items above threshold for %s)",
                     len(items), date_label)
            return 0, content
        digest_id = db.save_digest(
            conn,
            date_label=date_label,
            content=content,
            item_ids=[i["id"] for i in items],
        )
    return digest_id, content


def run_backfill_digests(days: int = 5, min_relevance: float = 0.4,
                         *, only_missing: bool = False) -> list[dict]:
    """Compose `days` daily briefs, one per day for the past N days at 06:30 GST.

    Iterates from oldest to newest. Each composed brief's item_ids are added
    to an in-run exclusion set so the same item never appears in two briefs.
    Day boundaries are at 06:30 in cfg.timezone.

    When `only_missing` is True, days that already have a persisted digest are
    skipped without an LLM call — so re-running a backfill over a wide window
    only spends tokens on the genuinely-missing days. Their item_ids are still
    folded into the dedup set so a freshly-composed neighbouring day doesn't
    reuse stories already published.
    """
    import pytz
    cfg = get_config()
    tz = pytz.timezone(cfg.timezone)
    hour, minute = (int(x) for x in cfg.digest_time.split(":"))
    today_local = datetime.now(tz).replace(hour=hour, minute=minute, second=0, microsecond=0)

    # Build the list of as_of timestamps oldest → newest so the dedup set grows
    # forward through time (later briefs exclude items used in earlier ones).
    days_back = sorted(range(days), reverse=True)   # [days-1, days-2, ..., 0]
    results: list[dict] = []
    used_ids: set[int] = set()

    for d in days_back:
        as_of_local = today_local - timedelta(days=d)
        if as_of_local > datetime.now(tz):
            continue
        as_of_utc = as_of_local.astimezone(timezone.utc)
        date_label = as_of_local.strftime("%A %d %B %Y")

        # Skip days that already have a brief — but still feed their item_ids
        # into the dedup set so newer days don't repeat already-published stories.
        if only_missing:
            with db.connect() as conn:
                if db.digest_exists_for_label(conn, date_label):
                    import json as _json
                    row = conn.execute(
                        "SELECT item_ids_json FROM digests WHERE date_label = ? "
                        "ORDER BY id DESC LIMIT 1", (date_label,),
                    ).fetchone()
                    try:
                        for x in (_json.loads(row["item_ids_json"]) or []):
                            if isinstance(x, int):
                                used_ids.add(x)
                    except Exception:
                        pass
                    log.info("backfill: %s already has a brief — skipping", date_label)
                    results.append({
                        "as_of": as_of_utc.isoformat(),
                        "digest_id": None,
                        "char_count": 0,
                        "date_label": date_label,
                        "items_used": 0,
                        "skipped": True,
                        "already_present": True,
                    })
                    continue

        log.info(
            "backfill: composing brief for %s (excluding %d already-used IDs)",
            date_label, len(used_ids),
        )
        try:
            digest_id, content = run_compose_digest(
                since_hours=24,
                min_relevance=min_relevance,
                max_items=25,
                as_of=as_of_utc,
                exclude_ids=used_ids,
                # Backfill controls dedup via the in-run set; don't double-up
                # by also pulling from the digests table (every digest we just
                # composed in this run is in there too).
                dedupe_against_recent_digests=False,
            )
        except Exception as exc:
            log.warning(
                "backfill: compose failed for %s: %s", as_of_local.date(), exc,
            )
            continue

        # Track this brief's item IDs so the next (newer) day's compose skips them.
        # digest_id == 0 means "no real brief composed" (empty fallback) — skip.
        ids_just_used: list[int] = []
        if digest_id:
            with db.connect() as conn:
                import json as _json
                row = conn.execute(
                    "SELECT item_ids_json FROM digests WHERE id = ?", (digest_id,),
                ).fetchone()
                try:
                    ids_just_used = _json.loads(row["item_ids_json"]) or []
                except Exception:
                    ids_just_used = []
        used_ids.update(int(x) for x in ids_just_used if isinstance(x, int))

        results.append({
            "as_of": as_of_utc.isoformat(),
            "digest_id": digest_id,
            "char_count": len(content),
            "date_label": as_of_local.strftime("%A %d %B %Y"),
            "items_used": len(ids_just_used),
            "skipped": digest_id == 0,
        })
    return results


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

        # Calculate true total ingested in the window
        total = db.count_reviewed_24h(conn, as_of=when or datetime.now(timezone.utc))

        items = _dedupe_by_simhash(items)
        items = items[:max_items]

        # Fetch prediction market signals scored against today's items
        market_signals: list[dict] = []
        try:
            from dalila.ingestors.prediction_markets import get_market_signals
            market_signals = get_market_signals(conn, digest_items=items)
        except Exception as exc:
            log.debug("market signals unavailable: %s", exc)

    html_str = render_digest(items, when=when, total_ingested=total,
                             market_signals=market_signals)
    return html_str, items



def run_publish_site(out_dir: "Path") -> dict:
    """Generate the static site at `out_dir`.

    Past digest pages (digests/YYYY-MM-DD.html) are immutable — existing
    files on disk are NEVER re-rendered, even if the template changes.
    Only today's digest is written/refreshed from the DB.

    The archive list is built by scanning whatever *.html files exist in
    digests/ on disk, so no DB query is needed for historical entries.

    Pages always regenerated on every call:
      index.html, archive.html, about.html, methodology.html,
      build.html, countries.html, markets.html
    """
    from pathlib import Path
    import json as _json

    from dalila.config import load_sources
    from dalila.html_digest import (
        render_about, render_archive, render_digest, render_index,
        render_methodology, render_markets, render_customize,
    )

    out_dir = Path(out_dir)
    digests_dir = out_dir / "digests"
    digests_dir.mkdir(parents=True, exist_ok=True)

    today_slug = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    latest_items: list[dict] | None = None
    latest_when: datetime | None = None
    latest_slug: str | None = None
    rendered_today = False
    index_html = None

    # ── Step 1: find today's digest + the most-recently-dated digest ────────
    # We pull all unique-per-date rows and sort by the actual date encoded in
    # date_label (not composed_at, which is always the backfill run date and
    # therefore meaningless for ordering purposes).
    #
    # Wrapped defensively: a `database is locked` here (common on the
    # resource-starved VM under WAL contention) must not abort the whole
    # publish. On failure we fall through with index_html=None and the archive
    # list below — built purely from on-disk digest files — still rebuilds
    # index.html, so the site keeps updating even when this DB read trips.
    try:
      with db.connect() as conn:
        rows = conn.execute(
            """SELECT d.id, d.composed_at, d.date_label, d.item_ids_json
               FROM digests d
               WHERE d.id IN (SELECT MAX(id) FROM digests GROUP BY date_label)
               ORDER BY d.id DESC""",
        ).fetchall()

        # Parse each row into (slug_dt, composed, items_fn) and sort by slug_dt
        # so the home page always reflects the most recent actual brief date.
        parsed: list[tuple] = []
        for row in rows:
            label = (row["date_label"] or "").strip()
            try:
                composed = datetime.fromisoformat(row["composed_at"].replace("Z", "+00:00"))
            except Exception:
                composed = datetime.now(timezone.utc)
            slug_dt = composed
            if label:
                try:
                    slug_dt = datetime.strptime(label, "%A %d %B %Y").replace(tzinfo=timezone.utc)
                except Exception:
                    pass
            try:
                item_ids = _json.loads(row["item_ids_json"]) or []
            except Exception:
                item_ids = []
            parsed.append((slug_dt, composed, item_ids))

        # Sort descending by actual brief date
        parsed.sort(key=lambda x: x[0], reverse=True)

        for slug_dt, composed, item_ids in parsed:
            if not item_ids:
                continue

            slug = slug_dt.strftime("%Y-%m-%d")
            items = _items_by_ids(conn, item_ids)
            if not items:
                continue

            # Capture the most-recent digest's items for index.html
            if latest_items is None:
                latest_items = items
                latest_when = slug_dt
                latest_slug = slug

            # Only write the file for today — all past files stay untouched.
            if slug == today_slug:
                items_deduped = _dedupe_by_simhash(items, threshold=16)
                total = db.count_reviewed_24h(conn, as_of=slug_dt) or len(items_deduped)
                html_str = render_digest(items_deduped, when=slug_dt, total_ingested=total)
                (digests_dir / f"{slug}.html").write_text(html_str, encoding="utf-8")
                rendered_today = True
                log.info("publish-site: rendered today's digest → %s", slug)

        # ── Step 2: index.html (needs conn for count query) ─────────────────
        if latest_items:
            total = db.count_reviewed_24h(conn, as_of=latest_when) or len(latest_items)
            index_html = render_index(latest_items, when=latest_when, total_ingested=total)
        else:
            index_html = None  # filled below after archive list is built
    except Exception:
        log.exception("publish-site: digest/index discovery failed; "
                      "falling back to on-disk archive for index.html")
        index_html = None

    # ── Step 3: build archive list entirely from files on disk ──────────────
    # Past digest HTML files are the source of truth; no DB query needed.
    all_digests: list[dict] = []
    for html_path in sorted(digests_dir.glob("*.html"), reverse=True):
        slug = html_path.stem
        try:
            dt = datetime.strptime(slug, "%Y-%m-%d")
            date_label = dt.strftime("%A %d %B %Y")
        except Exception:
            date_label = slug
        all_digests.append({"slug": slug, "date_label": date_label, "preview": ""})

    # If there are newer digest files on disk than what the DB returned (e.g.
    # the DB was re-initialised but rendered files survived), use the on-disk
    # file for index.html instead of the stale DB content.
    if all_digests and (latest_slug is None or all_digests[0]["slug"] > latest_slug):
        index_html = None  # force fallback below

    if index_html is None and all_digests:
        # Read the most recent on-disk digest and re-root its nav links so
        # they work from the site root instead of the digests/ subdirectory.
        latest_path = digests_dir / f"{all_digests[0]['slug']}.html"
        if latest_path.exists():
            raw = latest_path.read_text(encoding="utf-8")
            # Digest pages use link_prefix="../"; index.html lives at root.
            # Order matters: fix the home href first (../ → ./) then strip
            # the remaining ../ prefix from all other nav links.
            index_html = (
                raw
                .replace('href="../"', 'href="./"')
                .replace("href='../'", "href='./'")
                .replace('href="../', 'href="')
                .replace("href='../", "href='")
            )
            log.info(
                "publish-site: index.html sourced from on-disk %s (no DB items for this date)",
                all_digests[0]["slug"],
            )

    if index_html is None:
        index_html = render_archive(all_digests)

    # ── Step 4: write live root pages ───────────────────────────────────────
    (out_dir / "index.html").write_text(index_html, encoding="utf-8")
    (out_dir / "archive.html").write_text(render_archive(all_digests), encoding="utf-8")

    sources = load_sources()
    (out_dir / "about.html").write_text(render_about(sources), encoding="utf-8")

    try:
        (out_dir / "methodology.html").write_text(render_methodology(), encoding="utf-8")
    except Exception:
        log.exception("publish-site: methodology page generation failed")

    try:
        (out_dir / "build.html").write_text(render_customize(), encoding="utf-8")
    except Exception:
        log.exception("publish-site: build page generation failed")

    try:
        from dalila.config import load_countries
        from dalila.html_digest import render_countries

        cat = load_countries()
        window_days = 90
        timeline_days = 180
        with db.connect() as conn:
            counts = db.country_mention_counts(conn, since_hours=timeline_days * 24)
            items_by_country: dict[str, list[dict]] = {}
            cooccurrence: dict[str, dict[str, int]] = {}
            for iso in counts.keys():
                items_by_country[iso] = db.items_for_country(
                    conn, iso, since_hours=timeline_days * 24, limit=30,
                )
                cooccurrence[iso] = db.country_cooccurrence(
                    conn, iso, since_hours=timeline_days * 24,
                )
            timeline = db.country_timeline(conn, since_hours=timeline_days * 24)
        countries_html = render_countries(
            cat["countries"], cat["regions"], counts,
            items_by_country, cooccurrence, window_days=window_days,
            timeline=timeline,
        )
        (out_dir / "countries.html").write_text(countries_html, encoding="utf-8")
    except Exception:
        log.exception("publish-site: countries page generation failed")

    try:
        from dalila.ingestors.prediction_markets import get_market_signals
        with db.connect() as conn:
            markets_data = get_market_signals(conn, top_n=30)
        markets_html = render_markets(markets_data)
        (out_dir / "markets.html").write_text(markets_html, encoding="utf-8")
    except Exception:
        log.exception("publish-site: markets page generation failed")

    live_pages = [
        str(out_dir / "index.html"),
        str(out_dir / "archive.html"),
        str(out_dir / "countries.html"),
        str(out_dir / "markets.html"),
        str(out_dir / "methodology.html"),
        str(out_dir / "about.html"),
        str(out_dir / "build.html"),
    ]
    if rendered_today:
        live_pages.append(str(digests_dir / f"{today_slug}.html"))

    log.info(
        "publish-site: done — %d digest(s) on disk; today re-rendered=%s",
        len(all_digests), rendered_today,
    )
    return {
        "out_dir": str(out_dir),
        "digests": len(all_digests),
        "wrote": live_pages,
    }


def run_publish_backfilled_pages(out_dir: "Path") -> int:
    """Write digest HTML pages for any persisted brief whose page is missing.

    `run_publish_site` deliberately only (re)writes *today's* digest page and
    leaves all existing past pages untouched (the immutability invariant). That
    means a brief composed by a backfill run — for a past date — gets a row in
    the DB but never a page on disk, so it never shows up in the archive (which
    is built by scanning digests/*.html).

    This closes that gap: for every persisted digest (latest row per date_label)
    that has NO page on disk yet, it renders and writes one. Pages that already
    exist are never rewritten, so the immutability invariant still holds.

    Returns the number of new pages written.
    """
    from pathlib import Path
    import json as _json
    from dalila.html_digest import render_digest

    out_dir = Path(out_dir)
    digests_dir = out_dir / "digests"
    digests_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT d.id, d.composed_at, d.date_label, d.item_ids_json
               FROM digests d
               WHERE d.id IN (SELECT MAX(id) FROM digests GROUP BY date_label)
               ORDER BY d.id DESC""",
        ).fetchall()

        for row in rows:
            label = (row["date_label"] or "").strip()
            if not label:
                continue
            try:
                slug_dt = datetime.strptime(label, "%A %d %B %Y").replace(tzinfo=timezone.utc)
            except Exception:
                continue
            slug = slug_dt.strftime("%Y-%m-%d")
            page = digests_dir / f"{slug}.html"
            if page.exists():
                continue  # immutable — never rewrite an existing page

            try:
                item_ids = _json.loads(row["item_ids_json"]) or []
            except Exception:
                item_ids = []
            items = _items_by_ids(conn, [int(x) for x in item_ids if isinstance(x, int)])
            if not items:
                continue

            items_deduped = _dedupe_by_simhash(items, threshold=16)
            total = db.count_reviewed_24h(conn, as_of=slug_dt) or len(items_deduped)
            html_str = render_digest(items_deduped, when=slug_dt, total_ingested=total)
            page.write_text(html_str, encoding="utf-8")
            written += 1
            log.info("backfill-pages: wrote missing digest page → %s", slug)

    log.info("backfill-pages: wrote %d missing digest page(s)", written)
    return written


def _git_commit_and_push(repo, rel_paths: list, message: str) -> None:
    """Stage `rel_paths`, commit if anything changed, and push to origin/main.

    Pulls `--rebase --autostash` and retries once if the first push is rejected.
    The bot is not the only writer to origin/main — the sync_site cron and any
    manual pushes advance it too — so without a rebase-on-rejection the bot's
    market-signal and publish pushes pile up locally as non-fast-forward
    failures, only draining when the cron next rebases. That produced the
    bursty, irregular market-update cadence on the site.

    Best-effort: every failure is logged, none propagate. On a failed commit
    the staged paths are reset so the index doesn't stay dirty across calls.
    """
    import os
    import subprocess

    # Never let git block on an interactive credential / askpass prompt. On a
    # headless VM a missing-or-expired token turns `git push` into an indefinite
    # hang: the credential helper waits on /dev/tty, which stalls even the
    # subprocess timeout's pipe-drain cleanup. Forcing non-interactive mode makes
    # git fail fast and loudly (logged below) instead of freezing the publish.
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",  # git's own prompt (HTTPS username/password)
        "GCM_INTERACTIVE": "Never",  # Git Credential Manager, if installed
    }

    add_cmd = ["git", "add", "--"] + [str(p) for p in rel_paths]
    try:
        subprocess.run(add_cmd, cwd=repo, check=True, capture_output=True, text=True, timeout=30, env=env)
        staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo, timeout=15, env=env)
        if staged.returncode == 0:
            # Nothing new to stage. Do NOT return here: the local branch may
            # still be AHEAD of origin/main from an earlier run whose push
            # failed (e.g. the publish froze on a credential prompt *after*
            # committing). Falling through to the push loop drains that pending
            # commit — which is exactly the case that left the June backlog
            # committed locally but never pushed. A plain push when already
            # in sync is a harmless "Everything up-to-date".
            log.info("git push: no new changes in %s; pushing in case the branch is ahead", repo)
        else:
            subprocess.run(["git", "commit", "-m", message],
                           cwd=repo, check=True, capture_output=True, text=True, timeout=30, env=env)
    except Exception:
        log.exception("git commit failed in %s", repo)
        try:
            subprocess.run(["git", "reset", "HEAD", "--"] + [str(p) for p in rel_paths],
                           cwd=repo, capture_output=True, text=True, timeout=15, env=env)
        except Exception:
            pass
        return

    for attempt in (1, 2):
        push = subprocess.run(["git", "push", "origin", "main"],
                              cwd=repo, capture_output=True, text=True, timeout=120, env=env)
        if push.returncode == 0:
            log.info("git push: pushed to origin/main from %s", repo)
            return
        tail = ((push.stderr or "") + (push.stdout or "")).strip()[-300:]
        log.warning("git push attempt %d failed: %s", attempt, tail)
        if attempt == 1:
            pull = subprocess.run(
                ["git", "pull", "--rebase", "--autostash", "origin", "main"],
                cwd=repo, capture_output=True, text=True, timeout=120, env=env,
            )
            if pull.returncode != 0:
                ptail = ((pull.stderr or "") + (pull.stdout or "")).strip()[-300:]
                log.warning("git pull --rebase before retry failed: %s", ptail)
                return
    log.error("git push failed even after rebase retry in %s", repo)


def _find_repo_root(path: "Path"):
    """Walk up from `path` to find the enclosing git work-tree root, or None."""
    repo = path.resolve()
    for _ in range(8):
        if (repo / ".git").exists():
            return repo
        if repo.parent == repo:
            return None
        repo = repo.parent
    return None


def run_backfill_and_publish(
    days: int,
    *,
    only_missing: bool = True,
    min_relevance: float = 0.4,
    out_dir: "Path | None" = None,
    push: bool | None = None,
) -> dict:
    """Compose missing daily briefs over the last `days`, write their pages,
    regenerate the site, and optionally push.

    This is the one place that ties backfill → website together. Shared by the
    CLI (`backfill-digests --publish`) and the bot's one-shot startup backfill
    so both behave identically.

    `push` defaults to the `DALILA_SITE_GIT_PUSH=1` convention used elsewhere.
    Does NOT broadcast anything to Telegram — backfilled briefs are historical
    and only belong on the website/archive.
    """
    import os
    from pathlib import Path as _Path
    from datetime import datetime, timezone

    results = run_backfill_digests(days=days, min_relevance=min_relevance,
                                   only_missing=only_missing)

    if out_dir is None:
        out_dir = _Path(
            os.getenv("DALILA_SITE_OUT_DIR") or (_Path.home() / "dalila" / "docs")
        )
    out_dir = _Path(out_dir).resolve()

    pages_written = run_publish_backfilled_pages(out_dir)
    run_publish_site(out_dir)

    if push is None:
        push = os.getenv("DALILA_SITE_GIT_PUSH") == "1"

    pushed = False
    if push:
        repo = _find_repo_root(out_dir)
        if repo is None:
            log.warning("backfill-publish: no .git above %s; skipping push", out_dir)
        else:
            try:
                rel = out_dir.relative_to(repo)
            except ValueError:
                rel = out_dir
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            _git_commit_and_push(repo, [rel], f"Backfill briefs — {stamp}")
            pushed = True

    composed = [r for r in results if not r.get("skipped")]
    return {
        "results": results,
        "composed": len(composed),
        "pages_written": pages_written,
        "pushed": pushed,
        "out_dir": str(out_dir),
    }


def run_regenerate_markets_page(out_dir: "Path | None" = None) -> bool:
    """Regenerate markets.html and git-push it if DALILA_SITE_GIT_PUSH=1.

    Called after every market poll so the website reflects the latest
    probabilities and deltas without a full publish-site run.
    Returns True on success.
    """
    import os
    from pathlib import Path as _Path
    from datetime import datetime, timezone
    from dalila.html_digest import render_markets
    from dalila.ingestors.prediction_markets import get_market_signals

    if out_dir is None:
        out_dir = _Path(
            os.getenv("DALILA_SITE_OUT_DIR")
            or (_Path.home() / "dalila" / "docs")
        )
    out_dir = _Path(out_dir)
    if not out_dir.exists():
        return False

    try:
        with db.connect() as conn:
            markets_data = get_market_signals(conn, top_n=30)
        markets_html = render_markets(markets_data)
        markets_path = out_dir / "markets.html"
        markets_path.write_text(markets_html, encoding="utf-8")
        log.info("markets page regenerated (%d signals)", len(markets_data))
    except Exception:
        log.exception("run_regenerate_markets_page failed")
        return False

    if os.getenv("DALILA_SITE_GIT_PUSH") != "1":
        return True

    # Find repo root and push the single file
    repo = out_dir.resolve()
    for _ in range(8):
        if (repo / ".git").exists():
            break
        if repo.parent == repo:
            log.warning("markets push: no .git found above %s", out_dir)
            return True
        repo = repo.parent

    try:
        rel_path = markets_path.resolve().relative_to(repo)
    except ValueError:
        log.warning("markets push: %s not inside repo %s; skipping push", markets_path, repo)
        return True
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    _git_commit_and_push(repo, [rel_path], f"Update market signals — {stamp}")

    return True


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
