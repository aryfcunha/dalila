"""Telegram bot — delivery + inbound commands."""

from __future__ import annotations

import logging

from telegram import BotCommand, BotCommandScopeAllPrivateChats, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from dalila import db
from dalila.config import get_config
from dalila.pipeline import run_compose_digest, run_deep_dive

log = logging.getLogger(__name__)


HELP_TEXT = (
    "🌅 *Dalila* — your morning development brief\n\n"
    "Commands:\n"
    "• `/start` — subscribe to the daily digest\n"
    "• `/stop` — unsubscribe\n"
    "• `/digest` — show the latest daily digest (read-only). `/digest fresh` to recompose now.\n"
    "• `/more <topic>` — deep-dive synthesis on a topic from the last 30 days\n"
    "• `/doctrine` — list tracked UAE doctrine positions, or `/doctrine <topic>` for evolution log\n"
    "• `link N` — get the source URL for item #N from the latest digest\n"
    "• `/help` — show this help\n\n"
    "Daily digest arrives ~06:30 GST. Multi-user: every chat that runs `/start` gets its own copy."
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    with db.connect() as conn:
        db.upsert_user(
            conn,
            chat_id=update.effective_chat.id,
            username=update.effective_user.username,
            first_name=update.effective_user.first_name,
        )
    await update.message.reply_text(
        "Subscribed. You'll get the morning digest at 06:30 GST.\n\n" + HELP_TEXT,
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    with db.connect() as conn:
        db.set_user_enabled(conn, update.effective_chat.id, enabled=False)
    await update.message.reply_text("Unsubscribed. /start to come back.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pipeline state snapshot — what's classified, top entities, etc."""
    with db.connect() as conn:
        s = db.status_snapshot(conn, hours=24)

    lines = [
        "📊 *Dalila status*",
        "",
        f"*Items in DB:* {s['items_total']:,}",
        f"*Pending classification:* {s['items_pending']}",
        f"*Classified in last 24h:* {s['items_classified_window']}"
        + (f"  (errors: {s['classifier_errors_window']})" if s['classifier_errors_window'] else ""),
        f"*Subscribed chats:* {s['enabled_users']}",
        "",
    ]

    if s['llm_calls_today']:
        avg = s['llm_avg_ms_today'] or 0
        lines.append(f"*LLM calls today:* {s['llm_calls_today']}  (avg {avg/1000:.1f}s/call)")
    if s['last_digest_at']:
        lines.append(f"*Last digest composed:* {s['last_digest_at'][:19].replace('T', ' ')} UTC")

    if s['top_categories']:
        lines.append("")
        lines.append("*Top categories (last 24h):*")
        for cat, n in s['top_categories']:
            label = cat.replace('_', ' ')
            lines.append(f"  · {label}: {n}")

    if s['top_entities']:
        lines.append("")
        lines.append("*Most-mentioned entities (last 24h):*")
        for name, n in s['top_entities']:
            lines.append(f"  · {name}: {n}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return the latest persisted daily digest.

    By design we only have one daily digest per cycle (composed at 06:30 GST
    by the scheduler). On-demand recomposition is gated behind `/digest fresh`
    so casual `/digest` calls don't burn LLM cost re-running the editor.
    """
    if not update.message:
        return
    force_fresh = bool(context.args and context.args[0].lower() in ("fresh", "now", "--fresh"))

    if not force_fresh:
        with db.connect() as conn:
            latest = db.latest_digest(conn)
        if latest:
            header = f"_(latest digest, composed {latest['composed_at'][:16].replace('T', ' ')} UTC)_\n\n"
            await _send_long(update, header + latest["content"])
            return
        await update.message.reply_text(
            "No digest composed yet today. Use `/digest fresh` to compose one now "
            "(takes ~30 s and uses one LLM call), or wait for the scheduled 06:30 GST run.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await update.message.reply_text("Composing fresh digest — this takes a minute…")
    try:
        digest_id, content = run_compose_digest()
    except Exception as exc:
        log.exception("on-demand digest failed")
        await update.message.reply_text(f"Sorry, digest composition failed: {exc}")
        return
    await _send_long(update, content)


async def cmd_doctrine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List tracked doctrine positions, or show one topic's evolution log."""
    if not update.message:
        return
    topic_query = " ".join(context.args).strip().lower().replace(" ", "-") if context.args else ""

    with db.connect() as conn:
        facts = db.list_doctrine_facts(conn)

    if not facts:
        await update.message.reply_text(
            "No doctrine facts recorded yet. UAE leadership items take time to "
            "accumulate; check back after a few days of ingestion."
        )
        return

    if not topic_query:
        lines = ["🇦🇪 *UAE doctrine — tracked positions*", ""]
        for f in facts[:20]:
            conf_bar = "▓" * int(round(f["confidence"] * 5)) + "░" * (5 - int(round(f["confidence"] * 5)))
            lines.append(f"*{f['topic']}*  `{conf_bar}` {f['confidence']:.2f}")
            lines.append(f"  {f['position_summary']}")
            lines.append("")
        lines.append("_Use_ `/doctrine <topic>` _for the evolution log of one topic._")
        await _send_long(update, "\n".join(lines))
        return

    # Topic-specific view
    matches = [f for f in facts if topic_query in f["topic"]]
    if not matches:
        topics = ", ".join(f["topic"] for f in facts[:10])
        await update.message.reply_text(
            f"No doctrine topic matches `{topic_query}`. Tracked: {topics}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    f = matches[0]
    lines = [
        f"🇦🇪 *{f['topic']}* — confidence {f['confidence']:.2f}",
        "",
        f"*Position:* {f['position_summary']}",
    ]
    if f["nuance"]:
        lines.append(f"*Nuance:* {f['nuance']}")
    lines.append(f"*First stated:* {f['first_stated_at'][:10]}")
    lines.append(f"*Last confirmed:* {f['last_confirmed_at'][:10]}")
    lines.append("")
    lines.append("*Evolution log:*")
    for entry in (f["evolution_log"] or [])[-10:]:
        at = (entry.get("at") or "")[:10]
        rel = entry.get("relation", "?")
        summary = entry.get("summary", "")
        lines.append(f"  • _{at}_ ({rel}): {summary}")
    await _send_long(update, "\n".join(lines))


async def cmd_more(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deep-dive: /more <topic>. Pulls last-30d items, sends to Haiku for synthesis."""
    if not update.message:
        return
    topic = " ".join(context.args).strip() if context.args else ""
    # Strip a leading "on " — natural-language phrasing is "/more on Sudan".
    if topic.lower().startswith("on "):
        topic = topic[3:].strip()
    if not topic:
        await update.message.reply_text(
            "Usage: `/more <topic>` — e.g. `/more Sudan`, `/more climate finance`, `/more ERC`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    await update.message.reply_text(f"Researching *{topic}*… (≈30–60 s)", parse_mode=ParseMode.MARKDOWN)
    try:
        text, item_ids = run_deep_dive(topic)
    except Exception as exc:
        log.exception("/more failed for topic=%r", topic)
        await update.message.reply_text(f"Sorry, deep-dive failed: {exc}")
        return

    # Append a short source-list footer with clickable URLs for the items the
    # deep dive cited. We can't easily re-bind `link N` to deep-dive items
    # (digests table is single-stream), so inline the URLs here.
    if item_ids:
        with db.connect() as conn:
            urls: list[tuple[int, str]] = []
            for n, iid in enumerate(item_ids, start=1):
                url = db.get_url_for_item(conn, iid)
                if url:
                    urls.append((n, url))
        if urls:
            footer = "\n\n*Sources:*\n" + "\n".join(f"#{n}: {u}" for n, u in urls[:10])
            text = text + footer

    await _send_long(update, text)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle non-slash messages: 'link N' for now."""
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip().lower()

    # `link 3` or `source 3`
    if text.startswith(("link ", "source ")):
        try:
            n = int(text.split()[1])
        except (ValueError, IndexError):
            await update.message.reply_text("Usage: `link N` where N is the item number from the digest.")
            return
        await _reply_with_link(update, n)
        return

    # `more on Sudan` / `more Sudan` — natural-language form of /more
    if text.startswith("more "):
        topic = update.message.text.strip()[5:].strip()
        if topic.lower().startswith("on "):
            topic = topic[3:].strip()
        if topic:
            context.args = topic.split()
            await cmd_more(update, context)
            return

    await update.message.reply_text("Try /help for commands.")


async def _reply_with_link(update: Update, n: int) -> None:
    """Resolve a digest #N to a URL using the most recent digest's item_ids list."""
    import json
    with db.connect() as conn:
        row = conn.execute(
            "SELECT item_ids_json FROM digests ORDER BY composed_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            await update.message.reply_text("No digest on record yet.")
            return
        item_ids: list[int] = json.loads(row["item_ids_json"])
        if n < 1 or n > len(item_ids):
            await update.message.reply_text(f"Item #{n} is out of range (1–{len(item_ids)}).")
            return
        url = db.get_url_for_item(conn, item_ids[n - 1])
    if not url:
        await update.message.reply_text(f"Item #{n} has no source URL on record.")
        return
    await update.message.reply_text(f"#{n}: {url}")


async def _send_long(update: Update, text: str, chunk: int = 3800) -> None:
    """Telegram caps messages at ~4096 chars. Split on blank lines where possible."""
    if len(text) <= chunk:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return
    parts: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        if len(buf) + len(para) + 2 > chunk:
            if buf:
                parts.append(buf)
            buf = para
        else:
            buf = (buf + "\n\n" + para).strip()
    if buf:
        parts.append(buf)
    for part in parts:
        await update.message.reply_text(part, parse_mode=ParseMode.MARKDOWN)


# Canonical bot identity. Telegram allows up to 64 chars on name, 512 on
# description. The description is shown in the chat's profile pane; the
# short description is shown in the bot list.
BOT_DISPLAY_NAME = "Dalila | دليلة"
BOT_SHORT_DESCRIPTION = "Daily UAE-lens brief on humanitarian, development & philanthropy news."
BOT_DESCRIPTION = (
    "Dalila (دليلة, Arabic for 'guide') is a personal AI agent that delivers "
    "a curated daily brief on the global humanitarian, development, and "
    "philanthropy ecosystem — with a sharp focus on the UAE's role. "
    "Send /start to subscribe, /help for the command list."
)


# Commands that show up in the Telegram `/` autocomplete menu. `/status` is
# deliberately omitted — it's an operator/diagnostic command, kept functional
# but hidden from the user-facing menu and /help. Add new user commands here
# when they become production-ready.
USER_MENU_COMMANDS: list[tuple[str, str]] = [
    ("start",    "Subscribe to the daily digest"),
    ("digest",   "Show the latest daily digest"),
    ("more",     "Deep-dive on a topic (e.g. /more Sudan)"),
    ("doctrine", "Tracked UAE doctrine positions"),
    ("help",     "Show help"),
    ("stop",     "Unsubscribe from the daily digest"),
]


async def sync_bot_commands(app: Application) -> None:
    """Push the user-visible command menu to Telegram.

    The menu drives the `/` autocomplete popup in chats. Set per-scope
    (AllPrivateChats) so it only appears in 1-on-1 DMs, which is the only
    place Dalila is designed to run. Idempotent — Telegram dedupes on the
    server side anyway, but we still read+diff to keep logs quiet.
    """
    try:
        wanted = [BotCommand(name, desc) for name, desc in USER_MENU_COMMANDS]
        current = await app.bot.get_my_commands(scope=BotCommandScopeAllPrivateChats())
        current_tuples = [(c.command, c.description) for c in current]
        if current_tuples != USER_MENU_COMMANDS:
            await app.bot.set_my_commands(wanted, scope=BotCommandScopeAllPrivateChats())
            log.info("bot commands menu updated (%d entries)", len(wanted))
    except Exception as exc:
        log.warning("failed to sync bot commands: %s", exc)


async def sync_bot(app: Application) -> None:
    """Run all server-side sync steps (identity + commands menu). One call site."""
    await sync_bot_identity(app)
    await sync_bot_commands(app)


async def sync_bot_identity(app: Application) -> None:
    """Idempotently push the bot's display name + descriptions to Telegram.

    Telegram only stores one name+description per bot, so this is a global
    setting (visible to every user). It does NOT change the @username.
    Calls are cheap (one API call each) and Telegram is fine with re-setting
    the same values — we read first and skip if identical to avoid noise.
    """
    try:
        current = await app.bot.get_my_name()
        if (current.name or "") != BOT_DISPLAY_NAME:
            await app.bot.set_my_name(BOT_DISPLAY_NAME)
            log.info("bot name set to %r", BOT_DISPLAY_NAME)
        current_short = await app.bot.get_my_short_description()
        if (current_short.short_description or "") != BOT_SHORT_DESCRIPTION:
            await app.bot.set_my_short_description(BOT_SHORT_DESCRIPTION)
            log.info("bot short description updated")
        current_desc = await app.bot.get_my_description()
        if (current_desc.description or "") != BOT_DESCRIPTION:
            await app.bot.set_my_description(BOT_DESCRIPTION)
            log.info("bot description updated")
    except Exception as exc:
        # Non-fatal: identity sync failure shouldn't prevent the bot from running.
        log.warning("failed to sync bot identity: %s", exc)


def build_application() -> Application:
    cfg = get_config()
    if not cfg.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set; cannot start bot")
    # Default HTTPX timeouts (5s connect / 5s read) are too tight on slow or
    # latency-spiky networks — we saw `telegram.error.TimedOut` at startup on
    # the very first `get_me()` call. Bump generously; these only apply per
    # request and don't affect long-poll behaviour (set separately below).
    app = (
        ApplicationBuilder()
        .token(cfg.telegram_bot_token)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(10.0)
        .get_updates_connect_timeout(30.0)
        .get_updates_read_timeout(40.0)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("more", cmd_more))
    app.add_handler(CommandHandler("doctrine", cmd_doctrine))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


async def broadcast_digest(app: Application, content: str, digest_id: int) -> None:
    """Send a digest to every enabled user."""
    with db.connect() as conn:
        users = db.enabled_users(conn)

    for user in users:
        chat_id = user["chat_id"]
        try:
            # Split + send. We can't use update.message.reply_text from here;
            # call bot.send_message directly.
            for chunk in _chunk_long(content):
                await app.bot.send_message(chat_id=chat_id, text=chunk, parse_mode=ParseMode.MARKDOWN)
            with db.connect() as conn:
                db.record_delivery(conn, digest_id, chat_id, success=True, error=None)
            log.info("digest delivered to chat %s", chat_id)
        except Exception as exc:
            log.warning("digest delivery failed for chat %s: %s", chat_id, exc)
            with db.connect() as conn:
                db.record_delivery(conn, digest_id, chat_id, success=False, error=str(exc)[:500])


def _chunk_long(text: str, chunk: int = 3800) -> list[str]:
    if len(text) <= chunk:
        return [text]
    parts: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        if len(buf) + len(para) + 2 > chunk:
            if buf:
                parts.append(buf)
            buf = para
        else:
            buf = (buf + "\n\n" + para).strip()
    if buf:
        parts.append(buf)
    return parts
