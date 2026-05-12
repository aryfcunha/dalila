"""RSS / Atom feed ingestor (feedparser)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser

from dalila.models import RawItem

log = logging.getLogger(__name__)


def _parse_dt(entry) -> datetime | None:
    for key in ("published", "updated", "created"):
        val = entry.get(key)
        if not val:
            continue
        try:
            dt = parsedate_to_datetime(val)
            if dt is None:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            continue
    # Fall back to time.struct_time fields parsed by feedparser
    for key in ("published_parsed", "updated_parsed"):
        struct = entry.get(key)
        if not struct:
            continue
        try:
            return datetime(*struct[:6], tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def fetch(src: dict) -> list[RawItem]:
    url = src.get("url")
    if not url:
        log.warning("source %s has no url; skipping", src.get("id"))
        return []

    parsed = feedparser.parse(url)
    if parsed.bozo and not parsed.entries:
        msg = getattr(parsed, "bozo_exception", "unknown parse error")
        log.warning("feed parse failed for %s: %s", src.get("id"), msg)
        return []

    items: list[RawItem] = []
    for entry in parsed.entries:
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        link = entry.get("link") or None
        # body: prefer 'summary', fall back to 'content[0].value'
        body = entry.get("summary") or ""
        content_arr = entry.get("content")
        if content_arr and isinstance(content_arr, list):
            value = content_arr[0].get("value") if isinstance(content_arr[0], dict) else None
            if value and len(value) > len(body):
                body = value
        body = body.strip() or None

        items.append(RawItem(
            source_id=src["id"],
            title=title,
            url=link,
            body=body,
            author=entry.get("author") or None,
            published_at=_parse_dt(entry),
        ))
    return items
