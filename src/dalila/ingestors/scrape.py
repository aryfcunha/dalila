"""Generic HTML scraper for sources without an RSS feed.

This is a best-effort fallback. It uses CSS selectors from sources.yaml to pluck
candidate article cards out of a page, extracts a title + link from each, and
returns RawItems. Body text is whatever the card itself contains — full-article
fetching is not done in v1 (too many edge cases per site).

Reality: scrapers break when sites change layout. Each enabled scrape source
should be sanity-checked weekly. Failures are logged but don't crash the pipeline.
"""

from __future__ import annotations

import logging
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from dalila.models import RawItem

log = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; DalilaBot/0.1; +personal use; news monitoring)"
    ),
    "Accept": "text/html,application/xhtml+xml",
}


def fetch(src: dict) -> list[RawItem]:
    url = src.get("url")
    if not url:
        log.info("source %s scrape skipped: no url", src.get("id"))
        return []
    selector = src.get("selector") or "article"

    try:
        resp = httpx.get(url, headers=DEFAULT_HEADERS, timeout=30, follow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:
        log.warning("scrape fetch failed for %s: %s", src.get("id"), exc)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    cards = soup.select(selector)
    if not cards:
        log.info("scrape %s: 0 cards matched selector %r", src.get("id"), selector)
        return []

    items: list[RawItem] = []
    seen_titles: set[str] = set()
    for card in cards[:40]:  # cap per page to avoid pulling navigation links etc.
        # Find the first link with usable text
        a = card.find("a", href=True)
        if not a:
            continue
        title = (a.get_text() or "").strip()
        if not title or len(title) < 8 or title in seen_titles:
            continue
        seen_titles.add(title)
        href = urljoin(url, a["href"])
        # Body: card text minus link text
        body = card.get_text(separator=" ", strip=True)
        if body == title:
            body = None
        items.append(RawItem(
            source_id=src["id"],
            title=title,
            url=href,
            body=body[:2000] if body else None,
        ))
    return items
