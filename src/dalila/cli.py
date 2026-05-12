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
    sub.add_parser("ingest", help="Run one ingest pass across all enabled sources")
    p_classify = sub.add_parser("classify", help="Classify pending items")
    p_classify.add_argument("--limit", type=int, default=100, help="Max items to classify in this run")
    p_digest = sub.add_parser("digest", help="Compose today's digest and print to stdout")
    p_digest.add_argument("--since-hours", type=int, default=24)
    p_digest.add_argument("--min-relevance", type=float, default=0.4)
    p_digest.add_argument("--max-items", type=int, default=25)
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
        result = run_classify(limit=args.limit)
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

    if args.cmd == "bot":
        return _cmd_bot()

    parser.error(f"unknown command {args.cmd!r}")
    return 2


def _cmd_check() -> int:
    cfg = get_config()
    print(f"Project root:       {cfg.root}")
    print(f"DB path:            {cfg.db_path}  (exists={cfg.db_path.exists()})")
    print(f"Timezone:           {cfg.timezone}")
    print(f"Digest time:        {cfg.digest_time}")
    print(f"Ingest interval:    every {cfg.ingest_interval_minutes} min")
    print(f"Telegram token:     {'set' if cfg.telegram_bot_token else 'MISSING — bot will not start'}")
    print(f"ACLED creds:        {'set' if (cfg.acled_api_key and cfg.acled_email) else 'unset (ACLED ingestion skipped)'}")
    ok, msg = check_cli_available()
    print(f"Claude CLI:         {'OK' if ok else 'FAIL'} — {msg}")
    print()
    if not ok:
        return 1
    return 0


def _cmd_bot() -> int:
    cfg = get_config()
    if not cfg.telegram_bot_token:
        print("ERROR: TELEGRAM_BOT_TOKEN not set. Copy .env.example to .env and fill it in.", file=sys.stderr)
        return 1
    ok, msg = check_cli_available()
    if not ok:
        print(f"ERROR: {msg}", file=sys.stderr)
        return 1

    db.init_db()

    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    import pytz
    from dalila import bot
    from dalila.scheduler import attach_jobs

    app = bot.build_application()
    scheduler = AsyncIOScheduler(timezone=pytz.timezone(cfg.timezone))
    attach_jobs(scheduler, app)

    async def runner() -> None:
        scheduler.start()
        await app.initialize()
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
