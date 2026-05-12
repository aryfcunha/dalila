"""Editor — Haiku 4.5 via the `claude` CLI. Composes the daily digest.

Originally specced as Sonnet 4.6, but MVP runs on a single Claude Code
subscription that's also used for development. Using Haiku for both
classifier and editor keeps to one model (one cache, one rate-limit pool)
and is cheaper to iterate on. Flip back to Sonnet by changing the model=
argument in compose_digest() below — the prompt format works on either.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

import pytz

from dalila import llm
from dalila.config import get_config, load_prompt

log = logging.getLogger(__name__)


def compose_digest(items: list[dict], *, when: datetime | None = None) -> tuple[str, list[int]]:
    """Compose a digest from classified items. Returns (markdown, [item_ids]).

    If fewer than 3 items, returns a short fallback message.
    """
    cfg = get_config()
    tz = pytz.timezone(cfg.timezone)
    now = (when or datetime.now(tz)).astimezone(tz)
    # Cross-platform date label: build the day-of-month as an int to avoid
    # %-d (Unix-only) / %#d (Windows-only) format-string portability issues.
    date_label = f"{now.strftime('%A')} {now.day} {now.strftime('%B %Y')}"

    if len(items) < 3:
        msg = (
            f"🌅 *Dalila — {date_label}*\n\n"
            f"Quiet news cycle. {len(items)} items above threshold. Tomorrow."
        )
        return msg, [i["id"] for i in items]

    # Assign sequential #N numbers; the editor will reference them in the digest.
    numbered = []
    for n, it in enumerate(items, start=1):
        numbered.append({
            "n": n,
            "category": it["category"],
            "title": it["title"],
            "source": it["source"],
            "summary": it["summary"],
            "uae_relevance": round(it["uae_relevance"] or 0, 2),
            "severity": round(it["severity"] or 0, 2),
            "entities": [e.get("name") if isinstance(e, dict) else str(e) for e in (it.get("entities") or [])][:5],
            "doctrine_relation": it.get("doctrine_relation"),
        })

    user_payload = {
        "date": date_label,
        "item_count_total": len(items),
        "items": numbered,
    }
    user_msg = (
        "Compose the digest from the items below. Use the system prompt's format and rules.\n\n"
        "```json\n" + json.dumps(user_payload, ensure_ascii=False, indent=2) + "\n```"
    )

    digest_text = llm.call(
        model=llm.HAIKU,    # MVP: same model as classifier; flip to llm.SONNET when budget allows
        system_prompt=load_prompt("editor"),
        user_prompt=user_msg,
        purpose="editor",
        timeout=180,
    )

    # Map digest's #N references back to real item ids (so 'link N' works).
    item_ids = [items[n - 1]["id"] for n in range(1, len(items) + 1)]
    return digest_text, item_ids


def compose_deep_dive(topic: str, items: list[dict]) -> tuple[str, list[int]]:
    """Compose a deep-dive synthesis on `topic` from up-to-20 items.

    Same overall mechanism as compose_digest (numbered payload → Haiku → text)
    but a different prompt that produces an analytical brief, not a digest.
    Returns (markdown, [item_ids]) so the bot can offer `link N` over the
    sources cited.
    """
    if not items:
        return (
            f"🔍 *Deep dive — {topic}*\n\n"
            "No matching items in the last 30 days. Try a broader keyword "
            "(e.g. country name, organization, sector).",
            [],
        )

    numbered = []
    for n, it in enumerate(items, start=1):
        numbered.append({
            "n": n,
            "category": it["category"],
            "title": it["title"],
            "source": it["source"],
            "summary": it["summary"],
            "uae_relevance": round(it["uae_relevance"] or 0, 2),
            "severity": round(it["severity"] or 0, 2),
            "entities": [e.get("name") if isinstance(e, dict) else str(e) for e in (it.get("entities") or [])][:5],
            "ingested_at": it.get("ingested_at"),
        })

    user_payload = {"topic": topic, "items": numbered}
    user_msg = (
        f"Topic the user asked about: {topic!r}\n\n"
        "Compose the deep dive from the items below. Follow the system "
        "prompt's format and rules.\n\n"
        "```json\n" + json.dumps(user_payload, ensure_ascii=False, indent=2) + "\n```"
    )

    text = llm.call(
        model=llm.HAIKU,
        system_prompt=load_prompt("deepdive"),
        user_prompt=user_msg,
        purpose="deepdive",
        timeout=180,
    )
    item_ids = [it["id"] for it in items]
    return text, item_ids
