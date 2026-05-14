"""RSS / Atom feed ingestor.

Many publishers serve a Cloudflare / DataDome challenge HTML page to the
default `feedparser` User-Agent string ("UniversalFeedParser/..."), which
then arrives at `feedparser.parse` as malformed XML and fails. The fix
is to fetch the URL ourselves with `httpx` and a browser-like UA, then
hand the bytes to feedparser. This also lets us emit diagnostics when
a feed legitimately fails: HTTP status, content-type, and the first
~200 bytes of the body — distinguishing "URL moved" from "anti-bot
challenge" from "feed actually corrupt."
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
import httpx

from dalila.models import RawItem

log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        # A real browser-ish UA so anti-bot defences let us through.
        # Pure "Mozilla/5.0" + DalilaBot identifier so anyone watching
        # the logs on the publisher side can identify us if they want.
        "Mozilla/5.0 (compatible; DalilaBot/0.2; +https://github.com/aryfcunha/dalila; "
        "news monitoring)"
    ),
    "Accept": (
        "application/rss+xml, application/atom+xml, application/xml, "
        "text/xml;q=0.9, */*;q=0.5"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


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
    sid = src.get("id")
    if not url:
        log.warning("source %s has no url; skipping", sid)
        return []

    # Two-stage: fetch with our UA, then hand bytes to feedparser. The
    # publisher-side anti-bot defence usually only checks the UA, not
    # later headers, so this single change recovers most "malformed XML"
    # failures.
    try:
        resp = httpx.get(url, headers=_HEADERS, timeout=30, follow_redirects=True)
    except Exception as exc:
        log.warning("feed HTTP for %s failed: %s", sid, exc)
        return []

    if resp.status_code != 200:
        log.warning(
            "feed %s: HTTP %d (content-type=%s)",
            sid, resp.status_code, resp.headers.get("content-type", "?"),
        )
        return []

    content = resp.content
    content_type = (resp.headers.get("content-type") or "").lower()

    parsed = feedparser.parse(content)
    if parsed.bozo and not parsed.entries:
        msg = getattr(parsed, "bozo_exception", "unknown parse error")
        snippet = content[:200].decode("utf-8", errors="replace").replace("\n", " ").strip()
        # Heuristic: if content-type is HTML or the body looks HTML-ish,
        # this is an anti-bot challenge / error page — say so explicitly.
        is_html = "html" in content_type or snippet.lstrip().lower().startswith(("<!doctype html", "<html"))
        verdict = "HTML returned (likely anti-bot or error page)" if is_html else "XML parse error"
        log.warning(
            "feed parse failed for %s: %s | %s | content-type=%s | head=%r",
            sid, msg, verdict, content_type or "?", snippet[:160],
        )
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
