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
from dalila.pipeline import run_compose_digest, run_deep_dive, run_publish_site

log = logging.getLogger(__name__)


def _website_line() -> str:
    """Render the 'Web archive' line for /start and /help — only if a URL is set."""
    import os
    url = (os.getenv("DALILA_WEBSITE_URL") or "").strip()
    if url:
        return f"\n🌐 Web archive (digests, sources, about): {url}\n"
    return ""


def _help_text() -> str:
    return (
        "🌅 *Dalila* — your morning development brief\n"
        + _website_line()
        + "\nCommands:\n"
        "• `/start` — subscribe to the daily digest\n"
        "• `/stop` — unsubscribe\n"
        "• `/digest` — show the latest daily digest (read-only). `/digest fresh` to recompose now.\n"
        "• `/more <topic>` — deep-dive synthesis on a topic from the last 30 days\n"
        "• `/doctrine` — list tracked UAE doctrine positions\n"
        "• `/commitments` — recent development financial commitments\n"
        "• `/meetings` — recent UAE bilateral meetings\n"
        "• `/region [name]` — items aggregated by region\n"
        "• `/country <name>` — recent news mentioning a specific country\n"
        "• `link N` — get the source URL for item #N\n"
        "• `/help` — show this help\n\n"
        "Daily digest arrives ~06:30 GST. Powered by Strategic Foresight Engine (Log-Odds Volatility)."
    )


# Computed at handler-call time so DALILA_WEBSITE_URL changes don't require a restart.
HELP_TEXT = _help_text()


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
        "Subscribed. You'll get the morning digest at 06:30 GST.\n\n" + _help_text(),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    with db.connect() as conn:
        db.set_user_enabled(conn, update.effective_chat.id, enabled=False)
    await update.message.reply_text("Unsubscribed. /start to come back.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_help_text(), parse_mode=ParseMode.MARKDOWN)


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

    if s.get('commitments_window') or s.get('meetings_window') or s.get('graduation_signal_window'):
        lines.append("")
        lines.append("*Extracted in last 24h:*")
        if s.get('commitments_window'):
            lines.append(f"  · {s['commitments_window']} financial commitment(s)")
        if s.get('meetings_window'):
            lines.append(f"  · {s['meetings_window']} bilateral meeting(s)")
        if s.get('graduation_signal_window'):
            lines.append(f"  · {s['graduation_signal_window']} graduation/transition signal(s)")

    # Doctrine velocity — which topics moved the most in the last 30 days?
    with db.connect() as conn:
        velocity = db.doctrine_velocity_snapshot(conn, hours=720, limit=5)
    if velocity:
        lines.append("")
        lines.append("*Doctrine in motion (last 30d):*")
        for v in velocity:
            lines.append(f"  · {v['topic']}: {v['updates']} update(s), confidence {v['confidence']:.2f}")

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
        import asyncio
        digest_id, content = await asyncio.to_thread(run_compose_digest)
    except Exception as exc:
        log.exception("on-demand digest failed")
        await update.message.reply_text(f"Sorry, digest composition failed: {exc}")
        return
    await _send_long(update, content)


async def cmd_commitments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Recent development financial commitments extracted from news items."""
    if not update.message:
        return
    hours = 720  # last 30 days
    with db.connect() as conn:
        rows = db.recent_financial_commitments(conn, hours=hours, limit=20)
    if not rows:
        await update.message.reply_text(
            "No development financial commitments tracked in the last 30 days. "
            "These are extracted from news items mentioning concrete pledges, "
            "disbursements, MoUs, loans, or grants with an attached amount."
        )
        return
    lines = ["💰 *Development financial commitments — last 30 days*", ""]
    for r in rows:
        amt = _fmt_amount(r.get("amount"), r.get("currency"))
        when = (r.get("announced_at") or r.get("created_at") or "")[:10]
        ct = (r.get("commitment_type") or "").upper() or "—"
        fund = r.get("fund_name")
        source = r.get("source_entity") or r.get("source_country") or "(unspecified source)"
        beneficiary = (r.get("beneficiary_entity") or r.get("beneficiary_country")
                       or r.get("recipient") or "(unspecified beneficiary)")
        fund_tag = f" · via {fund}" if fund else ""
        lines.append(f"*{amt}* · {ct} · _{when}_")
        lines.append(f"  {source} → {beneficiary}{fund_tag}")
        if r.get("title"):
            title = r["title"][:80] + ("…" if len(r["title"]) > 80 else "")
            lines.append(f"  _{title}_")
        lines.append("")
    await _send_long(update, "\n".join(lines))


def _fmt_amount(amount, currency) -> str:
    if amount is None:
        return f"({currency or 'unspecified'})"
    cur = (currency or "").upper() or "?"
    # Heuristic: amounts ≥ 1000 likely already in millions/billions in the
    # source; smaller numbers display verbatim. We don't try to normalise
    # because the classifier prompt says "use the same unit the article uses".
    try:
        amt = float(amount)
        if amt >= 1000:
            return f"{cur} {amt:,.0f}"
        if amt >= 1:
            return f"{cur} {amt:,.1f}M"
        return f"{cur} {amt}"
    except (TypeError, ValueError):
        return f"{cur} {amount}"


async def cmd_meetings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Recent bilateral meetings / calls / visits between UAE and foreign leaders."""
    if not update.message:
        return
    with db.connect() as conn:
        rows = db.recent_bilateral_meetings(conn, hours=720, limit=20)
    if not rows:
        await update.message.reply_text(
            "No bilateral meetings tracked in the last 30 days. These are "
            "principal-level calls, visits, or summits between UAE leadership "
            "and foreign counterparts, extracted from news coverage."
        )
        return
    lines = ["🤝 *UAE bilateral meetings — last 30 days*", ""]
    for r in rows:
        when = (r.get("when_iso") or r.get("created_at") or "")[:10]
        mtype = (r.get("meeting_type") or "meeting").upper()
        uae = r.get("uae_principal") or "(UAE side)"
        foreign = r.get("foreign_principal") or "(foreign side)"
        country = r.get("foreign_country") or ""
        country_tag = f" ({country})" if country else ""
        n = r.get("mention_count") or 1
        count_tag = f"  ·  _{n} reports_" if n > 1 else ""
        lines.append(f"*{mtype}* · _{when}_  ·  {uae} ↔ {foreign}{country_tag}{count_tag}")
        if r.get("location"):
            lines.append(f"  📍 {r['location']}")
        topics = r.get("topics") or []
        if topics:
            lines.append(f"  Topics: {', '.join(topics[:5])}")
        lines.append("")
    await _send_long(update, "\n".join(lines))


async def cmd_country(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Recent news mentioning a specific country.

    Usage: `/country Sudan`, `/country USA`, `/country UAE`.

    Resolves common aliases (UK, U.S., DRC, etc.) to a canonical ISO code,
    then pulls items where the classifier tagged that country in
    country_focus. Defaults to a 14-day window.
    """
    if not update.message:
        return
    from dalila.config import load_countries, resolve_country

    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        await update.message.reply_text(
            "Usage: `/country <name>`. Examples:\n"
            "  `/country Sudan`\n"
            "  `/country USA`\n"
            "  `/country UAE`\n"
            "Aliases like USA, U.S., UK, DRC, KSA are recognised.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    iso = resolve_country(query)
    if not iso:
        await update.message.reply_text(
            f"Couldn't resolve `{query}` to a country. Try the full name "
            f"(e.g. `United States`) or the ISO-2 code (e.g. `US`).",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    cat = load_countries()
    spec = cat["countries"].get(iso, {})
    canonical = spec.get("name") or iso
    region_slug = spec.get("region") or ""
    region_label = (cat["regions"].get(region_slug) or {}).get("label", "")

    with db.connect() as conn:
        items = db.items_for_country(conn, iso, since_hours=14*24, limit=20)
        co = db.country_cooccurrence(conn, iso, since_hours=14*24)

    if not items:
        await update.message.reply_text(
            f"No items mentioning *{canonical}* in the last 14 days.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    lines = [f"🌍 *{canonical}*" + (f"  ·  _{region_label}_" if region_label else "")]
    lines.append(f"_{len(items)} item(s) in last 14 days_")
    if co:
        top_co = sorted(co.items(), key=lambda kv: kv[1], reverse=True)[:6]
        co_str = ", ".join(
            f"{cat['countries'].get(c, {}).get('name', c)} ({n})"
            for c, n in top_co
        )
        lines.append(f"_Often co-mentioned with: {co_str}_")
    lines.append("")
    for i, it in enumerate(items[:15], start=1):
        when = (it.get("published_at") or it.get("ingested_at") or "")[:10]
        title = (it.get("title") or "").strip()
        if len(title) > 140:
            title = title[:137] + "…"
        lines.append(f"{i}. {title}  _({when} · {it.get('source','')})_")

    await _send_long(update, "\n".join(lines))


async def cmd_region(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Aggregate recent items by region (per UAE Foreign Aid Policy Ch 5)."""
    if not update.message:
        return
    from dalila.config import load_regions, resolve_region

    query = " ".join(context.args).strip() if context.args else ""
    regions = load_regions()
    if not regions:
        await update.message.reply_text("Region mapping not configured (regions.yaml missing).")
        return

    if not query:
        # No arg → show the list of regions with item counts for the last 30 days.
        lines = ["🌍 *Regions tracked (last 30 days)*", ""]
        with db.connect() as conn:
            for slug, spec in regions.items():
                items = db.items_by_country_codes(conn, spec["countries"], since_hours=720, limit=200)
                top_country = ""
                if items:
                    counts: dict[str, int] = {}
                    for it in items:
                        for c in it.get("country_focus", []):
                            if c in spec["countries"]:
                                counts[c] = counts.get(c, 0) + 1
                    if counts:
                        top_country = " · top: " + ", ".join(
                            f"{c}({n})" for c, n in sorted(counts.items(), key=lambda kv: -kv[1])[:3]
                        )
                lines.append(f"*{spec['label']}* (`/{slug}`) — {len(items)} item(s){top_country}")
        lines.append("")
        lines.append("_Use_ `/region <name>` _for items in one region — e.g._ `/region africa`")
        await _send_long(update, "\n".join(lines))
        return

    resolved = resolve_region(query)
    if not resolved:
        names = ", ".join(f"`{s}`" for s in regions)
        await update.message.reply_text(
            f"Region `{query}` not recognised. Try one of: {names}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    slug, spec = resolved
    with db.connect() as conn:
        items = db.items_by_country_codes(conn, spec["countries"], since_hours=720, limit=20)
    if not items:
        await update.message.reply_text(
            f"No items in *{spec['label']}* in the last 30 days.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    lines = [f"🌍 *{spec['label']} — last 30 days*", ""]
    for it in items:
        countries = ",".join(it.get("country_focus") or [])
        sector = f"  [{it['policy_sector']}]" if it.get("policy_sector") else ""
        rel = it.get("uae_relevance") or 0
        title = (it.get("title") or "")[:90]
        lines.append(f"• `{countries}`{sector}  rel={rel:.2f}")
        lines.append(f"  {title}")
        if it.get("summary"):
            lines.append(f"  _{it['summary'][:140]}_")
        lines.append("")
    await _send_long(update, "\n".join(lines))


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
    """Telegram caps messages at ~4096 chars. Split on blank lines where possible.

    If a single paragraph exceeds `chunk` (e.g. a dense market block), it is
    hard-split at `chunk` boundaries so we never send a message Telegram would
    reject with MessageTooLong.
    """
    if len(text) <= chunk:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return
    parts: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        # Hard-split oversized single paragraphs before they enter the buffer.
        while len(para) > chunk:
            parts.append(para[:chunk])
            para = para[chunk:]
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
    ("start",       "Subscribe to the daily digest"),
    ("digest",      "Show the latest daily digest"),
    ("more",        "Deep-dive on a topic (e.g. /more Sudan)"),
    ("doctrine",    "Tracked UAE doctrine positions"),
    ("commitments", "Recent UAE financial commitments"),
    ("meetings",    "UAE bilateral meetings & calls"),
    ("region",      "Items aggregated by region"),
    ("help",        "Show help"),
    ("stop",        "Unsubscribe from the daily digest"),
]


async def cmd_publish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Regenerate the static site and push to GitHub. Operator-only, hidden command."""
    import asyncio, os, subprocess
    from pathlib import Path
    await update.message.reply_text("Publishing site…")

    # 1. Render
    try:
        out_dir = Path(os.getenv("DALILA_SITE_OUT_DIR") or (Path.home() / "dalila" / "docs"))
        result = await asyncio.to_thread(run_publish_site, out_dir)
        await update.message.reply_text(
            f"Site rendered — {result.get('digests', 0)} digest(s) on disk."
        )
    except Exception as exc:
        log.exception("publish: render failed")
        await update.message.reply_text(f"Render failed: {exc}")
        return

    # 2. Git push
    await update.message.reply_text("Pushing to GitHub…")
    try:
        repo = out_dir.resolve()
        for _ in range(8):
            if (repo / ".git").exists():
                break
            if repo.parent == repo:
                await update.message.reply_text("Could not find git repo above docs dir.")
                return
            repo = repo.parent
        rel_out = out_dir.resolve().relative_to(repo)

        def _push():
            subprocess.run(["git", "add", "--", str(rel_out)],
                           cwd=repo, check=True, capture_output=True, text=True, timeout=30)
            diff = subprocess.run(["git", "diff", "--cached", "--quiet"],
                                  cwd=repo, text=True, timeout=15)
            if diff.returncode == 0:
                return "No changes to push."
            from datetime import datetime, timezone
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            subprocess.run(["git", "commit", "-m", f"Publish site — {stamp}"],
                           cwd=repo, check=True, capture_output=True, text=True, timeout=30)
            subprocess.run(["git", "push", "origin", "main"],
                           cwd=repo, check=True, capture_output=True, text=True, timeout=120)
            return f"Pushed to GitHub ({stamp})."

        msg = await asyncio.to_thread(_push)
        await update.message.reply_text(msg)
    except subprocess.CalledProcessError as exc:
        tail = ((exc.stderr or "") + (exc.stdout or "")).strip()[-300:]
        await update.message.reply_text(f"Git push failed:\n{tail}")
    except Exception as exc:
        log.exception("publish: git push failed")
        await update.message.reply_text(f"Git push failed: {exc}")


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
    app.add_handler(CommandHandler("commitments", cmd_commitments))
    app.add_handler(CommandHandler("meetings", cmd_meetings))
    app.add_handler(CommandHandler("region", cmd_region))
    app.add_handler(CommandHandler("country", cmd_country))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("publish", cmd_publish))
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
