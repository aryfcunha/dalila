"""Command-line interface."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from dalila import db
from dalila.config import get_config
from dalila.llm import check_cli_available


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Quiet noisy libs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.INFO)
    logging.getLogger("telegram").setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dalila", description="Daily UAE-focused development news digest.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable DEBUG logging")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Create the SQLite DB and seed sources")
    sub.add_parser("check", help="Verify environment (claude CLI, telegram token, etc.)")
    sub.add_parser("status", help="DB and pipeline snapshot (queue, classified, top entities)")
    sub.add_parser("country-debug", help="Show top-mentioned countries from country_focus_json (diagnostic)")
    sub.add_parser("ingest", help="Run one ingest pass across all enabled sources")
    p_classify = sub.add_parser("classify", help="Classify pending items")
    p_classify.add_argument("--limit", type=int, default=100, help="Max items to classify in this run")
    p_classify.add_argument("--batch-size", type=int, default=25, help="Items per call (default 25, empirically tuned for Claude CLI; safe for DeepSeek too)")
    p_classify.add_argument("--backend", choices=("claude", "deepseek"), default="claude",
                            help="LLM backend (default 'claude'). Use 'deepseek' for cost-bounded backfill — requires DEEPSEEK_API_KEY env var.")
    p_classify.add_argument("--workers", type=int, default=1,
                            help="Parallel batches in flight (DeepSeek only; default 1). 4-6 is a good sweet spot — each call takes ~60s, so 5 workers ≈ 5x throughput.")
    p_digest = sub.add_parser("digest", help="Compose today's digest and print to stdout")
    p_digest.add_argument("--since-hours", type=int, default=24)
    p_digest.add_argument("--min-relevance", type=float, default=0.4)
    p_digest.add_argument("--max-items", type=int, default=25)
    p_html = sub.add_parser("html-digest", help="Render today's digest as a single HTML file (no LLM call)")
    p_html.add_argument("--since-hours", type=int, default=24)
    p_html.add_argument("--min-relevance", type=float, default=0.4)
    p_html.add_argument("--max-items", type=int, default=25)
    p_html.add_argument("--out", type=str, default="digest.html", help="Output path (default ./digest.html)")
    p_site = sub.add_parser("publish-site", help="Generate the full static site (index, archive, about, digests/) — ready for GitHub Pages")
    p_site.add_argument("--out", type=str, default="docs", help="Output directory (default ./docs, matches GitHub Pages source)")
    p_site.add_argument("--push", action="store_true",
                        help="After rendering, commit + push docs/ to origin/main (self-healing git, rebase-on-reject). Use to force an immediate site update outside the scheduler.")
    p_back = sub.add_parser("backfill-digests", help="Compose a brief for each of the past N days (one LLM call per day)")
    p_back.add_argument("--days", type=int, default=5, help="How many days back to backfill (default 5)")
    p_back.add_argument("--min-relevance", type=float, default=0.4)
    p_back.add_argument("--classify-first", action="store_true",
                        help="Run a classify pass over all unclassified items before composing (recommended for deep historical backfills).")
    p_back.add_argument("--backend", choices=("claude", "deepseek"), default="claude",
                        help="LLM backend for the classify pass when --classify-first is set (default 'claude'). Digests themselves always use Claude.")
    p_back.add_argument("--classify-limit", type=int, default=2000,
                        help="Max items to classify in the pre-pass when --classify-first is set (default 2000).")
    p_back.add_argument("--classify-workers", type=int, default=1,
                        help="Parallel batches in the classify pre-pass (DeepSeek only; default 1).")
    p_back.add_argument("--only-missing", action="store_true",
                        help="Skip days that already have a brief (no LLM call) — only compose the gaps. Use for retroactive backlog fills.")
    p_back.add_argument("--publish", action="store_true",
                        help="After composing, write each backfilled day's HTML page, regenerate index/archive, and (if DALILA_SITE_GIT_PUSH=1) commit + push docs/ so the new briefs appear on the website.")
    p_bf = sub.add_parser("backfill", help="Historical archive backfill via XML sitemaps + GDELT/ACLED date ranges (does NOT classify — run `dalila classify` after)")
    p_bf.add_argument("--since", required=True, help="ISO date YYYY-MM-DD (inclusive)")
    p_bf.add_argument("--until", default=None, help="ISO date YYYY-MM-DD (inclusive; default today)")
    p_bf.add_argument("--source", default=None,
                       help="Single source id: wam_en, the_national, gulf_news, gdelt_v2, or acled. Default: all five.")
    p_bf.add_argument("--no-body", action="store_true",
                       help="Skip per-article HTML fetch for sitemap sources (10x faster; classifier works off title-from-URL-slug + sitemap lastmod).")
    p_bf.add_argument("--concurrency", type=int, default=12,
                       help="Parallel article fetches per sitemap source (default 12).")
    p_bf.add_argument("--max-per-source", type=int, default=4000,
                       help="Cap candidate URLs per sitemap source (default 4000).")
    p_bf.add_argument("--gdelt-step", type=int, default=60,
                       help="GDELT slice cadence in minutes — 60=one per hour (default), 15=full coverage 4x/hr, 240=light sample. Lower = more data, slower.")
    p_doctrine = sub.add_parser("doctrine", help="Run the doctrine extraction pass over pending classified items")
    p_doctrine.add_argument("--limit", type=int, default=20, help="Max items to process this run")
    sub.add_parser("verify-sources", help="Probe every enabled source and report fetched count + sample title")
    sub.add_parser("set-name", help="Push the canonical bot name + descriptions to Telegram (one-shot)")
    sub.add_parser("run-pipeline", help="Run a full pipeline pass (ingest, classify, doctrine)")
    sub.add_parser("bot", help="Start the Telegram bot with the scheduler (long-running)")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if args.cmd == "init":
        db.init_db()
        cfg = get_config()
        print(f"Initialised DB at {cfg.db_path}")
        return 0

    if args.cmd == "check":
        return _cmd_check()

    if args.cmd == "status":
        return _cmd_status()

    if args.cmd == "country-debug":
        db.init_db()
        with db.connect() as conn:
            n_total = conn.execute("SELECT COUNT(*) AS n FROM items WHERE classified_at IS NOT NULL").fetchone()["n"]
            n_with_cf = conn.execute(
                "SELECT COUNT(*) AS n FROM items "
                "WHERE country_focus_json IS NOT NULL AND country_focus_json != '[]'"
            ).fetchone()["n"]
            print(f"items classified ever:                       {n_total}")
            print(f"items with country_focus_json populated:     {n_with_cf}")
            if n_with_cf == 0:
                print()
                print("→ ZERO items have country_focus_json populated.")
                print("  Either no items have been classified since migration 004,")
                print("  or the classifier isn't writing the field. Re-classify a")
                print("  few items: `dalila classify --limit 20`")
                return 0
            counts = db.country_mention_counts(conn, since_hours=30 * 24)
            top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:15]
            print()
            print(f"top countries (last 30d, {len(counts)} countries seen, {sum(counts.values())} total tags):")
            for iso, n in top:
                print(f"  {iso}  {n:>4d}")
        return 0

    if args.cmd == "ingest":
        from dalila.pipeline import run_ingest
        db.init_db()
        stats = run_ingest()
        for sid, s in stats.items():
            print(f"  {sid:20s}  fetched={s['fetched']:4d}  new={s['new']:4d}  passed={s['prefilter_passed']:4d}  err={s['error'] or '-'}")
        return 0

    if args.cmd == "classify":
        from dalila.pipeline import run_classify
        db.init_db()
        result = run_classify(limit=args.limit, batch_size=args.batch_size,
                              backend=args.backend, workers=args.workers)
        print(f"classify done: {result}")
        return 0

    if args.cmd == "digest":
        from dalila.pipeline import run_compose_digest
        db.init_db()
        digest_id, content = run_compose_digest(
            since_hours=args.since_hours,
            min_relevance=args.min_relevance,
            max_items=args.max_items,
        )
        print(f"--- Digest #{digest_id} ---\n")
        print(content)
        return 0

    if args.cmd == "html-digest":
        from pathlib import Path
        from dalila.pipeline import run_render_html_digest
        db.init_db()
        html_str, items = run_render_html_digest(
            since_hours=args.since_hours,
            min_relevance=args.min_relevance,
            max_items=args.max_items,
        )
        out = Path(args.out).resolve()
        out.write_text(html_str, encoding="utf-8")
        print(f"Wrote {out}  ({len(items)} items, {len(html_str):,} bytes)")
        return 0

    if args.cmd == "publish-site":
        from pathlib import Path
        from dalila.pipeline import run_publish_site
        db.init_db()
        out_dir = Path(args.out).resolve()
        result = run_publish_site(out_dir)
        print(f"Site updated — {result['digests']} digest(s) on disk, {len(result['wrote'])} page(s) written:")
        for p in result["wrote"]:
            print(f"  · {p}")
        if args.push:
            from datetime import datetime, timezone
            from dalila.pipeline import _find_repo_root, _git_commit_and_push
            repo = _find_repo_root(out_dir)
            if repo is None:
                print("  ! no git repo found above output dir — skipping push")
                return 1
            try:
                rel = out_dir.relative_to(repo)
            except ValueError:
                rel = out_dir
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            _git_commit_and_push(repo, [rel], f"Publish site — {stamp}")
            print(f"  · pushed {rel} to origin/main")
        return 0

    if args.cmd == "backfill-digests":
        import os
        from dalila.pipeline import run_backfill_digests, run_classify, run_backfill_and_publish
        db.init_db()
        if args.classify_first:
            print(f"pre-pass: classifying up to {args.classify_limit} pending items (backend={args.backend})…")
            cres = run_classify(limit=args.classify_limit, backend=args.backend,
                                workers=args.classify_workers)
            print(f"  → classified={cres.get('classified')} errors={cres.get('errors')} "
                  f"rate_limited={cres.get('rate_limited')}")

        if args.publish:
            summary = run_backfill_and_publish(
                days=args.days, min_relevance=args.min_relevance,
                only_missing=args.only_missing,
            )
            results = summary["results"]
        else:
            results = run_backfill_digests(days=args.days, min_relevance=args.min_relevance,
                                           only_missing=args.only_missing)

        composed = [r for r in results if not r.get("skipped")]
        print(f"Backfill over {len(results)} day(s) — {len(composed)} brief(s) composed:")
        for r in results:
            if r.get("already_present"):
                print(f"  · ---  {r['date_label']:>25s}  (already present, skipped)")
            elif r.get("skipped"):
                print(f"  · ---  {r['date_label']:>25s}  (no items above threshold)")
            else:
                print(f"  · #{r['digest_id']:>3d}  {r['date_label']:>25s}  {r['char_count']:>5d} chars")

        if args.publish:
            print(f"Published site: wrote {summary['pages_written']} backfilled digest page(s); "
                  f"index/archive regenerated.")
            if summary["pushed"]:
                print("Committed + pushed docs/ to origin/main.")
            elif os.getenv("DALILA_SITE_GIT_PUSH") != "1":
                print("Set DALILA_SITE_GIT_PUSH=1 to also commit + push docs/ automatically.")
        return 0

    if args.cmd == "backfill":
        from datetime import date
        from dalila.pipeline import run_backfill
        db.init_db()
        try:
            since = date.fromisoformat(args.since)
            until = date.fromisoformat(args.until) if args.until else None
        except ValueError as exc:
            print(f"ERROR: bad date format: {exc}", file=sys.stderr)
            return 2
        stats = run_backfill(
            since=since, until=until, source=args.source,
            fetch_body=not args.no_body,
            concurrency=args.concurrency,
            max_per_source=args.max_per_source,
            gdelt_step_minutes=args.gdelt_step,
        )
        print(f"\nBackfill complete (since={since}, until={until or 'today'}):")
        for sid, s in stats.items():
            print(f"  {sid:20s}  fetched={s['fetched']:5d}  new={s['new']:5d}  "
                  f"passed={s['prefilter_passed']:5d}  err={s['error'] or '-'}")
        print("\nNext step: classify the new items, e.g.")
        print("  dalila classify --backend deepseek --limit 5000")
        return 0

    if args.cmd == "doctrine":
        from dalila import doctrine as doctrine_mod
        db.init_db()
        result = doctrine_mod.run_pass(limit=args.limit)
        print(f"doctrine pass: {result}")
        return 0

    if args.cmd == "verify-sources":
        return _cmd_verify_sources()

    if args.cmd == "set-name":
        return _cmd_set_name()

    if args.cmd == "run-pipeline":
        return _cmd_run_pipeline()

    if args.cmd == "bot":
        return _cmd_bot()

    parser.error(f"unknown command {args.cmd!r}")
    return 2


def _cmd_status() -> int:
    db.init_db()
    with db.connect() as conn:
        s = db.status_snapshot(conn, hours=24)
    print(f"items in DB:           {s['items_total']:>5d}")
    print(f"pending classify:      {s['items_pending']:>5d}")
    print(f"classified (24h):      {s['items_classified_window']:>5d}"
          + (f"  (errors: {s['classifier_errors_window']})" if s['classifier_errors_window'] else ""))
    print(f"subscribed users:      {s['enabled_users']:>5d}")
    print(f"llm calls today:       {s['llm_calls_today']:>5d}"
          + (f"  (avg {(s['llm_avg_ms_today'] or 0)/1000:.1f}s/call)" if s['llm_calls_today'] else ""))
    print(f"last digest composed:  {s['last_digest_at'] or '-'}")
    if s['top_categories']:
        print("\ntop categories (24h):")
        for cat, n in s['top_categories']:
            print(f"  {cat:30s}  n={n}")
    if s['top_entities']:
        print("\ntop entities (24h):")
        for name, n in s['top_entities']:
            print(f"  {name:30s}  n={n}")
    return 0


def _cmd_check() -> int:
    cfg = get_config()
    print(f"Project root:       {cfg.root}")
    print(f"DB path:            {cfg.db_path}  (exists={cfg.db_path.exists()})")
    print(f"Timezone:           {cfg.timezone}")
    print(f"Digest time:        {cfg.digest_time}")
    print(f"Ingest interval:    every {cfg.ingest_interval_minutes} min")
    print(f"Telegram token:     {'set' if cfg.telegram_bot_token else 'MISSING — bot will not start'}")
    print(f"ACLED creds:        {'set (oauth)' if (cfg.acled_username and cfg.acled_password) else 'unset (ACLED ingestion skipped — set ACLED_USERNAME + ACLED_PASSWORD)'}")
    print(f"ACAPS creds:        {'set' if cfg.acaps_api_key else 'unset (INFORM ingestion skipped — set ACAPS_API_KEY)'}")
    ok, msg = check_cli_available()
    print(f"Claude CLI:         {'OK' if ok else 'FAIL'} — {msg}")

    import os as _os
    from dalila.llm import _auto_fallback_enabled, _fallback_cooldown_seconds
    ds_key = bool(_os.getenv("DEEPSEEK_API_KEY"))
    if _os.getenv("DALILA_LLM_BACKEND", "").strip().lower() == "deepseek" and ds_key:
        print("LLM backend:        DeepSeek (forced via DALILA_LLM_BACKEND=deepseek)")
    elif _auto_fallback_enabled():
        cd = _fallback_cooldown_seconds() // 60
        print(f"DeepSeek fallback:  ON — auto-routes on Claude quota/rate-limit (cooldown {cd}m)")
    elif ds_key:
        print("DeepSeek fallback:  OFF (key set but DALILA_DEEPSEEK_FALLBACK=0) — Claude-only")
    else:
        print("DeepSeek fallback:  unavailable (set DEEPSEEK_API_KEY to enable live redundancy)")
    print()

    # The CLI being down is not fatal when a DeepSeek path can serve the live
    # calls — the bot can still produce briefs.
    if not ok and not (ds_key and (_auto_fallback_enabled()
                                   or _os.getenv("DALILA_LLM_BACKEND", "").strip().lower() == "deepseek")):
        return 1
    return 0


def _cmd_set_name() -> int:
    """Push the canonical bot name + descriptions to Telegram. Idempotent."""
    cfg = get_config()
    if not cfg.telegram_bot_token:
        print("ERROR: TELEGRAM_BOT_TOKEN not set.", file=sys.stderr)
        return 1
    from dalila import bot as bot_mod

    async def runner() -> int:
        app = bot_mod.build_application()
        await _initialize_with_retry(app)
        await bot_mod.sync_bot(app)
        print(f"Synced: name={bot_mod.BOT_DISPLAY_NAME!r}; "
              f"menu={len(bot_mod.USER_MENU_COMMANDS)} commands")
        await app.shutdown()
        return 0

    return asyncio.run(runner())


def _cmd_run_pipeline() -> int:
    """Run a full pipeline pass (ingest, classify, doctrine)."""
    from dalila import doctrine as doctrine_mod
    from dalila.pipeline import run_classify, run_ingest
    db.init_db()

    print("Pipeline: Running ingest...")
    stats = run_ingest()
    for sid, s in stats.items():
        if s["new"] > 0 or s["error"]:
            print(f"  {sid:20s}  fetched={s['fetched']:4d}  new={s['new']:4d}  passed={s['prefilter_passed']:4d}  err={s['error'] or '-'}")

    print("Pipeline: Running classify...")
    cres = run_classify(limit=100)
    print(f"  → {cres}")

    print("Pipeline: Running doctrine extraction...")
    dres = doctrine_mod.run_pass(limit=20)
    print(f"  → {dres}")

    print("Pipeline complete.")
    return 0


def _cmd_verify_sources() -> int:
    """Probe each enabled source once; print fetched count, error (if any), and a sample title.

    Detects three regression classes without paying classifier cost:
      - Dead URLs (Reuters-style retirements, moved feeds)
      - Scraper selector rot (matched zero cards on a previously-working page)
      - Blank fetches (parser returns 0 items but no error — usually JS-rendered)
    """
    from dalila.ingestors.base import iter_enabled_sources, ingest_source

    rows: list[tuple[str, str, int, str, str]] = []
    for src in iter_enabled_sources():
        sid = src["id"]
        kind = src.get("kind", "?")
        sample = ""
        err = "-"
        n = 0
        try:
            items = ingest_source(src)
            n = len(items)
            if items:
                sample = (items[0].title or "")[:60]
            else:
                err = "0 items (selector rot? JS-rendered? feed empty?)" if kind == "scrape" else "0 items"
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"[:80]
        rows.append((sid, kind, n, err, sample))

    print(f"{'id':22s} {'kind':6s} {'n':>4s}  {'status / sample':<60s}")
    print("-" * 100)
    for sid, kind, n, err, sample in rows:
        status = sample if n > 0 else err
        marker = "OK " if n > 0 else "!! "
        print(f"{marker}{sid:20s} {kind:6s} {n:>4d}  {status}")
    healthy = sum(1 for _, _, n, _, _ in rows if n > 0)
    print("-" * 100)
    print(f"{healthy}/{len(rows)} sources returned items")
    return 0 if healthy == len(rows) else 1


async def _initialize_with_retry(app, attempts: int = 5, base_delay: float = 2.0) -> None:
    """Retry app.initialize() on transient network errors.

    The first call hits Telegram's `getMe` endpoint; on flaky or high-latency
    links it can time out once and then succeed. We back off exponentially
    (2s, 4s, 8s, 16s, 32s) before giving up so the bot survives brief outages
    instead of crashing the long-running process.
    """
    from telegram.error import NetworkError, TimedOut

    log = logging.getLogger("dalila.cli")
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            await app.initialize()
            return
        except (TimedOut, NetworkError) as exc:
            last_exc = exc
            delay = base_delay * (2 ** i)
            log.warning(
                "telegram initialize failed (%s); retrying in %.0fs (%d/%d)",
                exc.__class__.__name__, delay, i + 1, attempts,
            )
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


def _cmd_bot() -> int:
    cfg = get_config()
    if not cfg.telegram_bot_token:
        print("ERROR: TELEGRAM_BOT_TOKEN not set. Copy .env.example to .env and fill it in.", file=sys.stderr)
        return 1
    log = logging.getLogger(__name__)
    ok, msg = check_cli_available()
    if not ok:
        # Do NOT hard-exit here. The bot runs under systemd Restart=always, so
        # returning non-zero on a failed/slow CLI probe (e.g. a wedged or slow
        # `claude --version` on a resource-starved VM) crash-loops the service
        # every RestartSec — which starves the box further and means the
        # startup catch-up + boot-backfill jobs never get to run. Degrade
        # gracefully instead: log loudly and bring the scheduler up anyway.
        # Ingest and market polling don't need the CLI; the classify/digest/
        # doctrine jobs will fail and back off until `claude` recovers, then
        # self-heal. (A missing TELEGRAM_BOT_TOKEN above stays fatal — without
        # it the bot genuinely cannot do anything.)
        log.warning(
            "claude CLI not usable at startup (%s) — starting anyway; "
            "LLM-backed jobs will back off until it recovers.", msg,
        )
    else:
        log.info("claude CLI check: %s", msg)

    db.init_db()

    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    import pytz
    from dalila import bot
    from dalila.scheduler import attach_jobs

    app = bot.build_application()
    # job_defaults make every job resilient to process restarts (the bot runs
    # under systemd Restart=always). Without these, APScheduler's default
    # misfire_grace_time of 1s means any job whose fire time is missed by more
    # than a second during a restart is silently skipped — which is how daily
    # briefs went missing. coalesce collapses a backlog of missed interval
    # ticks into a single catch-up run rather than a thundering burst.
    scheduler = AsyncIOScheduler(
        timezone=pytz.timezone(cfg.timezone),
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 300,
        },
    )
    attach_jobs(scheduler, app)

    async def runner() -> None:
        scheduler.start()
        await _initialize_with_retry(app)
        await bot.sync_bot(app)
        await app.start()
        await app.updater.start_polling()
        print(f"Bot running. Send /start to your bot in Telegram. Ctrl-C to stop.")
        try:
            # Hold open until cancelled
            await asyncio.Event().wait()
        finally:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
            scheduler.shutdown()

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        print("Bot stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
