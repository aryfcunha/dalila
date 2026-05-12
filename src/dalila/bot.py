"""Telegram bot — delivery + inbound commands."""

from __future__ import annotations

import logging

from telegram import Update
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
from dalila.pipeline import run_compose_digest

log = logging.getLogger(__name__)


HELP_TEXT = (
    "🌅 *Dalila* — your morning development brief\n\n"
    "Commands:\n"
    "• `/start` — subscribe to the daily digest\n"
    "• `/stop` — unsubscribe\n"
    "• `/digest` — get today's digest now\n"
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


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """On-demand digest. Recomposes from the last 24h."""
    await update.message.reply_text("Composing digest — this takes a minute…")
    try:
        digest_id, content = run_compose_digest()
    except Exception as exc:
        log.exception("on-demand digest failed")
        await update.message.reply_text(f"Sorry, digest composition failed: {exc}")
        return
    await _send_long(update, content)


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


def build_application() -> Application:
    cfg = get_config()
    if not cfg.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set; cannot start bot")
    app = ApplicationBuilder().token(cfg.telegram_bot_token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("digest", cmd_digest))
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
